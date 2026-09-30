"""The card's two actions in a real page (§73).

Two shells, two behaviours, and the difference is the whole point: the desktop
page *is* the machine that holds the file, so a click anywhere on the card opens
it; the phone only gets the buttons, because opening there would pop a window on
somebody else's desk. A card is the same renderer in every session, so the
"ordinary session" case is asserted too, not just the file-transfer one.

The /open requests are intercepted below: no window ever opens during the suite.
"""

import json
import threading
import time

import pytest

from fungi import server as webui_server
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # any launch failure means "no browser here"
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture()
def room(tmp_path, monkeypatch):
    """A WebUI server whose sessions land in tmp, never in the developer's data/."""
    from fungi import session as session_mod

    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path / "sessions")
    runtime = webui_server.WebUIRuntime()
    server = webui_server.make_webui_server(0, runtime)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", runtime
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def page(browser, room):
    """The desktop shell, open on a page whose session list is loaded."""
    base, _runtime = room
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.goto(f"{base}/?t={WEBUI_TOKEN}")
    pg.wait_for_function("() => typeof loadSessions === 'function'")
    pg.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    try:
        yield pg
    finally:
        ctx.close()


@pytest.fixture()
def mobile_page(browser, room):
    base, _runtime = room
    ctx = browser.new_context(viewport={"width": 420, "height": 860})
    pg = ctx.new_page()
    pg.goto(f"{base}/m?t={WEBUI_TOKEN}")
    pg.wait_for_function("() => typeof loadSessions === 'function'")
    try:
        yield pg
    finally:
        ctx.close()


def _card_in_session(runtime, session_id: str, path, title: str = "打开文件") -> None:
    """One ordinary-session row carrying a card, the way a transfer row is stored."""
    src = path
    runtime.sessions_save(
        session_id,
        title,
        [
            {
                "role": "user",
                "content": str(src),
                "ts": time.time(),
                "direction": "computer",
                "file": {
                    "name": src.name,
                    "size": src.stat().st_size,
                    "path": str(src),
                    "direction": "computer",
                },
            }
        ],
    )


def _open_the_session(pg, session_id: str) -> None:
    pg.evaluate("async () => { await loadSessions(); }")
    pg.evaluate("(id) => switchSession(id)", session_id)


def _watch_open(pg) -> list:
    """Record every /open the page sends, and answer it (nothing is launched)."""
    seen: list = []

    def handler(route):
        seen.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "action": "open", "raised": True}),
        )

    pg.route("**/open*", handler)
    return seen


def _card_name_text(pg) -> str:
    return pg.evaluate(
        "() => (document.querySelector('#messages .file-card .fc-name') || {}).textContent || ''"
    )


def test_a_desktop_card_opens_on_a_click_and_finds_itself_in_the_folder(page, room, tmp_path):
    """Both halves of §73 on the shell that has the file: the card *is* the
    double-click, and the button is the folder. The button must not also fire the
    card's own action — one gesture, one request."""
    _base, runtime = room
    src = tmp_path / "交给我.bin"
    src.write_bytes(b"x" * 32)
    _card_in_session(runtime, "20260930-120000", src)
    seen = _watch_open(page)

    _open_the_session(page, "20260930-120000")
    assert _wait(lambda: _card_name_text(page) == "交给我.bin"), "the card never showed up"
    assert (
        page.evaluate("() => document.querySelector('#messages .file-card .fc-reveal').textContent")
        == "打开所在目录"
    )
    assert (
        page.evaluate("() => !!document.querySelector('#messages .file-card.fc-openable')") is True
    )

    page.click("#messages .file-card .fc-name")
    assert _wait(lambda: seen == [{"path": str(src), "action": "open"}]), seen

    page.click("#messages .file-card .fc-reveal")
    assert _wait(lambda: len(seen) == 2), seen
    assert seen[1] == {"path": str(src), "action": "reveal"}, seen


def test_a_phone_card_offers_the_folder_button_but_never_opens_on_a_tap(
    mobile_page, room, tmp_path
):
    """The phone's file lives on the PC: the button asks the PC to show it, and
    the card itself stays inert (§73). The transfer session is where a phone
    actually sees cards, so that is where this is asserted."""
    _base, runtime = room
    src = tmp_path / "handover.bin"
    src.write_bytes(b"y" * 16)
    webui_server.shuttle_ensure(runtime)
    webui_server.shuttle_post(
        runtime,
        f"给手机 {src}",
        {"name": src.name, "size": src.stat().st_size, "path": str(src), "direction": "computer"},
    )
    seen = _watch_open(mobile_page)

    mobile_page.evaluate("async () => { await loadSessions(); }")
    shuttle_id = mobile_page.evaluate("() => allSessions.find(s => s.shuttle).id")
    mobile_page.evaluate("(id) => switchSession(id)", shuttle_id)
    assert _wait(lambda: _card_name_text(mobile_page) == "handover.bin"), "the card never showed up"
    assert (
        mobile_page.evaluate("() => !!document.querySelector('#messages .file-card .fc-pull')")
        is True
    )
    assert mobile_page.evaluate("() => !!document.querySelector('.file-card.fc-openable')") is False

    mobile_page.click("#messages .file-card .fc-name")
    time.sleep(0.5)  # a request would have been near-instant; none may arrive
    assert seen == [], seen

    mobile_page.click("#messages .file-card .fc-reveal")
    assert _wait(lambda: seen == [{"path": str(src), "action": "reveal"}]), seen


def test_selecting_the_path_is_not_a_click_to_open(page, room, tmp_path):
    """The path sits on the card to be copied, and a drag that selects it ends in
    a real click event — which must not launch the file. A plain click still does."""
    _base, runtime = room
    src = tmp_path / "copy-me.bin"
    src.write_bytes(b"x" * 16)
    _card_in_session(runtime, "20260930-120002", src)
    seen = _watch_open(page)

    _open_the_session(page, "20260930-120002")
    assert _wait(lambda: _card_name_text(page) == "copy-me.bin"), "the card never showed up"

    page.evaluate(
        """() => {
          const path = document.querySelector('#messages .file-card .fc-path');
          const range = document.createRange();
          range.selectNodeContents(path);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          document.querySelector('#messages .file-card').click();
        }"""
    )
    time.sleep(0.4)  # a request would be near-instant; none may arrive
    assert seen == [], seen

    page.evaluate("() => window.getSelection().removeAllRanges()")
    page.click("#messages .file-card .fc-name")
    assert _wait(lambda: seen == [{"path": str(src), "action": "open"}]), seen


def test_a_card_whose_file_is_gone_says_so_where_the_finger_was(page, room, tmp_path):
    """Nothing silent: the server's 404 lands inside the card it belongs to, and
    nothing at all is launched."""
    _base, runtime = room
    gone = tmp_path / "gone.bin"
    gone.write_bytes(b"z" * 8)  # the file was there when the card was written
    _card_in_session(runtime, "20260930-120001", gone)
    gone.unlink()  # ... and it is not there any more

    _open_the_session(page, "20260930-120001")
    assert _wait(lambda: _card_name_text(page) == "gone.bin"), "the card never showed up"

    page.click("#messages .file-card .fc-name")
    assert _wait(
        lambda: page.evaluate(
            "() => (document.querySelector('#messages .file-card .fc-error')||{}).textContent || ''"
        )
        != ""
    ), "the failure was swallowed"
    assert "gone.bin" in page.evaluate(
        "() => document.querySelector('#messages .file-card .fc-error').textContent"
    )
