"""The run log: what this program tried, and what the other end answered.

Fungi ships as a windowed exe (`--noconsole`), so there is no terminal to scroll
back through and `sys.stdout`/`sys.stderr` are None. A release test that ends in
"连不上" used to leave nothing behind, while the one thing worth having is exactly
that: which address was tried, by which process, and what came back.

So: one file per day under `logs/`, beside config.json and data/ — the folder
the tester is already looking at. Outbound attempts and their outcomes go in,
plus whatever exception nobody caught, plus a banner of startup facts. The file
is append-only, so a tester can open it while the program still owns it, and
`logs/` keeps the last fortnight.

This is plumbing evidence, not the room transcript: it is what you read when
something did not connect.
"""

import logging
import os
import platform
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

LOGGER_NAME = "fungi"
FOLDER = "logs"
KEEP_DAYS = 14
THROTTLE_S = 60.0  # a poll that fails every second still gets one line a minute
MAX_ARG = 500  # a tool result or an stderr tail is quoted, never dumped whole

_logger = logging.getLogger(LOGGER_NAME)
# Importable without a file: not-yet-configured notes go nowhere instead of
# tripping logging's lastResort and spraying stderr at library users.
_logger.addHandler(logging.NullHandler())
_logger.propagate = False

_lock = threading.Lock()
_path: Path | None = None
_last: dict[str, float] = {}


def logs_dir() -> Path:
    """The folder the log lives in: `logs/` beside config.json (i.e. the exe's own)."""
    from .config import PROJECT_ROOT  # noqa: PLC0415 (deferred: config imports us back)

    return PROJECT_ROOT / FOLDER


def current_path() -> Path:
    """Where this run writes: one file per day, so a tester can hand over a day."""
    return _path or logs_dir() / f"fungi-{time.strftime('%Y%m%d')}.log"


def _logs_folder() -> Path:
    """`logs/` beside config.json, or the per-user folder when that is read-only.

    The fallback rule lives in config.writable_dir: the inbox depends on the very
    same decision (§49), and two copies of it would drift.
    """
    from .config import writable_dir  # noqa: PLC0415 (deferred: config imports us back)

    return writable_dir(logs_dir(), FOLDER)


def setup(path: Path | None = None, level: int = logging.INFO) -> Path:
    """Install the file handler (once) and return the file being written.

    Called from the entry points only; every other module just calls `note` /
    `problem`, so importing Fungi as a library, or running the tests, writes
    nothing. Passing `path` re-targets the log — that is what tests use.
    """
    global _path  # noqa: PLW0603 (one process, one log file)
    with _lock:
        if _path is not None and path is None:
            return _path
        folder = path.parent if path is not None else _logs_folder()
        target = path or folder / f"fungi-{time.strftime('%Y%m%d')}.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        for old in _logger.handlers:
            if not isinstance(old, logging.NullHandler):
                old.close()
        _logger.handlers = [handler]
        _logger.setLevel(level)
        _last.clear()
        _path = target
        if path is None:
            _prune(folder)
        _install_hooks()
        return target


def _prune(folder: Path, keep_days: int = KEEP_DAYS) -> None:
    cutoff = time.time() - keep_days * 86400
    for old in folder.glob("fungi-*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def note(message: str, *args: object) -> None:
    """One fact worth keeping: an address we reached, a child we started."""
    _logger.info(message, *args)


def say(text: str) -> None:
    """A line meant for a person: the console when there is one, the log when not.

    `sys.stdout` is None in the packaged windowed exe, where `print` would raise
    instead of saying anything.
    """
    if sys.stdout is None:
        note("%s", text)
    else:
        print(text)


def problem(message: str, *args: object) -> None:
    """Something failed. The release tester's log is largely made of these."""
    _logger.warning(message, *args)


def warn_once(key: str, message: str, *args: object, interval: float = THROTTLE_S) -> bool:
    """Log a repeating failure once per `interval`, keyed by what failed.

    A room polls its hub every second and a dead hub fails every time; without
    this the file fills with one error and nothing else. Returns whether this
    call is the one that got written.
    """
    now = time.monotonic()
    with _lock:
        if now - _last.get(key, 0.0) < interval:
            return False
        _last[key] = now
    _logger.warning(message, *args)
    return True


def forget(key: str) -> None:
    """Drop a throttle so the next failure of that key is written at once."""
    with _lock:
        _last.pop(key, None)


def short(text: object, limit: int = MAX_ARG) -> str:
    """One line of somebody else's output, safe to paste into the log."""
    flat = " ".join(str(text).split())
    return flat[:limit]


def _source_of(name: str) -> str:
    """Where a config field's value came from: the generic env var or the file."""
    return f"env:{name}" if os.environ.get(name) else "config.json"


def environment(mode: str, argv: list[str] | None = None) -> None:
    """The banner a diagnosis starts from: one run, one block of facts.

    Never the api key — whether one is set is the fact; which one it is never is.
    Failure is contained on purpose: a program must not refuse to start because
    its log could not describe the start.
    """
    try:
        _banner(mode, argv)
    except Exception as exc:
        problem("could not write the startup banner: %s", exc)


def _banner(mode: str, argv: list[str] | None) -> None:
    from .config import CONFIG_PATH, PROJECT_ROOT, load_config, local_version  # noqa: PLC0415

    note(
        "--- run %s pid=%d mode=%s log=%s",
        time.strftime("%Y-%m-%d %H:%M:%S"),
        os.getpid(),
        mode,
        current_path(),
    )
    note(
        "fungi %s | python %s | %s",
        local_version(),
        platform.python_version(),
        platform.platform(),
    )
    note("frozen=%s root=%s", bool(getattr(sys, "frozen", False)), PROJECT_ROOT)
    note("argv=%s", argv if argv is not None else sys.argv)
    note("config=%s exists=%s", CONFIG_PATH, CONFIG_PATH.is_file())
    try:
        cfg = load_config()
    except Exception as exc:  # a broken config must not break the banner
        problem("config could not be loaded: %s", exc)
        return
    note("model=%s endpoint=%s api_key=%s", cfg.model, cfg.endpoint, bool(cfg.api_key))
    # Where each of the three came from. `load_config` lets three generic names
    # override the file, and a key belongs to its endpoint: on 2026-09-25 this
    # box handed a Xiaomi MiMo key (OPENAI_API_KEY, set machine-wide for other
    # agents) to api.deepseek.com and got a 401 whose message named a key the
    # config file did not contain. The banner said only `api_key=True`.
    key_src = _source_of("OPENAI_API_KEY")
    note(
        "来源: model=%s endpoint=%s api_key=%s",
        _source_of("OPENAI_MODEL"),
        _source_of("OPENAI_ENDPOINT"),
        key_src,
    )
    if key_src != "config.json" and _source_of("OPENAI_ENDPOINT") == "config.json":
        problem(
            "OPENAI_API_KEY 环境变量压过了 config.json 里的钥匙，但端点还是 config.json 的（%s）："
            "这把钥匙很可能不属于这个端点 —— 若报 401/403，先看这条。"
            "要用配置里那把钥匙：启动前清掉 OPENAI_API_KEY；真要换端点，就把 OPENAI_ENDPOINT 一起设上。",
            cfg.endpoint,
        )
    note("ghostworld=%s dir=%r", cfg.ghostworld, cfg.ghostworld_dir)


def _install_hooks() -> None:
    """Uncaught exceptions land in the file.

    This is the single most valuable line the log holds: a windowed exe dies in
    silence, and PyQt5 routes a slot's exception through `sys.excepthook` before
    it calls `qFatal`, so the traceback is still available here.
    """
    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def _hook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        _console_hook(exc_type, exc, tb)
        return
    _logger.critical(
        "uncaught exception\n%s", "".join(traceback.format_exception(exc_type, exc, tb))
    )
    _console_hook(exc_type, exc, tb)


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    where = args.thread.name if args.thread is not None else "thread"
    _logger.critical(
        "uncaught exception in %s\n%s",
        where,
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
    )
    if sys.stderr is not None:
        threading.__excepthook__(args)


def _console_hook(exc_type, exc, tb) -> None:
    """Keep the normal last-words on stderr — when there is a stderr to write to."""
    if sys.stderr is not None:
        sys.__excepthook__(exc_type, exc, tb)


def open_folder() -> Path:
    """Reveal the log folder (tray menu / help page); returns it for the caller."""
    folder = current_path().parent
    webbrowser.open(folder.as_uri())
    return folder
