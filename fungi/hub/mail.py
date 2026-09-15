"""Per-host text mailboxes on the hub: data/mail/<host>.jsonl.

amail is the text sibling of send_file: an agent (or the courier-off direct
path) delivers an envelope of type "mail"; the hub consumes it here instead of
buffering it on the relay — mail is server-authoritative data, so it survives
the receiver being offline and never wakes their agent. The WebUI reads and
marks-read over the same token-authenticated API as everything else.

Append-only jsonl with a per-box cap: the oldest messages roll off. One lock
per box; every mutation rewrites the file (boxes are small by construction).
"""

import json
import threading
import time
from pathlib import Path

MAX_BOX = 500  # messages kept per host; older roll off
MAX_FIELDS = 2000  # chars per subject/body field


def _clip(text, limit: int = MAX_FIELDS) -> str:
    return str(text or "")[:limit]


class Mailbox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def _path(self, host: str) -> Path:
        return self.root / f"{host}.jsonl"

    def _load(self, host: str) -> list[dict]:
        path = self._path(host)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _save(self, host: str, messages: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path(host).with_suffix(".tmp")
        tmp.write_text(
            "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages),
            encoding="utf-8",
        )
        tmp.replace(self._path(host))

    def deliver(
        self,
        host: str,
        sender: str,
        subject: str,
        body: str,
        peer: str | None = None,
        mine: bool = False,
        read: bool = False,
    ) -> dict:
        """Append one message; returns its id. Oldest rolls off past MAX_BOX.

        ``peer`` is the counterpart host (conversation key), ``mine`` marks
        the sender's own copy, ``read`` pre-marks a copy as read."""
        if not host:
            return {"error": "missing recipient host"}
        with self._lock_for(host):
            messages = self._load(host)
            rec = {
                "id": f"m{int(time.time() * 1000):x}-{len(messages)}",
                "from": _clip(sender, 200),
                "peer": _clip(peer or sender.split(":")[0], 200),
                "subject": _clip(subject),
                "body": _clip(body),
                "ts": time.time(),
                "read": bool(read),
                "mine": bool(mine),
            }
            messages.append(rec)
            del messages[:-MAX_BOX]
            self._save(host, messages)
        return {"ok": True, "id": rec["id"]}

    def deliver_pair(self, sender_addr: str, recipient_host: str, subject: str, body: str) -> dict:
        """Deliver one mail into both ends of a conversation: the recipient's
        box (unread) and the sender's own box (pre-read, marked mine), so
        both WebUIs can render the same thread. Returns the recipient id."""
        sender_host = sender_addr.split(":")[0]
        out = self.deliver(
            recipient_host, sender_addr, subject, body, peer=sender_host, mine=False, read=False
        )
        if out.get("error"):
            return out
        self.deliver(
            sender_host, sender_addr, subject, body, peer=recipient_host, mine=True, read=True
        )
        return out

    def list(self, host: str) -> dict:
        with self._lock_for(host):
            messages = self._load(host)
        unread = sum(1 for m in messages if not m.get("read"))
        return {"host": host, "mails": messages, "unread": unread}

    def mark_read(self, host: str, mail_id: str) -> dict:
        with self._lock_for(host):
            messages = self._load(host)
            hit = False
            for m in messages:
                if m.get("id") == mail_id:
                    m["read"] = True
                    hit = True
            if hit:
                self._save(host, messages)
        return {"ok": hit}
