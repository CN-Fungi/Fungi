"""LAN probes and room assembly: discovery sweep, port scan, room start."""

import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .. import config as config_mod
from .. import runlog
from ..config import (
    PROJECT_ROOT,
)
from ..protocol import valid_host_name
from .const import GUI_PORT, PORT_SCAN_LIMIT, PROBE_TIMEOUT, SWEEP_TIMEOUT


def lan_ip() -> str:
    """Best-effort LAN address (UDP connect trick: no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def default_host_name() -> str:
    return socket.gethostname().split(".")[0]


def _wire_candidate(text: str) -> str | None:
    """ASCII-safe wire name derived from arbitrary input, or None.

    The wire name rides envelope addresses, URLs, and file names, so it must
    stay ASCII (an emoji name breaks http.client's ASCII URL selector); the
    nickname carries everything the user actually wants to be called."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-_")[:32]
    return cleaned if valid_host_name(cleaned) else None


def _resolve_wire_name(host: str, nick: str) -> tuple[str, str]:
    """Self-heal an invalid wire name instead of rejecting it: sanitize what is
    sanitizable, else fall back to the machine name (or "pc"). The original
    input becomes the nickname when that field is empty. Returns (wire, nick)."""
    if valid_host_name(host):
        return host, nick
    wire = _wire_candidate(host) or _wire_candidate(default_host_name()) or "pc"
    return wire, (nick or host)


def _port_bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def find_free_port(start: int = GUI_PORT, limit: int = PORT_SCAN_LIMIT) -> int:
    """Face-style upward scan: first bindable port from `start`, else OSError."""
    for port in range(start, start + limit):
        if _port_bindable(port):
            return port
    raise OSError(f"no free port in [{start}, {start + limit})")


def _port_open(ip: str, port: int, timeout: float = SWEEP_TIMEOUT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0


def local_subnet_hosts() -> list[str]:
    """All /24 neighbors of the current LAN IP (own IP included: same-machine
    rooms are reachable through the LAN address too)."""
    prefix = lan_ip().rsplit(".", 1)[0]
    return [f"{prefix}.{i}" for i in range(1, 255)]


def _room_accepts(ip: str, port: int, token: str) -> bool:
    url = f"http://{ip}:{port}/api/peers?token={urllib.request.quote(token)}&host=probe"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
            return resp.status == 200  # valid token and "probe" listed: our room
    except urllib.error.HTTPError as exc:
        return exc.code == 404  # token OK, host unknown: our room
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def discover_room(token: str) -> tuple[str, int] | None:
    """Find the LAN room that accepts `token` — no IP input needed.

    Same subnet is a LAN given, so sweep the /24 one port at a time across all
    hosts (parallel): the common case (room at the 8899 anchor) lands in the
    first round instead of walking a 32-port window per host."""
    hosts = local_subnet_hosts()
    runlog.note(
        "sweeping %s.* ports %d..%d for a room",
        lan_ip().rsplit(".", 1)[0],
        GUI_PORT,
        GUI_PORT + PORT_SCAN_LIMIT - 1,
    )
    with ThreadPoolExecutor(max_workers=128) as pool:
        for offset in range(PORT_SCAN_LIMIT):
            port = GUI_PORT + offset
            for ip, ok in zip(
                hosts, pool.map(lambda h, p=port: _port_open(h, p), hosts), strict=True
            ):
                if ok and _room_accepts(ip, port, token):
                    runlog.note("room found: %s:%d accepted the token", ip, port)
                    return ip, port
    runlog.problem(
        "no room answered on this subnet (%s.*, ports %d..%d) — the host may not have "
        "started one, the token may differ, or the two machines are not on one LAN",
        lan_ip().rsplit(".", 1)[0],
        GUI_PORT,
        GUI_PORT + PORT_SCAN_LIMIT - 1,
    )
    return None


def probe_room_port(ip: str, token: str, start: int = GUI_PORT, limit: int = PORT_SCAN_LIMIT):
    """Scan upward for a Fungi hub that accepts `token`; returns its port or None.

    Distinguish via /api/peers: 404 = token accepted (our room), 403 = a hub
    with a different token (skip), refused/timeout = nothing there (skip).
    """
    for port in range(start, start + limit):
        if not _port_open(ip, port):
            continue
        url = f"http://{ip}:{port}/api/peers?token={urllib.request.quote(token)}&host=probe"
        try:
            with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
                if resp.status == 200:  # valid token and "probe" listed: our room
                    runlog.note("room found: %s:%d accepted the token", ip, port)
                    return port
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # token OK, host "probe" just unknown: our room
                runlog.note("room found: %s:%d accepted the token", ip, port)
                return port
            runlog.problem(
                "%s:%d answered HTTP %d — wrong token, or another room", ip, port, exc.code
            )
            continue  # 403: different room / wrong token
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            runlog.problem("%s:%d answered but not as a hub: %s", ip, port, exc)
            continue
    runlog.problem(
        "no room at %s (ports %d..%d): nothing listening, or the token differs",
        ip,
        start,
        start + limit - 1,
    )
    return None


def start_server_room(host: str, display: str, token: str, port: int):
    """Host the room inside this GUI process: hub + clones run on threads."""
    from ..events import ConsoleSink  # noqa: PLC0415 (Qt-free, cheap)
    from ..room import RoomServer  # noqa: PLC0415

    runlog.note("hosting a room as %r on port %d (LAN %s)", host, port, lan_ip())
    room = RoomServer(
        host,
        config_mod.load_config(),
        ConsoleSink(),
        token,
        PROJECT_ROOT / "data",
        display=display,
        port=port,
    )
    room.start()
    return room


def start_client_room(host: str, display: str, url: str, token: str):
    """Join a room inside this GUI process (poller + clones on threads)."""
    from ..events import ConsoleSink  # noqa: PLC0415
    from ..room import RoomClient  # noqa: PLC0415

    runlog.note("joining the room at %s as %r", url, host)
    room = RoomClient(host, config_mod.load_config(), ConsoleSink(), url, token, display=display)
    room.start()
    runlog.note("joined %s", url)
    return room


_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # token rides URLs / join commands


def _valid_token(token: str) -> bool:
    return _TOKEN_RE.fullmatch(token) is not None
