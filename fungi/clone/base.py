"""Clone base: transports + the inbox/turn thread model.

Thread model (docs/architecture.md): the loop thread polls the transport and dispatches
control envelopes (answer/result/err) immediately — that is what wakes blocked
asks while a turn is running. Turn envelopes (chat/task/transfer) are queued
to a single worker thread, keeping turns serial per clone.
"""

import queue
import shutil
import threading
from pathlib import Path

from .. import tools
from ..agent import Agent, BoundTool
from ..config import Config
from ..events import Sink
from ..hub.app import fs_via_hub
from ..hub.client import HubClient
from ..hub.relay import Relay
from ..pending import PendingAsks
from ..protocol import Envelope, parse_addr
from ..trilayer import TriLayer

TURN_TYPES = ("chat", "task", "transfer")
DIRECT_TYPES = ("chat", "transfer")  # courier-off delivers these without a turn
MAX_CHAT_HISTORY = 200  # chat messages kept per clone; older entries are dropped


def _qa_lines(questions: list, value) -> str:
    """Carrier question + answer, one pair per line, for a late answer turn.

    The courier's inquire does not block, so by the time the owner answers, the
    turn that asked is over and its tool call is not in the clone's history —
    the answer must carry its own question, or the courier reads "周六" with no
    idea what it refers to.
    """
    answers = value if isinstance(value, list) else [value]
    out = []
    for i, q in enumerate(questions):
        item = q if isinstance(q, dict) else {}
        answer = answers[i] if i < len(answers) else ""
        out.append(f"问: {item.get('question') or ''}\n答: {answer}")
    return "\n".join(out)


class LocalTransport:
    """For clones hosted on the server process itself: direct relay + store."""

    def __init__(self, relay: Relay, self_addr: str, hub=None):
        self.relay = relay
        self.hub = hub
        self.inbox = relay.register_local(self_addr)
        self.host, _role, _peer = parse_addr(self_addr)

    def mail(self) -> dict:
        if self.hub is None:
            return {"mails": []}
        return self.hub.mail.list(self.host)

    def send(self, env: Envelope) -> dict:
        # via hub.send so ask/answer registry maintenance applies locally too
        if self.hub is not None:
            return self.hub.send(env)
        return self.relay.deliver(env)

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        return self.inbox.after(after, timeout)

    def wake(self) -> None:
        """End a poll now (see Inbox.wake)."""
        self.inbox.wake()

    def fs(self, op: str, path: str, **kw) -> dict:
        if self.hub is None:
            return {"error": "no hub attached"}
        return fs_via_hub(self.hub.store, self.host, op, path, **kw)

    def transfer(self, path: str, name: str, to_host: str) -> dict:
        if self.hub is None:
            return {"error": "no hub attached"}
        return self.hub.create_transfer(self.host, path, to_host, name)

    def upload_transfer(self, path: str, name: str, to_host: str, progress=None) -> dict:
        """Stage a real local file (server-role: the file is on this disk).

        `progress(sent, total)` mirrors the remote client's: the send-file modal
        shows the same bar whether the bytes leave this process over HTTP or
        land on the hub in-process.
        """
        if self.hub is None:
            return {"error": "no hub attached"}
        src = Path(path)
        if not src.is_file():
            return {"error": f"no such file: {path}"}
        total = src.stat().st_size
        sent = 0

        def read(size: int) -> bytes:
            nonlocal sent
            chunk = src_fh.read(size)
            sent += len(chunk)
            if progress is not None:
                progress(sent, total)
            return chunk

        try:
            with src.open("rb") as src_fh:
                return self.hub.upload_transfer(self.host, to_host, name, read)
        except OSError as exc:
            return {"error": f"cannot read {path}: {exc}"}

    def download_transfer(self, transfer_id: str, dest: Path) -> None:
        if self.hub is None:
            raise RuntimeError("no hub attached")
        found = self.hub.transfers.fetchable(transfer_id, self.host)
        if found is None:
            raise RuntimeError("transfer not found or not for this host")
        _rec, path = found
        shutil.copyfile(path, dest)

    def discard_transfer(self, transfer_id: str) -> None:
        if self.hub is not None:
            self.hub.transfers.discard(transfer_id)


class RemoteTransport:
    """For clones on client hosts: HTTP to the hub.

    Poll source is injectable: a multi-clone client host runs one HostPoller
    over the shared host buffer and feeds each clone its own Inbox — polling
    the shared buffer directly would let clones steal each other's messages.
    """

    def __init__(self, client: HubClient, inbox=None):
        self.client = client
        self.inbox = inbox

    def send(self, env: Envelope) -> dict:
        return self.client.send(env)

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        if self.inbox is not None:
            return self.inbox.after(after, timeout)
        return self.client.poll(after, timeout)

    def mail(self) -> dict:
        return self.client.mail()

    def fs(self, op: str, path: str, **kw) -> dict:
        return self.client.fs(op, path, **kw)

    def transfer(self, path: str, name: str, to_host: str) -> dict:
        return self.client.create_transfer(path, name, to_host)

    def upload_transfer(self, path: str, name: str, to_host: str, progress=None) -> dict:
        return self.client.upload_transfer(path, name, to_host, progress)

    def download_transfer(self, transfer_id: str, dest: Path) -> None:
        self.client.download_transfer(transfer_id, dest)

    def discard_transfer(self, transfer_id: str) -> None:
        self.client.discard_transfer(transfer_id)


class Clone:
    """One Orchestrator clone: role prompt + toolset + inbox loop."""

    def __init__(
        self,
        addr: str,
        transport: LocalTransport | RemoteTransport,
        cfg: Config,
        sink: Sink,
        tools: dict[str, BoundTool] | None = None,
        system_prompt: str = "",
        llm=None,
        model: str | None = None,
        poll_timeout: float = 5.0,
        on_ask=None,
        on_transfer=None,
        on_turn_end=None,
        subagents: bool = True,
        on_direct=None,
        pending: PendingAsks | None = None,
        tool_names: frozenset[str] | set[str] = frozenset(tools.BASE_TOOL_NAMES),
        child_tool_names: frozenset[str] | None = None,
        child_extra_tools: dict[str, BoundTool] | None = None,
        skill_save: bool = False,
    ):
        self.addr = addr
        self.host, self.role, self.peer = parse_addr(addr)
        self.transport = transport
        self.cfg = cfg
        self.sink = sink
        self.tools = tools or {}
        # Native base-tool whitelist; comm clones pass frozenset() so only the
        # guarded extra tools exist (spec 6.1; the path guard is server-side).
        self.tool_names = frozenset(tool_names)
        # Spawned worker surface (None -> TriLayer native defaults)
        self.child_tool_names = child_tool_names
        self.child_extra_tools = child_extra_tools
        # User-facing clones may author skills; comm clones stay readonly.
        self.skill_save = skill_save
        # str, or a callable returning str (re-evaluated every turn: the comm
        # clone re-reads courier_memory so GUI edits apply live)
        self.system_prompt = system_prompt
        self.llm = llm
        self.model = model
        self.poll_timeout = poll_timeout
        self.on_ask = on_ask
        # transfer envelopes: handled by comm clones (consent -> download);
        # None -> the transfer is answered with an error result.
        self.on_transfer = on_transfer
        # courier-off hook: called for chat/transfer envelopes BEFORE a turn
        # is queued. Returning True means "delivered directly to the user"
        # (friend-view transcript / consent card) — the LLM never wakes.
        self.on_direct = on_direct
        # called with (env_type, messages, agent) after every chat/task turn —
        # the room records per-peer transcripts for the friend view.
        self.on_turn_end = on_turn_end
        # Courier clones run unattended for every peer message: no fan-out
        # (see TriLayer.build_clone_agent's subagents switch).
        self.subagents_enabled = subagents
        # `is not None` (not `or`): an empty PendingAsks is falsy via __len__
        self.pending = pending if pending is not None else PendingAsks()
        self.history: list[dict] = []  # chat exchanges kept locally (no-reply stays)
        self._cursor = 0
        self._stop = threading.Event()
        self._work: queue.Queue[Envelope] = queue.Queue()
        self._loop_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

    # ── lifecycle ──

    def note(self, text: str, *, source: str = "owner") -> None:
        """A local turn for this clone, labelled with where it came from.

        The owner's feedback on one of this clone's reports rides the chat path
        (same history, same report back to the owner) but is never handed to the
        transport, so the counterpart hears nothing: the feedback box in the
        friend view is between the owner and their courier (2026-09-11 user
        instruction). An external channel the agent watches (a game it plays)
        takes the same path under its own label, so the agent never reads a
        player as its owner.
        """
        body: dict = {"text": text}
        if source == "owner":
            body["from_owner"] = True
        else:
            body["from_channel"] = source
        self._work.put(Envelope(src=f"{self.host}:{source}", dst=self.addr, type="chat", body=body))

    def start(self) -> None:
        self._loop_thread = threading.Thread(
            target=self._loop, name=f"clone-loop-{self.addr}", daemon=True
        )
        self._worker_thread = threading.Thread(
            target=self._work_loop, name=f"clone-turn-{self.addr}", daemon=True
        )
        self._loop_thread.start()
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._work.put(None)  # unblock the worker
        wake = getattr(self.transport, "wake", None)
        if wake is not None:
            wake()  # in-process inbox: end the wait now instead of after the timeout
        # Both threads are daemons, and a poll against a REMOTE inbox (HTTP
        # long-poll) cannot be aborted from here — it ends on its own and then
        # sees the flag. Waiting for it used to block the caller for the whole
        # poll timeout per clone (the GUI's "leave room" froze for seconds).
        for t in (self._loop_thread, self._worker_thread):
            if t is not None:
                t.join(timeout=0.2)

    # ── threads ──

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                messages, cursor = self.transport.poll(self._cursor, self.poll_timeout)
            except Exception:
                self._stop.wait(1.0)  # transient network hiccup: back off, keep looping
                continue
            self._cursor = cursor
            for env in messages:
                self.dispatch(env)

    def _work_loop(self) -> None:
        while True:
            env = self._work.get()
            if env is None or self._stop.is_set():
                return
            try:
                self.run_turn(env)
            except Exception as exc:  # a turn must never kill the worker
                self.sink.emit("error", f"{self.addr}: turn failed: {exc}")

    def dispatch(self, env: Envelope) -> None:
        """Control envelopes wake blocked tools inline; turns are queued."""
        if env.type == "answer" and env.reply_to:
            if not self.pending.resolve(env.reply_to, env.body.get("value")):
                self._answer_turn(env)
        elif env.type == "result" and env.reply_to:
            self.pending.resolve(env.reply_to, env.body)
        elif env.type == "err":
            self.sink.emit("error", f"{self.addr}: {env.body.get('error')}")
        elif env.type == "ask":
            if self.on_ask is not None:
                self.on_ask(env)
        elif env.type in DIRECT_TYPES:
            # courier-off: the room may deliver chats/transfers straight to
            # the user (friend-view transcript / consent card) without a turn
            if self.on_direct is not None and self.on_direct(env):
                return
            self._work.put(env)
        elif env.type in TURN_TYPES:
            self._work.put(env)

    def _answer_turn(self, env: Envelope) -> None:
        """An answer nobody is blocked on: the owner answering a question this
        clone asked (its inquire does not block, so the turn that asked it
        ended long ago). Turn it into a chat turn of its own.

        A verdict without carrier questions gets no turn: it belongs to a
        consent ask whose tool already timed out, and there is nothing to
        interpret — a bare "yes" would only confuse the courier.
        """
        questions = env.body.get("questions")
        if not isinstance(questions, list) or not questions:
            return
        self._work.put(
            Envelope(
                src=env.src,
                dst=self.addr,
                type="chat",
                body={"text": _qa_lines(questions, env.body.get("value")), "owner_answer": True},
            )
        )

    # ── turns ──

    def render_input(self, env: Envelope) -> str:
        if env.type == "task":
            body = env.body
            parts = [f"[TASK from {env.src}]", f"Goal: {body.get('goal', '')}"]
            if body.get("reply_format"):
                parts.append(f"Reply format: {body['reply_format']}")
            if body.get("context"):
                parts.append(f"Context: {body['context']}")
            return "\n".join(parts)
        if env.body.get("from_channel"):
            # Something outside Fungi is being watched (a game the agent
            # plays): the label names it, so this is not mistaken for the
            # owner's words.
            return f"[{env.body['from_channel']}] {env.body.get('text', '')}"
        if env.body.get("from_human"):
            # A human sent this from their friend view: the courier relays
            # it faithfully instead of passing it off as the peer clone.
            who = str(env.body.get("sender_name") or parse_addr(env.src)[0])
            return f"[来自 {who} 的用户] {env.body.get('text', '')}"
        if env.body.get("from_owner"):
            # 评价, from the friend view's box under a report: OUR owner talking
            # to us. Nothing about it goes to the counterpart. (The label was
            # [主人的反馈] until 2026-09-11 — the prompt in clone/comm.py carries
            # the same string, and must, or the courier cannot recognise it.)
            return f"[评价] {env.body.get('text', '')}"
        if env.body.get("owner_answer"):
            # The owner's answer to a question this clone asked (see
            # _answer_turn): a turn of its own, carrying the question with it.
            return f"[主人的答复] {env.body.get('text', '')}"
        return f"[{env.src}] {env.body.get('text', '')}"

    def resolved_prompt(self) -> str:
        """system_prompt, callable or not, evaluated fresh for this turn."""
        sp = self.system_prompt
        return sp() if callable(sp) else sp

    def build_agent(self, system_prompt: str | None = None) -> Agent:
        """Per-turn agent: clone's whitelist + guarded extra tools + spawn
        (TriLayer L2/L3; children inherit this clone's file surface)."""
        trilayer = TriLayer(
            self.cfg,
            self.sink,
            llm=self.llm,
            child_tool_names=self.child_tool_names,
            child_extra_tools=self.child_extra_tools,
            skill_save=self.skill_save,
        )
        return trilayer.build_clone_agent(
            self.sink,
            system_prompt=system_prompt if system_prompt is not None else self.resolved_prompt(),
            extra_tools=self.tools,
            tool_names=self.tool_names,
            model=self.model,
            subagents=self.subagents_enabled,
        )

    def run_turn(self, env: Envelope) -> None:
        if env.type == "transfer":
            self._run_transfer(env)
            return
        agent = self.build_agent()
        if env.type == "chat":
            # Chat turns ride the accumulated local history.
            messages = [*self.history, {"role": "user", "content": self.render_input(env)}]
        else:
            messages = [{"role": "user", "content": self.render_input(env)}]
        result = agent.run(messages)
        if self.on_turn_end is not None:
            self.on_turn_end(env.type, messages, agent)
        reply = (result.content or "").strip()
        if env.type == "chat":
            # The counterpart hears from this clone only through send_peer; the
            # turn's own text is the report to this host's user, and it stays in
            # the transcript (see comm._chat_end for what doubled as a message
            # before 2026-09-10). A report of <<SILENT>> is stripped downstream.
            self.history.append({"role": "user", "content": self.render_input(env)})
            if reply:
                self.history.append({"role": "assistant", "content": reply})
            del self.history[:-MAX_CHAT_HISTORY]
            return
        self.transport.send(
            Envelope(
                src=self.addr,
                dst=env.src,
                type="result",
                body={"ok": True, "payload": reply},
                reply_to=env.id,
            )
        )

    def _run_transfer(self, env: Envelope) -> None:
        """Incoming file transfer: consent via on_transfer, result back to src."""
        if self.on_transfer is None:
            body: dict = {"ok": False, "error": "transfers not supported here"}
        else:
            try:
                body = self.on_transfer(env)
            except Exception as exc:
                body = {"ok": False, "error": str(exc)}
        self.transport.send(
            Envelope(
                src=self.addr,
                dst=env.src,
                type="result",
                body=body,
                reply_to=env.id,
            )
        )
