"""GhostWorld channel: play a character in a running GhostWorld game.

The contract lives in the game's own repo (`docs/PROTOCOL-agent-channel.md`): a
loopback line-JSON socket behind two CLIs. Fungi speaks it only through those
CLIs, as a child process — it never imports GhostWorld, so the game can change
whatever it likes inside, and nothing here can reach into the game's world.

Two halves, on purpose:

- the `ghostworld` tool (bound, per turn, gated by `config.json` `ghostworld`):
  one command in, one ack out, inside a turn.
- the watcher (armed once per room, like the courier's mail watch): supervises
  `ghostworld-wait --follow` and hands each player line to a callback, so speech
  *wakes* the agent instead of being polled for. It only starts that child once the
  game is really there (its own `.channel.json` is the evidence): no game, no child
  — just a cheap re-check. The child is restarted when the game exits and comes
  back: starting the game again is enough, and no event is lost meanwhile, because
  the game keeps a cursor for the follower.

Both are inert until the switch is on — off means the tool is never attached, so
the model cannot see it (same rule as `pc_control`, spec §35).
"""

import atexit
import contextlib
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fungi.config import Config, load_config

SEND_TIMEOUT_S = 20.0
WAIT_TIMEOUT_S = 25.0  # the CLI's own read deadline; it re-arms itself after
RESTART_DELAY_S = 5.0  # the game went away mid-stream: this is a restart, not a fault
NO_GAME_DELAY_S = 30.0  # exit 2 = nothing to talk to; re-check rather than spin
STOP_JOIN_S = 2.0
MAX_LINE = 1 << 20

SCHEMA = {
    "type": "function",
    "function": {
        "name": "ghostworld",
        "description": (
            "Play your character in a running GhostWorld game on this machine. One call runs "
            "ONE command and returns the game's answer, so ask before you act: 'pos' (where you "
            "are), 'look' (what surrounds you), 'inv' (what you carry), then 'goto' or 'move' "
            "(x, y), 'say' (message) to speak to the player, 'pickup' (item_id or x,y) to take "
            "something within reach, 'turn'/'track' to face someone, 'dump_map' when something "
            "looks wrong. The player's speech arrives by itself as a [GhostWorld] note: reply to "
            "the player with 'say', and tell the user about it in your own words afterwards. "
            "If the game is not running this returns an ERROR — say so instead of retrying."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Command: say / pos / look / inv / goto / move / turn / track / pickup / place / give / snapshot / dump_map",
                },
                "message": {"type": "string", "description": "Text for 'say'"},
                "x": {
                    "type": "number",
                    "description": "Target x for goto / move / turn / pickup / place",
                },
                "y": {
                    "type": "number",
                    "description": "Target y for goto / move / turn / pickup / place",
                },
                "item_id": {"type": "string", "description": "Item id for pickup / place / give"},
                "target": {"type": "string", "description": "Avatar or item name for track / give"},
            },
            "required": ["action"],
        },
    },
}


def enabled(cfg: Config | None = None) -> bool:
    """Is character control on? Re-read per call so the switch is live."""
    return bool((cfg or load_config()).ghostworld)


def _cli(verb: str, directory: str) -> list[str]:
    """How to reach the channel CLI.

    With `ghostworld_dir` set we run the game's module directly, which needs no
    PATH entry and no installed package — the state every fresh checkout is in.
    Without it we fall back to the console scripts the package installs.
    """
    if directory:
        return [sys.executable, "-m", "metaverse.cli_channel", verb]
    return [f"ghostworld-{verb}"]


def _run_cli(
    verb: str, args: list[str], directory: str, timeout: float
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_cli(verb, directory), *args],
        cwd=directory or None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def send_command(cmd: dict, directory: str) -> str:
    """One command → the game's ack, or an ERROR line the model can act on."""
    try:
        proc = _run_cli("send", [json.dumps(cmd, ensure_ascii=False)], directory, SEND_TIMEOUT_S)
    except FileNotFoundError:
        return (
            "ERROR: the GhostWorld CLI was not found — install the game "
            "(pip install -e <repo>) or set ghostworld_dir in config.json"
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: GhostWorld did not answer within {SEND_TIMEOUT_S:.0f}s"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        return out or "(no output)"
    if proc.returncode == 2:
        return (
            "ERROR: no GhostWorld channel — the game is not running (the user may need to start it)"
        )
    if proc.returncode == 1:
        return f"ERROR: the game took the command but never answered (its frame loop is not running){_tail(err)}"
    return f"ERROR: ghostworld-send exited {proc.returncode}{_tail(err or out)}"


def _tail(text: str) -> str:
    text = " ".join(text.split())
    return f": {text[:300]}" if text else ""


# ── the watcher: player speech arrives, nobody polls ──────────────────────────


@dataclass
class _Watch:
    """The one watcher a process may have: its stop flag, thread and child."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event | None = None
    thread: threading.Thread | None = None
    child: subprocess.Popen | None = None


_watch = _Watch()


def is_armed() -> bool:
    with _watch.lock:
        return _watch.thread is not None and _watch.thread.is_alive()


CHANNEL_REL = ("metaverse", ".channel.json")


def game_is_up(directory: str) -> bool:
    """Is there a game to talk to? Its channel file is the only evidence we have.

    Without a checkout (console-script mode) we cannot look, so we let the child
    find out and report exit 2 — the caller retries either way.
    """
    if not directory:
        return True
    return Path(directory).joinpath(*CHANNEL_REL).exists()


def arm(directory: str, on_wake: Callable[[dict], None]) -> bool:
    """Start watching for player speech; returns True if it was started now.

    `on_wake(event)` is called from the watcher thread with each `heard` event,
    which is the only thing the game ever pushes (observations are readable with
    the tool instead of being streamed into the agent's day).
    """
    with _watch.lock:
        if _watch.thread is not None and _watch.thread.is_alive():
            return False
        _watch.stop = threading.Event()
        _watch.thread = threading.Thread(
            target=_watch_loop,
            args=(directory, on_wake, _watch.stop),
            name="ghostworld-watch",
            daemon=True,
        )
        _watch.thread.start()
    return True


def disarm() -> None:
    """Stop watching and kill the child; safe to call when never armed."""
    with _watch.lock:
        stop, thread, child = _watch.stop, _watch.thread, _watch.child
        _watch.stop, _watch.thread, _watch.child = None, None, None
    if stop is not None:
        stop.set()
    if child is not None:
        _kill(child)
    if thread is not None:
        thread.join(timeout=STOP_JOIN_S)


def _spawn(directory: str) -> subprocess.Popen | None:
    """The follower child. A seam: tests replace this instead of a real process."""
    try:
        return subprocess.Popen(
            [*_cli("wait", directory), "--timeout", str(int(WAIT_TIMEOUT_S)), "--follow"],
            cwd=directory or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None


def _kill(child: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        child.kill()
    with contextlib.suppress(Exception):
        child.wait(timeout=STOP_JOIN_S)


def _watch_loop(directory: str, on_wake: Callable[[dict], None], stop: threading.Event) -> None:
    while not stop.is_set():
        if not game_is_up(directory):
            # Nothing to watch yet: launching the follower now would only buy a
            # child that exits at once. Waiting for the game to appear costs one
            # stat call, and its cursor means the first lines are still ours.
            stop.wait(NO_GAME_DELAY_S)
            continue
        child = _spawn(directory)
        if child is None:
            stop.wait(NO_GAME_DELAY_S)
            continue
        with _watch.lock:
            _watch.child = child
        try:
            _read_events(child, on_wake, stop)
        finally:
            with _watch.lock:
                _watch.child = None
            _kill(child)
        if stop.is_set():
            return
        # The follower ended on its own. Exit 2 means "nothing to talk to" (the
        # game is not up): re-check slowly instead of spinning, and start
        # quickly once the game was really there. Either way no event is lost —
        # the game holds the follower's cursor, so a reconnect resumes where the
        # last session stopped.
        delay = NO_GAME_DELAY_S if child.returncode == 2 else RESTART_DELAY_S
        stop.wait(delay)


def _read_events(
    child: subprocess.Popen, on_wake: Callable[[dict], None], stop: threading.Event
) -> None:
    assert child.stdout is not None
    for line in child.stdout:
        if stop.is_set():
            return
        if len(line) > MAX_LINE:
            continue
        event = _parse(line)
        if event is not None:
            on_wake(event)


def _parse(line: str) -> dict | None:
    line = line.strip()
    if not line or not line.startswith("{"):
        return None  # the CLI's own chatter, not an event
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("kind") != "wake":
        return None
    return event


def wake_text(event: dict) -> str:
    """The note text for one player line — the agent reads this as the input."""
    who = str(event.get("from") or "player")
    message = str(event.get("message") or "").strip()
    return f"GhostWorld 的玩家（{who}）说：{message}"


# ── the per-turn tool ────────────────────────────────────────────────────────


def bound(cfg: Config) -> dict:
    """The `ghostworld` tool for one local-agent turn (spec §43)."""
    from fungi.agent import BoundTool  # noqa: PLC0415 (deferred: agent imports tools)

    directory = str(cfg.ghostworld_dir or "")

    def ghostworld(args: dict) -> str:
        # Call-time gate, like the screen tool: an off switch takes effect at
        # once, even inside a turn that started while it was on.
        if not enabled():
            return "ERROR: GhostWorld control is off in settings"
        action = str(args.get("action") or "").strip()
        if not action:
            return "ERROR: action is required"
        cmd = {k: v for k, v in args.items() if k != "action" and v not in (None, "")}
        return send_command({"cmd": action, **cmd}, directory)

    return {"ghostworld": BoundTool(schema=SCHEMA, fn=ghostworld)}


atexit.register(disarm)
