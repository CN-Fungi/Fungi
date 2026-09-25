"""The run log: what it records, how loud it stays, and what it must never hold.

The log is what a release test hands over when something does not connect, so
its contract is: one file per run pointing at the day, one line per fact however
often a poll fails, the api key never, and a traceback even where the exe has no
console left to print one.
"""

import json
import os
import sys
import threading
import time

import pytest

from fungi import runlog
from fungi.events import ConsoleSink


@pytest.fixture(autouse=True)
def log_file(tmp_path):
    """Every test here writes to its own file, never the program's own logs/."""
    return runlog.setup(tmp_path / "run.log")


def _lines(path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _containing(path, needle: str) -> list[str]:
    return [ln for ln in _lines(path) if needle in ln]


def test_notes_land_in_the_file_with_a_timestamp(log_file):
    runlog.note("hub listening on 0.0.0.0:%d", 8899)
    assert len(_containing(log_file, "hub listening on 0.0.0.0:8899")) == 1
    assert _lines(log_file)[0][:4].isdigit(), "a log line without a time cannot be read"


def test_setting_up_twice_writes_each_line_once(tmp_path):
    first = runlog.setup(tmp_path / "run.log")
    assert runlog.setup() == first, "a second entry point must reuse the same file"
    runlog.note("only once")
    assert len(_containing(first, "only once")) == 1


def test_a_repeating_failure_is_logged_once_per_interval(log_file):
    for _ in range(5):  # a room polls a dead hub this often and then some
        runlog.warn_once("hub:10.0.0.2", "hub %s request failed", "http://10.0.0.2:8899")
    assert len(_containing(log_file, "request failed")) == 1
    runlog.warn_once("hub:10.0.0.3", "hub %s request failed", "http://10.0.0.3:8899")
    assert len(_containing(log_file, "request failed")) == 2, "throttled per key, not globally"


def test_forgetting_a_key_lets_the_next_failure_through(log_file):
    runlog.warn_once("k", "failure one")
    runlog.warn_once("k", "failure two")
    assert not _containing(log_file, "failure two")
    runlog.forget("k")  # the hub answered again: the next failure is news
    runlog.warn_once("k", "failure three")
    assert len(_containing(log_file, "failure three")) == 1


def test_the_banner_records_the_switches_but_never_the_key(log_file, tmp_path, monkeypatch):
    from fungi import config as config_mod

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "api_key": "sk-must-not-appear",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-v4-pro",
                "ghostworld": True,
                "ghostworld_dir": "C:/games/GhostWorld",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    runlog.environment("gui", ["Fungi.exe"])

    text = log_file.read_text(encoding="utf-8")
    assert "sk-must-not-appear" not in text, "the log gets handed to other people"
    assert "api_key=True" in text
    assert "ghostworld=True dir='C:/games/GhostWorld'" in text
    assert "mode=gui" in text


def test_an_uncaught_exception_lands_in_the_file(log_file, capsys):
    try:
        raise RuntimeError("ghostworld watcher blew up")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())  # what the interpreter would call
    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text
    assert "RuntimeError: ghostworld watcher blew up" in text
    assert "ghostworld watcher blew up" in capsys.readouterr().err, "the console keeps its copy"


def test_a_thread_crash_is_recorded_with_the_thread_name(log_file):
    args = threading.ExceptHookArgs(
        (RuntimeError, RuntimeError("heartbeat died"), None, threading.current_thread())
    )
    runlog._thread_hook(args)
    assert "heartbeat died" in log_file.read_text(encoding="utf-8")


def test_saying_something_without_a_console_goes_to_the_log(log_file, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)  # the windowed exe's condition
    runlog.say("Fungi GUI 已在运行")
    assert "Fungi GUI 已在运行" in log_file.read_text(encoding="utf-8")


def test_the_console_sink_keeps_the_trace_when_there_is_no_console(log_file, monkeypatch):
    """The packaged exe has no stdout: the agent's tool calls must not vanish."""
    monkeypatch.setattr(sys, "stdout", None)
    sink = ConsoleSink()
    sink.emit("tool", {"name": "ghostworld", "args": '{"action": "say"}'})
    sink.emit("tool_result", {"content": "ERROR: no GhostWorld channel — the game is not running"})
    sink.emit("error", "cli: turn failed: boom")
    text = log_file.read_text(encoding="utf-8")
    assert 'tool ghostworld {"action": "say"}' in text
    assert "no GhostWorld channel" in text
    assert "agent error: cli: turn failed: boom" in text


def test_a_long_payload_is_cut_to_one_readable_line():
    assert runlog.short("a\nb\tc" + "x" * 900, 20) == "a b c" + "x" * 15
    assert len(runlog.short("y" * 5000)) == runlog.MAX_ARG


def test_old_files_are_pruned_and_the_current_one_is_kept(tmp_path):
    old = tmp_path / "fungi-20200101.log"
    old.write_text("ancient", encoding="utf-8")
    stale = time.time() - (runlog.KEEP_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    kept = tmp_path / "fungi-20260101.log"
    kept.write_text("recent", encoding="utf-8")

    runlog._prune(tmp_path)

    assert not old.exists()
    assert kept.exists()


def test_a_broken_banner_does_not_stop_the_program(log_file, monkeypatch):
    """The log must never be the reason the program refuses to start."""
    from fungi import config as config_mod

    def _boom() -> str:
        raise FileNotFoundError("pyproject.toml not found (bundled copy missing?)")

    monkeypatch.setattr(config_mod, "local_version", _boom)
    runlog.environment("gui", ["Fungi.exe"])  # must not raise
    assert "could not write the startup banner" in log_file.read_text(encoding="utf-8")


def test_the_log_falls_back_when_the_program_folder_cannot_be_written(tmp_path, monkeypatch):
    """Unpacked into Program Files, the exe's own folder is read-only: a log that
    cannot be written is the same as no log."""
    from fungi import config as config_mod

    monkeypatch.setattr(runlog, "logs_dir", lambda: tmp_path / "program-files" / "logs")
    # the writability rule lives in config (the inbox shares it, §49)
    monkeypatch.setattr(
        config_mod, "writable_dir", lambda preferred, name: tmp_path / "localappdata" / "logs"
    )
    monkeypatch.setattr(runlog, "_path", None)  # as if this were the program's first run

    path = runlog.setup()
    runlog.note("landed in the fallback")

    assert path.parent == tmp_path / "localappdata" / "logs"
    assert "landed in the fallback" in path.read_text(encoding="utf-8")
