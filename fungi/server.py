"""WebUI server: ThreadingHTTPServer + NDJSON streaming + static files from web/.

Single-host mode keeps the original YESIR behavior (TriLayer in the request
thread, local session files). Room mode (fungi/room.py) injects a WebUIRuntime
that runs the local clone's toolset, backs sessions per host (server: hub
store; client: its own disk — never the peer-operated hub), and routes
card answers back out as answer envelopes.
"""

import json
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fungi import runlog, session
from fungi.agent import SYSTEM_PROMPT, Agent, public_messages
from fungi.config import PROJECT_ROOT, RESOURCE_ROOT, load_config, save_config
from fungi.events import Sink
from fungi.hub.app import safe_name
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


def _extract_upload(body: bytes, boundary: bytes) -> tuple[str, bytes] | None:
    """Pull the first file part out of a multipart/form-data body. Hand-rolled
    because stdlib cgi is gone in 3.13. Returns (filename, content) or None."""
    for part in body.split(b"--" + boundary):
        if part[:2] in (b"", b"--"):
            continue  # preamble/empty chunk, or the closing "--" terminator
        chunk = part[2:] if part.startswith(b"\r\n") else part
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep or b'filename="' not in head:
            continue
        m = re.search(rb'filename="([^"]*)"', head)
        if not m:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]  # the \r\n before the next delimiter is framing
        return m.group(1).decode("utf-8", "replace"), content
    return None


def _inbox_save(filename: str, data: bytes) -> Path:
    """Land an uploaded file in the configured inbox (same dir the comm-clone
    transfer flow uses), sanitizing the name and numbering collisions."""
    cfg = load_config()
    inbox = Path(cfg.inbox_dir) if cfg.inbox_dir else PROJECT_ROOT / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / safe_name(filename)
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = inbox / f"{stem}-{n}{suffix}"
        n += 1
    dest.write_bytes(data)
    return dest


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
            sessions = self.runtime.sessions_list()
            with _TURNS_LOCK:
                for s in sessions:
                    tape = _TURN_TAPES.get(str(s.get("id")))
                    s["running"] = tape is not None and not any(
                        ev.get("type") == "done" for ev in tape
                    )
            self._send_json({"sessions": sessions})
        elif route == "/session":
            session_id = (parse_qs(url.query).get("id") or [None])[0]
            data = self.runtime.sessions_load(session_id) if session_id else None
            if data is None:
                self._send_json({"error": "not found"}, status=404)
            else:
                self._send_json(data)
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
                data.get("title") or existing.get("title") or "",
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
            self._handle_upload()
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
        if not self._gate():
            return
        url = urlparse(self.path)
        if url.path == "/session":
            session_id = (parse_qs(url.query).get("id") or [None])[0]
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
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_chat(self) -> None:
        data = self._read_body()
        self._run_turn(data.get("sessionId"), user_msg=str(data.get("message") or ""))

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
            sink.emit("sessionId", session_id)
            sink.emit("done", None)
        except Exception as exc:
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

    def _handle_upload(self) -> None:
        """Phone -> PC file upload: multipart/form-data with a "file" field.
        Lands in the configured inbox (default PROJECT_ROOT/inbox); the mobile
        UI inserts the returned absolute path into the message box, so the
        agent reads it like any local file."""
        ctype = self.headers.get("Content-Type") or ""
        match = re.search(r'boundary="?([^";]+)"?', ctype)
        if not ctype.startswith("multipart/form-data") or not match:
            self._send_json({"error": "need multipart/form-data"}, status=400)
            return
        limit = max(1, load_config().max_file_mb) * 1024 * 1024
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            self.close_connection = True
            drained = 0  # drain before answering: closing mid-upload RSTs the
            while drained < length:  # socket and the client never sees the 413
                chunk = self.rfile.read(min(65536, length - drained))
                if not chunk:
                    break
                drained += len(chunk)
            self._send_json({"error": "file too large"}, status=413)
            return
        part = _extract_upload(self.rfile.read(length), match.group(1).encode("latin-1"))
        if part is None:
            self._send_json({"error": "no file part"}, status=400)
            return
        name, data = part
        path = _inbox_save(name, data)
        self._send_json({"ok": True, "path": str(path), "name": path.name, "size": len(data)})

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
