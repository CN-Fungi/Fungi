"""WebUI server: ThreadingHTTPServer + NDJSON streaming + static files from web/.

Single-host mode keeps the original YESIR behavior (TriLayer in the request
thread, local session files). Room mode (fungi/room.py) injects a WebUIRuntime
that runs the local clone's toolset, backs sessions per host (server: hub
store; client: its own disk — never the peer-operated hub), and routes
card answers back out as answer envelopes.
"""

import contextlib
import json
import re
import secrets
import socket
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from fungi import landing, runlog, session
from fungi.agent import SYSTEM_PROMPT, Agent, public_messages
from fungi.config import PROJECT_ROOT, RESOURCE_ROOT, load_config, save_config
from fungi.events import Sink
from fungi.hub.app import RangeNotSatisfiableError, range_window, safe_name
from fungi.tools.ask import resolve_ask
from fungi.tools.mcp import mcp_extra_tools
from fungi.trilayer import TriLayer

WEB_DIR = RESOURCE_ROOT / "web"

_mime = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}


def _webui_token() -> str:
    """LAN access token: generated once and persisted, so QR codes and mobile
    bookmarks survive restarts."""
    path = Path.home() / ".fungi" / "webui_token"
    try:
        t = path.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(t, encoding="utf-8")
    except OSError:
        pass
    return t


WEBUI_TOKEN = _webui_token()


def lan_ip() -> str:
    """Best-effort LAN address (routing lookup only, no packet sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def lan_payload(port: int, loopback: bool) -> dict:
    """QR material for the GUI page. The token is a LAN secret: it is only
    ever echoed to callers already on the loopback (i.e. the desktop itself)."""
    payload = {"ip": lan_ip(), "port": port}
    if loopback:
        payload["token"] = WEBUI_TOKEN
        payload["url"] = f"http://{payload['ip']}:{payload['port']}/m?t={WEBUI_TOKEN}"
    return payload


_TAPE_GRACE_S = 60.0  # how long a sealed (done) tape stays for late reattach


_STATIC_ROUTES = frozenset(
    ("/", "/m", "/app.js", "/common.js", "/style.css", "/motion.js", "/m.css", "/m.js")
)


def _inbox_dir() -> Path:
    """The inbox, created if need be (same dir the comm-clone flow lands in)."""
    inbox = landing.inbox_root(load_config().inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _inbox_path(filename: str) -> Path:
    """Where an uploaded file lands (same dir the comm-clone transfer flow
    uses), sanitizing the name and numbering collisions."""
    inbox = _inbox_dir()
    dest = inbox / safe_name(filename)
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = inbox / f"{stem}-{n}{suffix}"
        n += 1
    return dest


# ── phone uploads, in windows (§51) ──

UPLOAD_TTL_S = 1800.0  # a page that walked away: its parts stop claiming disk
UPLOAD_PART_MIN = 4 * 1024 * 1024  # the smallest window worth its own connection
UPLOAD_READ = 64 * 1024  # one read/write step while streaming a body
DOWNLOAD_CHUNK = 256 * 1024  # one read/write step while serving a file (as the hub does)
SID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def upload_sid(value: str) -> str:
    """A client's session id if it is safe to put into a file name, else ""."""
    sid = str(value or "")
    if not sid or len(sid) > 64 or not set(sid) <= SID_CHARS:
        return ""
    return sid


def _int_or_none(value: str) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


# ── the file-transfer session (§53) ──
#
# One session both devices share for moving files: the phone drops what it picks
# there, the computer drops what it wants the phone to have, and every row is a
# transfer with a path that is tappable on the phone (it pulls the file back,
# §52). It is a shuttle, not a conversation: nothing here runs a model, because
# "send a file to my phone" must not cost a turn (and a model cannot be trusted
# to hand a path back unedited).

SHUTTLE_ID = "file-transfer"
SHUTTLE_TITLE = "文件传输助手"


def human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def shuttle_ensure(runtime) -> None:
    """The transfer session exists — made the first time anyone looks."""
    if runtime.sessions_load(SHUTTLE_ID) is None:
        runtime.sessions_save(SHUTTLE_ID, SHUTTLE_TITLE, [])


def shuttle_post(runtime, text: str, file: dict | None = None, side: str | None = None) -> None:
    """Append one transfer to the shuttle; both directions land here (§53).

    `file` is what turns the row into a card (§56): the name, the size, where it
    is and which side sent it. Without it the row is just a message — and then
    `side` is the only thing that says which device typed it, which is what puts
    a row on the right side of the transcript (§59).
    """
    with _session_lock(SHUTTLE_ID):
        stored = runtime.sessions_load(SHUTTLE_ID) or {}
        messages = list(stored.get("messages") or [])
        row: dict = {"role": "user", "content": text, "ts": time.time()}
        if file:
            row["file"] = file
        who = (file or {}).get("direction") or side
        if who:
            row["direction"] = who
        messages.append(row)
        runtime.sessions_save(
            SHUTTLE_ID,
            SHUTTLE_TITLE,
            messages,
            subagents=stored.get("subagents") or [],
            asks=stored.get("asks") or [],
        )


def _shuttle_row(who: str, landed: dict) -> str:
    """What a landed transfer says in the session, in words (the card carries it
    as fields too — §56; the text stays for logs and for anything that reads the
    transcript without the web UI)."""
    return f"{who}：{landed['name']}（{human_size(landed['size'])}）\n{landed['path']}"


DRIVE_RE = re.compile(r"[A-Za-z]:[\\/]")
# A path inside a sentence: a blank ends it — right for prose, wrong for a file
# name that has blanks of its own (`屏幕录制 2026-09-17 090847.mp4`).
FILE_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'<>|*?\u3002\uff0c\uff09\uff1f\uff01\u3011\u3015\uff1b\uff1a]+"
)
# The same run with blanks allowed: no pattern can say where such a name ends,
# so the disk decides instead.
SPACED_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\r\n\"'<>|*?\u3002\uff0c\uff09\uff1f\uff01\u3011\u3015\uff1b\uff1a]+"
)
TRAILING_CHARS = "\\.,;:\uff09)]"


def _path_candidates(text: str) -> Iterator[str]:
    """What a message could be naming, most literal first.

    A message that *is* a path is this session's normal shape (§53), so its own
    lines are tried before any scan: that is the one rule a file name with
    blanks survives. Only then come the runs found inside prose — the narrow
    ones as they stand, the wide ones handed back one word at a time.
    """
    seen: set[str] = set()

    def clean(candidate: str) -> str | None:
        candidate = candidate.strip().rstrip(TRAILING_CHARS).strip()
        if not candidate or candidate in seen:
            return None
        seen.add(candidate)
        return candidate

    for line in (text or "").splitlines():
        candidate = clean(line)
        if candidate is not None and DRIVE_RE.match(candidate):
            yield candidate
    for pattern in (FILE_PATH_RE, SPACED_PATH_RE):
        for match in pattern.finditer(text or ""):
            run = clean(match.group(0))
            while run is not None:
                yield run
                run = clean(run.rsplit(" ", 1)[0]) if " " in run else None


def file_in(text: str, direction: str) -> dict | None:
    """The transfer a message carries, if it carries one.

    The computer hands the phone a file by *sending its path* (§53) — but a row
    that names a file the machine can actually open is a transfer, and it should
    look like one. `is_file()` is the only judge there is: blanks are legal in a
    name, so no pattern can say where one ends. Anything else stays a plain
    message — a path that is not there is not a card, it is a typo.
    """
    for candidate in _path_candidates(text):
        path = Path(candidate)
        if path.is_file():
            return {
                "name": path.name,
                "size": path.stat().st_size,
                "path": str(path),
                "direction": direction,
            }
    return None


class UploadParts:
    """The windows of one phone upload, while the pieces are still arriving (§51).

    A phone upload used to be one long POST; split across several connections it
    is several short ones, and nothing on the wire says which window a body
    belongs to unless the host remembers. So it keeps the name, the announced
    size and the byte ranges that have really landed — `landing.Spans`, a union,
    so a window retried after a drop does not count twice — writes each window in
    place at its offset in one part file, and lets the real name appear only once
    every byte is covered (§49, one hop earlier in the flow: this is the host's
    disk, not the receiver's).

    Sessions are cheap and rare (one per upload in flight); a page that goes away
    mid-upload leaves one behind, and `sweep()` takes it — and its part file —
    after UPLOAD_TTL_S.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._guard = threading.Lock()

    def sweep(self) -> None:
        """Forget the sessions nobody came back for, and their parts with them."""
        cutoff = time.time() - UPLOAD_TTL_S
        with self._guard:
            stale = [(sid, s) for sid, s in self._sessions.items() if s["ts"] < cutoff]
            for sid, _session in stale:
                self._sessions.pop(sid, None)
        for _sid, up in stale:
            with contextlib.suppress(OSError):
                up["part"].unlink()

    def session(self, sid: str, name: str, total: int, inbox: Path) -> dict:
        """The upload `sid` names, created by whichever window arrives first.

        Every window carries the same name and size, so any of them can be the
        one that starts the session; the lock is what keeps two concurrent
        firsts from making two part files.
        """
        with self._guard:
            found = self._sessions.get(sid)
            if found is None:
                part = inbox / f"{safe_name(name)}.{sid}{landing.PART_SUFFIX}"
                part.parent.mkdir(parents=True, exist_ok=True)
                part.touch()
                found = {
                    "name": safe_name(name),
                    "total": int(total),
                    "part": part,
                    "spans": landing.Spans(),
                    "ts": time.time(),
                }
                self._sessions[sid] = found
            found["ts"] = time.time()
            return found

    def get(self, sid: str) -> dict | None:
        with self._guard:
            up = self._sessions.get(sid)
        if up is not None:
            up["ts"] = time.time()
        return up

    def drop(self, sid: str) -> None:
        """Forget a session and delete whatever part it left."""
        with self._guard:
            up = self._sessions.pop(sid, None)
        if up is not None:
            with contextlib.suppress(OSError):
                up["part"].unlink()

    def missing(self, up: dict) -> list[tuple[int, int]]:
        return up["spans"].gaps(0, int(up["total"]))


UPLOADS = UploadParts()


def land_upload(up: dict) -> dict:
    """Put the part in place if it is whole; the §49 rule, one hop earlier.

    The reply is the page's answer either way: `done` with the path it landed at,
    or the byte ranges that are still missing, so a sender that cannot see this
    disk re-sends exactly those instead of guessing.
    """
    total = int(up["total"])
    spans = up["spans"]
    part = up["part"]
    if not spans.covers(0, total) or part.stat().st_size != total:
        return {
            "ok": True,
            "done": False,
            "received": min(total, spans.bytes),
            "total": total,
            "missing": spans.gaps(0, total),
        }
    # The name is chosen now, not when the upload started: a file that landed
    # under this name while the bytes were in flight gets its own "-1" instead
    # of being overwritten.
    dest = _inbox_path(up["name"])
    part.replace(dest)
    return {
        "ok": True,
        "done": True,
        "path": str(dest),
        "name": dest.name,
        "size": total,
        "received": total,
        "total": total,
    }


RETRY_STRIP_PREFIXES = ("(LLM error:", "(Hit max tool rounds", "(Aborted")


def sanitize_for_retry(messages: list[dict]) -> list[dict]:
    """Drop the synthetic tail a failed turn left behind, so Alt+R continues
    from real context. Marker lines are recognized anywhere in the tail block;
    everything from the first marker on is discarded (tool calls without their
    results would poison the next completion)."""
    out = list(messages)
    while out:
        last = out[-1]
        content = last.get("content")
        if isinstance(content, str) and content.startswith(RETRY_STRIP_PREFIXES):
            out.pop()
            continue
        if last.get("role") == "assistant" and last.get("content") is None:
            out.pop()  # dangling tool_calls
            continue
        break
    return out


def repair_tool_gaps(messages: list[dict]) -> list[dict]:
    """Ensure every assistant tool_call is followed by a tool result.

    A turn that died between appending tool_calls and their results used to
    poison the saved session: the next completion request fails with HTTP 400
    (tool_calls must be answered), and the conversation is bricked from there
    on. Synthesize an explicit failure result for any unanswered call.
    """
    out: list[dict] = []
    unanswered: dict[str, str] = {}  # tool_call_id -> tool name
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            out.append(m)
            for tc in m["tool_calls"]:
                unanswered[str(tc.get("id"))] = str(
                    (tc.get("function") or {}).get("name") or "tool"
                )
            continue
        if role == "tool":
            unanswered.pop(str(m.get("tool_call_id")), None)
            out.append(m)
            continue
        if unanswered and role in ("user", "assistant"):
            # History gap: answer the dangling calls before moving on.
            for call_id, name in unanswered.items():
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": f"ERROR: turn was interrupted before {name} could run.",
                    }
                )
            unanswered = {}
        out.append(m)
    for call_id, name in unanswered.items():
        out.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"ERROR: turn was interrupted before {name} could run.",
            }
        )
    return out


class WebUIRuntime:
    """Turn/sessions/answer wiring for the WebUI. Default = single-host mode."""

    # Monotonic timestamp of the last WebUI HTTP request: the "is anyone
    # actually looking" signal. ask notifications fire only when this is
    # stale (nobody has the page open). Class attribute because RoomRuntime
    # does not chain __init__; touch() shadows it per instance.
    last_seen: float = 0.0

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def sessions_list(self) -> list[dict]:
        return session.list_sessions()

    def sessions_load(self, session_id: str) -> dict | None:
        return session.load_session(session_id)

    def sessions_save(
        self,
        session_id: str,
        title: str,
        messages: list[dict],
        subagents: list | None = None,
        asks: list | None = None,
    ) -> None:
        session.save_session(session_id, title, messages, subagents=subagents, asks=asks)

    def sessions_delete(self, session_id: str) -> None:
        session.delete_session(session_id)

    def new_session_id(self) -> str:
        return session.new_session_id()

    def new_session_prompt(self) -> str:
        return SYSTEM_PROMPT

    def build_agent(self, sink: Sink, should_abort) -> Agent:
        sid = getattr(sink, "session_id", None) or ""
        # Fresh event per turn: /stop pops+sets it to kill this turn's
        # background subagents; a new turn must start with a clean one.
        bg_abort = threading.Event()
        _BG_ABORTS[sid] = bg_abort

        def _abort() -> bool:
            return bool(should_abort and should_abort()) or bg_abort.is_set()

        layer = TriLayer(
            load_config(),
            sink,
            should_abort=_abort,
            spawn_done=lambda rec: _PENDING_SPAWNS.setdefault(sid, []).append(rec),
        )
        return layer.build_orchestrator(sink)

    def route_answer(self, ask_id: str, value: str | list[str]) -> bool:
        """Resolve an /answer submission. Default: in-process inquire only."""
        return resolve_ask(ask_id, value)

    def pending_asks(self) -> list[dict]:
        """Out-of-band asks awaiting a card answer (room mode: envelope asks)."""
        return []

    def peers(self) -> list[str]:
        """Other hosts currently in the room (room mode)."""
        return []

    def comm_log(self, host: str) -> dict:  # noqa: ARG002 (room mode overrides)
        """Friend view payload (room mode returns the real transcript)."""
        return {"messages": [], "subagents": [], "asks": [], "events": []}

    def comm_send(self, data: dict) -> dict:  # noqa: ARG002 (room mode overrides)
        """Human direct-send from the friend view (room mode)."""
        return {"error": "friend direct send requires room mode"}

    def comm_note(self, data: dict) -> dict:  # noqa: ARG002 (room mode overrides)
        """Feedback on a courier report (room mode)."""
        return {"error": "courier feedback requires room mode"}

    def consent_mode(self, host: str) -> str:  # noqa: ARG002 (room mode overrides)
        """Per-friend consent mode: "allow" or "ask" (room mode)."""
        return "ask"

    def mail(self) -> dict:
        """This host's mailbox (room mode returns the real one)."""
        return {"host": "", "mails": [], "unread": 0}

    def mail_read(self, mail_id: str) -> dict:  # noqa: ARG002 (room mode overrides)
        return {"ok": False}

    def transfer_progress(self, job_id: str) -> dict:  # noqa: ARG002 (room mode overrides)
        """Send-file progress for the browser's own job id (room mode)."""
        return {"error": "no transfer jobs outside room mode"}

    def set_consent_mode(self, host: str, mode: str) -> None:
        pass

    def mcp_tools(self) -> dict:
        return mcp_extra_tools(load_config().mcp_servers)


# Interrupt support: one Event per running turn, keyed by session id. /stop
# sets them; the agent checks between rounds and on every SSE line read.
_ACTIVE_TURNS: dict[str, set[threading.Event]] = {}
_TURNS_LOCK = threading.Lock()

# One writer per session: a same-session turn started while another is still
# finishing would otherwise overwrite its saved context (last-writer-wins).
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()

# Finished background subagents awaiting re-activation: session_id ->
# [{"id","goal","status","answer"}]. Fed from spawn threads (TriLayer
# spawn_done), drained atomically by /resume which injects them as a new
# turn's input.
_PENDING_SPAWNS: dict[str, list[dict]] = {}
# Per-turn background aborts: /stop pops+sets the session's event so
# already-dispatched background subagents die with the turn.
_BG_ABORTS: dict[str, threading.Event] = {}

# Tombstones for sessions deleted while a turn was still running: the turn's
# exit-path save must not resurrect the file the user just deleted.
_TURN_DELETED: set[str] = set()

# Per-turn event tapes: the WebSink records every event it emits so a client
# that reloads mid-turn can reattach via /events and replay what it missed.
# A tape lives until its turn's done marker is consumed / grace-popped.
_TURN_TAPES: dict[str, list[dict]] = {}


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.Lock())


# ── session alerts (§61): what wants the user's eyes ──
#
# Three things in a session want the user: the agent asked a question (an
# in-turn `ask`), a turn finished, a turn failed. An alert is raised only when
# nobody is *showing* that session: the page that has it open — visible and
# focused, because a minimized webUI is not "opened" (user rule, 2026-09-21)
# — says so every few seconds, and the server reads a fresh claim as "the user
# is watching". A hidden or unfocused page sends nothing, its claim goes stale,
# and the next event is real: the GUI rings (fungi/gui/app.py) and the red dot
# rides /sessions. Opening the session is what stops it — the shape of the
# unread-mail ring (§25.2), with the page's own claim in place of the grace.

ALERT_KINDS = ("ask", "done", "error")
SEEN_TTL_S = 8.0  # a claim reads as "watching" this long (page heartbeat: 3 s)
_ALERTS: dict[str, str] = {}  # session id -> kind ("ask" | "done" | "error")
_SEEN: dict[str, float] = {}  # session id -> monotonic of the last claim
_ALERT_LOCK = threading.Lock()


def note_alert(session_id: str | None, kind: str) -> None:
    """Raise a session's alert, unless a page is showing it right now."""
    if not session_id or kind not in ALERT_KINDS:
        return
    with _ALERT_LOCK:
        seen = _SEEN.get(session_id)
        if seen is not None and time.monotonic() - seen <= SEEN_TTL_S:
            return
        _ALERTS[session_id] = kind


def session_alerts() -> dict[str, str]:
    """A copy of the outstanding alerts, session id -> kind (the GUI rings on it)."""
    with _ALERT_LOCK:
        return dict(_ALERTS)


def forget_session(session_id: str) -> None:
    """Drop a session's alert and claim (it was deleted)."""
    with _ALERT_LOCK:
        _ALERTS.pop(session_id, None)
        _SEEN.pop(session_id, None)


def clear_alerts() -> None:
    """Forget every alert and claim: a stopped room leaves no ring behind."""
    with _ALERT_LOCK:
        _ALERTS.clear()
        _SEEN.clear()


def mark_seen(session_id: str, visible: bool) -> dict[str, str]:
    """A page's report about the session it is showing.

    `visible` claims it (and drops its alert); otherwise the claim is released
    — the page switched away, or went hidden. Either way the answer is the
    alert map, so one request carries both the heartbeat and the red dots.
    """
    with _ALERT_LOCK:
        if session_id:
            if visible:
                _SEEN[session_id] = time.monotonic()
                _ALERTS.pop(session_id, None)
            else:
                _SEEN.pop(session_id, None)
        return dict(_ALERTS)


def unanswered_asks(asks: list[dict] | None) -> bool:
    """A turn that ends holding an unanswered ask still wants the user (§61)."""
    return any(str(ask.get("status") or "") != "answered" for ask in asks or [])


class WebSink:
    """Thread-safe NDJSON writer over the /chat response stream."""

    def __init__(self, handler: "YesSirHandler", session_id: str | None = None):
        self.handler = handler
        self.session_id = session_id
        self.closed = False

    def emit(self, kind: str, content) -> None:
        if self.session_id is not None and kind != "done":
            # Record for /events reattach; the done marker is appended by the
            # turn's exit path so replay consumers never miss tail events.
            with _TURNS_LOCK:
                tape = _TURN_TAPES.get(self.session_id)
                if tape is not None:
                    tape.append({"type": kind, "content": content})
        if kind == "ask":
            # The agent is blocked on an answer: an ask that nobody watches is
            # raised right here, mid-turn (it can sit for ASK_TIMEOUT_S).
            note_alert(self.session_id, "ask")
        if self.closed:
            return
        try:
            data = json.dumps({"type": kind, "content": content}, ensure_ascii=False)
            self.handler.wfile.write((data + "\n").encode("utf-8"))
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.closed = True


class YesSirHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    runtime: WebUIRuntime = None  # type: ignore[assignment]

    # ---- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _answer(self, routes) -> None:
        """Every request gets an answer — a route that blows up says so.

        A dropped connection is indistinguishable from a network failure in the
        browser, and Chromium then silently RE-SENDS the POST: a route that
        raised before replying left /comm-send unanswered, the send-file modal
        said "Failed to fetch", and each re-send restarted its bar from zero
        (spec §47). So a route exception becomes a 500 the page can print, and
        a dead reader is just closed (nothing left to say to it).
        """
        try:
            routes()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # the point is to answer, not to type the failure
            runlog.problem("webui %s %s failed: %s", self.command, self.path.split("?")[0], exc)
            self.close_connection = True
            with contextlib.suppress(Exception):  # already streaming: no reply room
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def _authorized(self) -> bool:
        """Loopback clients (desktop WebUI, GUI) pass freely. LAN clients must
        carry the QR token for every data route. Static shell assets (page,
        css, js) stay open: they carry no data, their sub-resource URLs cannot
        append ?t=, and a phone opening /m without a valid token then gets a
        working page whose m.js shows the rescan overlay — not a broken
        half-styled one."""
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        route = urlparse(self.path).path
        if route in _STATIC_ROUTES:
            return True
        if route.startswith("/vendor/") and "/" not in route[8:] and ".." not in route:
            return True  # flat vendor dir; same guard as the route itself
        q = parse_qs(urlparse(self.path).query)
        return (q.get("t") or [""])[0] == WEBUI_TOKEN

    def _gate(self) -> bool:
        if self._authorized():
            return True
        self._send_json({"error": "unauthorized — rescan the QR code"}, status=403)
        return False

    def _send_static(self, filename: str) -> None:
        path = WEB_DIR / filename
        if not path.is_file():
            self._send_json({"error": "not found"}, status=404)
            return
        body = path.read_bytes()
        mime = _mime.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET --------------------------------------------------------------
    def do_GET(self):
        self._answer(self._get_routes)

    def _get_routes(self):
        if not self._gate():
            return
        self.runtime.touch()  # anyone still polling = someone is looking
        url = urlparse(self.path)
        route = url.path
        if route == "/":
            self._send_static("index.html")
        elif route == "/m":
            self._send_static("m.html")
        elif route in ("/app.js", "/common.js", "/style.css", "/motion.js", "/m.css", "/m.js"):
            self._send_static(route.lstrip("/"))
        elif (
            route.startswith("/vendor/") and "/" not in route[8:] and ".." not in route
        ):  # flat vendor dir; no traversal
            self._send_static(route[1:])  # web/vendor/<file> — keep the dir prefix
        elif route == "/model":
            self._send_json({"model": load_config().model})
        elif route == "/config-status":
            self._send_json({"configured": load_config().configured})
        elif route == "/lan":
            loopback = self.client_address[0] in ("127.0.0.1", "::1")
            self._send_json(lan_payload(self.server.server_address[1], loopback))
        elif route == "/asks":
            self._send_json({"asks": self.runtime.pending_asks()})
        elif route == "/sessions":
            shuttle_ensure(self.runtime)
            sessions = self.runtime.sessions_list()
            alerts = session_alerts()
            # Pinned first, and flagged: it is a device-to-device channel, not a
            # chat, and the shells poll it so the other device's files show up
            # without anyone reloading (§53).
            for entry in sessions:
                entry["shuttle"] = str(entry.get("id")) == SHUTTLE_ID
                # §61: the red dot — what the session wants, or None for quiet.
                entry["alert"] = alerts.get(str(entry.get("id")))
            sessions.sort(key=lambda entry: not entry["shuttle"])
            with _TURNS_LOCK:
                for s in sessions:
                    tape = _TURN_TAPES.get(str(s.get("id")))
                    s["running"] = tape is not None and not any(
                        ev.get("type") == "done" for ev in tape
                    )
            self._send_json({"sessions": sessions})
        elif route == "/session":
            params = parse_qs(url.query)
            session_id = (params.get("id") or [None])[0]
            if session_id == SHUTTLE_ID:
                # Opening it is a reason for it to exist (§53): the phone must
                # not find a 404 where the transfer session should be.
                shuttle_ensure(self.runtime)
            data = self.runtime.sessions_load(session_id) if session_id else None
            if data is None:
                self._send_json({"error": "not found"}, status=404)
                return
            after = _int_or_none((params.get("after") or [""])[0])
            if after is None:
                self._send_json(data)
            else:
                # `after=N`: only the rows added since the caller's cursor, so a
                # page watching for new transfers can append them instead of
                # repainting the transcript (which flickers: the scrollbar, the
                # selection, the images — §55).
                messages = data.get("messages") or []
                self._send_json(
                    {
                        "id": data.get("id"),
                        "title": data.get("title"),
                        "messages": messages[max(0, after) :],
                        "total": len(messages),
                    }
                )
        elif route == "/peers":
            self._send_json({"peers": self.runtime.peers()})
        elif route == "/consent-mode":
            host = (parse_qs(url.query).get("host") or [None])[0]
            if not host:
                self._send_json({"error": "missing host"}, status=400)
            else:
                self._send_json({"mode": self.runtime.consent_mode(host)})
        elif route == "/comm-log":
            host = (parse_qs(url.query).get("host") or [None])[0]
            if not host:
                self._send_json({"error": "missing host"}, status=400)
            else:
                # comm_log already returns the full friend-view payload
                # {messages, subagents, asks, events}; re-wrapping it under
                # "messages" handed the frontend an object where it expects
                # an array, and the render threw into the swallowed catch —
                # the friend view stayed blank forever.
                self._send_json(self.runtime.comm_log(host))
        elif route == "/mail":
            self._send_json(self.runtime.mail())
        elif route == "/download":
            # PC -> phone (§52): the file itself, or `?meta=1` for its name and
            # size so a downloader can plan windows before any bytes travel.
            self._handle_download(url)
        elif route == "/upload":
            # No sid: the capability question ("do you take windows?"), which a
            # page asks once before cutting a file up (§51). With one: how far
            # it has got, and which ranges are still missing.
            self._handle_upload_status(url)
        elif route == "/transfer-progress":
            job = (parse_qs(url.query).get("id") or [""])[0]
            self._send_json(self.runtime.transfer_progress(job))
        elif route == "/events":
            self._handle_events((parse_qs(url.query).get("sessionId") or [None])[0])
        elif route == "/spawn-pending":
            sid = (parse_qs(url.query).get("sessionId") or [None])[0]
            with _TURNS_LOCK:
                items = list(_PENDING_SPAWNS.get(sid or "") or [])
            self._send_json({"pending": len(items), "items": items})
        else:
            self._send_json({"error": "not found"}, status=404)

    # ---- POST -------------------------------------------------------------
    def do_POST(self):
        self._answer(self._post_routes)

    def _post_routes(self):
        if not self._gate():
            return
        self.runtime.touch()
        url = urlparse(self.path)
        if url.path == "/chat":
            self._handle_chat()
        elif url.path == "/retry":
            self._handle_retry()
        elif url.path == "/stop":
            data = self._read_body()
            sid = str(data.get("sessionId") or "")
            with _TURNS_LOCK:
                events = _ACTIVE_TURNS.pop(sid, set())
                bg = _BG_ABORTS.pop(sid, None)
                # Stop means stop: undelivered background reports must not
                # auto-reactivate the session 3s later (frontend resume poll).
                _PENDING_SPAWNS.pop(sid, None)
            for event in events:
                event.set()
            if bg is not None:
                bg.set()  # kill background subagents of already-ended turns too
            self._send_json({"ok": bool(events) or bg is not None})
        elif url.path == "/resume":
            self._handle_resume()
        elif url.path == "/answer":
            data = self._read_body()
            value = data.get("value")
            if isinstance(value, list):
                value = [str(v) for v in value]
            else:
                value = str(value or "")
            ok = self.runtime.route_answer(str(data.get("id") or ""), value)
            self._send_json({"ok": ok}, status=200 if ok else 404)
        elif url.path == "/session/seen":
            # §61: the page's claim about what it is showing (and the answer is
            # every outstanding alert, which is what paints the sidebar's dots).
            data = self._read_body()
            self._send_json(
                {"alerts": mark_seen(str(data.get("id") or ""), bool(data.get("visible")))}
            )
        elif url.path == "/mail/read":
            data = self._read_body()
            self._send_json(self.runtime.mail_read(str(data.get("id") or "")))
        elif url.path == "/configure":
            data = self._read_body()
            cfg = load_config()
            if data.get("api_key"):
                cfg.api_key = data["api_key"]
            if data.get("endpoint"):
                cfg.endpoint = data["endpoint"]
            if data.get("model"):
                cfg.model = data["model"]
            save_config(cfg)
            self._send_json({"ok": True})
        elif url.path == "/save":
            data = self._read_body()
            existing = self.runtime.sessions_load(data.get("id", ""))
            if existing is None:
                self._send_json({"ok": False}, status=400)
                return
            self.runtime.sessions_save(
                data["id"],
                # The transfer session's name is its identity (§54): a save
                # carrying another title keeps the real one.
                SHUTTLE_TITLE
                if data.get("id") == SHUTTLE_ID
                else (data.get("title") or existing.get("title") or ""),
                existing.get("messages", []),
                subagents=existing.get("subagents", []),
                asks=existing.get("asks", []),
            )
            self._send_json({"ok": True})
        elif url.path == "/new":
            session_id = self.runtime.new_session_id()
            self.runtime.sessions_save(
                session_id,
                "(new session)",
                [{"role": "system", "content": self.runtime.new_session_prompt()}],
            )
            self._send_json({"id": session_id, "title": "(new session)"})
        elif url.path == "/consent-mode":
            data = self._read_body()
            host = str(data.get("host") or "")
            mode = str(data.get("mode") or "")
            if not host or mode not in ("allow", "ask"):
                self._send_json({"error": "need host and mode (allow|ask)"}, status=400)
            else:
                self.runtime.set_consent_mode(host, mode)
                self._send_json({"ok": True, "mode": mode})
        elif url.path == "/upload":
            self._handle_upload(url)
        elif url.path == "/pickfile":
            self._handle_pickfile()
        elif url.path == "/comm-send":
            self._send_json(self.runtime.comm_send(self._read_body()))
        elif url.path == "/comm-note":
            self._send_json(self.runtime.comm_note(self._read_body()))
        else:
            self._send_json({"error": "not found"}, status=404)

    # ---- DELETE -----------------------------------------------------------
    def do_DELETE(self):
        self._answer(self._delete_routes)

    def _delete_routes(self):
        if not self._gate():
            return
        url = urlparse(self.path)
        if url.path == "/session":
            session_id = (parse_qs(url.query).get("id") or [None])[0]
            if session_id == SHUTTLE_ID:
                # Permanent by design (§54): deleting the channel between the
                # two devices is not a thing the UI hides, it is not a thing.
                self._send_json(
                    {"error": "文件传输助手不能删除（它是两台设备之间的通道）"}, status=400
                )
                return
            if session_id:
                with _TURNS_LOCK:
                    events = _ACTIVE_TURNS.pop(session_id, set())
                    if events:
                        # A turn still runs in this session: abort it and
                        # tombstone the id so its exit-path save cannot
                        # resurrect the file the user just deleted.
                        _TURN_DELETED.add(session_id)
                for event in events:
                    event.set()
                self.runtime.sessions_delete(session_id)
                forget_session(session_id)  # a deleted session has nothing to ring for
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_chat(self) -> None:
        data = self._read_body()
        session_id = data.get("sessionId")
        if session_id == SHUTTLE_ID:
            self._shuttle_turn(str(data.get("message") or ""), data.get("side"))
            return
        self._run_turn(session_id, user_msg=str(data.get("message") or ""))

    def _shuttle_turn(self, text: str, side: str | None = None) -> None:
        """A send in the transfer session: one row, and no model runs (§53).

        The page streams turns, so it gets the same NDJSON shape as any other
        turn — only there are no tokens behind it: the row is already on disk by
        the time `done` arrives, and the shells' usual reload paints it.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        sink = WebSink(self, SHUTTLE_ID)
        try:
            message = text.strip()
            if message:
                if side not in ("phone", "computer"):
                    side = None  # an unknown sender beats a wrong side (§59)
                shuttle_post(
                    self.runtime,
                    message,
                    file_in(message, side or "computer"),
                    side=side,
                )
            sink.emit("sessionId", SHUTTLE_ID)
        except Exception as exc:
            sink.emit("error", str(exc))
        sink.emit("done", None)
        with contextlib.suppress(OSError):
            self.wfile.flush()

    def _handle_retry(self) -> None:
        """Alt+R: rerun the last turn with no new prompt, continuing from real
        context (synthetic error tail is stripped; see sanitize_for_retry)."""
        data = self._read_body()
        session_id = data.get("sessionId")
        stored = self.runtime.sessions_load(session_id) if session_id else None
        if not stored:
            self._send_json({"error": "no session to retry"}, status=400)
            return
        messages = sanitize_for_retry(stored["messages"])
        if not any(m.get("role") != "system" for m in messages):
            self._send_json({"error": "nothing to retry"}, status=400)
            return
        self._run_turn(session_id, user_msg=None, messages=messages)

    def _handle_resume(self) -> None:
        """Re-activate a session whose background subagent(s) finished: pop the
        pending reports atomically and run a turn with them injected."""
        data = self._read_body()
        sid = str(data.get("sessionId") or "")
        with _TURNS_LOCK:
            items = _PENDING_SPAWNS.pop(sid, None)
        if not items:
            self._send_json({"ok": True, "injected": 0})
            return
        if _session_lock(sid).locked():
            # A turn is running: hand the reports back so the client retries
            # after it ends (pop-then-409 keeps results from being lost).
            with _TURNS_LOCK:
                _PENDING_SPAWNS.setdefault(sid, []).extend(items)
            self._send_json({"busy": True}, status=409)
            return
        self._run_turn(sid, user_msg=None, resume_items=items)

    @staticmethod
    def _spawn_report(items: list[dict]) -> str:
        rows = "\n".join(
            f"- id={i['id']} ({i['status']}) goal: {str(i['goal'])[:120]}\n"
            f"  report: {str(i['answer'])[:2000]}"
            for i in items
        )
        return (
            "[background report] Subagent task(s) you dispatched have finished. "
            "This note is for you - the user sees your reply, not this note.\n" + rows
        )

    def _run_turn(
        self,
        session_id: str | None,
        user_msg: str | None,
        messages=None,
        resume_items: list[dict] | None = None,
    ) -> None:
        if messages is None and not session_id:
            # Generate before registering: /stop keys on the real session id.
            session_id = self.runtime.new_session_id()
        abort_event = threading.Event()
        with _TURNS_LOCK:
            _ACTIVE_TURNS.setdefault(session_id, set()).add(abort_event)
        sink = WebSink(self, session_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            if _session_lock(session_id).locked():
                # A queued turn must not look dead: say why nothing streams yet.
                sink.emit("status", "Waiting for the still-running turn in this session...")
            with _session_lock(session_id):
                # Load context inside the lock: a queued turn must continue
                # from the previous turn's persisted reply, not a snapshot
                # taken before the wait (which dropped that reply on save).
                stored = self.runtime.sessions_load(session_id) if session_id else None
                if messages is None:
                    if stored:
                        messages = list(stored["messages"])
                    else:
                        messages = [
                            {"role": "system", "content": self.runtime.new_session_prompt()}
                        ]
                    if user_msg is not None:
                        messages.append({"role": "user", "content": user_msg})
                if resume_items:
                    # Patch the persisted spawn records with their final
                    # status/answer, then inject the report as this turn's input.
                    for rec in (
                        (stored or {}).get("subagents", []) if isinstance(stored, dict) else []
                    ):
                        for item in resume_items:
                            if rec.get("id") == item["id"]:
                                rec["status"] = item["status"]
                                rec["answer"] = item["answer"]
                    # Their own agent_status events went to the (already
                    # closed) spawning turn's stream, so the frontend bubbles
                    # would never close: re-emit the final status here, on the
                    # live resume stream.
                    for item in resume_items:
                        sink.emit("agent_status", {"id": item["id"], "status": item["status"]})
                    messages.append({"role": "user", "content": self._spawn_report(resume_items)})
                messages = repair_tool_gaps(messages)

                # Persist at turn start: the user message must be on disk while
                # the turn streams. Otherwise a refresh mid-turn shows an empty
                # session and invites chatting into / deleting one that is busy.
                prior = (stored or {}).get("subagents", []) if isinstance(stored, dict) else []
                prior_asks = (stored or {}).get("asks", []) if isinstance(stored, dict) else []
                self.runtime.sessions_save(
                    session_id,
                    session.get_session_title(messages),
                    public_messages(messages),
                    subagents=prior,
                    asks=prior_asks,
                )
                # Start recording after the queue wait: a queued turn must not
                # clobber the still-running turn's tape for the same session.
                with _TURNS_LOCK:
                    _TURN_TAPES[session_id] = []
                agent = self.runtime.build_agent(sink, abort_event.is_set)
                try:
                    agent.run(messages)
                finally:
                    # Persist on every exit path (success, abort, crash): a
                    # turn that never saves is a turn whose context is lost.
                    subs = getattr(agent, "subagents", None)
                    new_subs = list(subs.values()) if isinstance(subs, dict) else []
                    new_asks = list(getattr(agent, "asks", None) or [])
                    with _TURNS_LOCK:
                        resurrects = session_id in _TURN_DELETED
                        if resurrects:
                            _TURN_DELETED.discard(session_id)
                    if not resurrects:
                        self.runtime.sessions_save(
                            session_id,
                            session.get_session_title(messages),
                            public_messages(messages),
                            subagents=prior + new_subs,
                            asks=prior_asks + new_asks,
                        )
            # §61: a finished turn is news — unless the user stopped it (they
            # were right there) or deleted the session under it. note_alert
            # drops it anyway while a page is showing that session.
            if not resurrects and not abort_event.is_set():
                note_alert(session_id, "ask" if unanswered_asks(new_asks) else "done")
            sink.emit("sessionId", session_id)
            sink.emit("done", None)
        except Exception as exc:
            if not abort_event.is_set():  # a stopped turn must not ring, however it ended
                note_alert(session_id, "error")
            sink.emit("error", str(exc))
            sink.emit("done", None)
        finally:
            # Seal the tape with a done marker (replay consumers close on it)
            # and pop it after a grace window so late reattach still sees it.
            # The pop must be generation-aware: a NEW turn in the same session
            # may have installed a fresh tape within the grace window — popping
            # by session id alone would delete a live turn's tape mid-run.
            with _TURNS_LOCK:
                tape = _TURN_TAPES.get(session_id)
                if tape is not None:
                    tape.append({"type": "done", "content": None})

            def _pop_tape_if_current(_t=tape):
                with _TURNS_LOCK:
                    if _TURN_TAPES.get(session_id) is _t:
                        _TURN_TAPES.pop(session_id, None)

            seal = threading.Timer(_TAPE_GRACE_S, _pop_tape_if_current)
            seal.daemon = True
            seal.start()
            with _TURNS_LOCK:
                events = _ACTIVE_TURNS.get(session_id)
                if events is not None:
                    events.discard(abort_event)
                    if not events:
                        _ACTIVE_TURNS.pop(session_id, None)
            self.wfile.flush()

    def _handle_events(self, session_id: str | None) -> None:
        """Reattach to a (recently) running turn: replay its recorded events,
        then live-stream new ones until the tape's done marker. A missing tape
        means nothing is running — answer with a bare done so the client
        simply reloads from disk."""
        if not session_id:
            self._send_json({"error": "need sessionId"}, status=400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        idx = 0
        try:
            while True:
                with _TURNS_LOCK:
                    tape = _TURN_TAPES.get(session_id)
                    fresh = tape[idx:] if tape is not None else []
                    idx += len(fresh)
                try:
                    for ev in fresh:
                        self.wfile.write(
                            (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
                        )
                    if fresh:
                        self.wfile.flush()
                        if any(ev.get("type") == "done" for ev in fresh):
                            return
                    elif tape is None:
                        self.wfile.write(b'{"type": "done", "content": null}\n')
                        self.wfile.flush()
                        return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_upload(self, url) -> None:
        """Phone -> PC file upload: raw bytes, name in X-Fungi-Filename.

        Lands in the configured inbox (default: inbox/ beside the program, or
        the per-user folder when that is read-only); the mobile UI inserts the
        returned absolute path into the message box, so the agent reads it like
        any local file.

        One POST or several (§51): with `sid`/`offset`/`size` in the query this
        body is one window of a file whose other windows are arriving on other
        connections; without them it is the whole file — which is what every
        page written before this one sends, and the two shapes land the same way.
        The bytes stream straight to disk (spec §48: no cap, no buffer) into
        `<name>.<sid>.part`, and the real name appears only once every byte is
        covered (§49): a phone that walks away, a host killed mid-body, a link
        that dies — none of them can leave a file under the real name that is
        not the whole file.
        """
        name = unquote(self.headers.get("X-Fungi-Filename") or "")
        if not name:
            # A page from before this wire changed (spec §48) still posts
            # multipart; say what to do about it instead of "missing header".
            self._send_json(
                {"error": "upload must be raw bytes with X-Fungi-Filename — reload the page"},
                status=400,
            )
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_json({"error": "empty upload"}, status=400)
            return
        params = parse_qs(url.query)
        offset_raw = (params.get("offset") or [""])[0]
        size_raw = (params.get("size") or [""])[0]
        sid = upload_sid((params.get("sid") or [""])[0])
        windowed = bool(offset_raw or size_raw)
        if windowed:
            offset = _int_or_none(offset_raw)
            total = _int_or_none(size_raw)
            # A session id becomes part of a file name, so an unusable one is a
            # refusal — never a quiet fallback to "then this body is the file".
            if not sid or offset is None or total is None or total <= 0 or offset + length > total:
                self._send_json({"error": "bad upload window"}, status=400)
                return
        else:
            # The page handed over the file in one body: it is a window too, the
            # one that covers everything, and the id is ours to mint.
            sid = secrets.token_hex(4)
            offset, total = 0, length
        UPLOADS.sweep()
        up = UPLOADS.session(sid, name, total, _inbox_dir())
        if up["total"] != total or up["name"] != safe_name(name):
            self._send_json({"error": "this upload session belongs to another file"}, status=400)
            return
        try:
            got = self._receive_upload(up, offset, length)
        except OSError as exc:
            UPLOADS.drop(sid)
            self._send_json({"error": f"cannot save {name}: {exc}"}, status=507)
            return
        if got < length:
            # The body stopped early (the phone went away, the link dropped).
            # The bytes that did arrive stay: the page is told what is missing
            # and re-sends just that. In one body there is nothing to resume —
            # the part goes, exactly as it did before this change.
            if not windowed:
                UPLOADS.drop(sid)
                self._send_json({"error": "truncated upload"}, status=400)
                return
            reply = {
                "error": "truncated upload",
                "received": up["spans"].bytes,
                "missing": UPLOADS.missing(up),
            }
            self._send_json(reply, status=400)
            return
        reply = land_upload(up)
        if reply["done"]:
            UPLOADS.drop(sid)  # the part has been renamed; the session is done
            self._note_transfer(reply)
        self._send_json(reply)

    def _note_transfer(self, landed: dict) -> None:
        """A phone upload that landed is worth a row in the transfer session.

        Best effort: the file is on disk either way, and a broken session store
        must not turn a successful upload into a failure (§53).
        """
        try:
            shuttle_post(
                self.runtime,
                _shuttle_row("手机上传", landed),
                file={
                    "name": landed["name"],
                    "size": landed["size"],
                    "path": landed["path"],
                    "direction": "phone",
                },
            )
        except Exception as exc:
            runlog.warn_once("shuttle-post", "could not write the transfer session: %s", exc)

    def _handle_upload_status(self, url) -> None:
        """How far a windowed upload has got — and whether it is already whole.

        No `sid` is the capability question a page asks once, before cutting a
        file up: an old host answers 404 and the page sends one body, which it
        always did. With a `sid` it is the answer a sender cannot get any other
        way: which byte ranges the host is still missing (§51).
        """
        sid = upload_sid((parse_qs(url.query).get("sid") or [""])[0])
        if not sid:
            self._send_json({"ok": True, "parts": True, "min_part": UPLOAD_PART_MIN})
            return
        UPLOADS.sweep()
        up = UPLOADS.get(sid)
        if up is None:
            self._send_json({"error": "unknown upload session"}, status=404)
            return
        reply = land_upload(up)
        if reply["done"]:
            UPLOADS.drop(sid)
        self._send_json(reply)

    def _receive_upload(self, up: dict, offset: int, length: int) -> int:
        """Stream this request's body into the part at `offset`; bytes that arrived.

        Streaming is the point (§48): the heap cost of a gigabyte is one read's
        worth. Coverage is recorded after the handle is closed, so what is
        counted as landed is what is really on the disk.
        """
        at = offset
        left = length
        with up["part"].open("r+b") as out:
            out.seek(offset)
            while left > 0:
                chunk = self.rfile.read(min(UPLOAD_READ, left))
                if not chunk:
                    break
                out.write(chunk)
                at += len(chunk)
                left -= len(chunk)
        if at > offset:
            up["spans"].add(offset, at)
        return at - offset

    def _handle_download(self, url) -> None:
        """PC -> phone: hand the phone a file that is on this machine (§52).

        `?meta=1` answers what a downloader needs to plan with — name and size —
        without sending bytes; without it this is the file itself, and `Range`
        is the hub's contract (§50): 206 for the window asked for, 416 when it
        starts past the end, 200 for the whole file when no Range is asked. So
        the phone can fetch several windows at once and resume one that died,
        while a plain link, `curl`, or the browser's own downloader takes it in
        one piece.

        The WebUI token is the door, and it means "the owner's own device" — the
        phone that scanned the host's QR code (room friends hold the *room*
        token, which opens nothing here). That is the same standing as the
        agent's own `read` tool, which takes any path on this disk: this route
        adds no new kind of access, only a quicker way to get the bytes.
        """
        params = parse_qs(url.query)
        raw = (params.get("path") or [""])[0]
        if not raw:
            self._send_json({"error": "missing path"}, status=400)
            return
        target = Path(raw)
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        if not target.is_file():
            self._send_json({"error": f"no such file: {raw}"}, status=404)
            return
        size = target.stat().st_size
        if (params.get("meta") or [""])[0]:
            self._send_json({"ok": True, "name": target.name, "size": size})
            return
        try:
            window = range_window(self.headers.get("Range") or "", size)
        except RangeNotSatisfiableError:
            # Past the last byte: the size is the correction, and no bytes travel.
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        first, last = window if window is not None else (0, size - 1)
        length = last - first + 1
        # Headers are latin-1 only: RFC 5987 the unicode name, as the hub does.
        quoted = quote(target.name, safe="")
        try:
            with target.open("rb") as fh:
                fh.seek(first)
                self.send_response(206 if window is not None else 200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Type", _mime.get(target.suffix.lower(), "application/octet-stream")
                )
                self.send_header("Content-Length", str(length))
                if window is not None:
                    self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}",
                )
                self.end_headers()
                sent = 0
                while sent < length:
                    chunk = fh.read(min(DOWNLOAD_CHUNK, length - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
        except OSError:
            pass  # the phone stopped asking (it closed the tab); nothing to repair

    def _handle_pickfile(self) -> None:
        try:
            import tkinter as tk  # noqa: PLC0415 (heavy GUI import, only on demand)
            from tkinter import filedialog  # noqa: PLC0415

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(title="Select a file")
            root.destroy()
            self._send_json({"path": path or None})
        except Exception as exc:
            self._send_json({"path": None, "error": str(exc)})


def _free_port(preferred: int | None) -> int:
    port = preferred or 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        return sock.getsockname()[1]


def run_server(port: int | None = None, runtime: WebUIRuntime | None = None) -> None:
    import webbrowser  # noqa: PLC0415 (only needed to open the browser)

    rt = runtime or WebUIRuntime()
    server = make_webui_server(port, rt)
    url = f"http://localhost:{server.server_address[1]}"
    print(f"  YESIR web UI: {url}")
    print("  Press Ctrl+C to stop")
    webbrowser.open(url)
    mcp_tools = rt.mcp_tools()
    if mcp_tools:
        print(f"  MCP: {len(mcp_tools)} tools loaded -> {', '.join(sorted(mcp_tools))}")
    elif load_config().mcp_servers:
        print("  MCP: servers configured but none loaded (see stderr)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class WebUIServer(ThreadingHTTPServer):
    """Mobile browsers open speculative connections and reset them constantly;
    those are noise, not errors — don't dump a traceback per reset."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)
        ):
            return
        super().handle_error(request, client_address)


def make_webui_server(port: int | None, runtime: WebUIRuntime) -> WebUIServer:
    """Build (not start) the WebUI server; room mode embeds this in-process."""
    handler = type("BoundHandler", (YesSirHandler,), {"runtime": runtime})
    server = WebUIServer(("0.0.0.0", _free_port(port)), handler)
    # The one fact a phone-shaped report is missing: which port the page is on
    # (the room picks upward from the anchor, so it is not always 8899).
    runlog.note("WebUI listening on 0.0.0.0:%d", server.server_address[1])
    return server
