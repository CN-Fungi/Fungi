"""GhostWorld connector: the CLI contract, the switch, and the speech watcher.

Nothing here starts a game or a real child process — the CLI seam
(`ghostworld.subprocess.run`) and the child seam (`ghostworld._spawn`) are
replaced, which is the same way the screen tests keep off the real desktop.
"""

import base64
import json
import subprocess
import sys
import time
import types

import pytest

from fungi import room as room_mod
from fungi.clone.base import Clone, Envelope
from fungi.clone.local import build_local_clone
from fungi.config import Config
from fungi.events import FnSink, NullSink
from fungi.llm import LLMResult
from fungi.tools import ghostworld

CFG = Config(api_key="k", endpoint="e", model="m")
GAME_DIR = "C:/games/ghostworld"


def _on(**kwargs) -> Config:
    return Config(
        api_key="k", endpoint="e", model="m", ghostworld=True, ghostworld_dir=GAME_DIR, **kwargs
    )


class _Cli:
    """Stand-in for subprocess.run: records argv/cwd, replays scripted replies."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("cwd")))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _done(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["cli"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _tool(cfg: Config):
    return ghostworld.bound(cfg)["ghostworld"].fn


class _NoTransport:
    """Constructing a local clone must not touch the transport."""

    def __getattr__(self, name):
        raise AssertionError(f"the transport was used during construction: {name}")


def _local_clone(cfg: Config) -> Clone:
    return build_local_clone("alpha", _NoTransport(), cfg, NullSink())


# ── the tool ─────────────────────────────────────────────────────────────────


def test_the_ack_comes_back_verbatim(monkeypatch):
    cli = _Cli(_done(0, '{"type": "position", "x": 7.5, "y": 1.5}\n'))
    monkeypatch.setattr(ghostworld.subprocess, "run", cli)
    monkeypatch.setattr(ghostworld, "load_config", _on)
    monkeypatch.setattr(ghostworld.shutil, "which", lambda name: None)  # a checkout, not a release

    out = _tool(_on())({"action": "pos"})

    assert json.loads(out)["type"] == "position"
    argv, cwd = cli.calls[0]
    assert argv[:4] == [sys.executable, "-m", "metaverse.cli_channel", "send"]
    assert json.loads(argv[4]) == {"cmd": "pos"}
    assert cwd == GAME_DIR, "the game's own checkout is the working directory"


def test_parameters_become_the_command_and_empties_are_dropped(monkeypatch):
    cli = _Cli(_done(0, "{}"))
    monkeypatch.setattr(ghostworld.subprocess, "run", cli)
    monkeypatch.setattr(ghostworld, "load_config", _on)

    _tool(_on())({"action": "say", "message": "你好", "x": None, "item_id": ""})

    assert json.loads(cli.calls[0][0][4]) == {"cmd": "say", "message": "你好"}


def test_console_scripts_are_used_when_no_checkout_is_configured(monkeypatch):
    cli = _Cli(_done(0, "{}"))
    monkeypatch.setattr(ghostworld.subprocess, "run", cli)
    monkeypatch.setattr(ghostworld, "load_config", lambda: Config(ghostworld=True))
    monkeypatch.setattr(ghostworld.shutil, "which", lambda name: None)  # no release on PATH either

    _tool(Config(ghostworld=True))({"action": "pos"})

    argv, cwd = cli.calls[0]
    assert argv == ["ghostworld-send", '{"cmd": "pos"}'], "the console script is the fallback"
    assert cwd is None


def test_the_cli_is_started_without_a_console_window(monkeypatch):
    """The exe is windowed, so a console child is given a console of its own: the
    CLI's window would flash on every send, and the follower's would sit on the
    desktop for as long as it is watched (2026-09-16 report: 连这个 CLI 都不该
    显示出来). CREATE_NO_WINDOW is what Windows needs to hear."""
    seen: dict = {}

    class _Done:
        returncode, stdout, stderr = 0, "{}", ""

    def run(argv, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(ghostworld.subprocess, "run", run)
    ghostworld.send_command({"cmd": "pos"}, GAME_DIR)
    assert seen["creationflags"] == ghostworld.NO_WINDOW
    assert getattr(subprocess, "CREATE_NO_WINDOW", 0) == ghostworld.NO_WINDOW, (
        "the Windows flag itself"
    )

    class _Proc:
        stdout = None

        def kill(self) -> None:
            return None

        def wait(self, timeout=None) -> int:
            return 0

    seen.clear()
    monkeypatch.setattr(
        ghostworld.subprocess, "Popen", lambda argv, **kwargs: (seen.update(kwargs), _Proc())[1]
    )
    monkeypatch.setattr(ghostworld.shutil, "which", lambda name: None)  # module form: same flags
    assert ghostworld._spawn(GAME_DIR) is not None
    assert seen["creationflags"] == ghostworld.NO_WINDOW


@pytest.mark.parametrize(
    ("reply", "needle"),
    [
        (_done(2, "", "channel not found"), "not running"),
        (_done(1, "", "no ack"), "never answered"),
        (subprocess.TimeoutExpired("cli", 20), "did not answer"),
        (FileNotFoundError("cli"), "not found"),
    ],
)
def test_failures_explain_themselves(monkeypatch, reply, needle):
    monkeypatch.setattr(ghostworld.subprocess, "run", _Cli(reply))
    monkeypatch.setattr(ghostworld, "load_config", _on)

    out = _tool(_on())({"action": "pos"})

    assert out.startswith("ERROR: ") and needle in out


def test_the_off_switch_stops_the_tool_before_any_process(monkeypatch):
    cli = _Cli(_done(0, "{}"))
    monkeypatch.setattr(ghostworld.subprocess, "run", cli)
    monkeypatch.setattr(ghostworld, "load_config", lambda: Config(ghostworld=False))

    out = _tool(_on())({"action": "pos"})

    assert "off in settings" in out
    assert cli.calls == [], "a call-time gate must not spawn anything"


def test_missing_action_is_reported(monkeypatch):
    monkeypatch.setattr(ghostworld, "load_config", _on)
    assert _tool(_on())({}).startswith("ERROR: action is required")


def test_the_clone_carries_the_tool_only_when_the_switch_is_on():
    assert "ghostworld" in _local_clone(_on()).tools
    assert "ghostworld" not in _local_clone(CFG).tools
    assert "inquire" in _local_clone(CFG).tools, "other extras are untouched"


# ── snapshot: a look comes back as a picture ─────────────────────────────────

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _snapshot_reply(path) -> subprocess.CompletedProcess:
    return _done(0, json.dumps({"type": "snapshot_done", "caption": "看看", "local": str(path)}))


def test_a_snapshot_answer_carries_the_picture(tmp_path, monkeypatch):
    """The agent asked to look at the world: it gets the pixels, not a filename."""
    png = tmp_path / "agent_1.png"
    png.write_bytes(_PNG_1PX)
    monkeypatch.setattr(ghostworld.subprocess, "run", _Cli(_snapshot_reply(png)))
    monkeypatch.setattr(ghostworld, "load_config", _on)

    out = _tool(_on())({"action": "snapshot"})

    assert isinstance(out, ghostworld.ImageRead), "a look must reach the model as an image"
    assert out.data_url.startswith("data:image/")
    assert str(png) in out, "the path rides along so the user can open it too"


def test_a_snapshot_without_a_readable_file_is_still_an_answer(tmp_path, monkeypatch):
    """No picture to attach: the ack and its path must survive, not vanish."""
    missing = tmp_path / "gone.png"
    monkeypatch.setattr(ghostworld.subprocess, "run", _Cli(_snapshot_reply(missing)))
    monkeypatch.setattr(ghostworld, "load_config", _on)

    out = _tool(_on())({"action": "snapshot"})

    assert isinstance(out, str) and not isinstance(out, ghostworld.ImageRead)
    assert "gone.png" in out


# ── the note the watcher injects ─────────────────────────────────────────────


def test_a_channel_note_is_not_labelled_as_the_owner():
    clone = _local_clone(CFG)
    channel = Envelope(
        src="alpha:GhostWorld",
        dst=clone.addr,
        type="chat",
        body={"text": "在吗", "from_channel": "GhostWorld"},
    )
    owner = Envelope(
        src="alpha:owner", dst=clone.addr, type="chat", body={"text": "说得好", "from_owner": True}
    )
    assert clone.render_input(channel) == "[GhostWorld] 在吗"
    assert clone.render_input(owner) == "[评价] 说得好"


def test_a_channel_note_queues_a_local_turn():
    clone = _local_clone(CFG)
    clone.note("player said hi", source="GhostWorld")
    env = clone._work.get_nowait()
    assert env.type == "chat" and env.body["from_channel"] == "GhostWorld"
    assert env.body.get("from_owner") is None


def test_wake_text_labels_the_player_once():
    """The [GhostWorld] prefix comes from the note; naming it here would double it."""
    text = ghostworld.wake_text({"kind": "wake", "from": "player", "message": "在吗"})
    assert text == "玩家（player）说：在吗"


# ── the player is heard *during* a turn, not after it ────────────────────────


class _IdleTransport:
    """Enough transport for a started clone: polls empty, never sends."""

    def poll(self, cursor, timeout):
        time.sleep(0.01)
        return [], cursor


class _SlowRoundLLM:
    """Keeps a turn in flight: every round asks for one more (bogus) tool call.

    A bogus tool is answered with an error string, so the turn stays in its
    tool loop — which is where a turn is long, and where the abort has to land.
    """

    def __init__(self, per_round_s: float = 0.2) -> None:
        self.rounds = 0
        self.per_round_s = per_round_s

    def __call__(self, _messages, _tool_defs):
        self.rounds += 1
        time.sleep(self.per_round_s)
        return LLMResult(
            tool_calls=[
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "no_such_tool", "arguments": "{}"},
                }
            ]
        )


def _eventually(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_a_player_line_cuts_a_turn_that_has_been_thinking():
    """The clone's own turns get the WebUI's /stop: a line heard now is not
    queued behind a turn that is walking somewhere or calling tools."""
    events: list[tuple] = []
    llm = _SlowRoundLLM()
    clone = build_local_clone(
        "alpha", _IdleTransport(), CFG, FnSink(lambda t, c: events.append((t, c))), llm=llm
    )
    clone.start()
    try:
        clone.note("player said hi", source="GhostWorld")
        assert _eventually(lambda: llm.rounds >= 1, 3.0), "the turn never started"

        # A turn that has only just begun has nothing worth cutting: a burst of
        # speech must not restart the answer over and over.
        assert clone.interrupt_turn(min_age_s=60.0) is False
        assert llm.rounds == 1, "the spared turn kept running"

        assert clone.interrupt_turn() is True
        assert clone.turn_aborted() is True
        assert _eventually(lambda: clone._turn_abort is None, 5.0), "the cut turn must end"
        assert clone.turn_aborted() is False, "the flag belongs to the turn, not the clone"
    finally:
        clone.stop()
    assert ("error", "Aborted by user") in events, "the turn ends the way /stop ends one"


def test_a_wake_interrupts_before_it_queues(monkeypatch):
    """Order matters: queue first and a turn that is already winding down takes
    the new line into a reply it had almost finished."""
    calls: list[tuple] = []
    stub = types.SimpleNamespace(
        _local=types.SimpleNamespace(tools={}),
        local=types.SimpleNamespace(
            interrupt_turn=lambda min_age_s=0.0: calls.append(("interrupt", min_age_s)) or True,
            note=lambda text, **kw: calls.append(("note", text, kw)),
        ),
    )
    armed: list = []
    monkeypatch.setattr(
        ghostworld, "arm", lambda directory, on_wake=None: armed.append(on_wake) or True
    )
    monkeypatch.setattr(room_mod, "load_config", _on)
    room_mod.ensure_ghostworld_watch(stub)

    armed[0]({"kind": "wake", "event": "heard", "from": "player", "message": "在吗"})
    assert calls == [
        ("interrupt", room_mod.PLAYER_INTERRUPT_AFTER_S),
        ("note", "玩家（player）说：在吗", {"source": "GhostWorld"}),
    ]


# ── the watcher ──────────────────────────────────────────────────────────────


class _FakeChild:
    """A follower the OS has already ended: kill() is a no-op, wait() reaps."""

    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = list(lines)
        self.returncode = None
        self._rc = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


class _LiveChild:
    """A follower still running: it prints until it is killed, then its pipe ends."""

    def __init__(self) -> None:
        self.killed = False
        self.returncode = None
        self.stdout = self._lines()

    def _lines(self):
        while not self.killed:
            time.sleep(0.005)
            yield _wake_line("还在")

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        self.returncode = -9 if self.killed else 0
        return self.returncode


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _wake_line(message: str = "在吗", who: str = "player") -> str:
    return (
        json.dumps(
            {"seq": 3, "ts": 1.0, "kind": "wake", "event": "heard", "from": who, "message": message}
        )
        + "\n"
    )


def test_only_player_speech_reaches_the_agent(monkeypatch):
    lines = [
        "ghostworld-wait: starting\n",  # the CLI's own chatter
        json.dumps({"seq": 1, "kind": "observation", "event": "see"}) + "\n",
        "{not json\n",
        _wake_line("在吗"),
        json.dumps({"seq": 4, "kind": "observation", "event": "position"}) + "\n",
    ]
    spawns: list[_FakeChild] = []

    def spawn(directory):
        # Only the first child carries lines: a real follower resumes from the
        # game's cursor, so a reconnect cannot replay what was already read.
        child = _FakeChild(lines if not spawns else [], returncode=2)
        spawns.append(child)
        return child

    monkeypatch.setattr(ghostworld, "_spawn", spawn)
    monkeypatch.setattr(ghostworld, "NO_GAME_DELAY_S", 0.01)
    monkeypatch.setattr(ghostworld, "game_is_up", lambda directory: True)
    seen: list[dict] = []
    try:
        assert ghostworld.arm(GAME_DIR, seen.append) is True
        assert _wait_for(lambda: bool(seen))
        time.sleep(0.1)  # a reconnect must not add a second copy
    finally:
        ghostworld.disarm()
    assert [e["message"] for e in seen] == ["在吗"]
    assert ghostworld.is_armed() is False, "disarm must leave nothing running"


def test_arming_twice_is_a_no_op(monkeypatch):
    spawns: list[int] = []

    def spawn(directory):
        spawns.append(len(spawns))
        return _FakeChild([_wake_line()], returncode=2)

    monkeypatch.setattr(ghostworld, "_spawn", spawn)
    monkeypatch.setattr(ghostworld, "NO_GAME_DELAY_S", 0.5)  # one child is enough to see
    monkeypatch.setattr(ghostworld, "game_is_up", lambda directory: True)
    try:
        assert ghostworld.arm(GAME_DIR, lambda evt: None) is True
        assert ghostworld.arm(GAME_DIR, lambda evt: None) is False
        time.sleep(0.05)
        assert len(spawns) == 1, "a second arm must not add a second watcher"
    finally:
        ghostworld.disarm()


def test_the_watcher_comes_back_when_the_game_does(monkeypatch):
    children: list[_FakeChild] = []
    killed: list[bool] = []

    def spawn(directory):
        child = _FakeChild([_wake_line("又见面了")], returncode=2)
        children.append(child)
        killed.append(False)
        return child

    monkeypatch.setattr(ghostworld, "_spawn", spawn)
    monkeypatch.setattr(ghostworld, "NO_GAME_DELAY_S", 0.01)
    monkeypatch.setattr(ghostworld, "game_is_up", lambda directory: True)
    seen: list[dict] = []
    try:
        ghostworld.arm(GAME_DIR, seen.append)
        # The follower exits 2 (game gone) and the watcher reconnects by itself:
        # starting the game again is enough, and the cursor means nothing is lost.
        assert _wait_for(lambda: len(children) >= 3)
    finally:
        ghostworld.disarm()
    assert all(child.killed for child in children), "every dead child is reaped"


def test_nothing_is_spawned_while_the_game_is_not_there(monkeypatch, tmp_path):
    """No game -> no child (and no spin): the channel file is the evidence."""
    spawns: list[int] = []

    def spawn(directory):
        spawns.append(len(spawns))
        return _FakeChild([_wake_line()], returncode=2)

    monkeypatch.setattr(ghostworld, "_spawn", spawn)
    monkeypatch.setattr(ghostworld, "NO_GAME_DELAY_S", 0.02)
    empty = str(tmp_path)  # a checkout with no .channel.json in it
    assert ghostworld.arm(empty, lambda evt: None) is True
    time.sleep(0.15)
    assert spawns == [], "a follower must not be launched with nothing to talk to"

    # ...and the game turning up is what starts one
    channel_dir = tmp_path / "metaverse"
    channel_dir.mkdir()
    (channel_dir / ".channel.json").write_text("{}", encoding="utf-8")
    assert _wait_for(lambda: bool(spawns)), "the watcher must notice the game appearing"
    ghostworld.disarm()


def test_disarm_kills_a_running_child_and_does_not_hang(monkeypatch):
    live: list[_LiveChild] = []

    def spawn(directory):
        child = _LiveChild()
        live.append(child)
        return child

    monkeypatch.setattr(ghostworld, "_spawn", spawn)
    monkeypatch.setattr(ghostworld, "game_is_up", lambda directory: True)
    seen: list[dict] = []
    assert ghostworld.arm(GAME_DIR, seen.append) is True
    assert _wait_for(lambda: bool(seen)), "the watcher must read the child's stream"
    assert _wait_for(lambda: bool(live))
    started = time.monotonic()
    ghostworld.disarm()
    assert time.monotonic() - started < 2.0, "disarm must not wait out the read timeout"
    assert live[0].killed, "the child process is killed, not left behind"
    assert ghostworld.is_armed() is False


def test_the_room_arms_and_disarms_the_watcher(monkeypatch):
    stub = types.SimpleNamespace(
        _local=types.SimpleNamespace(tools={}),
        local=types.SimpleNamespace(note=lambda *a, **k: None),
    )
    armed: list[tuple[str, object]] = []
    disarmed: list[bool] = []
    monkeypatch.setattr(
        ghostworld,
        "arm",
        lambda directory, on_wake=None: armed.append((directory, on_wake)) or True,
    )
    monkeypatch.setattr(ghostworld, "disarm", lambda: disarmed.append(True))

    monkeypatch.setattr(room_mod, "load_config", _on)
    room_mod.ensure_ghostworld_watch(stub)
    assert armed and armed[0][0] == GAME_DIR
    assert "ghostworld" in stub._local.tools, "the clone's own dict covers wake turns"

    monkeypatch.setattr(room_mod, "load_config", lambda: Config(ghostworld=False))
    room_mod.ensure_ghostworld_watch(stub)
    assert disarmed == [True], "turning the switch off disarms at once"


def test_the_watcher_child_is_started_with_the_follow_contract(monkeypatch):
    started: list[list[str]] = []

    class _Proc:
        stdout = None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    def popen(argv, **kwargs):
        started.append(list(argv))
        return _Proc()

    monkeypatch.setattr(ghostworld.subprocess, "Popen", popen)
    monkeypatch.setattr(ghostworld.shutil, "which", lambda name: None)
    child = ghostworld._spawn(GAME_DIR)
    assert child is not None
    argv = started[0]
    assert argv[:4] == [sys.executable, "-m", "metaverse.cli_channel", "wait"]
    assert "--follow" in argv and "--timeout" in argv


# ── a packed release: no Python, an exe, and its own app data ────────────────


def _release(tmp_path) -> str:
    """A folder shaped like the one a release zip unpacks to."""
    (tmp_path / ghostworld.CLI_EXE).write_bytes(b"")
    return str(tmp_path)


def test_a_packed_release_is_driven_through_its_console_exe(tmp_path, monkeypatch):
    """The exe download has no Python and no pip: its channel CLI is an exe whose
    verbs are the console scripts' — `GhostWorldCLI.exe send <json>`."""
    cli = _Cli(_done(0, '{"type": "position"}'))
    monkeypatch.setattr(ghostworld.subprocess, "run", cli)
    monkeypatch.setattr(ghostworld, "load_config", _on)
    directory = _release(tmp_path)
    cfg = Config(api_key="k", endpoint="e", model="m", ghostworld=True, ghostworld_dir=directory)

    _tool(cfg)({"action": "pos"})

    argv, cwd = cli.calls[0]
    assert argv[:2] == [str(tmp_path / ghostworld.CLI_EXE), "send"]
    assert json.loads(argv[2]) == {"cmd": "pos"}
    assert cwd == directory


def test_the_follower_is_started_with_the_packed_exe_too(tmp_path, monkeypatch):
    started: list[list[str]] = []

    class _Proc:
        stdout = None

        def kill(self) -> None:
            return None

        def wait(self, timeout=None) -> int:
            return 0

    def popen(argv, **kwargs):
        started.append(list(argv))
        return _Proc()

    monkeypatch.setattr(ghostworld.subprocess, "Popen", popen)
    assert ghostworld._spawn(_release(tmp_path)) is not None
    argv = started[0]
    assert argv[:2] == [str(tmp_path / ghostworld.CLI_EXE), "wait"]
    assert "--follow" in argv and "--timeout" in argv


def test_the_packed_game_is_found_in_the_users_app_data(tmp_path, monkeypatch):
    """A release may sit in a read-only folder, so it advertises its channel under
    the user's app data — and that is the file the watcher must wait for. Looking
    only beside the code would mean the watcher never starts for a release."""
    directory = _release(tmp_path)
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    assert ghostworld.game_is_up(directory) is False, "nothing running yet"

    channel = appdata / "GhostWorld" / ".channel.json"
    channel.parent.mkdir(parents=True)
    channel.write_text("{}", encoding="utf-8")

    assert ghostworld.game_is_up(directory) is True


def test_a_checkout_still_answers_beside_its_code(tmp_path, monkeypatch):
    """The release's location must not become the only place that counts."""
    monkeypatch.setattr(ghostworld.shutil, "which", lambda name: None)
    (tmp_path / "metaverse").mkdir()
    (tmp_path / "metaverse" / ".channel.json").write_text("{}", encoding="utf-8")
    assert ghostworld.game_is_up(str(tmp_path)) is True
