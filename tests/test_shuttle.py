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


def test_a_phone_upload_row_carries_its_card(shuttle_env):
    """§56: the row is a record, not prose — the page renders a card from these
    fields, so nothing has to be parsed out of a sentence."""
    base, inbox = shuttle_env
    _upload(base, "拾荒集.zip", b"zipzip")
    card = _rows(base)[0]["file"]
    assert card == {
        "name": "拾荒集.zip",
        "size": 6,
        "path": str(inbox / "拾荒集.zip"),
        "direction": "phone",
    }


def test_a_message_naming_a_real_file_becomes_a_card(shuttle_env, tmp_path):
    """The computer's side of §53: send a path, and the phone gets a card it can
    act on. A path that is not a file stays a plain message — that is a typo,
    not a transfer."""
    base, _inbox = shuttle_env
    src = tmp_path / "handover.bin"
    src.write_bytes(b"x" * 2048)
    _send(base, f"给手机 {src} 收")
    card = _rows(base)[0]["file"]
    assert (card["name"], card["size"], card["direction"]) == ("handover.bin", 2048, "computer")
    assert card["path"] == str(src)

    _send(base, r"C:\nope\missing.bin")
    assert "file" not in _rows(base)[1]


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


def test_after_returns_only_what_is_new(shuttle_env):
    """A page watching for transfers gets the rows it has not seen (§55): that is
    what lets it append instead of repainting the whole transcript."""
    base, _inbox = shuttle_env
    _upload(base, "one.bin", b"1")
    _status, first = _get(base, "/session?id=" + webui.SHUTTLE_ID)
    assert len(first["messages"]) == 1 and "total" not in first

    _upload(base, "two.bin", b"22")
    _status, tail = _get(base, f"/session?id={webui.SHUTTLE_ID}&after=1")
    assert tail["total"] == 2 and len(tail["messages"]) == 1
    assert "two.bin" in tail["messages"][0]["content"]
    _status, nothing = _get(base, f"/session?id={webui.SHUTTLE_ID}&after=2")
    assert nothing["messages"] == [] and nothing["total"] == 2
    _status, past = _get(base, f"/session?id={webui.SHUTTLE_ID}&after=99")
    assert past["messages"] == [] and past["total"] == 2


def test_it_cannot_be_renamed(shuttle_env):
    """Its name is its identity (§54): a save that carries another title keeps
    the real one — the UI hides the ✎, and the server does not need the UI."""
    base, _inbox = shuttle_env
    _send(base, "开始用它")
    body = json.dumps({"id": webui.SHUTTLE_ID, "title": "随便改个名"}).encode()
    req = urllib.request.Request(
        base + "/save", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert json.loads(resp.read())["ok"] is True
    _status, data = _get(base, "/sessions")
    entry = next(item for item in data["sessions"] if item["id"] == webui.SHUTTLE_ID)
    assert entry["title"] == webui.SHUTTLE_TITLE
    assert [m["content"] for m in _rows(base)] == ["开始用它"], "the rows survived the save"


def test_it_cannot_be_deleted(shuttle_env):
    """The channel between the two devices is not something to lose by a click."""
    base, _inbox = shuttle_env
    _send(base, "别删我")
    req = urllib.request.Request(base + "/session?id=" + webui.SHUTTLE_ID, method="DELETE")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 400
    assert [m["content"] for m in _rows(base)] == ["别删我"]


def test_an_unknown_session_still_404s(shuttle_env):
    """The shuttle is a special id, not a wildcard: everything else behaves."""
    base, _inbox = shuttle_env
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(base, "/session?id=nope")
    assert err.value.code == 404
