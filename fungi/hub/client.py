"""HTTP client for the hub API: room ops + fs + sessions + transfers."""

import http.client
import json
import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import runlog
from ..landing import Landing, head_digest
from ..protocol import Envelope, ProtocolError, deserialize

POLL_CAP = 25.0
# How long the first peek waits for an early reply: the hub answers before the
# body when it cannot stage the file (no room, no permission), so its status
# lands within one RTT — never push a whole file into a drain just to read it
# (measured: 16 MiB sent for a 2 MiB cap back when sizes were capped, 0 B now).
REFUSAL_WAIT_S = 0.05

# ── how a delivery is fetched (§50) ──
#
# One TCP connection does not fill a link on its own: measured on his 2.4 GHz
# WiFi, 5.05 MB/s alone and 6.8 MB/s with two running. A window is also the unit
# of resume — one that dies is re-requested from the byte it stopped at instead
# of taking a 1 GB file from zero — which is the half of this that survives a
# link that is merely *fast enough*.
WINDOWS = 4
MIN_WINDOW_BYTES = 4 * 1024 * 1024  # a window smaller than this is not worth a connection
WINDOW_ATTEMPTS = 3
WINDOW_RETRY_WAIT_S = 0.5
STREAM_CHUNK = 256 * 1024  # per read/write; the hub stages at the same size


def _windows(size: int, streams: int) -> list[tuple[int, int]]:
    """`[0, size)` cut into at most `streams` ranges, as `(start, end)` pairs."""
    count = max(1, min(int(streams), size // MIN_WINDOW_BYTES))
    if count <= 1:
        return [(0, size)]
    step = size // count
    out: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        end = size if index == count - 1 else start + step
        out.append((start, end))
        start = end
    return out


def _tag(transfer_id: str) -> str:
    """The staged id's short form, which goes into the part file's name (§49)."""
    return str(transfer_id)[:8]


def _answered(conn: http.client.HTTPConnection, wait: float = 0.0) -> bool:
    """True when the hub has already replied — in practice, a refusal.

    A hub that answers before the body ends (see _Handler._transfer_upload) is
    one a client can otherwise never hear: it keeps pushing bytes until the
    send dies on the closed socket (WinError 10053), so the page says "Failed
    to fetch" instead of naming the reason. Peeking turns it back into a
    sentence.
    """
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], wait)
    except (OSError, ValueError):
        return False
    return bool(readable)


def _read_body(resp, want: int) -> tuple[bytes, bool]:
    """One read off a response body: `(bytes, ended_early)`.

    A connection that dies before its `Content-Length` shows up as an
    `IncompleteRead` carrying the bytes that did arrive. They are real bytes off
    the wire, so they are handed back rather than thrown away — the delivery
    that dropped only has to fetch what never came (§62).
    """
    try:
        return resp.read(want), False
    except http.client.IncompleteRead as exc:
        return bytes(exc.partial or b""), True


class HubError(Exception):
    pass


class HubClient:
    def __init__(self, base_url: str, token: str, host: str, display: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.host = host
        self.display = display
        self._trouble = False  # hub stopped answering: the log's throttling edge

    # ── plumbing ──

    def _request(self, method: str, path: str, obj: dict | None = None) -> dict:
        url = self.base + path
        data = None
        if obj is not None:
            obj = {"token": self.token, **obj}
            data = json.dumps(obj).encode("utf-8")
        elif method == "GET":
            sep = "&" if "?" in path else "?"
            url = f"{url}{sep}token={self.token}"
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("error", body)
            except json.JSONDecodeError:
                detail = body
            self._note_trouble(f"{path}: HTTP {exc.code}: {detail}")
            raise HubError(f"{path}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._note_trouble(f"{path}: {exc}")
            raise HubError(f"{path}: {exc}") from exc
        try:
            out = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            # A reply that is not JSON is a broken hub, whichever route it came
            # back on — including the one that says how big a staged file is, so
            # it is reported as such instead of escaping as a bare ValueError
            # (a delivery would have died on it, never reaching the fallback).
            self._note_trouble(f"{path}: not JSON ({exc})")
            raise HubError(f"{path}: not JSON ({exc})") from exc
        if self._trouble:
            self._trouble = False
            runlog.forget(f"hub:{self.base}")
            runlog.note("hub %s is answering again", self.base)
        return out

    def _note_trouble(self, why: str) -> None:
        """The hub stopped answering. A room polls every second, so the log gets
        one line a minute — and one line when it comes back, which is how you
        tell a flapping link from a room that never came up."""
        self._trouble = True
        runlog.warn_once(f"hub:{self.base}", "hub %s request failed: %s", self.base, why)

    # ── room ops ──

    def join(self) -> dict:
        return self._request("POST", "/api/join", {"name": self.host, "display": self.display})

    def leave(self) -> dict:
        return self._request("POST", "/api/leave", {"name": self.host})

    def heartbeat(self) -> dict:
        return self._request("POST", "/api/heartbeat", {"name": self.host})

    def send(self, env: Envelope) -> dict:
        return self._request("POST", "/api/send", {"envelope": env.serialize()})

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        timeout = min(timeout, POLL_CAP)
        out = self._request("GET", f"/api/poll?host={self.host}&after={after}&timeout={timeout}")
        messages = []
        for raw in out.get("messages", []):
            try:
                messages.append(deserialize(raw))
            except ProtocolError:
                continue
        return messages, int(out.get("cursor", after))

    # ── fs ops (path guard lives on the server) ──

    def fs(
        self,
        op: str,
        path: str,
        *,
        content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        pattern: str | None = None,
        consent_id: str | None = None,
    ) -> dict:
        body: dict = {"host": self.host, "path": path, "consent_id": consent_id}
        if content is not None:
            body["content"] = content
        if old_string is not None:
            body["old_string"] = old_string
        if new_string is not None:
            body["new_string"] = new_string
        if pattern is not None:
            body["pattern"] = pattern
        return self._request("POST", f"/api/fs/{op}", body)

    # ── sessions ──

    def list_sessions(self) -> list[dict]:
        return self._request("GET", "/api/sessions").get("sessions", [])

    def load_session(self, session_id: str) -> dict:
        return self._request("GET", f"/api/session?id={session_id}")

    def save_session(self, payload: dict) -> dict:
        return self._request("POST", "/api/save", payload)

    def delete_session(self, session_id: str) -> dict:
        return self._request("POST", "/api/session/delete", {"id": session_id})

    # ── peers / comm log ──

    def peers(self) -> list[dict]:
        """Peers as display records ``[{"name", "display"}]``."""
        return self._request("GET", f"/api/peers?host={self.host}").get("peers", [])

    def comm_log(self, other: str) -> list[dict]:
        out = self._request("GET", f"/api/comm-log?host={self.host}&with={other}")
        return out.get("messages", [])

    # ── mail ──

    def mail(self) -> dict:
        return self._request("GET", f"/api/mail?host={self.host}")

    def mail_read(self, mail_id: str) -> dict:
        return self._request("POST", "/api/mail/read", {"host": self.host, "id": mail_id})

    # ── file transfers (bytes live on the hub; only metadata is exchanged) ──

    def create_transfer(self, path: str, name: str, to_host: str) -> dict:
        return self._request(
            "POST", "/api/transfer", {"host": self.host, "path": path, "name": name, "to": to_host}
        )

    def download_transfer(self, transfer_id: str, dest, streams: int = WINDOWS) -> None:
        """Stream a staged transfer to a local file path (§49, §50).

        Lands atomically: bytes go to one `<dest>.<tag>.part`, and it is renamed
        onto the real name only once the delivery is whole — the length the hub
        announced *and* every byte covered — so a download that dies mid-stream
        leaves nothing behind (§49).

        The delivery travels as several byte ranges at once when the hub offers
        them (§50): one connection does not fill a link, and a range is what
        makes a window that dies cheap to resume. A hub that ignores `Range`
        answers the first request with 200 and the whole file, which is exactly
        the single stream this used to be — old and new versions still transfer.
        """
        size = self._staged_size(transfer_id) if streams > 1 else None
        if size:
            self._download_windows(transfer_id, dest, size, _windows(size, streams))
        else:
            self._download_stream(transfer_id, dest)

    def _staged_size(self, transfer_id: str) -> int | None:
        """How big the staged file is, so the delivery can be cut into windows.

        The receiver may ask (it is the dst of this transfer). A hub that cannot
        answer — one from before this route existed — simply means one stream.
        """
        try:
            out = self.transfer_progress(transfer_id)
        except HubError:
            return None
        total = out.get("total")
        return int(total) if isinstance(total, int) and total > 0 else None

    def _download_stream(self, transfer_id: str, dest) -> None:
        """One GET: the shape every hub version understands, continued if we can (§62).

        An earlier attempt at this same transfer left a part behind, so the request
        starts at the byte that part really reached — `Range: bytes=<at>-` — and a
        hub that does not do ranges (a 200 where a 206 was asked for) means starting
        the part over instead of writing its body at the wrong offset.
        """
        with Landing(Path(dest), None, tag=_tag(transfer_id), transfer=transfer_id) as land:
            start = land.spans.end_of_run(0)
            with urllib.request.urlopen(
                self._stream_request(transfer_id, start), timeout=120
            ) as resp:
                ranged = self._ranged_from(resp, start)
                if start and not ranged:
                    land.restart()
                    start = 0
                expect = self._announced_size(resp, start if ranged else 0)
                land.expect = expect
                self._pump(resp, land, start, expect)
                land.commit()

    def _stream_request(self, transfer_id: str, start: int):
        headers = {"Range": f"bytes={start}-"} if start else {}
        return urllib.request.Request(self._transfer_url(transfer_id), headers=headers)

    @staticmethod
    def _ranged_from(resp, start: int) -> bool:
        """Did this response begin exactly at `start` as a 206 (a 200 is the whole file)?"""
        if not start:
            return getattr(resp, "status", 200) == 206
        content_range = (resp.headers.get("Content-Range") or "").strip()
        return getattr(resp, "status", 200) == 206 and content_range.startswith(f"bytes {start}-")

    @staticmethod
    def _announced_size(resp, fallback_start: int) -> int | None:
        """How big the whole staged file is: `Content-Range`'s total, else the length.

        For a 206 the `Content-Length` is only what this request still owes, so the
        total has to come out of `Content-Range` — a part committed against the
        remaining length would be rejected as truncated on a resumed delivery.
        """
        content_range = (resp.headers.get("Content-Range") or "").strip()
        if content_range.startswith("bytes ") and "/" in content_range:
            total = content_range.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                return int(total)
        announced = (resp.headers.get("Content-Length") or "").strip()
        if announced.isdigit():
            return int(announced) + fallback_start
        return None

    def _download_windows(self, transfer_id: str, dest, size: int, windows) -> None:
        """Fetch a delivery as ranged GETs at once, one thread per window (§50).

        The first request is window 0's own range, opened here and closed again
        without reading a byte: it answers "does this hub do ranges?" for free.
        Everything else about the delivery is the same as a single stream — one
        part file, one commit — because the windows write disjoint stretches of
        it and the commit checks that all of them are there.

        A delivery that an earlier attempt left half done hands its windows back
        here with the stretches they already have (§62): a window that is whole
        is not requested at all, and one that is partly there starts where it
        stopped. The hub can only do that if it still offers ranges — a hub that
        answers 200 to the probe is asked for the file from the top instead.
        """
        stop = threading.Event()
        errors: list[BaseException] = []
        with Landing(Path(dest), size, tag=_tag(transfer_id), transfer=transfer_id) as land:
            first, last = windows[0]
            probe, ranged = self._open_window(transfer_id, first, last - 1)
            with probe:
                if not ranged:
                    if land.spans.bytes:
                        # Mid-file the whole-file answer starts at byte zero, so
                        # what is on disk cannot be added to: start the part over.
                        land.restart()
                    # No ranges on this hub: the answer is the whole file from
                    # byte zero, so it plays the part of the single stream.
                    self._pump(probe, land, 0, size)
                    land.commit()
                    return
            workers = [
                threading.Thread(
                    target=self._window,
                    args=(transfer_id, land, window, stop, errors),
                    name=f"fungi-window-{index}",
                    daemon=True,
                )
                for index, window in enumerate(windows[1:], start=1)
            ]
            for worker in workers:
                worker.start()
            self._window(transfer_id, land, windows[0], stop, errors)
            for worker in workers:
                worker.join()
            if errors:
                raise HubError(f"delivery failed: {errors[0]}")
            land.commit()

    def _window(self, transfer_id: str, land, window: tuple[int, int], stop, errors) -> None:
        """One window, retried in place: the two halves of resuming (§50, §62).

        A range that dies is re-requested from the byte it stopped at, so a drop
        costs a window's worth of re-download instead of the whole file. And a
        window the part already holds in full is not requested at all: that is
        what makes a delivery which was interrupted yesterday cost only what it
        had not fetched. Out of attempts it stops the other windows and the
        delivery fails — a part file with no future does not deserve more of the
        link.
        """
        start, end = window
        at = land.spans.end_of_run(start)  # what an earlier attempt already has (§62)
        if at >= end:
            return
        failure: BaseException | None = None
        for attempt in range(WINDOW_ATTEMPTS):
            if stop.is_set():
                return
            if attempt:
                time.sleep(WINDOW_RETRY_WAIT_S * attempt)
            failure = None
            try:
                self._fetch_window(transfer_id, land, at, end, stop)
            except (OSError, http.client.HTTPException, HubError) as exc:
                failure = exc
            # Where the disk actually got to, not where the request started: a
            # link that dies mid-body costs the stretch it carried, not the window.
            at = land.spans.end_of_run(at)
            if at >= end:
                return
            failure = failure or HubError(f"the hub ended the range at {at} of {end}")
        errors.append(failure)
        stop.set()

    def _fetch_window(self, transfer_id: str, land, start: int, end: int, stop) -> None:
        """One ranged GET into the landing, `[start, end)` of the file."""
        resp, ranged = self._open_window(transfer_id, start, end - 1)
        with resp:
            if not ranged:
                # Mid-file the whole-file answer is useless: it starts at byte
                # zero, and this window is not there.
                raise HubError(f"the hub ignored the range from {start}")
            self._pump(resp, land, start, end, stop)

    def _open_window(
        self, transfer_id: str, first: int, last: int
    ) -> tuple[http.client.HTTPResponse, bool]:
        """Open one ranged GET; returns `(response, ranged)`.

        False means the hub answered 200 with the whole file — a hub older than
        §50. A 206 whose `Content-Range` does not start where we asked counts as
        False too: bytes would land at the wrong offset, which is worse than
        fetching the file again.
        """
        req = urllib.request.Request(
            self._transfer_url(transfer_id), headers={"Range": f"bytes={first}-{last}"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        content_range = (resp.headers.get("Content-Range") or "").strip()
        ranged = getattr(resp, "status", 200) == 206 and content_range.startswith(f"bytes {first}-")
        return resp, ranged

    @staticmethod
    def _pump(resp, land, start: int, end: int | None, stop=None) -> None:
        """Copy the response body into the landing from `start`.

        `end` is exclusive — None (an unknown length) reads to the end of the
        body. Returning early means the hub ended the body early or `stop` was
        raised; for a window that is the signal to resume, and the caller reads
        the resume point off the landing (what is really on disk) rather than
        off this call.

        A body that stops short of its `Content-Length` is written down too: the
        drop arrives as an `IncompleteRead` that carries the bytes already off
        the socket, and throwing those away would mean fetching a stretch the
        link has already paid for (§50, §62).
        """
        at = start
        with land.writer(start) as fh:
            while end is None or at < end:
                if stop is not None and stop.is_set():
                    break
                want = STREAM_CHUNK if end is None else min(STREAM_CHUNK, end - at)
                chunk, ended = _read_body(resp, want)
                if chunk:
                    began = at
                    fh.write(chunk)
                    at += len(chunk)
                    land.written(began, at)
                if ended or not chunk:
                    break

    def _transfer_url(self, transfer_id: str) -> str:
        return f"{self.base}/api/transfer?id={transfer_id}&host={self.host}&token={self.token}"

    def discard_transfer(self, transfer_id: str) -> dict:
        """Receiver-side: drop the hub's staged copy after a delivery."""
        return self._request("DELETE", "/api/transfer", {"id": transfer_id, "host": self.host})

    def transfer_progress(self, transfer_id: str) -> dict:
        """How far the hub has carried a staged transfer to its receiver (§49).

        The sender's page polls the ROOM for its own job; the room asks the hub
        this, because the hub is the one moving the last leg.
        """
        return self._request("GET", f"/api/transfer/progress?host={self.host}&id={transfer_id}")

    def upload_transfer(self, path: str, name: str, to_host: str, progress=None) -> dict:
        """Stream a local file's raw bytes to the hub staging area.

        `progress(sent, total)` rides along per chunk: the send-file modal in
        the WebUI renders it (room.py transfer jobs), and nothing else needs to
        know how the bytes travelled.

        A file this hub is *already* holding is not sent over the wire again
        (§62). The sender asks first (`/api/transfer/pending`), and the answer
        covers both cases at once: a partial staging from an attempt that died is
        continued from where it stopped, and a whole one the receiver has simply
        not taken yet is reported as staged — sending it again would only rewrite
        the bytes it already has. Identity is (name, destination, length) plus the
        digest of the first 64 KiB, because there is no checksum on the wire to
        appeal to.
        """
        src = Path(path)
        total = src.stat().st_size
        head = head_digest(src) if total else ""
        known = self._staged_already(name, to_host, total, head)
        if known and not known.get("partial"):
            runlog.note("hub already holds %s whole — not sending it again", name)
            return {
                "ok": True,
                "id": known.get("id"),
                "name": known.get("name") or name,
                "size": total,
                "staged": True,
            }
        offset = int(known.get("received") or 0) if known else 0
        out = self._post_upload(
            src, name, to_host, total, offset, str(known.get("id") or ""), progress
        )
        if out.get("restart"):
            # The staging moved on between the question and the body (swept, or
            # finished by another attempt): this body is a tail, so it cannot be
            # attached anywhere. Once, from the top.
            runlog.note("the hub's staging of %s moved on; sending it from the start", name)
            out = self._post_upload(src, name, to_host, total, 0, "", progress)
        return out

    def _staged_already(self, name: str, to_host: str, size: int, head: str) -> dict:
        """The hub's answer about this file, or `{}` when it holds nothing of it.

        A hub older than §62 has no such route: 404 means one thing only here —
        send the whole file, which is what every version before this did.
        """
        if not size or not head:
            return {}
        query = urllib.parse.urlencode(
            {
                "token": self.token,
                "host": self.host,
                "to": to_host,
                "name": name,
                "size": size,
                "head": head,
            }
        )
        try:
            out = self._request("GET", f"/api/transfer/pending?{query}")
        except HubError:
            return {}
        return out if out.get("ok") else {}

    def _post_upload(
        self, src: Path, name: str, to_host: str, total: int, offset: int, resume: str, progress
    ) -> dict:
        """One upload body: from `offset` to the end, appended to `resume` or started fresh."""
        u = urllib.parse.urlparse(self.base)
        params = {
            "token": self.token,
            "host": self.host,
            "to": to_host,
            "name": name,
            "total": total,
        }
        if resume:
            params["id"] = resume
            params["offset"] = offset
        q = urllib.parse.urlencode(params)
        sent = offset
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=600)
        try:
            with src.open("rb") as fh:
                fh.seek(offset)
                conn.putrequest("POST", f"/api/transfer/upload?{q}")
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(total - offset))
                conn.endheaders()
                if progress is not None and offset:
                    progress(offset, total)
                refused = _answered(conn, REFUSAL_WAIT_S)
                while not refused:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        progress(sent, total)
                    refused = _answered(conn)  # the hub answered mid-body: stop
            resp = conn.getresponse()
            body = resp.read()
            status = resp.status
        except (OSError, http.client.HTTPException) as exc:
            # A hub that closed on us must not take this thread down: an
            # exception here leaves /comm-send unanswered and the page says
            # "Failed to fetch" (Chromium then re-sends the POST, restarting
            # the modal's bar).
            return {"error": f"upload failed: {exc}"}
        finally:
            conn.close()
        try:
            out = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return {"error": f"upload failed: HTTP {status}"}
        if status == 409 and out.get("restart"):
            return {"restart": True, "error": str(out.get("error") or "stale resume")}
        if status != 200 and "error" not in out:
            out = {"error": f"upload failed: HTTP {status}"}
        return out
