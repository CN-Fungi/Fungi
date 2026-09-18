"""Shared fixtures: a real hub room with a HubClient-shaped urllib client."""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from fungi.hub.app import Hub
from fungi.protocol import Envelope, deserialize


class Client:
    """HubClient-compatible surface over raw urllib (no dependency on fungi.hub.client).

    Raw methods (post/get/poll_raw) return (status_code, body_dict) for API-level
    assertions; the HubClient-shaped methods (send/poll/fs/join) return parsed payloads.
    """

    def __init__(self, base: str, token: str, host: str):
        self.base = base.rstrip("/")
        self.token = token
        self.host = host

    # ── raw HTTP ──

    def _json(self, method: str, path: str, obj: dict) -> tuple[int, dict]:
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post(self, path: str, obj: dict) -> tuple[int, dict]:
        return self._json("POST", path, obj)

    def delete(self, path: str, obj: dict) -> tuple[int, dict]:
        return self._json("DELETE", path, obj)

    def discard_transfer(self, transfer_id: str) -> dict:
        """Receiver-side drop of the hub's staged copy — HubClient-compatible,
        token in the body (that is where the real client puts it)."""
        return self.delete(
            "/api/transfer", {"token": self.token, "id": transfer_id, "host": self.host}
        )[1]

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=40) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def poll_raw(self, host: str, after: int = 0, timeout: float = 0.0) -> tuple[int, dict]:
        return self.get(f"/api/poll?host={host}&token={self.token}&after={after}&timeout={timeout}")

    # ── HubClient-compatible surface ──

    def join(self) -> dict:
        return self.post("/api/join", {"name": self.host, "token": self.token})[1]

    def send(self, env) -> dict:
        return self.post("/api/send", {"token": self.token, "envelope": env.serialize()})[1]

    def poll(self, after: int, timeout: float) -> tuple[list[Envelope], int]:
        _code, out = self.poll_raw(self.host, after, timeout)
        return [deserialize(m) for m in out["messages"]], out["cursor"]

    def fs(self, op: str, path: str, **kw) -> dict:
        body = {"token": self.token, "host": self.host, "path": path, **kw}
        return self.post(f"/api/fs/{op}", body)[1]

    def upload_transfer(self, path: str, name: str, to_host: str, progress=None) -> dict:
        """Raw-bytes upload, HubClient.upload_transfer compatible."""
        q = urllib.parse.urlencode(
            {"token": self.token, "host": self.host, "to": to_host, "name": name}
        )
        data = Path(path).read_bytes()
        if progress is not None:
            progress(len(data), len(data))  # one shot: this double does not stream
        req = urllib.request.Request(
            self.base + f"/api/transfer/upload?{q}",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())

    def download_transfer(self, transfer_id: str, dest) -> None:
        url = f"{self.base}/api/transfer?id={transfer_id}&host={self.host}&token={self.token}"
        with urllib.request.urlopen(url, timeout=60) as resp, Path(dest).open("wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)

    def poll_env(
        self, host: str, after: int = 0, timeout: float = 0.0
    ) -> tuple[list[Envelope], int]:
        """Poll another host's buffer (test-side observation)."""
        _code, out = self.poll_raw(host, after, timeout)
        return [deserialize(m) for m in out["messages"]], out["cursor"]


@pytest.fixture()
def room(tmp_path):
    hub = Hub("srv", "room-token", tmp_path)
    hub.start()
    names = ("alpha", "beta", "srv")
    clients = {n: Client(f"http://127.0.0.1:{hub.port}", "room-token", n) for n in names}
    yield hub, clients
    hub.stop()


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole pytest session.

    A module-scoped app gets released when its module's fixtures finalize;
    destroying a QApplication mid-run takes qfluentwidgets' qconfig singleton
    with it and the next module that builds fluent widgets dies with
    "wrapped C/C++ object of type QConfig has been deleted" (or crashes).
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session", autouse=True)
def _hermetic_gui_settings():
    """Keep the real QSettings out of every test run.

    The GUI remembers the user's identity there — token, nickname, host name,
    LAN ip — and on Windows that is `HKCU\\Software\\Offblink\\FungiGUI`. The
    join tests used to write their dummy values into it and then `remove()` the
    keys "so they don't leak", which deleted the user's own saved token and
    nickname every time the suite ran (2026-09-10 user report: 每次进去都是新的).

    The redirection is by app name, not by format: on Windows
    `QSettings.setDefaultFormat(IniFormat)` does not move the `QSettings(org,
    app)` constructor off the registry (verified), while a test-only app name
    lands in its own key. Session-scoped because the module-scoped `window`
    builds its two QSettings once, before any function-scoped patch could apply.

    Qt is a GUI-only extra: without it (CI, headless runs) there are no GUI
    tests to sandbox, and importing the pages here must not take the whole
    suite down (it did — 404 errors on the first CI run of this guard).
    """
    try:
        from PyQt5.QtCore import QSettings

        from fungi.gui import host as host_mod
        from fungi.gui import join as join_mod
    except ImportError:
        yield None
        return

    real = join_mod.SETTINGS_APP
    sandbox = f"{real}-test"
    join_mod.SETTINGS_APP = host_mod.SETTINGS_APP = sandbox
    try:
        yield sandbox
    finally:
        join_mod.SETTINGS_APP = host_mod.SETTINGS_APP = real
        QSettings(join_mod.SETTINGS_ORG, sandbox).clear()


@pytest.fixture(autouse=True)
def _never_write_the_user_config(tmp_path, monkeypatch):
    """Point the one config path at a throwaway copy.

    2026-09-10: the GUI tests patched `gui.load_config`/`gui.save_config`, which
    the split `fungi/gui/` package no longer reads — so a "save the courier
    memory" test wrote its own string into the user's real long-term memory
    (and the settings test wrote its key). Per-test patching is one rename away
    from missing again: redirect the path itself, here, for every test.
    """
    from fungi import config as config_mod

    # A subdirectory, not tmp_path itself: tests that treat tmp_path as a data
    # directory (test_session's SESSIONS_DIR) glob it for *.json.
    target = tmp_path / "user-config" / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if config_mod.CONFIG_PATH.is_file():
        target.write_text(config_mod.CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", target)


@pytest.fixture(autouse=True)
def _fast_room_clocks(monkeypatch):
    """Rooms poll on production intervals; tests wait on those clocks for no
    reason (a courier wake is a 3s mail poll, a roster diff is a 10s heartbeat).
    Turn the clocks down so the only wait in a test is its own readiness check.

    Every one of these is a module constant read at each loop turn, so the patch
    reaches the running threads. Semantics are unchanged — only how often the
    room looks.
    """
    from fungi import room as room_mod
    from fungi.hub import app as hub_app

    monkeypatch.setattr(room_mod, "MONITOR_INTERVAL_S", 0.05)
    monkeypatch.setattr(room_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(room_mod.RoomBase, "MAIL_POLL_S", 0.05)
    monkeypatch.setattr(hub_app, "REAP_INTERVAL", 0.05)
