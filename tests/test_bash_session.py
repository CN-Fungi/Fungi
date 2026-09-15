"""Session bash (bash_start/bash_send/bash_kill): REPL-style interaction.

Acceptance: a two-input script answers both prompts; interactive `date` sees
EOF and exits on stdin=nul; sessions die on abort (=/stop), when the owning
agent is garbage-collected (turn end), and on kill; an EXITED session stays
registered and readable (post-mortem) until the idle cap.
"""

import gc
import pathlib
import sys
import tempfile
import time

import pytest

from fungi.tools import dispatch
from fungi.tools import shell as shell_mod
from fungi.tools.shell import tool_bash_kill, tool_bash_send, tool_bash_start


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    with shell_mod._SESSIONS_LOCK:
        sessions = list(shell_mod._SESSIONS.values())
        shell_mod._SESSIONS.clear()
    for s in sessions:
        shell_mod._kill_session_tree(s)


def _sid(out: str) -> str:
    return out.split(" ", 1)[0].removeprefix("id=")


def _alive(sid: str) -> bool:
    with shell_mod._SESSIONS_LOCK:
        s = shell_mod._SESSIONS.get(sid)
    return s is not None and s.proc.poll() is None


def _wait_exit(sid: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(sid):
            return True
        time.sleep(0.1)
    return False


def test_two_question_script_full_interaction(tmp_path):
    script = tmp_path / "two.py"
    script.write_text(
        "print('Q1', flush=True)\n"
        "input()\n"
        "print('Q2', flush=True)\n"
        "input()\n"
        "print('DONE', flush=True)\n",
        encoding="utf-8",
    )
    out = tool_bash_start(f'"{sys.executable}" "{script}"', stdin_arg="pipe")
    sid = _sid(out)
    assert "interactive" in out
    assert "Q1" in out

    second = tool_bash_send(id=sid, text="answer1")
    assert "Q2" in second

    final = tool_bash_send(id=sid, text="answer2")
    assert "DONE" in final
    assert "[session" in final and "exited" in final


def test_interactive_date_eof_exits_on_nul():
    out = tool_bash_start("date")  # bare date waits for input
    sid = _sid(out)
    assert sid
    assert _wait_exit(sid), "interactive date should read EOF and exit"
    # Post-mortem: an exited session stays registered and readable until the
    # idle cap — its output and exit code are the only way to see why it died.
    r = tool_bash_send(id=sid, text="x")
    assert "exit code" in r
    assert sid in shell_mod._SESSIONS


def test_output_arriving_before_first_send_is_visible():
    """Regression: bash_send consumed from 'since this send', so output that
    landed between start and the first send was swallowed (npm's prompt was
    never seen). send must return from the session-wide read cursor."""
    script = pathlib.Path(tempfile.gettempdir()) / "fungi-prompt-test.py"
    script.write_text(
        "import time; time.sleep(1.2)\n"  # print AFTER bash_start's 1s window
        "print('PROMPT', flush=True)\ninput()\nprint('BYE', flush=True)\n",
        encoding="utf-8",
    )
    out = tool_bash_start(f'"{sys.executable}" "{script}"', stdin_arg="pipe")
    assert "PROMPT" not in out, "prompt must arrive after start returns"
    sid = _sid(out)
    time.sleep(1.5)  # the child prints its prompt while we "think"
    r = tool_bash_send(id=sid, text="answer")
    assert "PROMPT" in r


def test_send_rejects_nul_session():
    out = tool_bash_start("ping -n 10 127.0.0.1 >nul")  # default stdin=nul
    sid = _sid(out)
    assert "stdin=nul" in tool_bash_send(id=sid, text="x")
    tool_bash_kill(id=sid)


def test_abort_kills_session():
    flag = {"on": False}
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul", should_abort=lambda: flag["on"])
    sid = _sid(out)
    assert _alive(sid)
    flag["on"] = True  # /stop pressed
    shell_mod._reap_sessions_once()
    assert sid not in shell_mod._SESSIONS
    assert not _alive(sid)


def test_turn_end_orphans_session_via_weakref():
    class Agent:
        def aborted(self):
            return False

    agent = Agent()
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul", should_abort=agent.aborted)
    sid = _sid(out)
    shell_mod._reap_sessions_once()
    assert _alive(sid)  # owning agent alive: session survives
    del agent
    gc.collect()
    shell_mod._reap_sessions_once()  # turn ended: agent GC'd
    assert sid not in shell_mod._SESSIONS


def test_bash_kill():
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul")
    sid = _sid(out)
    assert "killed" in tool_bash_kill(id=sid)
    assert sid not in shell_mod._SESSIONS


def test_idle_cap_reaps(monkeypatch):
    monkeypatch.setattr(shell_mod, "BASH_SESSION_IDLE", 0)
    out = tool_bash_start("ping -n 30 127.0.0.1 >nul")
    sid = _sid(out)
    shell_mod._reap_sessions_once(force_idle_check=True)
    assert sid not in shell_mod._SESSIONS


def test_dispatch_wiring():
    out = dispatch("bash_start", {"command": "echo hi"})
    sid = _sid(out)
    assert "hi" in out
    assert "unknown" not in tool_bash_kill(id=sid).lower()


def test_session_children_see_utf8_wsl_env():
    """wsl.exe writes UTF-16LE unless WSL_UTF8=1 is in the child env, so a
    session running `wsl -l -v` used to come back as `W\\x00S\\x00L\\x002\\x00`."""
    out = tool_bash_start("set WSL_UTF8")
    tool_bash_kill(id=_sid(out))
    assert "WSL_UTF8=1" in out
