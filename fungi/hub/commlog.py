"""Comm conversation log: server-side mirror of clone-to-clone traffic.

The hub records every chat/task/result/transfer envelope it delivers into one
append-only JSONL file per host pair (both directions share the file, sorted
host names). Powers the WebUI read-only friend conversation view.
"""

import json
import threading
from pathlib import Path

from ..protocol import Envelope, parse_addr

RECORDED_TYPES = ("chat", "task", "result", "transfer")
_MAX_FIELD = 2000  # cap stored text per row (results can be huge)


def _clip(value) -> str:
    text = str(value or "")
    return text[:_MAX_FIELD] + ("…" if len(text) > _MAX_FIELD else "")


def pair_name(src: str, dst: str) -> str:
    """File stem for a host pair: sorted names joined, e.g. 'alpha__beta'."""
    a, _ra, _pa = parse_addr(src)
    b, _rb, _pb = parse_addr(dst)
    return "__".join(sorted((a, b)))


class CommLog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._guard = threading.Lock()

    def record(self, env: Envelope) -> None:
        """Mirror one delivered envelope; best effort — logging never breaks delivery."""
        if env.type not in RECORDED_TYPES:
            return
        row: dict = {
            "ts": env.ts,
            "src": env.src,
            "dst": env.dst,
            "kind": env.type,
        }
        if env.type == "chat":
            row["text"] = _clip(env.body.get("text"))
        elif env.type == "task":
            row["text"] = _clip(env.body.get("goal"))
        elif env.type == "result":
            row["text"] = _clip(env.body.get("payload") or env.body.get("error"))
        elif env.type == "transfer":
            row["text"] = _clip(
                f"[file] {env.body.get('name')} ({env.body.get('size')} bytes) — "
                f"{env.body.get('reason') or 'no reason given'}"
            )
            # Structured fields so the friend view can draw the transfer as the
            # shuttle's file card (§78). `text` stays: it feeds logs and any row
            # written before this existed falls back to it on the page.
            row["file"] = {
                "name": _clip(env.body.get("name")),
                "size": env.body.get("size"),
                "reason": _clip(env.body.get("reason")),
                "from_human": bool(env.body.get("from_human")),
                "sender": _clip(env.body.get("sender_name")),
            }
            # The sender's own picked path rides along when it was sent with
            # one (human direct send, §79); a reader shows it only while a
            # file with that path exists on ITS disk — the row travels to
            # both machines, the path belongs to the sender's.
            if env.body.get("path"):
                row["file"]["path"] = _clip(env.body.get("path"))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / (pair_name(env.src, env.dst) + ".jsonl")
            with self._guard, path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def read(self, host_a: str, host_b: str) -> list[dict]:
        """Merged, time-sorted conversation for one host pair (both directions)."""
        path = self.root / ("__".join(sorted((host_a, host_b))) + ".jsonl")
        if not path.is_file():
            return []
        rows: list[dict] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        rows.sort(key=lambda r: float(r.get("ts") or 0.0))
        return rows
