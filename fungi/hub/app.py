"""Hub HTTP API: join/leave/heartbeat/send/poll + fs + sessions + transfers.

Token auth on every endpoint. Transfers are store-and-forward: file bytes are
copied server-side out of the data/ store (never through an envelope), the
receiver pulls them via GET /api/transfer. The comm log mirrors clone-to-clone
traffic for the WebUI read-only conversation view.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import runlog
from ..protocol import (
    BAD_NAME_MSG,
    Envelope,
    ProtocolError,
    clean_display,
    deserialize,
    new_id,
    parse_addr,
    valid_host_name,
)
from .asks import Asks
from .commlog import CommLog
from .mail import Mailbox
from .relay import Relay
from .roster import Roster
from .store import GuardError, Store

MAX_BODY = 5 * 1024 * 1024
MAX_POLL = 25.0
# Sweep stale ask records. MUST exceed the longest waiter (the consent card
# holds a clone for ask_timeout_s=1800): a swept record outlives its card,
# so a late click resolves nothing. Pinned by tests/test_ask.py.
ASK_TIMEOUT = 1900.0
REAP_INTERVAL = 5.0


def fs_via_hub(
    store: Store,
    host: str,
    op: str,
    path: str,
    *,
    content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    pattern: str | None = None,
    consent_id: str | None = None,
) -> dict:
    """Shared fs dispatch for the HTTP handler and server-local clones."""
    try:
        if op in {"glob", "grep"}:
            root = store.check_search_root(host, path)
            if op == "glob":
                result = store.glob(root, pattern or "*")
            else:
                result = store.grep(root, pattern or "")
        else:
            target = store.resolve(
                host, path, consent_id=consent_id, mutating=op in {"write", "edit"}
            )
            if op == "ls":
                result = store.ls(target)
            elif op == "read":
                result = store.read(target)
            elif op == "write":
                result = store.write(target, content or "")
            elif op == "edit":
                result = store.edit(target, old_string or "", new_string or "")
            else:
                return {"error": f"unknown op: {op}"}
        return {"ok": True, "result": result}
    except GuardError as exc:
        return {"error": str(exc)}


def safe_name(name: str) -> str:
    """Basename-only file name for anything stored under a hub directory."""
    return Path(str(name or "file").replace("\\", "/")).name or "file"


class Transfers:
    """In-memory registry of staged file transfers (metadata only on the wire).

    No size cap (spec §48): a staged file is streamed to disk in 256 KiB
    chunks, so the only limit is free space — and it fails as an OSError the
    callers report, never as a dropped connection.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._records: dict[str, dict] = {}
        self._guard = threading.Lock()

    def stage_from(self, name: str, src_host: str, dst_host: str, read) -> dict:
        """Stage streamed bytes (read(n) -> b"" ends input) into the transfers dir."""

        tid = new_id()
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self.root / f"{tid}__{safe_name(name)}"
        size = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = read(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        rec = {
            "id": tid,
            "name": safe_name(name),
            "size": size,
            "src": src_host,
            "dst": dst_host,
            "sent": 0,  # bytes handed to the receiver so far (Transfers.moved)
        }
        with self._guard:
            self._records[tid] = rec
        return rec

    def stage(self, source: Path, name: str, src_host: str, dst_host: str) -> dict:
        """Copy a store file into the transfers dir; returns its record."""
        with source.open("rb") as fh:
            return self.stage_from(name, src_host, dst_host, fh.read)

    def discard(self, transfer_id: str) -> None:
        """Drop a staged transfer (registry + bytes); for aborted uploads."""
        with self._guard:
            rec = self._records.pop(str(transfer_id), None)
        if rec is not None:
            (self.root / f"{rec['id']}__{rec['name']}").unlink(missing_ok=True)

    def discard_for(self, transfer_id: str, host: str) -> bool:
        """Receiver-authorized discard: only the designated dst host may drop
        a staged transfer (used after a successful delivery)."""
        with self._guard:
            rec = self._records.get(str(transfer_id))
            if rec is None or rec.get("dst") != host:
                return False
            del self._records[str(transfer_id)]
        (self.root / f"{rec['id']}__{rec['name']}").unlink(missing_ok=True)
        return True

    def fetchable(self, transfer_id: str, host: str) -> tuple[dict, Path] | None:
        """Record + file path, only for the designated receiver host."""
        with self._guard:
            rec = self._records.get(str(transfer_id))
        if rec is None or rec["dst"] != host:
            return None
        path = self.root / f"{rec['id']}__{rec['name']}"
        if not path.is_file():
            return None
        return rec, path

    def moved(self, transfer_id: str, sent: int) -> None:
        """How many bytes the hub has handed to the receiver so far.

        The download route streams, so this is the only place in the system
        that knows the delivery is progressing (§49): the sender's page used to
        stare at a step with nothing behind it while 1 GB crawled over WiFi.
        """
        with self._guard:
            rec = self._records.get(str(transfer_id))
        if rec is not None:
            rec["sent"] = max(0, int(sent))

    def progress_for(self, transfer_id: str, host: str) -> dict | None:
        """Delivery progress, for the two hosts allowed to know: sender and
        designated receiver. Any other caller gets nothing (the room token is
        the outer door; this is the same door one step in)."""
        with self._guard:
            rec = self._records.get(str(transfer_id))
        if rec is None or host not in (rec.get("src"), rec.get("dst")):
            return None
        return {"sent": int(rec.get("sent") or 0), "total": int(rec["size"])}


class Hub:
    """Room server: roster + relay + asks + store behind one HTTP API."""

    def __init__(
        self,
        name: str,
        token: str,
        data_root: Path,
        heartbeat_timeout: float = 30.0,
        port: int = 0,
    ):
        self.name = name
        self.token = token
        self._bind_port = port
        data_root = Path(data_root)
        self.roster = Roster(heartbeat_timeout)
        self.relay = Relay(name)
        self.asks = Asks()
        self.store = Store(data_root, self.asks)
        self.commlog = CommLog(data_root / "comm")
        self.mail = Mailbox(data_root / "mail")
        self.transfers = Transfers(data_root / "transfers")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("hub not started")
        return self._server.server_address[1]

    def start(self) -> None:
        handler = type("HubHandler", (_Handler,), {"hub": self})
        self._server = ThreadingHTTPServer(("0.0.0.0", self._bind_port), handler)
        self._server.daemon_threads = True
        runlog.note("hub listening on 0.0.0.0:%d as %r", self.port, self.name)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fungi-hub", daemon=True
        )
        self._thread.start()
        self._reaper = threading.Thread(target=self._reap_loop, name="fungi-reaper", daemon=True)
        self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            runlog.note("hub on port %d stopped", self._server.server_address[1])
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _reap_loop(self) -> None:
        while not self._stop.wait(REAP_INTERVAL):
            for name in self.roster.reap():
                self.relay.drop_host(name)
            self.asks.sweep(ASK_TIMEOUT)

    # ── operations shared by handler, clones, and tests ──

    def join(self, name: str, addr: str, display: str = "") -> dict:
        if not valid_host_name(name):
            raise ValueError(BAD_NAME_MSG)
        new = self.roster.join(name, addr, clean_display(display))
        self.store.ensure_home(name)
        self.relay.host_buffer(name)
        return {
            "ok": True,
            "host": name,
            "new": new,
            "peers": self.roster.peers(name),
            "roster": self.roster.entries(name),
        }

    def send(self, env: Envelope) -> dict:
        # The peer writes both addresses and the hub turns the host part into
        # data/ file names (data/mail/<host>.jsonl, the comm log) and a relay
        # key: reject anything that is not a legal hostname before that.
        for addr in (env.src, env.dst):
            try:
                host_part = parse_addr(addr)[0]
            except ProtocolError:
                return {"ok": False, "status": "bounced"}
            if not valid_host_name(host_part):
                return {"ok": False, "status": "bounced"}
        # ask/answer envelopes maintain the consent registry transparently:
        # ask -> opened with the envelope id (so consent_id == envelope id),
        # answer -> resolves the referenced ask.
        if env.type == "ask":
            host, _role, _peer = parse_addr(env.dst)
            self.asks.open(host, env.body, ask_id=env.id, src=env.src)
        elif env.type == "answer" and env.reply_to:
            self.asks.resolve(env.reply_to, value=env.body.get("value"))
        if env.type == "mail":
            # Text mail is consumed by the hub itself: it lands in the
            # recipient's mailbox (server-authoritative, survives offline)
            # instead of buffering on the relay — the receiving agent never
            # wakes for it.
            host, _role, _peer = parse_addr(env.dst)
            out = self.mail.deliver_pair(
                str(env.body.get("from") or env.src),
                host,
                str(env.body.get("subject") or ""),
                str(env.body.get("text") or ""),
            )
            return {**out, "status": "mailed" if out.get("ok") else "bounced"}
        status = self.relay.deliver(env)
        if status != "bounced":
            self.commlog.record(env)  # mirror clone-to-clone traffic for the WebUI
        return {"ok": status != "bounced", "status": status}

    def create_transfer(self, host: str, path: str, to_host: str, name: str) -> dict:
        """Stage a store file for delivery to another host (server-side copy)."""
        if not self.roster.known(host) or not self.roster.known(to_host):
            return {"error": "unknown host"}
        if to_host == host:
            return {"error": "cannot transfer to yourself"}
        try:
            target = self.store.resolve(host, path, consent_id=None, mutating=False)
        except GuardError as exc:
            return {"error": str(exc)}
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        try:
            rec = self.transfers.stage(target, name or target.name, host, to_host)
        except OSError as exc:
            return {"error": str(exc)}
        return {"ok": True, **rec}

    def upload_transfer(self, host: str, to_host: str, name: str, read) -> dict:
        """Stage bytes streamed from a host's local disk (raw upload path).

        Unlike create_transfer (store-side copy), the bytes come straight off
        the sender's machine — this is how the user-facing clone sends a real
        local file. An unwritable staging disk raises OSError; callers report
        it (nobody refuses on size any more, spec §48).
        """
        if not self.roster.known(host) or not self.roster.known(to_host):
            return {"error": "unknown host"}
        if to_host == host:
            return {"error": "cannot transfer to yourself"}
        rec = self.transfers.stage_from(name or "file", host, to_host, read)
        return {"ok": True, **rec}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: Hub = None  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:
        pass  # quiet

    # ── plumbing ──

    def _reply(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Long-poll replies race client reconnects/disconnects all the
            # time; a gone reader is routine, not an error worth a traceback.
            self.close_connection = True

    def _bad_token(self) -> None:
        """Someone knocked with the wrong token — the host's half of "I cannot join".

        Throttled per caller: a client holding a stale token polls once a second,
        and the log is worth reading only if this is one line a minute. The route
        is logged without its query: on a GET the query *is* the token, and this
        file gets attached to bug reports (the repo is public).
        """
        peer = self.client_address[0]
        runlog.warn_once(
            f"hub-bad-token:{peer}",
            "hub: %s used the wrong token on %s — a friend holding an old token?",
            peer,
            self.path.split("?", 1)[0],
        )
        self._reply({"error": "bad token"}, 403)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        return data

    # ── routing ──

    def do_POST(self) -> None:
        try:
            url = urlparse(self.path)
            if url.path == "/api/transfer/upload":
                self._transfer_upload(url)
                return
            body = self._body()
            if body.get("token") != self.hub.token:
                self._bad_token()
                return
            path = url.path
            if path == "/api/join":
                self._join(body)
            elif path == "/api/leave":
                self._leave(body)
            elif path == "/api/heartbeat":
                self._heartbeat(body)
            elif path == "/api/send":
                self._send(body)
            elif path.startswith("/api/fs/"):
                self._fs(path[len("/api/fs/") :], body)
            elif path == "/api/save":
                self._save(body)
            elif path == "/api/session/delete":
                self._session_delete(body)
            elif path == "/api/transfer":
                self._transfer(body)
            elif path == "/api/mail/read":
                self._mail_read(body)
            else:
                self._reply({"error": "not found"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply({"error": str(exc)}, 400)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        params = parse_qs(url.query)
        token = (params.get("token") or [""])[0]
        if token != self.hub.token:
            self._bad_token()
            return
        if url.path == "/api/mail":
            self._mail(params)
        elif url.path == "/api/poll":
            self._poll(params)
        elif url.path == "/api/sessions":
            self._reply({"sessions": self.hub.store.sessions.list_sessions()})
        elif url.path == "/api/session":
            self._session_load(params)
        elif url.path == "/api/peers":
            self._peers(params)
        elif url.path == "/api/comm-log":
            self._comm_log(params)
        elif url.path == "/api/transfer":
            self._transfer_download(params)
        elif url.path == "/api/transfer/progress":
            self._transfer_progress(params)
        else:
            self._reply({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply({"error": str(exc)}, 400)
            return
        # The token rides in the body, like every other non-GET route: reading it
        # from the query here rejected the client's own discard with 403 (its
        # body-carried token was never looked at), so a delivered transfer kept
        # its staged copy on the hub until restart — a whole file per delivery.
        if body.get("token") != self.hub.token:
            self._bad_token()
            return
        if url.path == "/api/transfer":
            ok = self.hub.transfers.discard_for(
                str(body.get("id") or ""), str(body.get("host") or "")
            )
            self._reply({"ok": bool(ok)})
        else:
            self._reply({"error": "not found"}, 404)

    # ── room ops ──

    def _join(self, body: dict) -> None:
        name = body.get("name")
        try:
            self._reply(
                self.hub.join(str(name), self.client_address[0], str(body.get("display") or ""))
            )
        except ValueError as exc:
            self._reply({"error": str(exc)}, 400)

    def _leave(self, body: dict) -> None:
        name = body.get("name")
        if self.hub.roster.leave(str(name)):
            self.hub.relay.drop_host(str(name))
        self._reply({"ok": True})

    def _heartbeat(self, body: dict) -> None:
        name = str(body.get("name"))
        if not self.hub.roster.beat(name):
            self._reply({"error": "unknown host"}, 404)
            return
        self._reply(
            {
                "ok": True,
                "peers": self.hub.roster.peers(name),
                "roster": self.hub.roster.entries(name),
                "pending_asks": self.hub.asks.pending_for(name),
            }
        )

    def _peers(self, params: dict) -> None:
        host = (params.get("host") or [""])[0]
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 404)
            return
        self._reply({"ok": True, "peers": self.hub.roster.entries(host)})

    # ── mail ──

    def _mail(self, params: dict) -> None:
        host = (params.get("host") or [""])[0]
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 404)
            return
        self._reply(self.hub.mail.list(host))

    def _mail_read(self, body: dict) -> None:
        host = str(body.get("host") or "")
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 403)
            return
        self._reply(self.hub.mail.mark_read(host, str(body.get("id") or "")))

    def _comm_log(self, params: dict) -> None:
        host = (params.get("host") or [""])[0]
        other = (params.get("with") or [""])[0]
        if not self.hub.roster.known(host) or not other:
            self._reply({"error": "bad request"}, 400)
            return
        self._reply({"ok": True, "messages": self.hub.commlog.read(host, other)})

    def _send(self, body: dict) -> None:
        try:
            env = deserialize(body.get("envelope") or {})
        except ProtocolError as exc:
            self._reply({"error": str(exc)}, 400)
            return
        self._reply(self.hub.send(env))

    def _poll(self, params: dict) -> None:
        host = (params.get("host") or [""])[0]
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 404)
            return
        after = int((params.get("after") or ["0"])[0])
        timeout = min(float((params.get("timeout") or ["0"])[0]), MAX_POLL)
        inbox = self.hub.relay.host_buffer(host)
        messages, cursor = inbox.after(after, timeout)
        self._reply({"messages": [e.serialize() for e in messages], "cursor": cursor})

    # ── fs ops ──

    def _fs(self, op: str, body: dict) -> None:
        host = str(body.get("host") or "")
        if not self.hub.roster.known(host):
            self._reply({"error": "unknown host"}, 403)
            return
        out = fs_via_hub(
            self.hub.store,
            host,
            op,
            str(body.get("path") or ""),
            content=body.get("content"),
            old_string=body.get("old_string"),
            new_string=body.get("new_string"),
            pattern=body.get("pattern"),
            consent_id=body.get("consent_id"),
        )
        self._reply(out, 403 if "error" in out else 200)

    # ── sessions ──

    def _save(self, body: dict) -> None:
        store = self.hub.store.sessions
        store.save(
            str(body.get("id") or ""),
            str(body.get("title") or ""),
            body.get("messages") or [],
            body.get("subagents"),
            body.get("asks"),
        )
        self._reply({"ok": True})

    def _session_load(self, params: dict) -> None:
        sid = (params.get("id") or [""])[0]
        data = self.hub.store.sessions.load(sid)
        if data is None:
            self._reply({"error": "not found"}, 404)
            return
        self._reply(data)

    def _session_delete(self, body: dict) -> None:
        self.hub.store.sessions.delete(str(body.get("id") or ""))
        self._reply({"ok": True})

    # ── transfers ──

    def _transfer(self, body: dict) -> None:
        host = str(body.get("host") or "")
        to_host = str(body.get("to") or "")
        out = self.hub.create_transfer(
            host, str(body.get("path") or ""), to_host, str(body.get("name") or "")
        )
        self._reply(out, 400 if "error" in out else 200)

    def _transfer_upload(self, url) -> None:
        """Raw-bytes upload: /api/transfer/upload?token&host&to&name (streamed)."""
        params = parse_qs(url.query)
        token = (params.get("token") or [""])[0]
        if token != self.hub.token:
            self._bad_token()
            return
        host = (params.get("host") or [""])[0]
        to_host = (params.get("to") or [""])[0]
        name = (params.get("name") or [""])[0] or "file"
        remaining = int(self.headers.get("Content-Length") or 0)
        if remaining <= 0:
            self._reply({"error": "empty upload"}, 400)
            return

        def read(n: int) -> bytes:
            nonlocal remaining
            if remaining <= 0:
                return b""
            chunk = self.rfile.read(min(n, remaining))
            remaining -= len(chunk)
            return chunk

        try:
            out = self.hub.upload_transfer(host, to_host, name, read)
        except OSError as exc:
            # No room / no permission on the staging disk. Say so and drain:
            # the sender is mid-body and can only read this reply if we eat the
            # rest (a close here aborts its socket, which the page shows as
            # "Failed to fetch"; see spec §47).
            self._reply({"error": f"staging failed: {exc}"}, 507)
            self._drain(remaining)
            return
        if out.get("ok") and remaining != 0:
            self.hub.transfers.discard(str(out.get("id")))
            self._reply({"error": "truncated upload"}, 400)
            return
        self._reply(out, 400 if "error" in out else 200)

    def _drain(self, length: int) -> None:
        """Eat a body we have already answered, so the writer gets that answer."""
        left = length
        try:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            self.close_connection = True  # the sender gave up first (see above)

    def _transfer_progress(self, params: dict) -> None:
        """Delivery progress for the sender's page (§49): how many bytes the hub
        has handed to the receiver so far, and the total it promised. Only the
        two ends of this transfer may ask."""
        host = (params.get("host") or [""])[0]
        tid = (params.get("id") or [""])[0]
        out = self.hub.transfers.progress_for(tid, host)
        if out is None:
            self._reply({"error": "not found"}, 404)
            return
        self._reply({"ok": True, **out})

    def _transfer_download(self, params: dict) -> None:
        host = (params.get("host") or [""])[0]
        tid = (params.get("id") or [""])[0]
        found = self.hub.transfers.fetchable(tid, host)
        if found is None:
            self._reply({"error": "not found"}, 404)
            return
        rec, path = found
        try:
            with path.open("rb") as fh:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(rec["size"]))
                # Headers are latin-1 only: RFC 5987 the unicode name, and
                # keep an ASCII fallback (拾荒集.zip crashed the whole reply).
                quoted = quote(str(rec["name"]), safe="")
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}",
                )
                self.end_headers()
                # Counted, not just copied: this loop is the only place that
                # knows how far the delivery has got (§49), and the sender's
                # modal reads that count to show a live second step. Reported a
                # megabyte at a time — a page polls it once a second.
                sent = 0
                reported = 0
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if sent - reported >= 1024 * 1024:
                        reported = sent
                        self.hub.transfers.moved(tid, sent)
                self.hub.transfers.moved(tid, sent)
        except OSError:
            pass
