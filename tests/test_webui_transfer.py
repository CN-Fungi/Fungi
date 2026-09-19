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
import urllib.parse
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
    sep = "&" if "?" in path else "?"
    url = f"{room.open_webui(False)}{path}{sep}t={WEBUI_TOKEN}"
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


def _answer_peer_card(room, value: str, timeout_s: float = 10.0) -> None:
    """The receiving user's click on the consent card (§49: nothing lands on
    their disk without it, and the sender's last step waits for this answer)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cards = room.webui_runtime().pending_asks()
        if cards:
            assert room.webui_runtime().route_answer(cards[0]["id"], value) is True
            return
        time.sleep(0.05)
    raise AssertionError("the peer never got a consent card")


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


def test_desktop_send_waits_for_the_peers_verdict(page, rooms, tmp_path):
    """One hop out, and the last word belongs to the other end (§49).

    The bar used to close when the upload ended — seconds on a local hub, while
    the peer's own download can run for minutes. Whoever closed the room in
    between left them with a truncated file wearing the real name.
    """
    server, client = rooms
    src = tmp_path / "notes.txt"
    src.write_text("会议纪要\n" * 2000, encoding="utf-8")
    # the peer's comm clone is built by the roster diff a moment after joining
    assert _wait(lambda: server._clones.get("beta") is not None), "comm clone never appeared"

    page.evaluate("() => openFriendChat('beta')")
    page.evaluate(XFER_WATCH)
    page.evaluate("async (p) => { await sendFileToFriend(p); }", str(src))
    # the second step is fed by the HUB's own count of what it has handed over
    # (§49). While the peer has not taken the file that count is 0 of N — and
    # the step must say so instead of sitting blank, which is exactly what the
    # user read as a dead progress bar on 2026-09-19.
    assert _wait(lambda: "/" in _log(page)[-1]["steps"][1]["note"], timeout_s=15), (
        f"the waiting step never got its bytes: {_log(page)}"
    )
    _answer_peer_card(client, "yes")
    assert _wait(
        lambda: not page.evaluate(
            "() => document.getElementById('xfer-overlay').classList.contains('show')"
        )
    ), f"the modal never closed: {_log(page)}"

    log = _log(page)
    assert log, "the modal never painted"
    last = log[-1]
    assert last["title"] == "发送文件给 beta"
    assert _labels(last) == ["① 上传到房间", "② 对方接收"]
    assert "done" in _job_states(last)[-1] and last["steps"][-1]["width"] == "100%"
    assert "对方已收到" in last["steps"][-1]["note"]
    assert "show" not in last["cls"]

    job = server.webui_runtime().transfer_progress(last["job"])
    assert job["state"] == "done" and job["phase"] == "deliver"
    assert job["done"] == job["total"] > 0
    assert job["name"] == "notes.txt"
    landed = Path(job["saved"])  # on the PEER's disk, and byte for byte
    try:
        assert landed.read_bytes() == src.read_bytes()
    finally:
        landed.unlink(missing_ok=True)
    # the bytes really went out: the hub staged them and logged the envelope
    assert any("[file]" in row["text"] for row in server.hub.commlog.read("alpha", "beta"))


def test_a_refused_delivery_stays_open_and_names_the_reason(page, rooms, tmp_path):
    """The peer's "no" is an ending too — and the only honest one on screen."""
    server, client = rooms
    src = tmp_path / "refused.txt"
    src.write_text("nope", encoding="utf-8")
    assert _wait(lambda: server._clones.get("beta") is not None), "comm clone never appeared"

    page.evaluate("() => openFriendChat('beta')")
    page.evaluate(XFER_WATCH)
    page.evaluate("async (p) => { await sendFileToFriend(p); }", str(src))
    _answer_peer_card(client, "no")
    assert _wait(lambda: "declined" in _log(page)[-1]["steps"][-1]["note"], timeout_s=10.0), (
        f"the refusal never reached the modal: {_log(page)}"
    )

    last = _log(page)[-1]
    assert "show" in last["cls"], "a refused send vanished as if it had landed"
    assert "failed" in last["steps"][-1]["cls"]
    job = server.webui_runtime().transfer_progress(last["job"])
    assert job["state"] == "error" and job["phase"] == "deliver"
    assert "declined" in job["error"]


def test_mobile_send_shows_three_steps_and_lands_both_hops(
    mobile_page, rooms, tmp_path, monkeypatch
):
    """The phone needs one hop more, and each gets its own line."""
    server, client = rooms
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
    assert _wait(lambda: "/" in _log(mobile_page)[-1]["steps"][2]["note"], timeout_s=15), (
        f"the waiting step never got its bytes: {_log(mobile_page)}"
    )
    _answer_peer_card(client, "yes")
    assert _wait(
        lambda: not mobile_page.evaluate(
            "() => document.getElementById('xfer-overlay').classList.contains('show')"
        )
    ), f"the modal never closed: {_log(mobile_page)}"

    log = _log(mobile_page)
    assert log, "the modal never painted"
    last = log[-1]
    assert last["title"] == "发送文件给 beta"
    assert _labels(last) == ["① 上传到电脑", "② 上传到房间", "③ 对方接收"]
    assert _job_states(last) == ["xf-step done", "xf-step done", "xf-step done"]
    assert [s["width"] for s in last["steps"]] == ["100%", "100%", "100%"]
    assert "show" not in last["cls"]

    # hop 1 landed on this host, hop 2 on the hub
    assert [p.name for p in inbox.iterdir()] == ["photo.bin"]
    job = server.webui_runtime().transfer_progress(last["job"])
    assert job["state"] == "done" and job["name"] == "photo.bin"
    landed = Path(job["saved"])
    try:
        assert landed.read_bytes() == b"x" * 4096  # hop 3 was byte for byte
    finally:
        landed.unlink(missing_ok=True)


def test_mobile_upload_cuts_a_big_file_into_windows(mobile_page, rooms, tmp_path, monkeypatch):
    """A phone's uplink is one connection until we make it several (§51): the
    page asks whether the host takes windows, then sends the file as ranges that
    travel at once — and the host's own coverage decides when it lands."""
    _server, _client = rooms
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(
        webui_server,
        "load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox)),
    )
    payload = bytes(range(256)) * (12 * 1024 * 1024 // 256)  # 12 MiB: 3 windows

    # What the page really put on the wire (the probe is a fetch, a window an
    # XHR): counting them is the only honest way to see the cut from outside.
    mobile_page.evaluate(
        """() => {
          window.__posts = []; window.__asks = [];
          const open = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function (m, u) {
            if (String(u).indexOf('/upload') === 0) window.__posts.push(String(u));
            return open.apply(this, arguments);
          };
          const of = window.fetch;
          window.fetch = function (u, o) {
            if (String(u).indexOf('/upload') === 0) window.__asks.push(String(u));
            return of.apply(this, arguments);
          };
        }"""
    )
    mobile_page.set_input_files(
        "#file-input",
        {"name": "big.bin", "mimeType": "application/octet-stream", "buffer": payload},
    )
    landed = inbox / "big.bin"
    assert _wait(lambda: landed.exists(), timeout_s=60), (
        f"the upload never landed: {mobile_page.evaluate('() => window.__posts')}"
    )
    assert landed.read_bytes() == payload, "the windows did not reassemble byte for byte"
    assert [p.name for p in inbox.iterdir()] == ["big.bin"], "a part file was left"

    posts = mobile_page.evaluate("() => window.__posts")
    asks = mobile_page.evaluate("() => window.__asks")
    assert asks, "the page never asked whether the host takes windows"
    assert len(posts) >= 2, posts
    covered = []
    for target in posts:
        q = urllib.parse.parse_qs(target.split("?", 1)[1])
        lo, size = int(q["offset"][0]), int(q["size"][0])
        assert int(q["size"][0]) == len(payload), target
        covered.append((lo, size))
    assert len({lo for lo, _size in covered}) == len(covered), "two windows share an offset"
    assert min(lo for lo, _size in covered) == 0
    assert sorted(lo for lo, _size in covered)[0] == 0
    assert {lo for lo, _size in covered} == {i * 4 * 1024 * 1024 for i in range(len(covered))}

    # the path the page got back is the one it puts in the message box
    assert _wait(
        lambda: str(landed) in mobile_page.evaluate("() => document.getElementById('input').value")
    )
    landed.unlink(missing_ok=True)


def test_the_phone_pulls_a_file_from_the_pc_in_windows(mobile_page, rooms, tmp_path):
    """The other direction (§52): the file is on the PC, the phone fetches it as
    ranges at once, and what the browser saves is byte for byte what was there."""
    _server, _client = rooms
    payload = bytes(range(256)) * (12 * 1024 * 1024 // 256)  # 12 MiB: three windows
    src = tmp_path / "gift.bin"
    src.write_bytes(payload)

    mobile_page.evaluate(
        """() => {
          window.__gets = [];
          const of = window.fetch;
          window.fetch = function (u, o) {
            const target = String(u);
            if (target.indexOf('/download') === 0) {
              window.__gets.push({ url: target, range: (o && o.headers && o.headers.Range) || '' });
            }
            return of.apply(this, arguments);
          };
        }"""
    )
    with mobile_page.expect_download(timeout=60000) as caught:
        mobile_page.evaluate("(p) => { Xfer.download(p); }", str(src))
    saved = tmp_path / "on-the-phone.bin"
    caught.value.save_as(str(saved))

    assert saved.read_bytes() == payload, "the windows did not reassemble byte for byte"
    gets = mobile_page.evaluate("() => window.__gets")
    assert any("meta=1" in g["url"] for g in gets), gets  # it asked how big before cutting
    windows = [g["range"] for g in gets if g["range"]]
    assert len(windows) >= 2, windows
    covered = []
    for spec in windows:
        lo, _, hi = spec.replace("bytes=", "").partition("-")
        covered.append((int(lo), int(hi)))
    assert min(lo for lo, _hi in covered) == 0
    assert max(hi for _lo, hi in covered) == len(payload) - 1
    assert sum(hi - lo + 1 for lo, hi in covered) == len(payload)
    assert caught.value.suggested_filename == "gift.bin"


def test_paths_in_the_transcript_are_taps(mobile_page):
    """A path the user can see is a path the phone can fetch: it becomes a link,
    and the rewrite never happens inside markup (a link inside an href is how
    this kind of thing breaks a page)."""
    found = mobile_page.evaluate(
        r"""() => {
          const box = document.createElement('div');
          box.innerHTML = marked.parse('做完了 C:\\tmp\\a.zip 和 C:\\tmp\\b.zip。')
            + '<a href="http://x/">C:\\tmp\\inside.zip</a>';
          document.body.appendChild(box);
          FC.linkifyPaths(box, () => {});
          return Array.from(box.querySelectorAll('a.file-link')).map(a => a.textContent);
        }"""
    )
    assert found == ["C:\\tmp\\a.zip", "C:\\tmp\\b.zip"], found
    assert "。" in mobile_page.evaluate("() => document.body.textContent")
    assert "inside.zip" not in "".join(found), "an existing link was rewritten"


def test_the_phone_sees_a_file_the_computer_dropped(mobile_page, rooms, tmp_path, monkeypatch):
    """The transfer session (§53): the computer hands over a path, the phone
    shows it — and the phone does not have to touch anything, because that
    session is the one it polls."""
    server, _client = rooms
    runtime = server.webui_runtime()
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(
        webui_server,
        "load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox)),
    )
    webui_server.shuttle_ensure(runtime)
    mobile_page.evaluate("() => loadSessions()")
    assert _wait(lambda: mobile_page.evaluate("() => (allSessions[0] || {}).shuttle === true")), (
        "the transfer session is not the first thing in the list"
    )

    mobile_page.evaluate("() => switchSession(allSessions.find(s => s.shuttle).id)")
    assert _wait(lambda: mobile_page.evaluate("() => currentSessionId && !processing"))

    # the computer side: a file it wants the phone to have
    dropped = r"C:\Users\someone\Desktop\gift.bin"
    webui_server.shuttle_post(runtime, dropped)

    def _seen() -> bool:
        return mobile_page.evaluate(
            "() => Array.from(document.querySelectorAll('#messages .file-link'))"
            ".some(a => a.textContent.indexOf('gift.bin') >= 0)"
        )

    assert _wait(_seen, timeout_s=12), "the phone never showed the row: " + str(
        mobile_page.evaluate("() => document.getElementById('messages').textContent")
    )

    # and the other direction lands in the same session: a phone upload writes
    # its own row, with the path it landed at
    mobile_page.set_input_files(
        "#file-input",
        {"name": "from-phone.bin", "mimeType": "application/octet-stream", "buffer": b"z" * 2048},
    )
    assert _wait(lambda: (inbox / "from-phone.bin").exists(), timeout_s=30), (
        "the upload never landed"
    )
    assert _wait(
        lambda: "手机上传"
        in mobile_page.evaluate("() => document.getElementById('messages').textContent"),
        timeout_s=12,
    ), "the upload never showed up in the session"


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
    assert _labels(last) == ["① 上传到房间", "② 对方接收"]
    # the failure belongs to the hop that failed: nothing ever went out
    assert "failed" in last["steps"][0]["cls"]
    assert "no such file" in last["steps"][0]["note"]


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
            other = client if room is server else server
            _answer_peer_card(other, "yes")
            job_id = pg.evaluate("() => document.getElementById('xfer-overlay').dataset.job")
            assert _wait(
                lambda rid=room, jid=job_id: rid.webui_runtime().transfer_progress(jid)["state"]
                == "done"
            ), "the delivery never landed"
            # the pump paints on its own tick: wait for the modal's own ending
            # before reading what it shows
            assert _wait(
                lambda p=pg: not p.evaluate(
                    "() => document.getElementById('xfer-overlay').classList.contains('show')"
                )
            ), f"the modal never closed: {_log(pg)}"
            last = _log(pg)[-1]
            assert "done" in last["steps"][-1]["cls"], last
            assert last["steps"][-1]["width"] == "100%"
            job = room.webui_runtime().transfer_progress(job_id)
            assert job["done"] == job["total"] == big.stat().st_size
            landed = Path(job["saved"])
            try:
                assert landed.read_bytes() == big.read_bytes()  # byte for byte, all 4 MiB
            finally:
                landed.unlink(missing_ok=True)

    # both sends staged the whole file on the hub, byte for byte — and both
    # deliveries dropped their staged copy again (§48): nothing of a delivered
    # transfer is left behind on the sender's disk
    assert list(Path(server.hub.transfers.root).glob("*__big.bin")) == []
