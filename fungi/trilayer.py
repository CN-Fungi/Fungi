"""TriLayer agent system.

L1 Orchestrator (user-facing) → spawn → L2 Task Agent → spawn → L3 Worker.

Dispatch contract (TaskSpec): the upper agent states the goal and the reply
format; the lower agent executes strictly within that scope and MUST answer in
the requested format. Final answers flow back as the spawn tool result.
"""

import contextlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fungi import diary, skills, todos, tools
from fungi.agent import SYSTEM_PROMPT, Agent, BoundTool
from fungi.config import Config
from fungi.events import FnSink, Sink
from fungi.llm import LLMResult
from fungi.tools.ask import make_ask_tool
from fungi.tools.mcp import mcp_extra_tools

MAX_SPAWNS_PER_TURN = 8
JSON_RETRIES = 2

L1_ADDENDUM = """

## TriLayer dispatch
You are the L1 Orchestrator — the only layer that talks to the user. For
substantial subtasks (research, multi-file work, independent checks, anything
slow) use the `spawn` tool to dispatch an L2 Task Agent instead of doing
everything inline.

spawn NEVER returns the answer. It returns "dispatched (id=...)" at once and
the subagent keeps running after your turn ends. This is how you run work in
the background while staying responsive. The rules are absolute:
- After spawning, tell the user the task is running and END YOUR TURN.
- NEVER wait for the result, NEVER poll, NEVER redo the spawned work with
  your own tools. Doing the work again is a bug, not diligence.
- The report arrives later as the input of a NEW turn ([background report]).
  Treat that turn like a fresh user message containing the findings and act
  on it then.
When spawning you MUST write:
- goal: what the subagent should accomplish (self-contained, no references to
  this conversation),
- reply_format: exactly how to report back (e.g. "yes/no plus a reason",
  "JSON with fields X and Y", "list of up to 3 file paths"),
- context: background material the subagent needs (optional),
- constraints: hard boundaries, e.g. files it must not touch (optional).
Trivial one-step actions (a single read or a single command) are better done
directly with your own tools.

## Asking the user
You are the only layer that can ask the user a question. Use `inquire` when
a decision genuinely needs the user's input (choosing between approaches,
confirming something hard to undo). Offer clear `options` when possible; the
user can also type a free-form answer. Asking blocks the turn until they
answer, so ask only when it truly matters — do not ask for permission to do
obvious work.
"""

L2_SYSTEM = """\
You are a Task Agent (layer 2 of a three-layer system). An orchestrator has
dispatched a task to you with an explicit goal and a required reply format.

Execute the task with your tools. You may use the `spawn` tool to dispatch an
L3 Worker for basic sub-steps (single file operations, single commands,
single lookups) — never for whole-task delegation. spawn returns "dispatched"
at once and the worker reports back via a re-activation, so do not wait for
it: end your reply while it runs.

Discipline (mandatory):
- Do exactly what the goal says. Do NOT widen the scope, touch unrelated
  files, or pursue improvements nobody asked for.
- Tool output is ground truth. NEVER fabricate file contents or command output.
- Your FINAL message must follow the required reply_format exactly — it is
  the only thing the orchestrator sees. No preamble, no meta commentary.
- If the task cannot be completed, say so inside the required reply format
  rather than improvising something else.
"""

L3_SYSTEM = """\
You are a basic Worker (layer 3, the lowest layer of a three-layer system).
A Task Agent has dispatched a small, concrete job to you.

Your toolset is intentionally limited to: read, write, edit, glob, grep, bash.

Discipline (mandatory):
- Do exactly what the goal says, in as few steps as possible. Do NOT widen
  the scope or fix anything beyond the goal.
- Tool output is ground truth. NEVER fabricate results.
- Your FINAL message must follow the required reply_format exactly — it is
  the only thing the dispatcher sees.
- If the job cannot be done, report that inside the required reply format.
"""

BACKGROUND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "background",
        "description": (
            "Run a shell command in the BACKGROUND. Returns 'dispatched (id=...)'"
            " immediately - the command's output arrives later as the input of a"
            " new turn ([background report]), NOT in this turn. Use it for slow"
            " commands (installs, builds, long tests) while you keep responding"
            " to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The exact shell command to run"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "timeout": {
                    "type": "number",
                    "description": (
                        "Optional wall-clock cap in seconds. When it expires the"
                        " command is killed and the report carries the partial"
                        " output plus a timeout note. Give it ONLY when you know"
                        " a reasonable upper bound for a slow command; omit = no"
                        " limit."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}
SPAWN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn",
        "description": (
            "Dispatch a subagent one layer below you. Write a self-contained goal and the"
            " exact format the subagent must use for its final reply. Returns 'dispatched"
            " (id=...)' immediately — the reply does NOT come back here; the dispatcher is"
            " re-activated with the report in a later turn (synchronous fallback contracts"
            " may return the reply directly)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "Self-contained description of the task"},
                "reply_format": {
                    "type": "string",
                    "description": (
                        'Required format of the final reply, e.g. "yes/no + reason", '
                        '"JSON {found: bool, paths: []}"'
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "Background material the subagent needs (optional)",
                },
                "constraints": {
                    "type": "string",
                    "description": "Hard boundaries, e.g. files it may touch (optional)",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Optional wall-clock cap in seconds. When it expires the"
                        " subagent is aborted and the report carries whatever it"
                        " produced plus a timeout note. Give it ONLY for tasks"
                        " with a known reasonable upper bound; omit = no limit."
                    ),
                },
            },
            "required": ["goal", "reply_format"],
        },
    },
}


@dataclass
class TaskSpec:
    id: str
    layer: int  # 2 or 3
    goal: str
    reply_format: str
    context: str = ""
    constraints: str = ""
    parent_id: str | None = None
    # Optional wall-clock cap in seconds; None = run until done or /stop.
    timeout: float | None = None


def task_brief(spec: TaskSpec) -> str:
    parts = [f"## Goal\n{spec.goal}", f"## Reply format (mandatory)\n{spec.reply_format}"]
    if spec.context:
        parts.append(f"## Context\n{spec.context}")
    if spec.constraints:
        parts.append(f"## Constraints\n{spec.constraints}")
    return "\n\n".join(parts)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


class TriLayer:
    """Spawns and supervises subagents for one agent turn tree."""

    def __init__(
        self,
        cfg: Config,
        sink: Sink,
        llm: Callable[[list[dict], list[dict]], LLMResult] | None = None,
        should_abort: Callable[[], bool] | None = None,
        child_tool_names: frozenset[str] | None = None,
        child_extra_tools: dict[str, BoundTool] | None = None,
        skill_save: bool = False,
        spawn_done: Callable[[dict], None] | None = None,
    ) -> None:
        """child_tool_names/child_extra_tools: when set, spawned subagents use
        this surface instead of the native defaults — a clone's spawn inherits
        the clone's file restrictions (spec 6.1)."""
        self.cfg = cfg
        self.sink = sink
        self._llm = llm
        self._should_abort = should_abort
        self._child_tool_names = child_tool_names
        self._child_extra_tools = child_extra_tools or {}
        # Whether agents built from this layer may author skills (user-facing
        # surfaces only; autonomous comm clones get a readonly tool).
        self._skill_save = skill_save
        self._active = 0
        self._lock = threading.Lock()
        # Called from the background thread when a subagent finishes:
        # {"id","goal","status","answer"}. The WebUI runtime wires this to the
        # session's pending-results registry — the resume turn injects the
        # report and re-activates the session. None (tests) = fire nowhere.
        self._spawn_done = spawn_done
        # spec_id -> {id, call_id, layer, goal, reply_format, status, events: [...]}
        self.subagents: dict[str, dict] = {}
        self.asks: list[dict] = []  # completed inquire records (for persistence)

    def bound_spawn(self, parent_layer: int) -> BoundTool:
        """The spawn tool bound to a parent layer: L1 spawns L2, L2 spawns L3."""

        return BoundTool(
            schema=SPAWN_SCHEMA,
            fn=lambda args, call_id=None: self._spawn(args, parent_layer, call_id),
            with_call_id=True,
        )

    def bound_background(self, parent_layer: int) -> BoundTool:
        """The `background` tool: spawn's async UX with a command-only surface.

        Accepts one shell command and runs it DIRECTLY on a worker thread (no
        subagent, no extra LLM calls). Instant 'dispatched' return, the output
        comes back via the [background report] re-activation, /stop kills it.
        """

        def _run(args: dict, call_id: str | None = None) -> str:
            from fungi.tools.shell import tool_bash  # noqa: PLC0415 (Qt-free, cheap)

            command = str(args.get("command") or "").strip()
            if not command:
                return "ERROR: Missing required argument: command"
            cwd = str(args.get("cwd") or "").strip() or None
            raw_timeout = args.get("timeout")
            try:
                timeout = float(raw_timeout) if raw_timeout else None
            except (TypeError, ValueError):
                return "ERROR: timeout must be a number of seconds"

            with self._lock:
                if self._active >= MAX_SPAWNS_PER_TURN:
                    return f"ERROR: background limit reached ({MAX_SPAWNS_PER_TURN} per turn)."
                self._active += 1
            record = {
                "id": uuid.uuid4().hex[:6],
                "call_id": call_id,
                "layer": parent_layer + 1,
                "tool": "background",
                "goal": command if len(command) <= 200 else command[:200] + "…",
                "reply_format": "command output",
                "status": "running",
                "events": [],
            }
            self.subagents[record["id"]] = record
            with contextlib.suppress(Exception):  # parent stream may be closed
                self.sink.emit(
                    "agent_spawn",
                    {
                        "id": record["id"],
                        "call_id": call_id,
                        "layer": record["layer"],
                        "tool": "background",
                        "goal": record["goal"],
                        "reply_format": "command output",
                    },
                )
                self.sink.emit(
                    "agent_status", {"id": record["id"], "status": "running"}
                )

            def _bg() -> None:
                started = time.monotonic()
                deadline = started + timeout if timeout is not None else None
                base = self._should_abort

                def _abort() -> bool:
                    if base is not None and base():
                        return True
                    return deadline is not None and time.monotonic() > deadline

                answer = tool_bash(command, cwd=cwd, should_abort=_abort)
                user_stopped = base is not None and base()
                if deadline is not None and time.monotonic() > deadline and not user_stopped:
                    status = "timeout"
                    answer += (
                        f"\n\n[timeout] The {timeout:.0f}s limit you set expired;"
                        " the command was killed. Everything above is the output"
                        " captured before the cutoff."
                    )
                elif user_stopped:
                    # Stopped while running: mark it, never re-activate (same
                    # contract as the spawn path - stop means stop).
                    status = "aborted"
                else:
                    status = "failed" if answer.startswith("ERROR:") else "done"
                record["status"] = status
                record["answer"] = answer
                with contextlib.suppress(Exception):
                    self.sink.emit(
                        "agent_status", {"id": record["id"], "status": status}
                    )
                with self._lock:
                    self._active -= 1
                if status != "aborted" and self._spawn_done is not None:
                    with contextlib.suppress(Exception):
                        self._spawn_done(
                            {
                                "id": record["id"],
                                "goal": record["goal"],
                                "status": status,
                                "answer": answer,
                            }
                        )

            threading.Thread(
                target=_bg, daemon=True, name=f"background-{record['id']}"
            ).start()
            return (
                f"dispatched (id={record['id']}). The command runs in the"
                " BACKGROUND - do not wait for it and do not run it again; its"
                " output arrives as the input of a new turn."
            )

        return BoundTool(schema=BACKGROUND_SCHEMA, fn=_run, with_call_id=True)

    def build_orchestrator(self, sink: Sink) -> Agent:
        """The L1 agent, ready to run user turns."""
        prompt = SYSTEM_PROMPT + L1_ADDENDUM + skills.section() + todos.RULES
        extra = {
            "spawn": self.bound_spawn(1),
            "background": self.bound_background(1),
            "inquire": make_ask_tool(
                sink, on_answer=self.asks.append, should_abort=self._should_abort
            ),
            **mcp_extra_tools(self.cfg.mcp_servers),
            **skills.bound(),
            **todos.bound(),
        }
        if self.cfg.diary:  # experimental: private diary off by default
            prompt += diary.section()
            extra.update(diary.bound())
        if self.cfg.pc_control:
            # Screen control (spec §35.3): the user-facing L1 agent only, never
            # a comm agent and never a spawned child. Imported here so a setup
            # without the desktop deps never pays for the module.
            from fungi.tools import screen  # noqa: PLC0415

            extra.update(
                screen.bound(
                    self.cfg,
                    sink,
                    should_abort=self._should_abort,
                    on_answer=self.asks.append,
                )
            )
        agent = Agent(
            self.cfg,
            sink,
            system_prompt=prompt,
            extra_tools=extra,
            parallel_tools={"spawn", "background", "diary", "todo"}
            if self.cfg.diary
            else {"spawn", "background", "todo"},
            llm=self._llm,
            model=self.cfg.model_for(1),
            should_abort=self._should_abort,
        )
        # Persisted by the WebUI turn runner (server.py / room.py): spawn
        # records + completed inquire records for session replay.
        agent.subagents = self.subagents
        agent.asks = self.asks
        return agent

    def build_clone_agent(
        self,
        sink: Sink,
        *,
        system_prompt: str,
        extra_tools: dict[str, BoundTool],
        tool_names: frozenset[str] | set[str] = frozenset(),
        model: str | None = None,
        subagents: bool = True,
    ) -> Agent:
        """A clone turn agent: clone's prompt/tools + spawn; children inherit
        the clone's file surface (see __init__ child_* params).

        `subagents=False` builds a courier-shaped agent: no spawn, no
        background. A courier's spawn would be SYNCHRONOUS (a clone has no
        re-activation channel, see `_spawn`) — an extra LLM hop that cannot run
        in parallel — and its background reports had nowhere to land. Both only
        widened the blast radius of an unattended, peer-driven agent
        (user decision 2026-09-10)."""
        dispatch = (
            {"spawn": self.bound_spawn(1), "background": self.bound_background(1)}
            if subagents
            else {}
        )
        agent = Agent(
            self.cfg,
            sink,
            system_prompt=system_prompt + skills.section(),
            tool_names=tool_names,
            extra_tools={
                **dispatch,
                **skills.bound(readonly=not self._skill_save),
                **extra_tools,
            },
            parallel_tools={"spawn", "background"} if subagents else frozenset(),
            llm=self._llm,
            model=model or self.cfg.model_for(1),
            should_abort=self._should_abort,
        )
        agent.subagents = self.subagents
        agent.asks = self.asks
        return agent

    def _spawn(
        self, args: dict, parent_layer: int, call_id: str | None = None, tool: str = "spawn"
    ) -> str:
        goal = str(args.get("goal") or "").strip()
        reply_format = str(args.get("reply_format") or "").strip()
        if not goal:
            return "ERROR: Missing required argument: goal"
        if not reply_format:
            return "ERROR: Missing required argument: reply_format"
        child_layer = parent_layer + 1
        if child_layer > 3:
            return "ERROR: You are at the deepest layer (L3); finish the job yourself."

        # Validate BEFORE taking a slot: the error return below must not leak
        # the counter (a few bad timeouts used to exhaust the per-turn limit).
        raw_timeout = args.get("timeout")
        try:
            timeout = float(raw_timeout) if raw_timeout else None
        except (TypeError, ValueError):
            return "ERROR: timeout must be a number of seconds"

        with self._lock:
            if self._active >= MAX_SPAWNS_PER_TURN:
                return f"ERROR: spawn limit reached ({MAX_SPAWNS_PER_TURN} per turn)."
            self._active += 1
        spec = TaskSpec(
            id=uuid.uuid4().hex[:6],
            layer=child_layer,
            goal=goal,
            reply_format=reply_format,
            context=str(args.get("context") or ""),
            constraints=str(args.get("constraints") or ""),
            timeout=timeout,
        )
        record = {
            "id": spec.id,
            "call_id": call_id,
            "layer": spec.layer,
            "tool": tool,
            "goal": goal,
            "reply_format": reply_format,
            "status": "running",
            "events": [],
        }
        self.subagents[spec.id] = record
        self.sink.emit(
            "agent_spawn",
            {
                "id": spec.id,
                "call_id": call_id,
                "layer": spec.layer,
                "tool": tool,
                "goal": goal,
                "reply_format": reply_format,
            },
        )
        self.sink.emit("agent_status", {"id": spec.id, "status": "running"})
        if self._spawn_done is None:
            # Synchronous contract (comm clones, direct tool use): no
            # re-activation channel exists, so the caller waits for the answer.
            try:
                answer = self._run_task(spec)
                status = "failed" if answer.startswith("FAIL") else "done"
            except Exception as exc:
                answer = f"FAIL: subagent crashed: {exc}"
                status = "failed"
            record["status"] = status
            self.sink.emit("agent_status", {"id": spec.id, "status": status})
            with self._lock:
                self._active -= 1
            return answer
        # Async contract (user-facing WebUI sessions): return at once, the
        # report comes back via spawn_done -> /resume re-activation.
        thread = threading.Thread(
            target=self._run_bg, args=(spec, record), daemon=True, name=f"spawn-{spec.id}"
        )
        thread.start()
        return (
            f"dispatched (id={spec.id}). It runs in the BACKGROUND - do not wait "
            "for it and do not repeat the task yourself; keep responding or end "
            "your turn. When it finishes, this session is automatically "
            "re-activated with its report as the input of a new turn."
        )

    def _timed_out(self, spec: TaskSpec, started: float) -> bool:
        """True when the spec's own deadline expired (user /stop is separate)."""
        return spec.timeout is not None and time.monotonic() - started >= spec.timeout

    def _run_bg(self, spec: TaskSpec, record: dict) -> None:
        """Background body of a spawn: run, finalize the record, notify."""
        started = time.monotonic()
        try:
            answer = self._run_task(spec)
            status = "failed" if answer.startswith("FAIL") else "done"
            if self._timed_out(spec, started):
                status = "timeout"
                answer += (
                    f"\n\n[timeout] The {spec.timeout:.0f}s limit you set expired;"
                    " the subagent was aborted mid-task. Everything above is what"
                    " it produced before the cutoff."
                )
        except Exception as exc:  # a crashed child must not kill the session
            answer = f"FAIL: subagent crashed: {exc}"
            status = "failed"
        if self._should_abort is not None and self._should_abort():
            # The turn was stopped while this child ran: mark it and do NOT
            # re-activate the session — /stop cleared the pending registry,
            # a resurrecting report would undo the stop from the user's view.
            status = "aborted"
            record["answer"] = answer
            with contextlib.suppress(Exception):  # stream may already be closed
                self.sink.emit("agent_status", {"id": spec.id, "status": status})
            with self._lock:
                self._active -= 1
            return
        record["status"] = status
        record["answer"] = answer
        with contextlib.suppress(Exception):  # parent turn's HTTP stream may be closed
            self.sink.emit("agent_status", {"id": spec.id, "status": status})
        with self._lock:
            self._active -= 1
        if self._spawn_done is not None:
            with contextlib.suppress(Exception):
                self._spawn_done(
                    {"id": spec.id, "goal": record["goal"], "status": status, "answer": answer}
                )

    def _child_abort(self, spec: TaskSpec, started: float) -> Callable[[], bool]:
        """Abort predicate for one child run: user /stop OR the spec deadline."""
        base = self._should_abort

        def _abort() -> bool:
            if base is not None and base():
                return True
            return self._timed_out(spec, started)

        return _abort

    def _run_task(self, spec: TaskSpec) -> str:
        started = time.monotonic()
        child_sink = FnSink(lambda t, c, _sid=spec.id: self._record_event(_sid, t, c))
        agent = Agent(
            self.cfg,
            child_sink,
            system_prompt=L3_SYSTEM if spec.layer == 3 else L2_SYSTEM,
            tool_names=(
                self._child_tool_names
                if self._child_tool_names is not None
                else (tools.L3_TOOL_NAMES if spec.layer == 3 else tools.BASE_TOOL_NAMES)
            ),
            extra_tools=(
                {
                    "spawn": self.bound_spawn(spec.layer),
                    **skills.bound(readonly=not self._skill_save),
                    **self._child_extra_tools,
                }
                if spec.layer == 2
                else {
                    **skills.bound(readonly=not self._skill_save),
                    **self._child_extra_tools,
                }
            ),
            parallel_tools={"spawn"},
            llm=self._llm,
            model=self.cfg.model_for(spec.layer),
            should_abort=self._child_abort(spec, started),
        )
        messages: list[dict] = [{"role": "user", "content": task_brief(spec)}]
        result = agent.run(messages)
        answer = result.content

        if "JSON" in spec.reply_format.upper():
            attempts = 0
            while not _is_json(answer) and attempts < JSON_RETRIES:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your reply does not satisfy the required reply format "
                            f'("{spec.reply_format}"), which demands valid JSON. '
                            "Reply again with valid JSON only."
                        ),
                    }
                )
                result = agent.run(messages)
                answer = result.content
                attempts += 1
            if not _is_json(answer):
                return f"FAIL: reply is not valid JSON per reply_format. Last reply: {answer[:500]}"

        if not answer.strip():
            return "FAIL: empty reply"
        return answer

    def _record_event(self, spec_id: str, event_type: str, content) -> None:
        """Store the child event for replay, and forward it to the UI stream."""
        record = self.subagents.get(spec_id)
        if record is not None:
            record["events"].append({"type": event_type, "content": content})
        self.sink.emit(
            "agent_event", {"id": spec_id, "event": {"type": event_type, "content": content}}
        )
