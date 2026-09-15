"""Room mode: one process per host — tray (main thread) + hub/client + clones.

Server role: hosts the hub itself; clones ride LocalTransport (direct relay).
Client role: one HubClient; a single HostPoller drains the host buffer on the
server and fans envelopes out to per-clone inboxes (clones must not share one
cursor). Roster diffs (server monitor thread / client heartbeat) add and
remove comm clones on both ends. The WebUI starts lazily from the tray and
runs the local clone's toolset with per-host sessions (server: hub store;
client: the joining machine's own disk — conversations must not land on the
peer-operated hub).

Consent modes (WebUI friend-view slider): a host set to "allow" gets its
consent-shaped asks (file ops, transfers) auto-answered yes at on_ask — no
notification, no card. Mode "ask" (default) raises a card; modes are visible
and reversible in the friend view.
"""

import contextlib
import json
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from .agent import Agent
from .cards import AskCards
from .clone.base import Clone, LocalTransport, RemoteTransport
from .clone.comm import SILENT_REPLY, build_comm_clone
from .clone.local import build_local_clone
from .config import PROJECT_ROOT, Config, load_config
from .consent_rules import ConsentRules
from .diary import bound as diary_bound
from .diary import section as diary_section
from .events import Sink
from .hub.app import Hub, safe_name
from .hub.client import HubClient, HubError
from .hub.relay import Inbox
from .protocol import Envelope, clean_display, parse_addr, valid_host_name
from .server import _BG_ABORTS, _PENDING_SPAWNS, WebUIRuntime, make_webui_server
from .session import SESSIONS_DIR, SessionStore
from .tools.ask import make_ask_tool, resolve_ask
from .trilayer import TriLayer
from .xfer import TransferJobs

MONITOR_INTERVAL_S = 2.0
HEARTBEAT_INTERVAL_S = 10.0
MAX_TRANSCRIPT_MESSAGES = 400  # friend-view transcript cap (messages per peer)


def _card_conv(src: str) -> str:
    """Friend conversation an ask envelope belongs to: the envelope src is
    the raising comm clone ("<host>:comm-<peer>"); its agent suffix names it."""
    host_part, _, agent_part = str(src).partition(":")
    return agent_part[len("comm-") :] if agent_part.startswith("comm-") else host_part


def _is_consent(body: dict) -> bool:
    """Consent-shaped ask: produced by confirm (action + path present)."""
    return bool(body.get("action")) and bool(body.get("path"))


def _comparable(msgs: list[dict]) -> list[tuple]:
    """Rows reduced to what makes a transcript row *that* row.

    Only identity: role, content, the tool call it answers and the calls it
    made. The clone's history copy of a turn is not byte-identical to the
    stored one — `run_turn` re-appends the assistant reply without the
    reasoning the agent's copy carried — so comparing whole dictionaries read
    an identical row as a brand-new one. That mis-alignment then took the
    "clone forgot rows" path, which duplicated the courier's last reply in the
    friend view and hoisted it above its own question (2026-09-10: the peer
    sent two messages, the courier answered both, the newest transcript showed
    the first reply twice).
    """
    out = []
    for m in msgs:
        calls = m.get("tool_calls") or []
        out.append(
            (
                m.get("role"),
                m.get("content"),
                m.get("tool_call_id"),
                tuple((c.get("id"), (c.get("function") or {}).get("name")) for c in calls),
            )
        )
    return out


def _drop_silent(msgs: list[dict]) -> list[dict]:
    """Transcript rows with the courier's abstention marker removed.

    `<<SILENT>>` is a delivery control token (clone/comm.SILENT_REPLY): when the
    courier has nothing to say, the bare marker suppresses the reply. It is not
    something to read later — and the browser renders the leftover marker as
    `<>`, because HTML throws the unknown tag away (2026-09-10 user report).

    Both sides of `merge_comm_history` go through this, so a stripped stored row
    still lines up with the clone's copy of it; a row whose only content was the
    marker keeps its reasoning/tool calls and loses the empty bubble.
    """
    out = []
    for m in msgs:
        content = m.get("content")
        if not isinstance(content, str) or SILENT_REPLY not in content:
            out.append(m)
            continue
        stripped = content.replace(SILENT_REPLY, "").strip()
        if not stripped and not m.get("tool_calls") and not m.get("reasoning"):
            continue  # a turn that only said "nothing" leaves no row at all
        out.append({**m, "content": stripped or None})
    return out


def merge_comm_history(prev: list[dict], fresh: list[dict], ts: float | None = None) -> list[dict]:
    """Transcript body (system prompt excluded) after a chat turn.

    The stored transcript is the base, and it only grows: a row the clone still
    carries keeps the transcript's own copy — its ts, and the display payload
    the lean history copy no longer has — and what the clone has and the store
    does not is appended, stamped with `ts` (the friend view merges the
    transcript with the timestamped ask/event/mail rows by that stamp).

    The clone's history is no row-for-row mirror of the transcript. It carries
    the conversation — the peer's rows and the courier's report text — while the
    transcript also holds what the agent wrote during the turn: its reasoning,
    its tool calls and their results. Comparing position by position therefore
    stopped at the first tool row, concluded "the clone forgot everything", and
    re-appended the whole conversation at the end. On a transcript that had
    already accumulated such rows that hoisted them: the tools and thinking
    recorded first stayed on top while every line of dialogue sank below them,
    and the file was already wrong on the next refresh (2026-09-11 user report:
    the dialogue sank to the bottom while the tools and thinking floated up).
    The match now walks *past* those rows — each stored row is claimed at most
    once — and only the carried rows after the last claim count as new; rows the
    store no longer has (history trimmed) are left alone rather than re-appended.
    """
    now = time.time() if ts is None else ts
    stored = _drop_silent([m for m in prev if m.get("role") != "system"])
    carried = _drop_silent([m for m in fresh if m.get("role") != "system"])
    stored_c, carried_c = _comparable(stored), _comparable(carried)
    merged = list(stored)
    si, last_claim = 0, -1
    for ci, row in enumerate(carried):
        comp = carried_c[ci]
        j = si
        while j < len(stored_c) and stored_c[j] != comp:
            j += 1  # a row only the store has (reasoning / tool call): it keeps its place
        if j == len(stored_c):
            continue  # the clone has it, the store does not (yet)
        merged[j] = {
            **row,
            **{k: v for k, v in stored[j].items() if k != "ts"},
            "ts": stored[j].get("ts", now),
        }
        si, last_claim = j + 1, ci
    return merged + [{**m, "ts": now} for m in carried[last_claim + 1 :]]


MAX_LIVE_EVENTS = 400  # per-peer live turn tape cap (friend-view spectating)
_LIVE_EVENT_KINDS = {
    "text",
    "reasoning",
    "reasoning_start",
    "reasoning_end",
    "tool",
    "tool_result",
    "status",
    "error",
}


class HostPoller:
    """Client-side fanout: one cursor on the shared host buffer, per-clone
    inboxes on this side. Unknown destinations are dropped (clone removed)."""

    def __init__(self, client: HubClient, host: str, poll_timeout: float = 5.0):
        self.client = client
        self.host = host
        self.poll_timeout = poll_timeout
        self._inboxes: dict[str, Inbox] = {}
        self._cursor = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def inbox_for(self, addr: str) -> Inbox:
        inbox = self._inboxes.get(addr)
        if inbox is None:
            inbox = Inbox()
            self._inboxes[addr] = inbox
        return inbox

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"poller-{self.host}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # The loop is a daemon and its in-flight poll is an HTTP long-poll we
        # cannot abort: it returns, sees the flag and exits. Joining it here
        # only made the caller wait out that timeout.
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                messages, self._cursor = self.client.poll(self._cursor, self.poll_timeout)
            except Exception:
                self._stop.wait(1.0)  # transient network hiccup: back off, keep looping
                continue
            for env in messages:
                inbox = self._inboxes.get(env.dst)
                if inbox is not None:
                    inbox.push(env)


def ensure_ghostworld_watch(room) -> None:
    """GhostWorld character control (spec §43): arm the player-speech wake.

    Module-level rather than a method: the two room roles that own a local clone
    are RoomBase subclasses, but the per-turn agent is built by RoomRuntime, which
    has no room of its own. Idempotent, and re-read live so the settings switch
    needs no restart — off disarms at once, because an agent woken by a game it may
    not touch is worse than no watcher. The connector owns the child process; this
    only decides whether it should exist.
    """
    live = load_config()
    module = sys.modules.get("fungi.tools.ghostworld")
    if module is None:
        if not live.ghostworld:
            return
        from fungi.tools import ghostworld as module  # noqa: PLC0415
    elif not live.ghostworld:
        module.disarm()
        return
    module.arm(
        str(live.ghostworld_dir or ""),
        on_wake=lambda evt: room.local.note(module.wake_text(evt), source="GhostWorld"),
    )
    # The clone's own tool dict drives inbox (wake) turns; the room's per-turn
    # dict is a copy of it, so adding it there covers both.
    if room._local is not None and "ghostworld" not in room._local.tools:
        room._local.tools.update(module.bound(live))


class RoomBase:
    """Shared assembly: local clone + comm clones + card asks + WebUI runtime."""

    role = "base"

    def __init__(
        self,
        host: str,
        cfg: Config,
        sink: Sink,
        llm=None,
        rules_path: Path | None = None,
        display: str = "",
    ):
        self.host = host
        self.display = display
        self.cfg = cfg
        self.sink = sink
        self.llm = llm
        self.cards = AskCards()
        self.rules = ConsentRules(rules_path)
        self.local_addr = f"{host}:local"
        self._local: Clone | None = None
        self._clones: dict[str, Clone] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._webui: ThreadingHTTPServer | None = None
        self._webui_guard = threading.Lock()
        # Per-peer comm clone transcripts (friend view); set by subclasses.
        self._comm_store: SessionStore | None = None
        # Live turn events per comm clone peer (friend-view spectating):
        # filled by the tagged sink while a turn runs, cleared on turn end
        # when the durable transcript takes over.
        self._live_tapes: dict[str, list] = {}
        self._live_lock = threading.Lock()
        # Courier-off transfers awaiting the user's card answer:
        # envelope id -> (original transfer envelope, sender host).
        # Courier mail wake (see _mail_watch_loop): started lazily with the
        # first comm clone.
        self._mail_thread: threading.Thread | None = None
        # Unread mail from the last mailbox poll (see _mail_watch_loop): the GUI
        # rings on it. One poller, two consumers — the GUI never asks the hub.
        self.last_unread = 0
        # Send-file jobs the WebUI's modal polls (see fungi/xfer.py).
        self.xfer_jobs = TransferJobs()
        self._direct_transfers: dict[str, tuple] = {}
        # Per-sid write lock for comm transcripts: the clone worker (turn
        # end), the clone poll loop (courier-off direct) and WebUI HTTP
        # threads all read-modify-write the same comm-<peer>.json.
        self._comm_locks: dict[str, threading.Lock] = {}

    def _comm_lock(self, sid: str) -> threading.Lock:
        with self._guard:
            lock = self._comm_locks.get(sid)
            if lock is None:
                lock = threading.Lock()
                self._comm_locks[sid] = lock
            return lock

    # ── clones ──

    @property
    def local(self) -> Clone:
        if self._local is None:
            raise RuntimeError("room not started")
        return self._local

    def _make_local(self, transport) -> Clone:
        return build_local_clone(
            self.host,
            transport,
            self.cfg,
            self.sink,
            llm=self.llm,
            peers_fn=self._peers,
            on_ask=self._on_ask,
            # The shared store sits on the hub host's disk: visible locally
            # (repo data/) for the server role, remote for a joiner.
            local_store=self.role == "server",
        )

    def _peers(self) -> list[str]:
        raise NotImplementedError

    def set_display(self, display: str) -> str:
        """Rename this host's presentation nickname, live.

        Only the nickname is renameable while the room runs: the wire name is
        baked into addresses, the roster key, data/ file names and every peer's
        comm clone, so it is fixed until the room is left and started again.
        A rename is enough on its own — `roster.join` refreshes the display the
        peers see, and every later message picks it up (`comm_send_human` reads
        `self.display` per send, the courier prompt re-reads it per turn).
        Returns the nickname the hub ended up storing.
        """
        wanted = clean_display(display)
        hub = getattr(self, "hub", None)
        if hub is not None:  # server role: its own roster is local
            hub.join(self.host, "127.0.0.1", wanted)
            self.display = hub.roster.display(self.host) or wanted
            return self.display
        client = getattr(self, "client", None)
        if client is not None:  # client role: re-join over HTTP refreshes it
            client.display = wanted
            with contextlib.suppress(Exception):  # a hiccup must not lose the name locally
                client.join()
        self.display = wanted
        return self.display

    def add_comm_clone(self, peer: str, transport) -> None:
        with self._guard:
            if peer in self._clones or self._stop.is_set():
                return
        clone = build_comm_clone(
            self.host,
            peer,
            transport,
            self.cfg,
            self._live_sink(peer),
            llm=self.llm,
            inbox_dir=Path(self.cfg.inbox_dir) if self.cfg.inbox_dir else None,
            on_turn_end=lambda env_type, messages, agent: self._record_comm_turn(
                peer, env_type, messages, agent
            ),
            on_direct=self._courier_direct,
        )
        with self._guard:
            self._clones[peer] = clone
        # Starts the poll/worker threads. Without this the clone never drains
        # its inbox: cross-host chat/delegate/send_file all died silently
        # while tests passed (they call clone.start() themselves).
        clone.start()
        # Courier mail wake: the hub consumes mail envelopes, so the message
        # never reaches this clone — the poller is what keeps the courier able
        # to answer it (see _mail_watch_loop).
        self._ensure_mail_watch()

    def _live_sink(self, peer: str) -> Sink:
        """Sink wrapper that mirrors turn events into the peer's live tape
        while still forwarding to the room sink (console/GUI). Must stay a
        Sink object — the agent calls .emit on it."""
        inner = self.sink

        class _TapedSink:
            def emit(self, event_type: str, content=None) -> None:
                if event_type in _LIVE_EVENT_KINDS:
                    with room._live_lock:
                        tape = room._live_tapes.setdefault(peer, [])
                        tape.append({"kind": event_type, "content": content})
                        del tape[:-MAX_LIVE_EVENTS]
                inner.emit(event_type, content)

        room = self
        return _TapedSink()

    def live_tape(self, peer: str) -> list:
        with self._live_lock:
            return list(self._live_tapes.get(peer) or [])

    def _clear_live_tape(self, peer: str) -> None:
        with self._live_lock:
            self._live_tapes.pop(peer, None)

    def remove_comm_clone(self, peer: str) -> None:
        with self._guard:
            clone = self._clones.pop(peer, None)
        if clone is not None:
            clone.stop()
        self._clear_live_tape(peer)  # a gone clone must not leave a stale live view

    def _record_comm_turn(self, peer: str, env_type: str, messages: list, agent) -> None:
        """Persist one comm clone turn for the friend view (session-style
        rendering: markdown, tool calls, spawn blocks). Chat turns carry the
        cumulative clone history; task turns append after it."""
        store = self._comm_store
        if store is None or not messages:
            return
        with self._comm_lock("comm-" + peer):
            try:
                sid = "comm-" + peer
                prev = store.load(sid) or {}
                ts = time.time()
                if env_type == "chat":
                    # Cumulative clone history: the overlap decides whether it
                    # replaces the stored copy or only extends it (see merge).
                    head = [messages[0]] if messages and messages[0].get("role") == "system" else []
                    msgs = head + merge_comm_history(prev.get("messages") or [], messages, ts)
                else:
                    msgs = [m for m in prev.get("messages") or [] if m.get("role") != "system"]
                    msgs += [
                        {**m, "ts": ts}
                        for m in _drop_silent([m for m in messages if m.get("role") != "system"])
                    ]
                    msgs.insert(0, messages[0])
                subs = list(prev.get("subagents") or [])
                new_subs = getattr(agent, "subagents", None)
                if isinstance(new_subs, dict):
                    known = {r.get("id") for r in subs}
                    subs += [r for r in new_subs.values() if r.get("id") not in known]
                asks = list(prev.get("asks") or []) + list(getattr(agent, "asks", None) or [])
                del msgs[:-MAX_TRANSCRIPT_MESSAGES]
                store.save(sid, f"comm: {peer}", msgs, subagents=subs[-100:], asks=asks[-100:])
            finally:
                # Durable transcript (or a dead turn) takes over from the live view.
                self._clear_live_tape(peer)

    def _append_comm_message(self, peer: str, msg: dict) -> None:
        """Append one message to the peer's friend-view transcript without a
        clone turn (courier-off direct chats, human direct sends). Merges
        with the stored history instead of replacing it."""
        store = self._comm_store
        if store is None:
            return
        sid = "comm-" + peer
        with self._comm_lock(sid):
            prev = store.load(sid) or {}
            msgs = list(prev.get("messages") or [])
            msgs.append({**msg, "ts": msg.get("ts") or time.time()})
            del msgs[:-MAX_TRANSCRIPT_MESSAGES]
            store.save(
                sid,
                prev.get("title") or f"comm: {peer}",
                msgs,
                subagents=prev.get("subagents") or [],
                asks=prev.get("asks") or [],
            )

    # ── incoming asks: auto-allow -> cards + notification ──

    def _on_ask(self, env: Envelope) -> None:
        # The logical requester is body["from"] (confirm/transfer both set
        # it). For transfer receipts the envelope src is the receiving host's
        # OWN comm clone — keying the mode on env.src would key the friend's
        # slider to the wrong host.
        src = str(env.body.get("from") or env.src)
        if _is_consent(env.body) and self.rules.allows(src):
            self._send_answer(env, "yes")  # slider says allow: silent grant
            return
        if not self.cards.record(env):
            return  # replay/duplicate

    def _send_answer(self, ask: Envelope, value) -> None:
        direct = self._direct_transfers.pop(ask.id, None)
        if direct is not None:
            env, src_host = direct
            if value == "yes":
                body = self._direct_download(env, src_host)
            else:
                body = {"ok": False, "error": "declined by the receiving user"}
            self.local.transport.send(
                Envelope(
                    src=self.local_addr,
                    dst=env.src,
                    type="answer",
                    body={"value": body},
                    reply_to=env.id,
                )
            )
            return
        body: dict = {"value": value}
        questions = ask.body.get("questions")
        if isinstance(questions, list) and questions:
            # The courier's inquire does not block, so this answer arrives after
            # the turn that asked it: the raising clone turns it into a fresh
            # turn of its own and needs the question to make it readable
            # (_qa_lines). A consent verdict carries `question`, never
            # `questions`, so it is never turned into a turn.
            body["questions"] = questions
        self.local.transport.send(
            Envelope(
                src=self.local_addr,
                dst=ask.src,
                type="answer",
                body=body,
                reply_to=ask.id,
            )
        )

    # ── courier-off direct delivery (zero agent cost) ──

    def _courier_direct(self, env: Envelope) -> bool:
        """Clone.on_direct hook. Courier on (default) -> normal LLM turns.
        Courier off -> chats land in the friend-view transcript and transfer
        consents become cards directly; the local comm clone never wakes.
        Returns True when the envelope was handled here."""
        if load_config().courier:
            return False  # re-read every envelope: the switch applies live
        if env.type == "chat":
            peer = parse_addr(env.src)[0]
            self._append_comm_message(
                peer, {"role": "user", "content": f"[{env.src}] {env.body.get('text', '')}"}
            )
            return True
        if env.type == "transfer":
            return self._direct_transfer(env)
        return False

    def _direct_transfer(self, env: Envelope) -> bool:
        """Courier-off transfer: synthesize the consent ask ourselves (same
        card pipeline as the courier path), remember the envelope so the
        answer downloads the file without waking any agent."""
        body = env.body
        name = safe_name(str(body.get("name") or "file"))
        size = body.get("size")
        reason = str(body.get("reason") or "")
        src_host, _role, _peer = parse_addr(str(body.get("from") or env.src))
        if not valid_host_name(src_host):
            # inbox/<src_host>/ is a path join: never let a malformed host reach it.
            self.sink.emit("error", f"rejected transfer with bad sender host {src_host!r}")
            return True
        ask = Envelope(
            src=env.src,
            dst=self.local_addr,
            type="ask",
            id=env.id,  # answer carries this id back to the waiting sender
            body={
                "from": env.src,
                "action": "receive file",
                "path": name,
                "reason": reason,
                "question": self._transfer_question(src_host, name, size, reason, body),
            },
        )
        self._direct_transfers[env.id] = (env, src_host)
        self._on_ask(ask)
        return True

    @staticmethod
    def _transfer_question(src_host: str, name: str, size, reason: str, body: dict) -> str:
        if body.get("from_human"):
            who = str(body.get("sender_name") or src_host)
            sender = f"来自 {who} 的用户"
        else:
            sender = src_host
        return (
            f"{sender} wants to send you a file: {name} "
            f"({size} bytes). Accept?\nReason: {reason}\n"
            f"接收后将保存到 inbox/{src_host}/{name}"
        )

    def _direct_download(self, env: Envelope, src_host: str) -> dict:
        """Courier-off accepted transfer: land the bytes like receive_transfer."""
        body = env.body
        dest_dir = (
            Path(self.cfg.inbox_dir) / src_host
            if self.cfg.inbox_dir
            else PROJECT_ROOT / "inbox" / src_host
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem, suffix = (
            Path(safe_name(str(body.get("name") or "file"))).stem,
            Path(safe_name(str(body.get("name") or "file"))).suffix,
        )
        dest = dest_dir / f"{stem}{suffix}"
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stem}-{n}{suffix}"
            n += 1
        try:
            self.local.transport.download_transfer(str(body.get("id")), dest)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        # delivered: drop the hub's staged copy so it does not linger
        with contextlib.suppress(Exception):
            self.local.transport.discard_transfer(str(body.get("id")))
        return {"ok": True, "saved": str(dest)}

    # ── courier mail wake ──

    MAIL_POLL_S = 3.0

    def _ensure_mail_watch(self) -> None:
        if self._mail_thread is not None:
            return
        self._mail_thread = threading.Thread(
            target=self._mail_watch_loop, name="mail-watch", daemon=True
        )
        self._mail_thread.start()

    def _mail_watch_loop(self) -> None:
        """Human text messages (type mail) are consumed by the hub: they land
        in the mailbox and the envelope never reaches the receiving clone, so
        the courier cannot answer them (470b07c decoupling regression). Poll
        the mailbox; unseen inbound mail + courier on -> synthesize a human
        chat turn for the comm clone. Courier off: ids are noted only — the
        friend view renders mail straight from the store."""
        seen: set[str] | None = None  # None = the first pass only seeds the cursor
        while not self._stop.is_set():
            if seen is None:
                # Seed at once (not after the first sleep): mail that arrives
                # after this room is up must wake the courier, only history
                # predating the room is skipped.
                try:
                    data = self.local.transport.mail()
                except Exception:
                    self._stop.wait(1.0)
                    continue
                seen = {str(m.get("id")) for m in data.get("mails") or []}
                self.last_unread = int(data.get("unread") or 0)
            else:
                self._mail_poll_once(seen)
            self._stop.wait(self.MAIL_POLL_S)

    def _mail_poll_once(self, seen: set[str]) -> None:
        try:
            data = self.local.transport.mail()
        except Exception:
            return
        rows = [m for m in (data.get("mails") or []) if not m.get("mine")]
        self.last_unread = int(data.get("unread") or 0)
        fresh = [m for m in rows if str(m.get("id")) not in seen]
        for m in fresh:
            seen.add(str(m.get("id")))
        if not fresh or not load_config().courier:
            return
        for m in fresh:
            sender = str(m.get("from") or "")
            sender_host = sender.split(":")[0] if sender else str(m.get("peer") or "")
            with self._guard:
                clone = self._clones.get(str(m.get("peer") or sender_host))
            if clone is None:
                continue
            clone.dispatch(
                Envelope(
                    src=sender or f"{sender_host}:human",
                    dst=clone.addr,
                    type="chat",
                    body={
                        "text": str(m.get("body") or ""),
                        "from_human": True,
                        "sender_name": sender_host,
                    },
                )
            )

    def _xfer_progress(self, job: str | None):
        """Progress sink for a send-file job, or None when nobody polls it."""
        if not job:
            return None

        def report(done: int, _total: int) -> None:
            self.xfer_jobs.progress(job, done)

        return report

    # ── human direct sends (friend view composer) ──
    def comm_send_human(
        self,
        peer: str,
        text: str | None = None,
        file_path: str | None = None,
        job: str | None = None,
    ) -> dict:
        """Human sends a message/file straight from the friend view. Text
        goes into the unified amail store (both mailboxes, zero agent
        involvement); files stage on the hub and the PEER's consent flow
        decides landing.

        `job` is the caller's own id for the send-file modal: the bytes to the
        hub are counted into it here (see fungi/xfer.py), and the reply keeps
        the same shape with or without one.
        """
        with self._guard:
            clone = self._clones.get(peer)
        if clone is None:
            return {"error": f"no comm clone for {peer}"}
        sender_name = self.display or self.host
        if file_path:
            path = Path(file_path)
            if not path.is_file():
                return {"error": f"no such file: {file_path}"}
            name = path.name
            if job:
                self.xfer_jobs.start(job, name, path.stat().st_size)
            staged = clone.transport.upload_transfer(
                str(path), name, peer, self._xfer_progress(job)
            )
            if staged.get("error"):
                self.xfer_jobs.fail(job or "", str(staged["error"]))
                return staged
            env = Envelope(
                src=clone.addr,
                dst=f"{peer}:comm-{self.host}",
                type="transfer",
                body={
                    "id": staged["id"],
                    "name": staged.get("name") or name,
                    "size": staged.get("size"),
                    "reason": "sent by the user",
                    "from": self.local_addr,
                    "from_human": True,
                    "sender_name": sender_name,
                },
            )
            # Fire-and-forget: the peer's answer resolves on their side; no
            # local agent is waiting on this transfer.
            clone.transport.send(env)
            self.xfer_jobs.finish(job or "")
            return {"ok": True, "kind": "transfer", "name": name}
        text = (text or "").strip()
        if not text:
            return {"error": "empty message"}
        # Text is unified with amail: the hub lands it in BOTH mailboxes
        # (peer's unread, ours pre-read) and never wakes any agent — the
        # friend view renders it from the mail store, courier-independent.
        clone.transport.send(
            Envelope(
                src=clone.addr,
                dst=f"{peer}:mail",
                type="mail",
                body={"from": f"{self.host}:human", "subject": "", "text": text},
            )
        )
        return {"ok": True, "kind": "chat"}

    def comm_note_human(self, peer: str, text: str) -> dict:
        """Owner feedback on a courier report (the friend view's feedback box).

        A turn for OUR courier and nothing else: no envelope is handed to the
        transport, so the counterpart never learns of it — the owner talking to
        their own courier (2026-09-11 user instruction)."""
        with self._guard:
            clone = self._clones.get(peer)
        if clone is None:
            return {"error": f"no comm clone for {peer}"}
        if not text.strip():
            return {"error": "empty feedback"}
        clone.note(text.strip())
        return {"ok": True, "kind": "note"}

    # ── WebUI ──

    def open_webui(self, open_browser: bool = True) -> str:
        """Start the WebUI lazily; returns its URL."""
        with self._webui_guard:
            if self._webui is None:
                self._webui = make_webui_server(None, self.webui_runtime())
                threading.Thread(
                    target=self._webui.serve_forever, name="webui", daemon=True
                ).start()
        url = f"http://localhost:{self._webui.server_address[1]}"
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return url

    def webui_runtime(self) -> WebUIRuntime:
        return RoomRuntime(self, self._sessions_backend())

    def _sessions_backend(self):
        raise NotImplementedError

    # ── lifecycle ──

    def stop(self) -> None:
        self._stop.set()
        with self._guard:
            peers = list(self._clones)
        # remove_comm_clone pops AND stops each clone. Clearing the dict first
        # made its pop() return None, so no clone was ever stopped: their poll
        # and worker threads kept draining envelopes after the room was gone.
        for peer in peers:
            self.remove_comm_clone(peer)
        if self._local is not None:
            self._local.stop()
            self._local = None
        # These loops wake on the stop flag, so a short join is enough — it just
        # keeps them from outliving the room they beat for.
        for thread in (getattr(self, "_monitor", None), getattr(self, "_mail_thread", None)):
            if thread is not None:
                thread.join(timeout=0.2)
        if self._webui is not None:
            self._webui.shutdown()
            self._webui.server_close()
            self._webui = None
        # Leaving the room (or quitting) ends any screen-control arming: the
        # capability belongs to a room with the user present (spec §35.2/§35.3).
        screen_module = sys.modules.get("fungi.tools.screen")
        if screen_module is not None:
            screen_module.disarm()
        # Same for character control (spec §43): neither the watcher thread nor
        # its child process outlives the room.
        ghostworld_module = sys.modules.get("fungi.tools.ghostworld")
        if ghostworld_module is not None:
            ghostworld_module.disarm()


class StoreSessions:
    """Server-role session backend: the hub's own SessionStore."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def list(self) -> list[dict]:
        return self.hub.store.sessions.list_sessions()

    def load(self, session_id: str) -> dict | None:
        return self.hub.store.sessions.load(session_id)

    def save(self, session_id, title, messages, subagents=None, asks=None) -> None:
        self.hub.store.sessions.save(session_id, title, messages, subagents, asks)

    def delete(self, session_id: str) -> None:
        self.hub.store.sessions.delete(session_id)


class ClientSessions:
    """Client-role session backend: a local store on the joining machine.

    Conversations must not be proxied into the hub store: the peer operates
    that machine, and a single shared sessions/ directory made every host's
    WebUI list every other host's chats (2026-09-04 real-machine finding).
    """

    def __init__(self, directory: Path):
        self.store = SessionStore(directory)

    def list(self) -> list[dict]:
        return self.store.list_sessions()

    def load(self, session_id: str) -> dict | None:
        return self.store.load(session_id)

    def save(self, session_id, title, messages, subagents=None, asks=None) -> None:
        self.store.save(session_id, title, messages, subagents, asks)

    def delete(self, session_id: str) -> None:
        self.store.delete(session_id)


class RoomServer(RoomBase):
    """Server role: hosts the hub; roster diff runs in a monitor thread."""

    role = "server"

    def __init__(
        self,
        host,
        cfg,
        sink,
        token,
        data_root: Path,
        llm=None,
        rules_path=None,
        display="",
        port: int = 0,
    ):
        super().__init__(host, cfg, sink, llm=llm, rules_path=rules_path, display=display)
        self.hub = Hub(host, token, data_root, max_file_mb=cfg.max_file_mb, port=port)
        self._monitor: threading.Thread | None = None
        # Friend-view transcripts live next to the hub's own sessions.
        self._comm_store = SessionStore(data_root / "comm-sessions")
        # selftest state (FUNGI_SELFTEST=1)
        self.selftest_answered: str | None = None
        self.selftest_inbox = None
        self.selftest_ask_id: str | None = None

    def start(self) -> None:
        self.hub.start()
        # Own roster entry: peers must see this host; the monitor keeps it alive.
        self.hub.join(self.host, "127.0.0.1", self.display)
        self._local = self._make_local(
            LocalTransport(self.hub.relay, self.local_addr, hub=self.hub)
        )
        self._local.start()
        ensure_ghostworld_watch(self)
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="roster-monitor", daemon=True
        )
        self._monitor.start()

    def _peers(self) -> list[str]:
        return self.hub.roster.peers(self.host)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(MONITOR_INTERVAL_S):
            try:
                self._monitor_once()
            except Exception as exc:
                # A beat against a hub that is shutting down, or a clone being
                # torn down, must not kill the monitor (and must not spray a
                # thread traceback at exit): the stop flag is the only exit.
                if self._stop.is_set():
                    break
                self.sink.emit("error", f"roster monitor: {exc}")

    def _monitor_once(self) -> None:
        """Beat our own roster entry and diff the peer set into comm clones."""
        self.hub.roster.beat(self.host)  # nobody else beats the server's own entry
        peers = set(self._peers())
        with self._guard:
            current = set(self._clones)
        for peer in peers - current:
            transport = LocalTransport(self.hub.relay, f"{self.host}:comm-{peer}", hub=self.hub)
            self.add_comm_clone(peer, transport)
        for peer in current - peers:
            self.remove_comm_clone(peer)

    def _sessions_backend(self) -> StoreSessions:
        return StoreSessions(self.hub)

    def stop(self) -> None:
        super().stop()
        self.hub.stop()


class RoomClient(RoomBase):
    """Client role: joins the hub over HTTP; roster diff rides the heartbeat."""

    role = "client"

    def __init__(
        self,
        host,
        cfg,
        sink,
        server_url: str,
        token: str,
        llm=None,
        rules_path=None,
        display="",
        sessions_dir: Path | None = None,
    ):
        super().__init__(host, cfg, sink, llm=llm, rules_path=rules_path, display=display)
        self.client = HubClient(server_url, token, host, display)
        self.poller = HostPoller(self.client, host)
        self._peers_known: set[str] = set()
        self._entries: list[dict] = []  # roster display records from join/heartbeat
        self._hb: threading.Thread | None = None
        # Client conversations live on THIS machine only (default: the shared
        # single-host location); never staged on the hub's disk.
        self._sessions = ClientSessions(sessions_dir or SESSIONS_DIR)
        # Friend-view transcripts: local disk, next to the client sessions.
        self._comm_store = SessionStore((sessions_dir or SESSIONS_DIR).parent / "comm-sessions")

    def start(self) -> None:
        out = self.client.join()
        self._peers_known = set(out.get("peers") or [])
        self._entries = list(out.get("roster") or [])
        self.poller.start()
        self._local = self._make_local(
            RemoteTransport(self.client, inbox=self.poller.inbox_for(self.local_addr))
        )
        self._local.start()
        ensure_ghostworld_watch(self)
        for peer in sorted(self._peers_known):
            self.add_comm_clone(peer, self._comm_transport(peer))
        self._hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._hb.start()

    def _peers(self) -> list[str]:
        return sorted(self._peers_known)

    def set_token(self, token: str) -> bool:
        """Adopt a rotated room token, verified before it sticks.

        The host can rotate its hub token while the room runs (every request
        re-reads it); a joined client that keeps the old one starts failing
        403. Nothing else caches the token, so one assignment plus a heartbeat
        is the whole swap — and a typo is rejected here instead of breaking the
        room silently.
        """
        previous = self.client.token
        self.client.token = token
        try:
            self.client.heartbeat()
        except HubError:
            self.client.token = previous
            return False
        return True

    def _comm_transport(self, peer: str) -> RemoteTransport:
        return RemoteTransport(self.client, inbox=self.poller.inbox_for(f"{self.host}:comm-{peer}"))

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                self._heartbeat_once()
            except Exception as exc:
                if self._stop.is_set():
                    break
                if not isinstance(exc, HubError):
                    self.sink.emit("error", f"heartbeat: {exc}")

    def _heartbeat_once(self) -> None:
        """One beat: refresh peers (add/remove comm clones), replay pending asks."""
        out = self.client.heartbeat()
        self._peers_known = set(out.get("peers") or [])
        self._entries = list(out.get("roster") or [])
        with self._guard:
            current = set(self._clones)
        for peer in self._peers_known - current:
            self.add_comm_clone(peer, self._comm_transport(peer))
        for peer in current - self._peers_known:
            self.remove_comm_clone(peer)
        for rec in out.get("pending_asks") or []:
            self._replay_ask(rec)

    def _replay_ask(self, rec: dict) -> None:
        """Heartbeat replay of unresolved asks: re-record (dedup)."""
        src = rec.get("src")
        if not src:
            return
        env = Envelope(
            id=str(rec.get("ask_id") or ""),
            src=str(src),
            dst=self.local_addr,
            type="ask",
            body=rec.get("payload") or {},
        )
        self._on_ask(env)

    def _sessions_backend(self) -> ClientSessions:
        return self._sessions

    def stop(self) -> None:
        super().stop()
        if self._hb is not None:
            self._hb.join(timeout=0.2)
        self.poller.stop()
        with contextlib.suppress(HubError):
            self.client.leave()


class RoomRuntime(WebUIRuntime):
    """WebUI wired to the local clone: per-host sessions, local toolset,
    card answers routed back out as answer envelopes."""

    def __init__(self, room: RoomBase, sessions):
        self.room = room
        self._sessions = sessions

    # ── sessions ──
    def sessions_list(self) -> list[dict]:
        return self._sessions.list()

    def sessions_load(self, session_id: str) -> dict | None:
        return self._sessions.load(session_id)

    def sessions_save(self, session_id, title, messages, subagents=None, asks=None) -> None:
        self._sessions.save(session_id, title, messages, subagents, asks)

    def sessions_delete(self, session_id: str) -> None:
        self._sessions.delete(session_id)

    def new_session_id(self) -> str:
        from fungi import session  # noqa: PLC0415 (avoid import weight at module load)

        return session.new_session_id()

    def new_session_prompt(self) -> str:
        return self.room.local.system_prompt

    # ── turns ──
    def build_agent(self, sink, should_abort) -> Agent:
        clone = self.room.local
        delegate_tools = getattr(clone, "delegate_tools", None)
        if delegate_tools is not None:
            delegate_tools.abort_fn = should_abort
        tools = dict(clone.tools)
        sid = getattr(sink, "session_id", None) or ""
        # Fresh event per turn: /stop pops+sets it to kill this turn's
        # background subagents; a new turn starts with a clean one.
        bg_abort = threading.Event()
        _BG_ABORTS[sid] = bg_abort

        def _abort() -> bool:
            return bool(should_abort and should_abort()) or bg_abort.is_set()

        trilayer = TriLayer(
            self.room.cfg,
            sink,
            llm=self.room.llm,
            should_abort=_abort,
            child_tool_names=clone.child_tool_names,
            child_extra_tools=clone.child_extra_tools,
            skill_save=clone.skill_save,
            spawn_done=lambda rec: _PENDING_SPAWNS.setdefault(sid, []).append(rec),
        )
        # inquire rides the turn sink so its card streams in the NDJSON flow;
        # resolution stays on the module-global registry (/answer in-process).
        # on_answer persists completed asks on the turn agent so sessions
        # replay answered cards after reload — room mode lost this wiring,
        # leaving every session's asks bucket permanently empty.
        tools["inquire"] = make_ask_tool(
            sink,
            on_answer=trilayer.asks.append,
            should_abort=should_abort,
        )
        # Experimental private diary: re-read the flag every turn so the
        # settings switch applies without a restart (self.room.cfg is the
        # cached room-start snapshot). Local clone only — comm clones never
        # get the diary (their turns go through clone/base.build_agent).
        live = load_config()
        if live.diary:
            tools["diary"] = diary_bound()["diary"]
            prompt = clone.system_prompt + diary_section()
        else:
            prompt = clone.system_prompt
        if live.pc_control:
            # Screen control (spec §35.3): same live-flag rule as the diary —
            # turning it off in the settings page takes effect next turn. The
            # user-facing agent only; children never inherit it.
            from fungi.tools import screen  # noqa: PLC0415

            tools.update(
                screen.bound(live, sink, should_abort=should_abort, on_answer=trilayer.asks.append)
            )
        if live.ghostworld:
            # GhostWorld character control (spec §43): arming here is what makes
            # the switch take effect on this turn instead of after a restart.
            ensure_ghostworld_watch(self.room)
        return trilayer.build_clone_agent(
            sink,
            system_prompt=prompt,
            extra_tools=tools,
            tool_names=clone.tool_names,
            model=clone.model,
        )

    # ── answers ──
    def route_answer(self, ask_id: str, value) -> bool:
        card = self.room.cards.take(
            ask_id, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        if card is not None:
            self.room._send_answer(card, value)
            self._record_card_verdict(card, value)
            return True
        return resolve_ask(ask_id, value)

    def _record_card_verdict(self, card: Envelope, value) -> None:
        """Persist a card ask's verdict into the friend conversation it
        belongs to, so the transcript keeps the decision after reload —
        same replay path as in-turn inquire records. Cross-host guard asks
        (conv == our own host) have no local transcript to attach to: the
        raising turn lives on the remote host."""
        try:
            conv = _card_conv(card.src)
            store = self.room._comm_store
            sid = "comm-" + conv
            if store is None or conv == self.room.host:
                return
            with self.room._comm_lock(sid):
                data = store.load(sid)
                if not data:
                    return
                body = card.body
                questions = body.get("questions")
                if not isinstance(questions, list) or not questions:
                    questions = [
                        {
                            "question": body.get("question") or "(ask)",
                            "options": [],
                            "allow_custom": True,
                        }
                    ]
                asks = list(data.get("asks") or [])
                asks.append(
                    {
                        "id": card.id,
                        "ts": time.time(),  # friend view slots it into the timeline
                        "questions": questions,
                        "answers": value if isinstance(value, list) else [value],
                        "status": "answered",
                    }
                )
                store.save(
                    sid,
                    data.get("title") or f"comm: {conv}",
                    data.get("messages") or [],
                    subagents=data.get("subagents") or [],
                    asks=asks[-100:],
                )
        except Exception as exc:  # a tracing failure must not break answering
            self.room.sink.emit("error", f"ask verdict trace: {exc}")

    def pending_asks(self) -> list[dict]:
        out = []
        for card in self.room.cards.pending():
            body = card["body"]
            conv = _card_conv(card["src"])
            src = body.get("from") or card["src"]
            questions = body.get("questions")
            if isinstance(questions, list) and questions:
                out.append(
                    {
                        "id": card["id"],
                        "from": src,
                        "kind": "ask",
                        "conv": conv,
                        "questions": questions,
                    }
                )
            elif body.get("question"):
                out.append(
                    {
                        "id": card["id"],
                        "from": src,
                        "conv": conv,
                        "kind": "consent",
                        "questions": [
                            {"question": body["question"], "options": [], "allow_custom": True}
                        ],
                    }
                )
            else:
                out.append(
                    {
                        "id": card["id"],
                        "from": card["src"],
                        "conv": conv,
                        "kind": "ask",
                        "questions": [
                            {"question": "(no question text)", "options": [], "allow_custom": True}
                        ],
                    }
                )
        return out

    # ── amail: this host's mailbox on the hub ──

    def mail(self) -> dict:
        room = self.room
        if getattr(room, "hub", None) is not None:  # server role: direct
            return room.hub.mail.list(room.host)
        return room.client.mail()  # client role: hub HTTP

    def mail_read(self, mail_id: str) -> dict:
        room = self.room
        if getattr(room, "hub", None) is not None:
            return room.hub.mail.mark_read(room.host, mail_id)
        return room.client.mail_read(mail_id)

    # ── consent slider (per-friend mode, visible + reversible) ──
    def consent_mode(self, host: str) -> str:
        return self.room.rules.mode_for(host)

    def set_consent_mode(self, host: str, mode: str) -> None:
        self.room.rules.set_mode(host, mode)

    # ── friends / comm conversations ──
    def peers(self) -> list[dict]:
        if getattr(self.room, "hub", None) is not None:  # server role: direct
            return self.room.hub.roster.entries(self.room.host)
        return list(self.room._entries)  # client role: cached roster

    def comm_log(self, host: str) -> dict:
        """Friend view payload: the comm clone's transcript (session-style
        messages + subagent/ask records), hub-side envelope events, and the
        unified mail thread with this host (both directions)."""
        out: dict = {
            "messages": [],
            "subagents": [],
            "asks": [],
            "events": [],
            "mails": [],
            "live": [],
        }
        store = self.room._comm_store
        if store is not None:
            data = store.load("comm-" + host)
            if data:
                out["messages"] = data.get("messages") or []
                out["subagents"] = data.get("subagents") or []
                out["asks"] = data.get("asks") or []
        if getattr(self.room, "hub", None) is not None:  # server role: direct
            out["events"] = self.room.hub.commlog.read(self.room.host, host)
            mails = self.room.hub.mail.list(self.room.host)["mails"]
        else:
            out["events"] = self.room.client.comm_log(host)  # client role: hub API
            mails = self.room.client.mail()["mails"]
        out["mails"] = sorted(
            (m for m in mails if m.get("peer") == host), key=lambda m: float(m.get("ts") or 0.0)
        )
        out["live"] = self.room.live_tape(host)  # in-flight turn events, if any
        return out

    def comm_send(self, data: dict) -> dict:
        """Human direct-send from the friend view composer (see
        RoomBase.comm_send_human). `job` is the send-file modal's own id: the
        upload's byte count lands in it for the modal to poll."""
        host = str(data.get("host") or "").strip()
        if not host:
            return {"error": "host required"}
        return self.room.comm_send_human(
            host,
            text=data.get("text"),
            file_path=data.get("file"),
            job=str(data.get("job") or "") or None,
        )

    def transfer_progress(self, job_id: str) -> dict:
        """Send-file modal poll: the job the browser minted for its own send."""
        return self.room.xfer_jobs.get(job_id)

    def comm_note(self, data: dict) -> dict:
        """Feedback on a courier report, from the friend view's feedback box:
        a turn for our own courier, nothing on the wire (RoomBase.comm_note_human)."""
        host = str(data.get("host") or "").strip()
        if not host:
            return {"error": "host required"}
        return self.room.comm_note_human(host, str(data.get("text") or ""))


# ── selftest hook (FUNGI_SELFTEST=1, server role) ──


def run_selftest(room: RoomServer, quit_fn, fail_fn) -> None:
    """Acceptance loop, no human input: start WebUI -> simulated comm clone
    sends an ask -> notification fires -> POST /answer yes -> blocker wakes."""
    from PyQt5.QtCore import QTimer  # noqa: PLC0415 (Qt only in tray mode)

    url = room.open_webui(open_browser=False)

    def _step1() -> None:
        threading.Thread(target=_requester, args=(room,), daemon=True).start()

    def _step2() -> None:
        pending = room.cards.pending()
        if not pending:
            fail_fn(f"no card recorded after ask (pending={pending})")
            return
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(
            url + "/answer",
            data=json.dumps({"id": pending[0]["id"], "value": "yes"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        if not body.get("ok"):
            fail_fn(f"/answer rejected: {body}")
            return

    def _step3() -> None:
        if room.selftest_answered is None:
            fail_fn("requester was not woken by the answer envelope")
            return
        if room.selftest_answered != "yes":
            fail_fn(f"unexpected answer value: {room.selftest_answered!r}")
            return
        print("FUNGI SELFTEST OK", flush=True)
        quit_fn(0)

    QTimer.singleShot(500, _step1)
    QTimer.singleShot(2500, _step2)
    QTimer.singleShot(4000, _step3)


def _requester(room: RoomServer) -> None:
    """Simulated comm clone: register blocking ask, send envelope, drain the
    inbox loop (dispatch answer -> pending), record the answer for step 3."""
    from .pending import PendingAsks  # noqa: PLC0415

    addr = f"{room.host}:comm-selftest"
    room.selftest_inbox = room.hub.relay.register_local(addr)
    pending = PendingAsks()
    env = Envelope(
        src=addr,
        dst=room.local_addr,
        type="ask",
        body={"question": "Selftest: allow writes to homes/report.md?", "from": addr},
    )
    room.selftest_ask_id = env.id
    pending.register(env.id)
    room.hub.send(env)
    cursor = 0
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        msgs, cursor = room.selftest_inbox.after(cursor, 0.5)
        for m in msgs:
            if m.type == "answer" and m.reply_to == env.id:
                room.selftest_answered = str(m.body.get("value"))
                pending.resolve(env.id, m.body.get("value"))
                return
