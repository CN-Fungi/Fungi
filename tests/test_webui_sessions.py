"""The sidebar session list: renaming a session has to stick in the list itself.

Rows are reused keyed by id, so a row's ✎ handler can hold the session object of
an older /sessions fetch; the rename then lands on the file (and on that stale
object) while `renderSessionList`'s guard compares the painted title against the
*live* list entry and puts the old name back. 2026-09-11 user report:
「会话列表的重命名回车后不更新命名（虽然已经改了，但是刷新才显示）」。

Skips (never fails) where playwright is missing, so CI without a browser stays
green.
"""

import pytest

from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="e", model="m")


class SilentLLM:
    """No courier chatter: this module drives the sidebar, it does not converse."""

    def __call__(self, _messages, _tool_defs):
        return LLMResult(content="<<SILENT>>")


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    root = tmp_path_factory.mktemp("sessions-room")
    server = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        root / "d1",
        llm=SilentLLM(),
        rules_path=root / "r1.json",
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


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
def page(browser, room):
    url = f"{room.open_webui(False)}?t={WEBUI_TOKEN}"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.goto(url)
    pg.wait_for_function("() => typeof loadSessions === 'function'")
    pg.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    try:
        yield pg
    finally:
        ctx.close()


def _titles(page) -> list:
    return page.evaluate("() => allSessions.map(s => s.title)")


def test_the_transfer_session_stands_apart_and_cannot_be_touched(page):
    """§54: it is a channel, not a chat. The sidebar says so (icon, its own look,
    "只搬文件") and offers no ✎/✕ — and the server refuses both anyway, which is
    what actually makes the guarantee (a UI-only guard is a suggestion)."""
    page.evaluate("async () => { await loadSessions(); }")
    row = page.locator(".session-row.shuttle")
    assert row.count() == 1, "the transfer session is not in the sidebar"
    assert page.evaluate(
        "() => document.querySelector('.session-row') === document.querySelector('.session-row.shuttle')"
    ), "it is not pinned first"
    assert row.locator(".session-row-icon").count() == 1
    assert row.locator(".session-row-act").count() == 0, "rename/delete must not be offered"
    assert row.locator(".session-row-meta").text_content().startswith("只搬文件")

    refused = page.evaluate(
        """async () => {
          const del = await fetch('/session?id=file-transfer', { method: 'DELETE' });
          const body = await del.json();
          const after = await (await fetch('/sessions')).json();
          return { code: del.status, error: body.error, still: after.sessions.some(s => s.id === 'file-transfer') };
        }"""
    )
    assert refused["code"] == 400 and refused["still"] is True, refused
    assert "不能删除" in refused["error"], refused


def test_renaming_a_session_updates_the_list_not_only_the_file(page):
    """The name must change on screen, in the live list, and on the server — all
    three, and without an uncaught error from the rename's own teardown."""
    errors: list = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    # the sidebar lives collapsed at some viewports; open it so the rows are clickable
    page.evaluate(
        """() => {
             const sb = document.getElementById('sidebar');
             if (sb && sb.classList.contains('collapsed')) {
               document.getElementById('hamburger-sidebar').click();
             }
           }"""
    )
    page.click("#btn-new-session")
    # Wait for THIS session, not for "a row": the pinned transfer session (§53)
    # is always there, so a row count says nothing about the new one.
    page.wait_for_function("() => allSessions.some(s => s.title === '(new session)')")
    # Rows are painted in list order and the transfer session is first, so
    # address this test's own row by index rather than "the first one".
    index = page.evaluate("() => allSessions.findIndex(s => s.title === '(new session)')")
    assert index >= 0, page.evaluate("() => allSessions.map(s => s.title)")
    row = page.locator(".session-row").nth(index)
    assert row.locator(".session-row-title").text_content() == "(new session)"

    # A second /sessions fetch — what the 3 s resume poll does — replaces the list
    # objects while the row (and its ✎ handler) is reused: the stale-closure setup.
    page.evaluate("async () => { await loadSessions(); }")
    row.locator(".session-row-act:not(.del)").click()
    page.fill(".rename-input", "改名要立刻生效")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)

    assert row.locator(".session-row-title").text_content() == "改名要立刻生效"
    assert "改名要立刻生效" in _titles(page)
    served = page.evaluate(
        "async () => (await (await fetch('/sessions')).json()).sessions.map(s => s.title)"
    )
    assert "改名要立刻生效" in served, "the save must have reached the server too"
    assert errors == [], "the rename must not throw on its way out (Enter then blur)"
