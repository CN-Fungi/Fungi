"""Session storage: one JSON file per session, schema compatible with the PowerShell original.

File shape: {id, title, created, updated, messages: [{role, content, ...}]}
"""

import contextlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fungi.config import PROJECT_ROOT

SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"  # one location for every mode


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """Per-file lock around every read and write of one session file.

    Two Windows facts make this necessary. A reader holds the file open for the
    length of the read, and Python opens files without FILE_SHARE_DELETE, so an
    atomic rename onto a file being read is *refused* (measured: 123 refusals in
    6s of concurrent save+list). And a listing that globs while a rename lands
    can miss the file entirely, which is how a running session vanished from
    `/sessions`. Serializing our own readers and writers removes both.
    """
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _write_atomic(path: Path, text: str) -> None:
    """Write beside the target, then rename it into place.

    A save interrupted half-way (killed process, full disk, an exception in the
    encoder) used to leave a truncated JSON document where the session was, and
    `load` reads that back as nothing at all — indistinguishable from a wiped
    conversation. A rename is atomic, so the old file survives until a complete
    new one exists. (Rename, not fsync: this is about torn writes, not power.)
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(5):
            try:
                tmp.replace(path)  # same directory, same volume: atomic
                return
            except PermissionError:
                # Someone outside this process holds the target open (an editor,
                # a virus scanner, a second Fungi). Wait for it to let go.
                if attempt == 4:
                    raise
                time.sleep(0.02)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


_SID_LOCK = threading.Lock()
_SID_STATE = {"tick": "", "seq": 0}  # the second the last id was issued in, and how many


def new_session_id() -> str:
    """A new session id, never twice in the same clock tick.

    The resolution is one second, and the id *is* the file name, so two `/new`
    calls inside the same second used to return the same id: the second session
    silently overwrote the first (the client had just created it, so it looked
    like the session had vanished — 2026-09-10). The first id in a tick keeps the
    readable shape; later ones in that tick get a counter. (Two processes
    sharing one sessions directory could still race — the GUI's single-instance
    guard and the per-role directories are what prevent that.)
    """
    tick = datetime.now().strftime("%Y%m%d-%H%M%S")
    with _SID_LOCK:
        if tick == _SID_STATE["tick"]:
            _SID_STATE["seq"] += 1
        else:
            _SID_STATE["tick"], _SID_STATE["seq"] = tick, 0
        seq = _SID_STATE["seq"]
    return tick if seq == 0 else f"{tick}-{seq}"


def get_session_title(messages: list[dict[str, Any]]) -> str:
    """Title = first user message, collapsed whitespace, capped at 50 chars."""
    for msg in messages:
        if msg.get("role") == "user":
            text = " ".join(str(msg.get("content", "")).split())
            return text[:47] + "..." if len(text) > 50 else text
    return "(empty)"


class SessionStore:
    """Session directory backend; the hub points it at data/sessions."""

    def __init__(self, directory: Path):
        self.dir = directory

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[dict[str, Any]]:
        """Metadata for all sessions, newest first. Broken files are skipped."""
        self.ensure_dir()
        result = []
        for path in sorted(
            self.dir.glob("*.json"),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        ):
            try:
                with _lock_for(path):
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            result.append(
                {
                    "id": data.get("id"),
                    "title": data.get("title"),
                    "created": data.get("created"),
                    "updated": data.get("updated"),
                    "msgCount": len(data.get("messages", [])),
                }
            )
        return result

    def save(
        self,
        session_id: str,
        title: str,
        messages: list[dict[str, Any]],
        subagents: list[dict[str, Any]] | None = None,
        asks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.ensure_dir()
        path = self.dir / f"{session_id}.json"
        with _lock_for(path):
            created = _now()
            if path.is_file():
                with contextlib.suppress(OSError, json.JSONDecodeError):
                    created = json.loads(path.read_text(encoding="utf-8-sig")).get(
                        "created", created
                    )
            payload = {
                "id": session_id,
                "title": title,
                "created": created,
                "updated": _now(),
                "messages": messages,
                "subagents": subagents or [],
                "asks": asks or [],
            }
            _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self.dir / f"{session_id}.json"
        with _lock_for(path):
            if not path.is_file():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except OSError:
                return None
            except json.JSONDecodeError:
                # Evidence over silence: keep the bytes aside as .corrupt so the
                # damage is inspectable and reportable, instead of the view going
                # blank again on every load of the same file.
                with contextlib.suppress(OSError):
                    path.replace(path.with_name(path.name + ".corrupt"))
                return None

    def delete(self, session_id: str) -> None:
        path = self.dir / f"{session_id}.json"
        with _lock_for(path):
            path.unlink(missing_ok=True)


def _store() -> SessionStore:
    """Rebuild from the current module global so tests can monkeypatch SESSIONS_DIR."""
    return SessionStore(SESSIONS_DIR)


def ensure_dir() -> None:
    _store().ensure_dir()


def list_sessions() -> list[dict[str, Any]]:
    return _store().list_sessions()


def save_session(
    session_id: str,
    title: str,
    messages: list[dict[str, Any]],
    subagents: list[dict[str, Any]] | None = None,
    asks: list[dict[str, Any]] | None = None,
) -> None:
    _store().save(session_id, title, messages, subagents, asks)


def load_session(session_id: str) -> dict[str, Any] | None:
    return _store().load(session_id)


def delete_session(session_id: str) -> None:
    _store().delete(session_id)
