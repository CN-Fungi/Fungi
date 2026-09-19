"""The file-transfer session (§53): one place both devices drop files into.

The phone picks a file and it lands in the computer's inbox; the computer hands
the phone a path and the phone pulls it back. Neither needs a conversation for
that — a turn would cost a model call and could rewrite the path — so this
session appends rows and runs nothing.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from fungi import server as webui
from fungi import session as session_mod
from fungi.config import Config


@pytest.fixture()
def shuttle_env(tmp_path, monkeypatch):
    """A WebUI server whose sessions and inbox stay in tmp."""
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(
        webui,
        "load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox)),
    )
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path / "sessions")

    class _NoTurns(webui.WebUIRuntime):
        """Any attempt to run a model is a failure, not a slow test."""

        def build_agent(self, sink, should_abort):
            raise AssertionError("the transfer session must not run a turn")

    srv = webui.make_webui_server(0, _NoTurns())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", inbox
    finally:
        srv.shutdown()
        srv.server_close()


def _get(base: str, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def _send(base: str, text: str) -> dict:
    body = json.dumps({"sessionId": webui.SHUTTLE_ID, "message": text}).encode()
    req = urllib.request.Request(
        base + "/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        events = [json.loads(line) for line in resp.read().decode().splitlines() if line]
    assert events[-1]["type"] == "done", events
    return events


def _rows(base: str) -> list[dict]:
    _status, data = _get(base, "/session?id=" + webui.SHUTTLE_ID)
    return data["messages"]


def _upload(base: str, name: str, body: bytes) -> dict:
    req = urllib.request.Request(
        base + "/upload",
        data=body,
        headers={"X-Fungi-Filename": urllib.parse.quote(name)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_the_session_exists_and_is_listed_first(shuttle_env):
    """It is the one session the phone needs to find without reading: first in
    the list, and flagged so the shells know to keep it fresh."""
    base, _inbox = shuttle_env
    _status, data = _get(base, "/sessions")
    sessions = data["sessions"]
    assert sessions, "the transfer session was never made"
    assert sessions[0]["id"] == webui.SHUTTLE_ID
    assert sessions[0]["title"] == webui.SHUTTLE_TITLE
    assert sessions[0]["shuttle"] is True
    assert not any(s.get("shuttle") for s in sessions[1:])


def test_sending_in_it_records_a_row_and_runs_no_turn(shuttle_env):
    """No model, no tokens, no rewrite of what the user typed — the row is the
    file's address, and it has to arrive exactly as it was sent."""
    base, _inbox = shuttle_env
    path = r"C:\Users\someone\Desktop\report.pdf"
    events = _send(base, path)
    assert [e["type"] for e in events] == ["sessionId", "done"], events
    assert [m["content"] for m in _rows(base)] == [path]
    assert _rows(base)[0]["ts"] > time.time() - 60


def test_a_phone_upload_writes_its_own_row(shuttle_env):
    """The other direction, and the whole point of the session: what the phone
    sent lands, and both devices can see where it went."""
    base, inbox = shuttle_env
    out = _upload(base, "拾荒集.zip", b"zipzip")
    assert out["done"] is True
    rows = _rows(base)
    assert len(rows) == 1, rows
    assert rows[0]["content"].startswith("手机上传：拾荒集.zip（6 B）")
    assert str(inbox / "拾荒集.zip") in rows[0]["content"]
    assert rows[0]["content"].count("\n") == 1, "the path belongs on its own line"


def test_a_phone_upload_that_never_landed_writes_nothing(shuttle_env):
    """Half an upload is not a transfer: no row while the file is not whole."""
    base, _inbox = shuttle_env
    query = urllib.parse.urlencode({"sid": "s1", "offset": 0, "size": 4096})  # 1 KiB of 4 KiB
    req = urllib.request.Request(
        base + "/upload?" + query,
        data=b"x" * 1024,
        headers={"X-Fungi-Filename": "half.bin"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert json.loads(resp.read())["done"] is False
    assert _rows(base) == []
    _status, data = _get(base, "/sessions")
    assert data["sessions"][0]["msgCount"] == 0


def test_a_broken_store_does_not_fail_the_upload(shuttle_env, monkeypatch):
    """The file is on disk or it is not; the session row is a courtesy."""
    base, inbox = shuttle_env

    def _boom(_runtime, _text):
        raise OSError("store on fire")

    monkeypatch.setattr(webui, "shuttle_post", _boom)
    out = _upload(base, "note.txt", b"fine")
    assert out["done"] is True and (inbox / "note.txt").read_bytes() == b"fine"


def test_an_unknown_session_still_404s(shuttle_env):
    """The shuttle is a special id, not a wildcard: everything else behaves."""
    base, _inbox = shuttle_env
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(base, "/session?id=nope")
    assert err.value.code == 404
