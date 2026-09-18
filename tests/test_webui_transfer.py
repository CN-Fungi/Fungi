"""Send-file progress modal: the real page, real rooms, real bytes.

The page never sees the bytes, so a bar can only be honest if the SERVER counts
them (the browser mints a job id, POST /comm-send carries it, GET
/transfer-progress reads it back — fungi/xfer.py). These run the whole flow:
desktop (one hop) and phone (browser -> this host, then this host -> the peer),
and read what the page actually painted.

Skips (never fails) where playwright is missing, so CI without a browser stays
green.
"""

import contextlib
import time
from pathlib import Path

import pytest

from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomClient, RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from fungi import server as webui_server  # noqa: E402  (after the skip guard)

CFG = Config(api_key="k", endpoint="e", model="m")


class SilentLLM:
    """No courier chatter: these tests send files, they do not converse."""

    def __call__(self, _messages, _tool_defs):
        return LLMResult(content="<<SILENT>>")


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@contextlib.contextmanager
def _rooms(tmp_path, cfg=CFG):
    """A host room and a joiner, both real: the friend view needs a peer."""
    server = RoomServer(
        "alpha",
        cfg,
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=SilentLLM(),
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta",
            cfg,
            NullSink(),
            f"http://127.0.0.1:{server.hub.port}",
            "tok",
            llm=SilentLLM(),
            sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(lambda: "beta" in server.hub.roster.peers("alpha")), "peer never joined"
            yield server, client
        finally:
            client.stop()
    finally:
        server.stop()


@pytest.fixture(scope="module")
def rooms(tmp_path_factory):
    with _rooms(tmp_path_factory.mktemp("xfer-rooms")) as pair:
        yield pair


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # any launch failure means "no browser here"
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@contextlib.contextmanager
def _page(browser, room, path="/"):
    url = f"{room.open_webui(False)}{path}?t={WEBUI_TOKEN}"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_function("() => typeof Xfer === 'object'")
    page.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    try:
        yield page
    finally:
        ctx.close()


@pytest.fixture()
def page(browser, rooms):
    server, _client = rooms
    with _page(browser, server) as pg:
        yield pg


@pytest.fixture()
def mobile_page(browser, rooms):
    server, _client = rooms
    with _page(browser, server, path="/m") as pg:
        yield pg


# Every paint of the modal, recorded: the flow can finish in a few hundred
# milliseconds, so sampling it from outside would race the page.
XFER_WATCH = """
() => {
  const ov = document.getElementById('xfer-overlay');
  window.__xferLog = [];
  const snap = () => window.__xferLog.push({
    cls: ov.className,
    job: ov.dataset.job || '',
    title: document.getElementById('xfer-title').textContent,
    steps: Array.from(document.querySelectorAll('#xfer-steps .xf-step')).map(s => ({
      lab: s.querySelector('.xf-lab').textContent,
      note: s.querySelector('.xf-note').textContent,
      width: s.querySelector('.xf-bar > i').style.width,
      cls: s.className,
    })),
  });
  new MutationObserver(snap).observe(ov, {attributes: true, childList: true, subtree: true, characterData: true});
  return true;
}
"""


def _log(page) -> list:
    return page.evaluate("() => window.__xferLog || []")


def _labels(snapshot: dict) -> list:
    return [s["lab"] for s in snapshot["steps"]]


def _job_states(snapshot: dict) -> list:
    return [s["cls"] for s in snapshot["steps"]]


def test_the_modal_renders_one_labelled_step_per_hop(page):
    """The bar itself: label, byte/percent note, bar width, done state."""
    opened = page.evaluate(
        "() => Xfer.open('发送文件给 beta', ['① 上传到电脑', '② 由电脑发送给对方'])"
    )
    assert opened is True
    shown = page.evaluate(
        """() => ({
             cls: document.getElementById('xfer-overlay').className,
             title: document.getElementById('xfer-title').textContent,
             labs: Array.from(document.querySelectorAll('#xfer-steps .xf-lab')).map(e => e.textContent),
           })"""
    )
    assert "show" in shown["cls"]
    assert shown["title"] == "发送文件给 beta"
    assert shown["labs"] == ["① 上传到电脑", "② 由电脑发送给对方"]

    page.evaluate("() => Xfer.progress(0, 512, 1024)")
    drawn = page.evaluate(
        "() => document.querySelector('#xfer-steps .xf-note').textContent"
        " + '|' + document.querySelector('#xfer-steps .xf-bar > i').style.width"
    )
    assert drawn == "512 B / 1.0 KB · 50%|50%"
    page.evaluate("() => Xfer.progress(1, 3 * 1024 * 1024, 6 * 1024 * 1024)")
    assert (
        page.evaluate("() => document.querySelectorAll('#xfer-steps .xf-note')[1].textContent")
        == "3.0 MB / 6.0 MB · 50%"
    )

    page.evaluate("() => Xfer.finish('已发出，等待对方接收')")
    assert (
        page.evaluate("() => document.querySelectorAll('#xfer-steps .xf-step')[1].className")
        == "xf-step done"
    )
    assert (
        page.evaluate("() => document.querySelectorAll('#xfer-steps .xf-bar > i')[1].style.width")
        == "100%"
    )
    page.evaluate("() => Xfer.close()")


def test_desktop_send_shows_the_bar_and_stages_the_file(page, rooms, tmp_path):
    """One hop: the file is already on this host, so one step covers it."""
    server, _client = rooms
    src = tmp_path / "notes.txt"
    src.write_text("会议纪要\n" * 2000, encoding="utf-8")
    # the peer's comm clone is built by the roster diff a moment after joining
    assert _wait(lambda: server._clones.get("beta") is not None), "comm clone never appeared"

    page.evaluate("() => openFriendChat('beta')")
    page.evaluate(XFER_WATCH)
    page.evaluate("async (p) => { await sendFileToFriend(p); }", str(src))
    assert _wait(
        lambda: not page.evaluate(
            "() => document.getElementById('xfer-overlay').classList.contains('show')"
        )
    ), f"the modal never closed: {_log(page)}"

    log = _log(page)
    assert log, "the modal never painted"
    last = log[-1]
    assert last["title"] == "发送文件给 beta"
    assert _labels(last) == ["发送给对方"]
    assert "done" in _job_states(last)[-1] and last["steps"][-1]["width"] == "100%"
    assert last["steps"][-1]["note"] == "已发出，等待对方接收"
    assert "show" not in last["cls"]

    job = server.webui_runtime().transfer_progress(last["job"])
    assert job["state"] == "done" and job["done"] == job["total"] > 0
    assert job["name"] == "notes.txt"
    # the bytes really went out: the hub staged them and logged the envelope
    assert any("[file]" in row["text"] for row in server.hub.commlog.read("alpha", "beta"))


def test_mobile_send_shows_two_steps_and_lands_both_hops(mobile_page, rooms, tmp_path, monkeypatch):
    """The phone needs one hop more, and each gets its own line."""
    server, _client = rooms
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(
        webui_server,
        "load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox)),
    )

    mobile_page.evaluate("() => openFriendChat('beta')")
    mobile_page.evaluate(XFER_WATCH)
    mobile_page.set_input_files(
        "#friend-file-input",
        {"name": "photo.bin", "mimeType": "application/octet-stream", "buffer": b"x" * 4096},
    )
    assert _wait(
        lambda: not mobile_page.evaluate(
            "() => document.getElementById('xfer-overlay').classList.contains('show')"
        )
    ), "the modal never closed"

    log = _log(mobile_page)
    assert log, "the modal never painted"
    last = log[-1]
    assert last["title"] == "发送文件给 beta"
    assert _labels(last) == ["① 上传到电脑", "② 由电脑发送给对方"]
    assert _job_states(last) == ["xf-step done", "xf-step done"]
    assert [s["width"] for s in last["steps"]] == ["100%", "100%"]
    assert "show" not in last["cls"]

    # hop 1 landed on this host, hop 2 on the hub
    assert [p.name for p in inbox.iterdir()] == ["photo.bin"]
    job = server.webui_runtime().transfer_progress(last["job"])
    assert job["state"] == "done" and job["name"] == "photo.bin"


def test_a_failed_send_says_so_and_stays_open(page, rooms, tmp_path):
    """A bar that cannot finish must say why, not vanish as if it had."""
    _server, _client = rooms
    page.evaluate("() => openFriendChat('beta')")
    page.evaluate(XFER_WATCH)
    page.evaluate(
        "async (p) => { try { await sendFileToFriend(p); } catch (e) {} }",
        str(tmp_path / "missing.txt"),  # not on disk: the server refuses it
    )
    log = _log(page)
    assert log, "the modal never painted"
    last = log[-1]
    assert "show" in last["cls"], "the modal closed on a failure"
    assert "failed" in last["steps"][-1]["cls"]
    assert "no such file" in last["steps"][-1]["note"]


def test_a_big_file_goes_through_from_either_role(browser, rooms, tmp_path):
    """No size cap (spec §48, the user's call): a file far bigger than the cap
    these tests used to prove stages whole from both sides, and the bar the page
    watches reaches 100%.

    Both roles matter: the host stages the bytes in-process, the client streams
    them to the hub over HTTP.
    """
    server, client = rooms
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x5a" * (4 * 1024 * 1024))

    for room, peer in ((server, "beta"), (client, "alpha")):
        assert _wait(lambda r=room, p=peer: r._clones.get(p) is not None), "comm clone never came"
        with _page(browser, room) as pg:
            pg.evaluate(XFER_WATCH)
            pg.evaluate(
                "async ([host, p]) => { await Xfer.sendOne('发送文件给 ' + host, host, p); }",
                [peer, str(big)],
            )
            last = _log(pg)[-1]
            assert "done" in last["steps"][-1]["cls"], last
            assert last["steps"][-1]["width"] == "100%"
            job = room.webui_runtime().transfer_progress(last["job"])
            assert job["state"] == "done", job
            assert job["done"] == job["total"] == big.stat().st_size

    # both sends staged the whole file on the hub, byte for byte (that the
    # receiver then accepts and drops it is test_room's delivered-transfer case)
    staged = sorted(Path(server.hub.transfers.root).glob("*__big.bin"))
    assert [p.stat().st_size for p in staged] == [big.stat().st_size] * 2
