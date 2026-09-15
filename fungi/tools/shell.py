"""Shell tool: run a command via cmd.exe with UTF-8 codepage and a hard timeout.

Uses a temp .bat file (chcp 65001 trick from the PowerShell original) to avoid
quoting hell when the command contains quotes, pipes, or redirections.

The command's output goes to files, not pipes (spec §42): a command is free to
hand its handles to something that outlives it — `start "" notepad <file>` does
exactly that — and a pipe whose write end lives on in that grandchild never
reaches EOF, so waiting on it means waiting out BASH_TIMEOUT with nothing to show
for it. The call ends with the process we launched, which is also the only thing
`start` was ever meant to wait for.
"""

import codecs
import contextlib
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from pathlib import Path

BASH_TIMEOUT = 600
TRUNCATE_BASH = 8000


def _truncate(text: str, limit: int = TRUNCATE_BASH) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [truncated {len(text) - limit} chars] ...\n{text[-half:]}"


def _child_env() -> dict[str, str]:
    """Env for child processes: make wsl.exe speak UTF-8.

    wsl.exe writes its own output (listings, diagnostics) as UTF-16LE whatever
    the console codepage is, so `wsl -l -v` came back through the chcp 65001
    wrapper as `W\\x00S\\x00L\\x002\\x00` — unreadable. WSL_UTF8=1 switches it
    to UTF-8, which is what the rest of this module decodes. Harmless for every
    other command (it is just an env var)."""
    return {**os.environ, "WSL_UTF8": "1"}


def _open_temporary(path: Path) -> int:
    """O_TEMPORARY: the OS deletes the file once the *last* handle closes.

    A launched app inherits the handles we pass to the command and may hold them
    for hours, so deletion cannot be ours to do at return time."""
    flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_TEMPORARY", 0)
    return os.open(str(path), flags)


def _read_temporary(fd: int) -> bytes:
    """Everything written so far, and let go of our handle."""
    chunks: list[bytes] = []
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
    except OSError:
        pass
    finally:
        _close_quietly(fd)
    return b"".join(chunks)


def _close_quietly(fd: int) -> None:
    if fd >= 0:
        with contextlib.suppress(OSError):
            os.close(fd)


def _unlink_quietly(path: Path) -> None:
    # A live grandchild makes this a sharing violation on Windows (O_TEMPORARY
    # covers that case); on the POSIX-named branch below it is the real cleanup.
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def tool_bash(
    command: str, cwd: str | None = None, should_abort: Callable[[], bool] | None = None
) -> str:
    tag = f"fungi-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    bat = Path(tempfile.gettempdir()) / f"{tag}.bat"
    out_path = Path(tempfile.gettempdir()) / f"{tag}.out"
    err_path = Path(tempfile.gettempdir()) / f"{tag}.err"
    out_fd = err_fd = -1
    try:
        bat.write_text(f"chcp 65001 >nul\r\n{command}\r\n", encoding="utf-8", newline="")
        out_fd = _open_temporary(out_path)
        err_fd = _open_temporary(err_path)
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(bat)],
            stdout=out_fd,
            stderr=err_fd,
            # stdin=NUL: an interactive command (date/pause/...) reads EOF and
            # exits at once instead of holding the turn hostage for the full
            # timeout — fast-fail without banning any command.
            stdin=subprocess.DEVNULL,
            cwd=cwd or None,
            env=_child_env(),
            start_new_session=os.name != "nt",  # own group: killpg on abort
        )

        def _kill_tree() -> None:
            """The command may have its own children; kill the whole tree."""
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                pass
            with contextlib.suppress(OSError):
                proc.kill()

        # Wait on the process instead of on its output: a stop press must take
        # effect within ~0.2s, and a command that handed its handles to a
        # grandchild (`start`) must not keep us here after the process we
        # launched is gone (measured 2026-09-14: the full 600s, with cmd.exe
        # already exited rc=0) — spec §42.
        deadline = BASH_TIMEOUT
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if should_abort is not None and should_abort():
                _kill_tree()
                return "ERROR: cancelled by user"
            deadline -= 0.2
            if deadline <= 0:
                _kill_tree()
                return f"ERROR: Timed out after {BASH_TIMEOUT}s"
        out = _read_temporary(out_fd).decode("utf-8", errors="replace")
        err = _read_temporary(err_fd).decode("utf-8", errors="replace")
        out_fd = err_fd = -1  # both handles are gone: the finally must not repeat it
        result = out
        if err:
            result += ("\n[stderr]\n" if result else "") + err
        if not result.strip():
            result = "(no output)"
        if proc.returncode != 0:
            result += f"\n[exit: {proc.returncode}]"
        return _truncate(result)
    except OSError as exc:
        return f"ERROR: {exc}"
    finally:
        _close_quietly(out_fd)
        _close_quietly(err_fd)
        _unlink_quietly(bat)
        _unlink_quietly(out_path)
        _unlink_quietly(err_path)


# ---------------------------------------------------------------------------
# Session bash: REPL-style read/write interaction with a child process.
# bash_start launches with stdin=PIPE and returns at once; bash_send feeds one
# line and collects what the child printed; the child's output is pumped into
# a buffer by reader threads (Windows pipes have no select()). Sessions die
# when the owning turn ends (the abort callable is held via WeakMethod — when
# the Agent is garbage-collected the session is orphaned), on /stop (abort
# fires), or after BASH_SESSION_IDLE seconds of silence. A daemon reaper
# enforces all three so no orphan cmd survives the turn.
# ---------------------------------------------------------------------------

BASH_SESSION_IDLE = 600
SEND_READ_WAIT = 2.0

_SESSIONS: dict[str, "_BashSession"] = {}
_SESSIONS_LOCK = threading.Lock()
_REAPER: threading.Thread | None = None


class _BashSession:
    def __init__(
        self,
        sid: str,
        proc: subprocess.Popen,
        bat: Path,
        interactive: bool,
        should_abort: Callable[[], bool] | None,
    ) -> None:
        self.id = sid
        self.proc = proc
        self.bat = bat
        self.interactive = interactive
        self.lock = threading.Lock()  # serializes bash_send per session
        self.buf_lock = threading.Lock()
        self.chunks: list[str] = []
        self.last_activity = time.monotonic()
        self.read_pos = 0  # consumed by take_unseen; bash_send returns from here
        # WeakMethod: a bound method (Agent._aborted) dies with its Agent, so
        # a finished turn orphans the session and the reaper collects it. A
        # bare lambda (tests, callers without an Agent) can't be weak-ref'd:
        # fall back to a strong ref, the idle timeout still bounds it.
        if should_abort is None:
            self._abort = None
        else:
            try:
                self._abort = weakref.WeakMethod(should_abort)
            except TypeError:
                self._abort = should_abort

    def aborted(self) -> bool:
        if self._abort is None:
            return False
        if isinstance(self._abort, weakref.WeakMethod):
            fn = self._abort()
            if fn is None:
                return True  # owning agent gone: turn ended
            return bool(fn())
        return bool(self._abort())

    def snapshot(self, pos: int) -> tuple[str, int]:
        with self.buf_lock:
            text = "".join(self.chunks)
        return text[pos:], len(text)

    def take_unseen(self) -> str:
        """Everything pumped since the last read, advancing the read cursor.

        bash_send must consume from this cursor, not from 'since this send':
        output arriving between calls (npm's prompt, an error, a crash) is
        exactly what the agent needs to see."""
        with self.buf_lock:
            text = "".join(self.chunks)
        extra = text[self.read_pos :]
        self.read_pos = len(text)
        return extra

    def _pump(self, stream) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            for raw in iter(lambda: stream.read1(65536), b""):
                piece = decoder.decode(raw)
                if piece:
                    with self.buf_lock:
                        self.chunks.append(piece)
            piece = decoder.decode(b"", True)
            if piece:
                with self.buf_lock:
                    self.chunks.append(piece)
        except (OSError, ValueError):
            pass  # pipe broke on kill: normal exit path for a killed session


def _kill_session_tree(s: _BashSession) -> None:
    proc = s.proc
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        with contextlib.suppress(OSError, ValueError):
            if stream is not None:
                stream.close()
    s.bat.unlink(missing_ok=True)


def _reap_sessions_once(force_idle_check: bool = False) -> None:
    """Kill + drop sessions that lost their turn or idled out.

    A merely EXITED session is kept (until the idle cap): its buffered output
    and exit code are the only way to find out why it died — the post-mortem
    must stay readable via bash_send. This bit us: npm create vite failed and
    the reaper erased the evidence within 2s, leaving only
    'ERROR: no such bash session'."""
    now = time.monotonic()
    with _SESSIONS_LOCK:
        victims = [
            s
            for s in _SESSIONS.values()
            if s.aborted() or (force_idle_check and now - s.last_activity > BASH_SESSION_IDLE)
        ]
        for s in victims:
            _SESSIONS.pop(s.id, None)
    for s in victims:
        _kill_session_tree(s)


def _ensure_reaper() -> None:
    global _REAPER  # noqa: PLW0603 (module-level singleton thread)
    if _REAPER is not None and _REAPER.is_alive():
        return

    def _loop() -> None:
        while True:
            time.sleep(2)
            _reap_sessions_once(force_idle_check=True)

    _REAPER = threading.Thread(target=_loop, name="bash-session-reaper", daemon=True)
    _REAPER.start()


def _start_session(
    command: str,
    cwd: str | None,
    stdin_arg: str,
    should_abort: Callable[[], bool] | None,
) -> str:
    if stdin_arg not in ("nul", "pipe"):
        return "ERROR: stdin_arg must be 'nul' or 'pipe'"
    bat = Path(tempfile.gettempdir()) / f"fungi-{os.getpid()}-{uuid.uuid4().hex[:8]}.bat"
    try:
        bat.write_text(
            f"@echo off\r\nchcp 65001 >nul\r\n{command}\r\n", encoding="utf-8", newline=""
        )
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(bat)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if stdin_arg == "nul" else subprocess.PIPE,
            cwd=cwd or None,
            env=_child_env(),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        bat.unlink(missing_ok=True)
        return f"ERROR: {exc}"
    sid = uuid.uuid4().hex[:8]
    s = _BashSession(sid, proc, bat, stdin_arg == "pipe", should_abort)
    threading.Thread(target=s._pump, args=(proc.stdout,), daemon=True).start()
    threading.Thread(target=s._pump, args=(proc.stderr,), daemon=True).start()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = s
    _ensure_reaper()
    _reap_sessions_once()
    # Brief window for the first output: the caller usually wants the
    # initial prompt/state, not a guaranteed "(no output yet)".
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if s.proc.poll() is not None:
            break
        out, _ = s.snapshot(0)
        if out.strip():
            break
        time.sleep(0.1)
    out, _ = s.snapshot(0)
    kind = (
        "interactive (bash_send can feed input)"
        if s.interactive
        else "stdin=NUL: interactive input sees EOF"
    )
    s.read_pos = len(out)  # what start already showed; sends return only newer output
    return f"id={sid} ({kind})\n{out.strip() or '(no output yet)'}"


def tool_bash_start(
    command: str,
    cwd: str | None = None,
    stdin_arg: str = "nul",
    should_abort: Callable[[], bool] | None = None,
) -> str:
    """Start a command as a background session; returns the session id and
    whatever it printed so far. See module docstring for lifecycle."""
    return _start_session(command, cwd, stdin_arg, should_abort)


def tool_bash_send(id: str, text: str = "") -> str:
    sid = str(id or "")
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
    if s is None:
        return f"ERROR: no such bash session: {sid} (it exited or was reaped; re-run bash_start)"
    with s.lock:
        if s.proc.poll() is not None:
            # Post-mortem works for ANY session (even stdin=nul): the buffered
            # output + exit code is how the caller finds out what happened.
            extra = s.take_unseen()
            s.last_activity = time.monotonic()
            return (extra.strip() or "(no output)") + (
                f"\n[session {sid} exited, exit code {s.proc.returncode}]"
            )
    if not s.interactive:
        return "ERROR: session was started with stdin=nul — start a new one with stdin_arg='pipe' to feed input"
    with s.lock:
        try:
            s.proc.stdin.write((text + "\n").encode("utf-8"))
            s.proc.stdin.flush()
        except OSError:
            extra = s.take_unseen()
            return (extra.strip() or "(no output)") + f"\n[session {sid} exited (stdin closed)]"
        s.last_activity = time.monotonic()
        deadline = time.monotonic() + SEND_READ_WAIT
        seen = s.read_pos  # watermark must move in-loop: comparing against
        while time.monotonic() < deadline:  # the frozen read_pos extends the
            time.sleep(0.15)  # deadline forever once output flows
            _, total = s.snapshot(0)
            if total > seen:
                seen = total
                deadline = min(deadline + 0.3, time.monotonic() + 0.5)
            if s.proc.poll() is not None:
                break
        extra = s.take_unseen()
    s.last_activity = time.monotonic()
    result = extra.strip() or "(no new output)"
    # An exited session STAYS registered (until the idle cap): the exit note
    # plus whatever it printed is the post-mortem. No registry pop here.
    if s.proc.poll() is not None:
        result += f"\n[session {sid} exited, exit code {s.proc.returncode}]"
    return _truncate(result)


def tool_bash_kill(id: str) -> str:
    sid = str(id or "")
    with _SESSIONS_LOCK:
        s = _SESSIONS.pop(sid, None)
    if s is None:
        return f"ERROR: no such bash session: {sid}"
    _kill_session_tree(s)
    return f"session {sid} killed"
