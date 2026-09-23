"""Hub HTTP API: join/leave/heartbeat/send/poll + fs + sessions + transfers.

Token auth on every endpoint. Transfers are store-and-forward: file bytes are
copied server-side out of the data/ store (never through an envelope), the
receiver pulls them via GET /api/transfer. The comm log mirrors clone-to-clone
traffic for the WebUI read-only conversation view.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import runlog
from ..landing import HEAD_BYTES, Spans, head_digest
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
# Bytes per write on a download, and how often the delivery count is updated:
# 1 MiB is a page poll's worth of resolution (§49), and a multiple of the sizes
# a window is cut into (§50).
DOWNLOAD_CHUNK = 256 * 1024
PROGRESS_STEP = 1024 * 1024
# How long a staging nobody finished is worth disk (§62). How much of a file
# proves it is the same file lives in landing — one definition, imported above.
PARTIAL_TTL_S = 24 * 3600.0


class RangeNotSatisfiableError(Exception):
    """A Range that starts past the end of the file: answer 416, serve nothing."""


def range_window(value: str, size: int) -> tuple[int, int] | None:
    """The byte window a `Range` header asks for, as inclusive `(first, last)`.

    None means "no range to honour, send the whole thing": no header, or a form
    this server deliberately does not guess at — several ranges, a suffix range
    (`bytes=-500`), another unit. RFC 9110 lets a server ignore a Range it does
    not understand, and guessing is exactly how the wrong bytes get served.
    Raises RangeNotSatisfiableError for `bytes=a-` past the end of the file.

    Only one form is promised, because it is the whole of what the receiver
    sends (§50): `bytes=a-b` and `bytes=a-`, clamped to the file like the RFC
    says, so a window is never empty and never reaches past the last byte.
    """
    spec = (value or "").strip()
    if not spec.lower().startswith("bytes="):
        return None
    wanted = spec[6:].strip()
    if "," in wanted:
        return None
    first_raw, _, last_raw = wanted.partition("-")
    first_raw = first_raw.strip()
    if not first_raw.isdigit():
        return None
    first = int(first_raw)
    last_raw = last_raw.strip()
    if last_raw:
        if not last_raw.isdigit():
            return None
        last = int(last_raw)
        if last < first:
            return None  # not a byte range at all: ignore the header (RFC 9110)
    else:
        last = size - 1
    if size <= 0 or first >= size:
        raise RangeNotSatisfiableError
    return first, min(last, size - 1)


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


def _query_int(params: dict, key: str) -> int | None:
    """A non-negative integer out of a query string, or None when it is not one."""
    raw = (params.get(key) or [""])[0].strip()
    return int(raw) if raw.isdigit() else None


class Transfers:
    """In-memory registry of staged file transfers (metadata only on the wire).

    No size cap (spec §48): a staged file is streamed to disk in 256 KiB
    chunks, so the only limit is free space — and it fails as an OSError the
    callers report, never as a dropped connection.

    A staging that is not whole yet is kept as a *partial* one (§62): the bytes
    that did arrive stay on disk with the length they were promised, so a sender
    that comes back hands over the rest instead of the whole file again. Nothing
    partial is ever fetchable — the deliverable is only the finished file.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._records: dict[str, dict] = {}
        # Which bytes have reached the receiver, per transfer. Kept out of the
        # record because that dict is handed to callers and serialized into
        # replies, and a Spans is neither JSON nor anyone else's business.
        self._spans: dict[str, Spans] = {}
        self._guard = threading.Lock()

    def path_of(self, rec: dict) -> Path:
        """Where a staged transfer's bytes live."""
        return self.root / f"{rec['id']}__{rec['name']}"

    def stage_from(
        self,
        name: str,
        src_host: str,
        dst_host: str,
        read,
        expect: int | None = None,
        resume: str = "",
    ) -> dict:
        """Stage streamed bytes (read(n) -> b"" ends input) into the transfers dir.

        `expect` is the length the sender promised (`Content-Length` upstream): a
        body that ends before it is a partial staging, not a failure to clean up
        (§62). `resume` names a partial staging to append to — the sender asked
        for the rest of a file it had already started, and the offset it claims is
        checked by the caller before this runs.
        """
        clean = safe_name(name)
        with self._guard:
            found = self._records.get(str(resume)) if resume else None
        if found is not None and not self._resumable(found, clean, src_host, dst_host):
            found = None
        if found is not None:
            tid, target = found["id"], self.path_of(found)
            size = int(found["size"])
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            tid = new_id()
            target = self.root / f"{tid}__{clean}"
            size = 0
        try:
            with target.open("ab" if found is not None else "wb") as out:
                while True:
                    chunk = read(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    out.write(chunk)
        except BaseException:
            # What did arrive is kept: resuming is the point (§62). Only a fresh
            # staging that never got a byte is dropped, so a refused upload
            # leaves nothing behind.
            if found is None and not target.stat().st_size:
                target.unlink(missing_ok=True)
            raise
        total = int(expect) if expect else size
        rec = {
            "id": tid,
            "name": clean,
            "size": size,
            "expect": total,
            "partial": size != total,
            "src": src_host,
            "dst": dst_host,
            "ts": time.time(),
        }
        with self._guard:
            self._records[tid] = rec
            self._spans.setdefault(tid, Spans())
        return rec

    @staticmethod
    def _resumable(rec: dict, name: str, src_host: str, dst_host: str) -> bool:
        """Is this staging the one the sender is asking to continue?"""
        return (
            bool(rec.get("partial"))
            and rec.get("name") == name
            and rec.get("src") == src_host
            and rec.get("dst") == dst_host
        )

    def pending(
        self, name: str, src_host: str, dst_host: str, size: int, head: str
    ) -> dict | None:
        """The staging this same file is already on its way into, if there is one (§62).

        Two things are being asked here, and they are the same question: *has this
        file already been staged, and is it still there?* — a partial one so the
        sender can finish it, a whole one so the sender does not send it again at
        all (the receiver may simply not have taken it yet).

        Identity is (name, source, destination, length) plus the digest of the
        first 64 KiB: the file's name and length alone would let a file that
        changed size-neutrally continue somebody else's bytes, and there is no
        protocol-level checksum to appeal to (§49.4). A staging too short to carry
        that much is never adopted — its head cannot prove anything.
        """
        clean = safe_name(name)
        wanted = int(size)
        with self._guard:
            candidates = [
                rec
                for rec in self._records.values()
                if rec.get("name") == clean
                and rec.get("src") == src_host
                and rec.get("dst") == dst_host
                and int(rec.get("expect") or rec.get("size") or 0) == wanted
            ]
        for rec in sorted(candidates, key=lambda item: str(item.get("id"))):
            target = self.path_of(rec)
            if not target.is_file() or int(rec.get("size") or 0) < min(wanted, HEAD_BYTES):
                continue
            if head and head_digest(target) == head:
                return dict(rec)
        return None

    def state(self, transfer_id: str, host: str) -> dict | None:
        """What the hub still holds of this transfer, for either of its two ends."""
        with self._guard:
            rec = self._records.get(str(transfer_id))
        if rec is None or host not in (rec.get("src"), rec.get("dst")):
            return None
        return dict(rec)

    def sweep_partials(self, ttl: float = PARTIAL_TTL_S) -> None:
        """Drop partial stagings nobody came back for, and their bytes with them."""
        cutoff = time.time() - ttl
        with self._guard:
            stale = [
                rec
                for rec in self._records.values()
                if rec.get("partial") and float(rec.get("ts") or 0) < cutoff
            ]
            for rec in stale:
                self._records.pop(rec["id"], None)
                self._spans.pop(rec["id"], None)
        for rec in stale:
            self.path_of(rec).unlink(missing_ok=True)

    def stage(self, source: Path, name: str, src_host: str, dst_host: str) -> dict:
        """Copy a store file into the transfers dir; returns its record."""
        with source.open("rb") as fh:
            return self.stage_from(name, src_host, dst_host, fh.read)

    def discard(self, transfer_id: str) -> None:
        """Drop a staged transfer (registry + bytes); for aborted uploads."""
        with self._guard:
            rec = self._records.pop(str(transfer_id), None)
            self._spans.pop(str(transfer_id), None)
        if rec is not None:
            self.path_of(rec).unlink(missing_ok=True)

    def discard_for(self, transfer_id: str, host: str) -> bool:
        """Receiver-authorized discard: only the designated dst host may drop
        a staged transfer (used after a successful delivery)."""
        with self._guard:
            rec = self._records.get(str(transfer_id))
            if rec is None or rec.get("dst") != host:
                return False
            del self._records[str(transfer_id)]
            self._spans.pop(str(transfer_id), None)
        self.path_of(rec).unlink(missing_ok=True)
        return True

    def fetchable(self, transfer_id: str, host: str) -> tuple[dict, Path] | None:
        """Record + file path, only for the designated receiver host.

        A partial staging is not one of these: the sender has not finished putting
        the file there, so there is nothing to fetch yet (§62).
        """
        with self._guard:
            rec = self._records.get(str(transfer_id))
        if rec is None or rec["dst"] != host or rec.get("partial"):
            return None
        path = self.path_of(rec)
        if not path.is_file():
            return None
        return rec, path

    def deliver_window(self, transfer_id: str, start: int, end: int) -> None:
        """Bytes `[start, end)` have been handed to the receiver.

        Ranges, not a running total (§50): a delivery split into windows reports
        each one as it runs — out of order, twice when a window is retried from
        where it stopped — and the count the sender's page reads must never walk
        backwards. A union also puts a whole-file delivery (one window, `[0,
        size)`) and a ranged one on the same scale, so `sent` means the same
        thing either way. The lock is `Spans`' own: windows report from their
        own request threads.
        """
        with self._guard:
            spans = self._spans.get(str(transfer_id))
        if spans is not None:
            spans.add(start, end)

    def progress_for(self, transfer_id: str, host: str) -> dict | None:
        """Delivery progress, for the two hosts allowed to know: sender and
        designated receiver. Any other caller gets nothing (the room token is
        the outer door; this is the same door one step in)."""
        with self._guard:
            rec = self._records.get(str(transfer_id))
            if rec is None or host not in (rec.get("src"), rec.get("dst")):
                return None
            spans = self._spans.get(str(transfer_id))
            # The length the sender promised, not what has arrived: a staging that
            # is still filling up must not make the bar's total move (§62).
            total = int(rec.get("expect") or rec["size"])
        sent = spans.bytes if spans is not None else 0
        # A window may report its whole range and then fail; never claim more
        # than the file has.
        return {"sent": min(total, sent), "total": total}


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
            self.transfers.sweep_partials()

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

    def upload_transfer(
        self,
        host: str,
        to_host: str,
        name: str,
        read,
        expect: int | None = None,
        resume: str = "",
    ) -> dict:
        """Stage bytes streamed from a host's local disk (raw upload path).

        Unlike create_transfer (store-side copy), the bytes come straight off the
        sender's machine — this is how the user-facing clone sends a real
        local file. An unwritable staging disk raises OSError; callers report
        it (nobody refuses on size any more, spec §48). `expect` is the length the
        sender promised, which is what tells a body that ended early from a whole
        file (§62); `resume` continues a staging it already started.
        """
        if not self.roster.known(host) or not self.roster.known(to_host):
            return {"error": "unknown host"}
        if to_host == host:
            return {"error": "cannot transfer to yourself"}
        rec = self.transfers.stage_from(
            name or "file", host, to_host, read, expect=expect, resume=resume
        )
        if rec["partial"]:
            # The sender's body ended before the length it promised — its
            # connection died. What arrived stays staged so it can come back for
            # the rest, but this is not a file the receiver may be told about
            # (§62): an announcement here is how a half file gets delivered.
            return {
                "error": f"truncated upload: {rec['size']} of {rec['expect']} bytes",
                "id": rec["id"],
                "received": rec["size"],
                "total": rec["expect"],
                "partial": True,
            }
        return {"ok": True, **rec}

    def pending_transfer(
        self, host: str, to_host: str, name: str, size: int, head: str
    ) -> dict:
        """What this hub already holds of the file the sender is about to send (§62).

        Answered for the sender only: it is the sender's own file, and the staging
        is keyed by (source, destination, name, length, head).
        """
        if not self.roster.known(host) or not self.roster.known(to_host):
            return {"ok": False, "reason": "unknown host"}
        if to_host == host:
            return {"ok": False, "reason": "cannot transfer to yourself"}
        rec = self.transfers.pending(name or "file", host, to_host, size, head)
        if rec is None:
            return {"ok": False, "reason": "nothing staged for this file"}
        return {"ok": True, "id": rec["id"], "received": int(rec["size"]),
                "partial": bool(rec.get("partial")), "name": rec["name"]}

    def transfer_state(self, transfer_id: str, host: str) -> dict:
        """Still staged, and how far it got — for either end of the transfer (§62)."""
        rec = self.transfers.state(transfer_id, host)
        if rec is None:
            return {"ok": False, "reason": "not found"}
        return {"ok": True, "id": rec["id"], "name": rec["name"], "size": int(rec["size"]),
                "total": int(rec.get("expect") or rec["size"]),
                "partial": bool(rec.get("partial"))}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: Hub = None  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:
        pass  # quiet

    # ── plumbing ──

    def _reply(self, obj: dict, code: int = 200, headers: dict | None = None) -> None:
        data = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
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
        elif url.path == "/api/transfer/pending":
            self._transfer_pending(params)
        elif url.path == "/api/transfer/state":
            self._transfer_state(params)
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
        """Raw-bytes upload: /api/transfer/upload?token&host&to&name[&id&offset&total].

        `total` is the whole file's length and `offset` where this body begins;
        with them a sender that lost its connection hands over the last stretch
        instead of the whole file again (§62). Without them the body is the file.
        """
        params = parse_qs(url.query)
        token = (params.get("token") or [""])[0]
        if token != self.hub.token:
            self._bad_token()
            return
        host = (params.get("host") or [""])[0]
        to_host = (params.get("to") or [""])[0]
        name = (params.get("name") or [""])[0] or "file"
        total = _query_int(params, "total")
        offset = _query_int(params, "offset") or 0
        resume = (params.get("id") or [""])[0]
        remaining = int(self.headers.get("Content-Length") or 0)
        if remaining <= 0:
            self._reply({"error": "empty upload"}, 400)
            return
        promised = total if total is not None else offset + remaining
        if resume:
            rec = self.hub.transfers.state(resume, host)
            if (
                rec is None
                or not rec.get("partial")
                or int(rec["size"]) != offset
                or int(rec["expect"]) != promised
            ):
                # The staging moved on (swept, finished by another attempt, or a
                # different file): this body holds a tail with nothing to attach
                # it to, so it cannot be written anywhere. Read it out and answer.
                self._reply(
                    {"error": "stale resume: send the file from the start", "restart": True}, 409
                )
                self._drain(remaining)
                return

        def read(n: int) -> bytes:
            nonlocal remaining
            if remaining <= 0:
                return b""
            chunk = self.rfile.read(min(n, remaining))
            remaining -= len(chunk)
            return chunk

        try:
            out = self.hub.upload_transfer(
                host, to_host, name, read, expect=promised, resume=resume
            )
        except OSError as exc:
            # No room / no permission on the staging disk. Say so and drain:
            # the sender is mid-body and can only read this reply if we eat the
            # rest (a close here aborts its socket, which the page shows as
            # "Failed to fetch"; see spec §47).
            self._reply({"error": f"staging failed: {exc}"}, 507)
            self._drain(remaining)
            return
        if out.get("partial"):
            # The body ended before the length it promised: the connection died.
            # What arrived stays staged — the sender comes back for the rest
            # (§62) — and the answer says where it stopped.
            self._reply({**out, "error": "truncated upload"}, 400)
            return
        if "error" in out:
            # Some guards answer before a byte is read (unknown host, a send to
            # itself): replying and closing with the body still in the socket is
            # what Windows turns into RST, so the sender sees a dead connection
            # instead of this reason — the same rule the OSError branch above
            # follows (spec §47).
            self._reply(out, 400)
            self._drain(remaining)
            return
        self._reply(out, 200)

    def _transfer_pending(self, params: dict) -> None:
        """Has this file already been staged here? The sender asks first (§62)."""
        self._reply(
            self.hub.pending_transfer(
                (params.get("host") or [""])[0],
                (params.get("to") or [""])[0],
                (params.get("name") or [""])[0],
                _query_int(params, "size") or 0,
                (params.get("head") or [""])[0],
            )
        )

    def _transfer_state(self, params: dict) -> None:
        """What the hub still holds of a transfer, for either of its two ends."""
        self._reply(
            self.hub.transfer_state(
                (params.get("id") or [""])[0], (params.get("host") or [""])[0]
            )
        )

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
        size = int(rec["size"])
        try:
            window = range_window(self.headers.get("Range") or "", size)
        except RangeNotSatisfiableError:
            # Past the last byte: the receiver has a stale idea of the file, and
            # the size is the correction. No bytes are served (§50).
            self._reply(
                {"error": "range not satisfiable"}, 416, {"Content-Range": f"bytes */{size}"}
            )
            return
        first, last = window if window is not None else (0, size - 1)
        length = last - first + 1
        try:
            with path.open("rb") as fh:
                fh.seek(first)
                self.send_response(206 if window is not None else 200)
                # A hub that answers ranges says so on every answer, including
                # the single-stream one a receiver gets when it asks for none.
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                if window is not None:
                    self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
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
                # megabyte at a time — a page polls it once a second — as the
                # range that is now on the wire, so several windows at once add
                # up instead of overwriting each other (§50).
                sent = 0
                reported = 0
                while sent < length:
                    chunk = fh.read(min(DOWNLOAD_CHUNK, length - sent))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if sent - reported >= PROGRESS_STEP:
                        reported = sent
                        self.hub.transfers.deliver_window(tid, first, first + sent)
                self.hub.transfers.deliver_window(tid, first, first + sent)
        except OSError:
            pass
