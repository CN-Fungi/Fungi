"""Agent turn loop: stream a completion, dispatch tool calls, repeat (max rounds)."""

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fungi import skills, tools
from fungi.config import Config
from fungi.events import EmitFn, Sink
from fungi.llm import LLMAbortedError, LLMError, LLMResult, stream_chat
from fungi.tools.files import ImageRead

MAX_TOOL_ROUNDS = 100
TRUNCATE_TOOL_RESULT = 16000

TRANSIENT_LLM_MARKERS = (
    "Stream interrupted",
    "Connection failed",
    "Stream ended without a finish signal",
)
TRANSIENT_HTTP_RE = re.compile(r"^HTTP (429|5\d\d)")

SYSTEM_PROMPT = """\
You are a coding assistant. You have tools to read, write, edit files, run
shell commands, search code, and access the web. Core rules:

- Be concise. Lead with the answer, then evidence.
- NEVER ask for permission — just do the work.
- BEFORE any task: use `glob` to see directory structure. NEVER `bash dir` or `bash ls`.
- To find files or code: use `grep`. NEVER `bash find` or `bash findstr`.
- To read a file: use `read`. NEVER `bash type` or `bash cat`.
  Images (png/jpg/webp/gif) read as attached pictures you can see and describe.
  `video` returns a timestamped transcript plus attached keyframes you can
  see; cite moments by their [start-end] ranges. The tool self-verifies its
  runtime on every call, so readiness needs NO separate environment checks:
  just call `video` with path "demo" (a built-in test clip). One successful
  call = video works; if it errors with missing components, tell the user what
  is missing and offer to set it up yourself (the error carries the exact
  recipe: global pip + mirrors). Install ONLY after the user agrees, then
  re-verify with a demo call. NEVER promise video analysis before a `video`
  call has actually succeeded here.
  A call blocks for ~15-60s on CPU (model loading) - announce that it is
  running instead of staying silent.
- To edit: use `edit`. NEVER `bash echo >` to overwrite files.
- `bash` is ONLY for: running programs, builds, tests, git, pip, npm, python, etc.
- When editing, match the existing code style. Use the edit tool (old_string /
  new_string) for surgical changes, not rewrite the whole file.
- When you need up-to-date information, use `web_search` to find sources,
  then `web` to read the full pages you need.

- Tool output is truncated at ~16000 chars. Plan reads accordingly.
- After completing a task, summarize what you did.
- NEVER fabricate file contents or command output.
- If a tool returns no results or an error, try a different approach —
  don't just give up.

- When answering in the browser, use full markdown: code blocks, lists, bold, etc.
"""


@dataclass
class BoundTool:
    """An agent-bound tool beyond the base registry (spawn, inquire, ...)."""

    schema: dict
    fn: Callable[..., str]
    with_call_id: bool = False  # fn also receives the tool_call id


# Tools whose calls are the agent's inner life: no WebUI card, no transcript
# event, nothing in the session replay. The user can learn about them only by
# the agent choosing to say so (e.g. "I flipped through my diary").
PRIVATE_TOOLS = frozenset({"diary"})


def public_messages(messages: list[dict]) -> list[dict]:
    """Session-transcript view of a turn: PRIVATE_TOOLS calls (diary reads
    and writes) are the agent's inner life. The live stream already suppresses
    their card events, but the end-of-turn re-render reads the persisted
    messages, where the assistant message itself carries tool_calls - so the
    persistence boundary must strip them too. The LLM context keeps them (the
    API requires the pairing); they are stateless tool calls, nothing is lost.

    Rows get their first-seen `ts` here: the WebUI labels each message with the
    moment it was sent (hover), and a session row is stamped once — the turn's
    start save fixes the user message, its end save the rows the turn produced.
    The stamp is store-only: `llm.stream_chat` strips it from the wire.
    """
    private_ids: set[str] = set()
    out: list[dict] = []
    for m in messages:
        if m.get("role") != "system" and "ts" not in m:
            # In place, so the next save (and the retry path) keeps the moment
            # this row first reached the transcript instead of re-stamping it.
            m["ts"] = time.time()
        role = m.get("role")
        tcs = m.get("tool_calls") if role == "assistant" else None
        if role == "tool" and m.get("tool_call_id") in private_ids:
            continue
        if tcs:
            visible = [tc for tc in tcs if tc.get("function", {}).get("name") not in PRIVATE_TOOLS]
            if len(visible) == len(tcs):
                out.append(m)
                continue
            private_ids.update(
                tc["id"] for tc in tcs if tc.get("function", {}).get("name") in PRIVATE_TOOLS
            )
            if not visible:
                continue  # the turn's only calls were private: drop wholesale
            out.append({**m, "tool_calls": visible})
            continue
        out.append(m)
    return out


def wrap_reasoning_events(sink: Sink) -> tuple[EmitFn, dict]:
    """Delta callback that brackets the first/last reasoning delta with
    reasoning_start / reasoning_end so the UI can render a live block."""
    state = {"started": False, "ended": False}

    def on_delta(kind: str, text: str) -> None:
        if kind == "reasoning":
            if not state["started"]:
                state["started"] = True
                sink.emit("reasoning_start", None)
            sink.emit("reasoning", text)
        elif state["started"] and not state["ended"]:
            state["ended"] = True
            sink.emit("reasoning_end", None)
            if text:
                sink.emit("text", text)
        else:
            sink.emit("text", text)

    return on_delta, state


def _is_transient_llm_error(message: str) -> bool:
    """Transport-level failures worth another attempt (bad requests are not:
    retrying a 400 only burns tokens)."""
    return any(m in message for m in TRANSIENT_LLM_MARKERS) or bool(
        TRANSIENT_HTTP_RE.match(message)
    )


def parse_tool_args(raw: str) -> tuple[dict, str | None]:
    """Parse a tool_call's JSON arguments; on failure try a light repair.

    Returns (args, error). error is None on success; otherwise it explains
    WHY the JSON was invalid — the caller surfaces that to the model instead
    of silently dispatching with {} (which produced misleading
    "Required arguments: host, goal" errors for valid-looking calls).
    """
    text = (raw or "").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        repaired = _repair_tool_json(text)
        if repaired is not None:
            return repaired, None
        return {}, f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
    if isinstance(parsed, dict):
        return parsed, None
    return {}, f"arguments must be a JSON object, got {type(parsed).__name__}"


def _repair_tool_json(text: str) -> dict | None:
    """Minimal repair pass: trailing commas before } / ]. Anything else
    (unescaped quotes, bare newlines in strings) stays a parse error."""
    repaired = re.sub(r",\s*(?=[}\]])", "", text)
    if repaired == text:
        return None
    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class Agent:
    """One agent conversation loop. Owns no history: the caller passes the message list.

    `extra_tools` (BoundTool by name) lets upper layers attach dispatch/ask
    tools (see fungi.trilayer / fungi.tools.ask). `llm` is injectable so tests
    can drive the loop with scripted completions.
    """

    def __init__(
        self,
        cfg: Config,
        sink: Sink,
        system_prompt: str = SYSTEM_PROMPT,
        tool_names: frozenset[str] | set[str] | None = None,
        extra_tools: dict[str, BoundTool] | None = None,
        llm: Callable[[list[dict], list[dict]], LLMResult] | None = None,
        model: str | None = None,  # per-agent model override; None -> cfg.model
        should_abort: Callable[[], bool] | None = None,
        parallel_tools: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        self.cfg = cfg
        self.sink = sink
        self.system_prompt = system_prompt
        self.tool_names = tool_names
        self.extra_tools = extra_tools or {}
        self.parallel_tools = frozenset(parallel_tools)
        self._llm = llm
        self.model = model
        self.should_abort = should_abort

    @property
    def tool_defs(self) -> list[dict]:
        defs = tools.tool_defs(self.tool_names)
        defs.extend(bound.schema for bound in self.extra_tools.values())
        return defs

    def _default_llm(self, messages: list[dict], tool_defs: list[dict]) -> LLMResult:
        on_delta, state = wrap_reasoning_events(self.sink)

        if self._aborted():
            raise LLMAbortedError(LLMResult())
        result = stream_chat(
            self.model or self.cfg.model,
            self.cfg.endpoint,
            self.cfg.api_key,
            messages,
            tool_defs,
            on_delta,
            should_abort=self._aborted,
            max_tokens=self.cfg.max_tokens or None,
        )
        # Reasoning-only turns (pure tool calls) never see a text delta.
        if state["started"] and not state["ended"]:
            self.sink.emit("reasoning_end", None)
        return result

    def _aborted(self) -> bool:
        return self.should_abort is not None and self.should_abort()

    def _dispatch(self, name: str, args: dict, call_id: str | None = None) -> str:
        bound = self.extra_tools.get(name)
        if bound is not None:
            try:
                if bound.with_call_id:
                    result = bound.fn(args, call_id)
                else:
                    result = bound.fn(args)
            except Exception as exc:
                return f"ERROR: {exc}"
            # ImageRead (str subclass) must survive: str() would flatten it and
            # the loop would lose the pixels a bound tool attached (screen shots).
            return result if isinstance(result, str) else str(result)
        return tools.dispatch(name, args, should_abort=self._aborted)

    def _run_tool_call(self, tc: dict, messages: list[dict]) -> None:
        name = tc["function"]["name"]
        if self._aborted():  # stop already pressed: don't launch another tool
            self.sink.emit("tool_result", {"content": "(Aborted)", "id": tc["id"]})
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": "(Aborted)"}
            )
            return
        raw_args = tc["function"]["arguments"]
        args, parse_error = parse_tool_args(raw_args)
        private = name in PRIVATE_TOOLS
        if not private:
            self.sink.emit("tool", {"name": name, "args": raw_args, "id": tc["id"]})
        if parse_error is not None:
            # Never dispatch with silently-emptied args: the tool's
            # "Required arguments" message would hide the real failure.
            output = (
                f"ERROR: the {name} tool call's arguments were not valid JSON "
                f"({parse_error}). Raw arguments received: {str(raw_args or '')[:600]}. "
                "Re-issue the tool call with valid JSON."
            )
        else:
            output = self._dispatch(name, args, call_id=tc["id"])
        if not private:
            self.sink.emit("tool_result", {"content": _truncate(output), "id": tc["id"]})
        messages.append(
            {"role": "tool", "tool_call_id": tc["id"], "content": _tool_content(output)}
        )

    def run(self, messages: list[dict]) -> LLMResult:
        """Drive one user turn to completion, mutating `messages` in place."""
        if messages and messages[0].get("role") == "system":
            # Loaded session: keep the stored system message, swap only the
            # managed skills suffix (fresh list each turn, never duplicated).
            base = messages[0]["content"] or ""
            cut = base.find(skills.SECTION_HEAD)
            if cut != -1:
                base = base[:cut]
            messages[0]["content"] = base + skills.section()
        else:
            messages.insert(0, {"role": "system", "content": self.system_prompt})

        result = LLMResult()
        for _round in range(MAX_TOOL_ROUNDS):
            if self._aborted():
                self.sink.emit("error", "Aborted by user")
                messages.append({"role": "assistant", "content": "(Aborted)"})
                return result
            llm = self._llm if self._llm is not None else self._default_llm
            try:
                result = self._complete_with_retry(llm, messages)
            except LLMAbortedError as exc:
                self.sink.emit("error", "Aborted by user")
                if exc.partial.content:
                    messages.append({"role": "assistant", "content": exc.partial.content})
                messages.append({"role": "assistant", "content": "(Aborted)"})
                return exc.partial
            except LLMError as exc:
                self.sink.emit("error", str(exc))
                messages.append({"role": "assistant", "content": f"(LLM error: {exc})"})
                return result

            if not result.tool_calls:
                messages.append(
                    {"role": "assistant", "content": result.content, "reasoning": result.reasoning}
                )
                return result

            messages.append(
                {
                    "role": "assistant",
                    "content": result.content or None,
                    "tool_calls": result.tool_calls,
                    "reasoning": result.reasoning,
                }
            )
            self._run_tool_calls(result.tool_calls, messages)

        self.sink.emit("error", f"Max tool rounds ({MAX_TOOL_ROUNDS}) reached")
        hit = f"(Hit max tool rounds: {MAX_TOOL_ROUNDS}.)"
        messages.append({"role": "assistant", "content": hit})
        return result

    def _complete_with_retry(self, llm, messages: list[dict]) -> LLMResult:
        """One completion; transient transport failures get up to two more
        attempts (a dropped stream must not kill the whole turn)."""
        for attempt in range(3):
            try:
                return llm(messages, self.tool_defs)
            except LLMAbortedError:
                raise
            except LLMError as exc:
                if attempt == 2 or not _is_transient_llm_error(str(exc)) or self._aborted():
                    raise
                time.sleep(1.0 * (attempt + 1))
        raise LLMError("unreachable")  # pragma: no cover

    def _run_tool_calls(self, tool_calls: list[dict], messages: list[dict]) -> None:
        """Dispatch tool calls; run them concurrently when all are whitelisted as parallel."""
        parallel = len(tool_calls) > 1 and all(
            tc["function"]["name"] in self.parallel_tools for tc in tool_calls
        )
        if not parallel:
            for tc in tool_calls:
                self._run_tool_call(tc, messages)
            return

        parsed = []
        for tc in tool_calls:
            args, parse_error = parse_tool_args(tc["function"]["arguments"])
            parsed.append([tc, args, parse_error])
        for entry in parsed:
            # Same privacy rule as the serial path: PRIVATE_TOOLS calls are the
            # agent's inner life — no card, no stream event, no tape entry.
            if entry[0]["function"]["name"] in PRIVATE_TOOLS:
                continue
            self.sink.emit(
                "tool",
                {
                    "name": entry[0]["function"]["name"],
                    "args": entry[0]["function"]["arguments"],
                    "id": entry[0]["id"],
                },
            )
        outputs: list[str] = [""] * len(parsed)

        def worker(index: int, tc: dict, args: dict) -> None:
            outputs[index] = self._dispatch(tc["function"]["name"], args, call_id=tc["id"])

        threads = []
        for i, (tc, args, parse_error) in enumerate(parsed):
            if parse_error is not None:
                name = tc["function"]["name"]
                outputs[i] = (
                    f"ERROR: the {name} tool call's arguments were not valid JSON "
                    f"({parse_error}). Raw arguments received: "
                    f"{str(tc['function']['arguments'] or '')[:600]}. "
                    "Re-issue the tool call with valid JSON."
                )
            else:
                threads.append(threading.Thread(target=worker, args=(i, tc, args), daemon=True))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for (tc, _args, _err), output in zip(parsed, outputs, strict=True):
            if tc["function"]["name"] not in PRIVATE_TOOLS:
                self.sink.emit("tool_result", {"content": _truncate(output), "id": tc["id"]})
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": _tool_content(output)}
            )


def _tool_content(output: str):
    """Image/video reads arrive as an ImageRead (a str subclass carrying pixel
    data URLs): upgrade the tool message to OpenAI multimodal content so a
    vision model actually sees the pictures. Everything else stays a string."""
    if isinstance(output, ImageRead):
        return [
            {"type": "text", "text": str(output)},
            *[{"type": "image_url", "image_url": {"url": u}} for u in output.data_urls],
        ]
    return output


def _truncate(text: str, limit: int = TRUNCATE_TOOL_RESULT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - limit} chars] ...\n{text[-half:]}"
