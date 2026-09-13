"""Entry point.

Single-host:  python -m fungi [query]           console single-shot
              python -m fungi --web [--port P]  WebUI server (browser opens)
Room mode:    python -m fungi --server [--name N] [--token T] [--port P] [--data DIR]
              python -m fungi --join URL --token T [--name N]
GUI launcher: python -m fungi --gui   (host / join / model config)

Room mode starts tray-only (PyQt5); the WebUI opens from the tray.
"""

import argparse
import os
import secrets
import socket
import sys
from pathlib import Path

from fungi.config import PROJECT_ROOT, local_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fungi",
        description="Fungi — LAN multi-host Orchestrator network on the TriLayer agent harness",
    )
    parser.add_argument("query", nargs="*", help="single-shot query (console mode)")
    parser.add_argument("--version", action="version", version=f"fungi {local_version()}")
    parser.add_argument("--web", action="store_true", help="single-host WebUI server mode")
    parser.add_argument(
        "--gui", action="store_true", help="graphical launcher (host / join / config)"
    )
    parser.add_argument("--server", action="store_true", help="room mode: host the hub (tray)")
    parser.add_argument("--join", metavar="URL", help="room mode: join the hub at URL (tray)")
    parser.add_argument("--token", help="room token (join requires the server's token)")
    parser.add_argument("--name", help="host name (default: hostname)")
    parser.add_argument(
        "--display", help="nickname shown to friends (default: config.json display or --name)"
    )
    parser.add_argument("--port", type=int, help="hub port (server) / WebUI port (--web)")
    parser.add_argument("--data", metavar="DIR", help="server data directory (default: ./data)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.query:
        from fungi.cli import run_single_shot  # noqa: PLC0415 (lazy: keep --help dependency-free)

        return run_single_shot(" ".join(args.query))
    if args.web:
        from fungi.server import run_server  # noqa: PLC0415

        run_server(args.port)
        return 0
    if args.gui:
        from fungi.gui import run_gui  # noqa: PLC0415

        return run_gui()
    if args.server or args.join:
        return run_room(args)
    build_parser().print_help()
    return 0


def _lan_ip() -> str:
    """Best-effort LAN address for the printed join command."""
    from fungi.server import lan_ip  # noqa: PLC0415 (lazy: server import is heavy)

    return lan_ip()


def run_room(args: argparse.Namespace) -> int:
    from PyQt5.QtCore import QSharedMemory, Qt  # noqa: PLC0415 (Qt only in tray mode)
    from PyQt5.QtWidgets import QApplication  # noqa: PLC0415

    from fungi.config import load_config  # noqa: PLC0415
    from fungi.events import ConsoleSink  # noqa: PLC0415
    from fungi.protocol import BAD_NAME_MSG, valid_host_name  # noqa: PLC0415
    from fungi.tray import TrayController  # noqa: PLC0415

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Fungi")
    app.setQuitOnLastWindowClosed(False)  # tray-resident: closing nothing must not quit

    # single instance: a second launch just exits
    shared = QSharedMemory("FungiRoomSingleton")
    if shared.attach() or not shared.create(1):
        print("Fungi is already running in this room (tray resident).")
        return 0

    cfg = load_config()
    host = args.name or socket.gethostname().split(".")[0]
    if not valid_host_name(host):
        # Reject before any network work: the name rides envelope addresses,
        # HTTP query strings, and data/ file names (emoji names break ASCII
        # URL encoding on a peer's client — 2026-09-04 real-machine finding).
        print(f"Invalid host name {host!r}: {BAD_NAME_MSG}")
        return 2
    # display is presentation-only: never validated as a host name, never
    # allowed to touch addresses/URLs/file names.
    display = (args.display or cfg.display or "").strip()
    sink = ConsoleSink()

    if args.server:
        from fungi.room import RoomServer  # noqa: PLC0415

        token = args.token or secrets.token_urlsafe(12)
        data_root = Path(args.data) if args.data else PROJECT_ROOT / "data"
        room = RoomServer(host, cfg, sink, token, data_root, display=display, port=args.port or 0)
        data_dir_for_tray: Path | None = data_root
    else:
        from fungi.room import RoomClient  # noqa: PLC0415

        if not args.join or not args.token:
            print("--join requires --token (ask the server host for the join command)")
            return 2
        room = RoomClient(host, cfg, sink, args.join, args.token, display=display)
        data_dir_for_tray = None

    tray = TrayController(on_open_webui=room.open_webui, data_root=data_dir_for_tray)
    room.start()
    tray.show()

    if args.server:
        port = args.port or room.hub.port
        print(
            f"Fungi server: name={host} data={data_dir_for_tray}"
            + (f" display={display}" if display else "")
        )
        print(f"Join command: python -m fungi --join http://{_lan_ip()}:{port} --token {token}")
    else:
        print(
            f"Fungi client: name={host} joined {args.join}"
            + (f" display={display}" if display else "")
        )

    if not args.server or not os.environ.get("FUNGI_SELFTEST"):
        tray.notify(
            "Fungi 已启动",
            f"host {display or host}" + (" · hub 已就绪" if args.server else " · 已加入房间"),
        )

    if args.server and os.environ.get("FUNGI_SELFTEST"):
        from fungi.room import run_selftest  # noqa: PLC0415

        run_selftest(
            room,
            quit_fn=app.exit,
            fail_fn=lambda msg: (print(f"FUNGI SELFTEST FAIL: {msg}", flush=True), app.exit(1)),
        )

    app.aboutToQuit.connect(room.stop)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
