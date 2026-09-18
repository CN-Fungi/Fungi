"""HTTP client for the hub API: room ops + fs + sessions + transfers."""

import http.client
import json
import select
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import runlog
from ..protocol import Envelope, ProtocolError, deserialize

POLL_CAP = 25.0
# How long the first peek waits for a refusal: the hub decides from the
# declared size, so its 413 lands within one RTT — never push a whole file into
# a drain just to read it (measured: 16 MiB sent for a 2 MiB cap without this).
REFUSAL_WAIT_S = 0.05


def _answered(conn: http.client.HTTPConnection, wait: float = 0.0) -> bool:
    """True when the hub has already replied — in practice, a refusal.

    The hub refuses an over-cap upload from the declared size, before reading
    the body. A client that keeps pushing bytes never gets to read that reply:
    the send dies on a closed socket (WinError 10053), so the page says "Failed
    to fetch" instead of "file too large". Peeking is what turns the refusal
    back into a sentence.
    """
    sock = getattr(conn, "sock", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], wait)
    except (OSError, ValueError):
        return False
    return bool(readable)


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
                out = json.loads(resp.read())
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

    def download_transfer(self, transfer_id: str, dest) -> None:
        """Stream a staged transfer to a local file path."""
        url = f"{self.base}/api/transfer?id={transfer_id}&host={self.host}&token={self.token}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as resp, Path(dest).open("wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)

    def discard_transfer(self, transfer_id: str) -> dict:
        """Receiver-side: drop the hub's staged copy after a delivery."""
        return self._request("DELETE", "/api/transfer", {"id": transfer_id, "host": self.host})

    def upload_transfer(self, path: str, name: str, to_host: str, progress=None) -> dict:
        """Stream a local file's raw bytes to the hub staging area.

        `progress(sent, total)` rides along per chunk: the send-file modal in
        the WebUI renders it (room.py transfer jobs), and nothing else needs to
        know how the bytes travelled.
        """
        src = Path(path)
        u = urllib.parse.urlparse(self.base)
        q = urllib.parse.urlencode(
            {"token": self.token, "host": self.host, "to": to_host, "name": name}
        )
        total = src.stat().st_size
        sent = 0
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=600)
        try:
            with src.open("rb") as fh:
                conn.putrequest("POST", f"/api/transfer/upload?{q}")
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(total))
                conn.endheaders()
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
            return {"error": f"upload failed: HTTP {resp.status}"}
        if resp.status != 200 and "error" not in out:
            out = {"error": f"upload failed: HTTP {resp.status}"}
        return out
