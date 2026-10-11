"""POST /open: what a file card's two actions turn into on the host (§73).

The page lives in a browser and cannot launch anything, so the click travels
here and the machine that holds the file does the double-click. Both launchers
are patched in this module — a test that let them through would open real
windows on whatever machine runs the suite.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from fungi import server as webui_server
from fungi.config import PROJECT_ROOT


@pytest.fixture()
def base():
    """A WebUI server on an ephemeral port (loopback passes the gate)."""
    server = webui_server.make_webui_server(0, webui_server.WebUIRuntime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def launched(monkeypatch):
    """Every dispatch is recorded, never performed."""
    calls: list[tuple[str, str]] = []

    def _open(path):
        calls.append(("open", str(path)))
        return {"raised": True}

    def _reveal(path):
        calls.append(("reveal", str(path)))
        return {"raised": False}

    monkeypatch.setattr(webui_server, "open_with_default", _open)
    monkeypatch.setattr(webui_server, "reveal_in_folder", _reveal)
    return calls


def _post(base: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + "/open",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _a_file(tmp_path) -> str:
    src = tmp_path / "note.txt"
    src.write_text("x", encoding="utf-8")
    return str(src)


def test_open_hands_the_path_to_the_shell_association(base, launched, tmp_path):
    """`open` is the double-click: the shell's own association decides what runs."""
    src = _a_file(tmp_path)
    status, body = _post(base, {"path": src, "action": "open"})
    assert status == 200
    assert body == {"ok": True, "action": "open", "raised": True}
    assert launched == [("open", src)]


def test_reveal_asks_explorer_to_select_the_file(base, launched, tmp_path):
    """`reveal` is the folder with the file in it — and `raised` is passed on
    honestly, because a window that only reached the taskbar must not be
    reported as if it came up (§73)."""
    src = _a_file(tmp_path)
    status, body = _post(base, {"path": src, "action": "reveal"})
    assert status == 200
    assert body == {"ok": True, "action": "reveal", "raised": False}
    assert launched == [("reveal", src)]


def test_open_is_the_action_when_none_is_given(base, launched, tmp_path):
    src = _a_file(tmp_path)
    status, _body = _post(base, {"path": src})
    assert status == 200
    assert launched == [("open", src)]


def test_a_missing_path_is_a_400_and_launches_nothing(base, launched):
    status, body = _post(base, {"action": "open"})
    assert status == 400
    assert "path" in body["error"]
    assert launched == []


def test_an_unknown_action_is_a_400_and_launches_nothing(base, launched, tmp_path):
    src = _a_file(tmp_path)
    status, body = _post(base, {"path": src, "action": "delete"})
    assert status == 400
    assert "delete" in body["error"]
    assert launched == []


def test_a_path_that_is_not_there_is_a_404_and_launches_nothing(base, launched, tmp_path):
    """A card outlives its file: the stale case must be answered, not attempted."""
    gone = str(tmp_path / "gone.bin")
    status, body = _post(base, {"path": gone, "action": "open"})
    assert status == 404
    assert gone in body["error"]
    assert launched == []


def test_a_relative_path_resolves_where_download_does(base, launched):
    """One rule for the two path-taking routes, so neither depends on the cwd."""
    status, _body = _post(base, {"path": "web/common.js", "action": "open"})
    assert status == 200
    assert launched == [("open", str(PROJECT_ROOT / "web" / "common.js"))]
