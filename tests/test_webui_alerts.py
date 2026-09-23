"""Session alerts in the two shells (§61): the red dot, and the claim that stops the
ring.

One request carries both halves (POST /session/seen {id, visible}): the page says
which session it is *showing* — which drops that session's alert, the wire that
stops the GUI's ring — and the answer carries the whole alert map, which is where
the sidebar's dots come from. A hidden page claims nothing: a minimized webUI is
not "opened" (user rule, 2026-09-21) — that is exactly when the ring has to fire.

Skips (never fails) where playwright is missing, so CI without a browser stays
green.
"""

import time

import pytest

from fungi import server as webui_server
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="e", model="m")


class SilentLLM:
    """No courier chatter: these tests watch the sidebar, they do not converse."""

    def __call__(self, _messages, _tool_defs):
        return LLMResult(content="<<SILENT>>")


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    root = tmp_path_factory.mktemp("alerts-room")
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
        server.stop()  # also clears the alert registry (§61)


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # any launch failure means "no browser here"
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _open_page(browser, room, path="/"):
    url = f"{room.open_webui(False)}{path}?t={WEBUI_TOKEN}"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_function("() => typeof Alerts === 'object'")
    # Same rule as the three sibling modules: on a machine without a configured
    # config.json the API-key modal covers the pane, and a modal that eats every
    # click would make the sample lie (or time out, as it did in CI).
    page.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    return ctx, page


@pytest.fixture()
def page(browser, room):
    ctx, pg = _open_page(browser, room)
    try:
        yield pg
    finally:
        ctx.close()


@pytest.fixture()
def mobile_page(browser, room):
    ctx, pg = _open_page(browser, room, path="/m")
    try:
        yield pg
    finally:
        ctx.close()


def _new_session(page) -> str:
    """One more session on disk, made through the server (the page's own /new)."""
    return page.evaluate("async () => (await (await fetch('/new', { method: 'POST' })).json()).id")


def _open_session(page, session_id: str) -> None:
    page.evaluate("async (sid) => { await switchSession(sid); }", session_id)


def _sessions(page) -> list:
    """The page's own list — the ids come from there, never from a copy here."""
    return page.evaluate("() => allSessions.map(s => s.id)")


def _dot(page, session_id: str) -> dict | None:
    """What one row's dot says, in whichever shell is on the page."""
    return page.evaluate(
        """(sid) => {
             const row = document.querySelector('.session-row[data-sid="' + sid + '"]');
             const dot = row && row.querySelector('.session-dot');
             return dot ? { title: dot.title } : null;
           }""",
        session_id,
    )


def _alert_of(page, session_id: str):
    """The session's alert as a fresh /sessions fetch reports it."""
    return page.evaluate(
        """async (sid) => {
             await loadSessions();
             const row = allSessions.find(s => s.id === sid);
             return row ? (row.alert || null) : 'missing';
           }""",
        session_id,
    )


def test_the_desktop_shows_a_dot_and_opening_the_session_takes_it_away(page, room):
    """The whole user-facing contract: a session that wants you wears a dot, and
    clicking into it clears the dot — and the server's alert with it (that is
    what stops the ring)."""
    page.evaluate("async () => { await loadSessions(); }")
    on_screen = _new_session(page)
    other = _new_session(page)
    _open_session(page, on_screen)
    assert page.evaluate("() => currentSessionId") == on_screen

    webui_server.note_alert(other, "done")
    page.evaluate("async () => { await loadSessions(); await Alerts.tick(); }")
    assert _wait(lambda: _dot(page, other) is not None), "no red dot on the session that finished"
    assert _dot(page, other) == {"title": "回答完毕"}, _dot(page, other)
    assert _dot(page, on_screen) is None, "the session on screen has nothing waiting"

    # The sidebar lives collapsed at some viewports; the rows must be clickable.
    page.evaluate(
        """() => {
             const sb = document.getElementById('sidebar');
             if (sb && sb.classList.contains('collapsed')) {
               document.getElementById('hamburger-sidebar').click();
             }
           }"""
    )
    page.locator('.session-row[data-sid="' + other + '"]').click()

    assert _wait(lambda: _dot(page, other) is None), "the dot survived opening the session"
    assert _wait(lambda: other not in webui_server.session_alerts()), (
        "the server still wants the user in that session"
    )
    assert _wait(lambda: _alert_of(page, other) is None), "a fresh /sessions fetch put the dot back"


def test_the_alert_feed_never_repaints_the_transcript(page):
    """§55 的纪律：心跳只回答「有没有新提醒」，转录里一个节点都不许动。"""
    page.evaluate(
        """() => {
             window.__alertsMutations = 0;
             new MutationObserver(muts => { window.__alertsMutations += muts.length; })
               .observe(document.getElementById('messages'),
                        { childList: true, subtree: true, characterData: true, attributes: true });
           }"""
    )
    for _ in range(3):
        page.evaluate("async () => { await Alerts.tick(); }")
        page.wait_for_timeout(100)
    assert page.evaluate("() => window.__alertsMutations") == 0
    assert _sessions(page), "the sidebar is still there"  # …and the page is alive


def test_the_phone_marks_it_and_hidden_is_not_opened(mobile_page, room):
    """手机端：抽屉里的那一行也有红点；而且**最小化不算点开** —— 页面一隐藏就
    不再声明「我在看」，服务端这才敢把提醒挂上（跑 GUI 那台机器的铃就靠这个响）。"""
    page = mobile_page
    page.evaluate("async () => { await loadSessions(); }")
    on_screen = _new_session(page)
    other = _new_session(page)
    _open_session(page, on_screen)
    assert page.evaluate("() => currentSessionId") == on_screen

    webui_server.note_alert(other, "ask")
    page.evaluate("async () => { await loadSessions(); await Alerts.tick(); }")
    assert _wait(lambda: _dot(page, other) is not None), "no red dot in the phone's drawer"
    assert _dot(page, other) == {"title": "Agent 在等你回答"}, _dot(page, other)

    # 页面活着、正看着 on_screen：它对那个会话的声明是新鲜的 → 提醒被压住。
    webui_server.note_alert(on_screen, "done")
    assert on_screen not in webui_server.session_alerts(), "the session on screen was not claimed"

    # 最小化 / 切走：页面停止声明，那份「我在看」随之作废 → 同一个提醒现在是真的。
    page.evaluate(
        """() => {
             Object.defineProperty(document, 'hidden', { value: true, configurable: true });
             Object.defineProperty(document, 'hasFocus', { value: () => false, configurable: true });
             document.dispatchEvent(new Event('visibilitychange'));
           }"""
    )
    page.evaluate("async () => { await Alerts.tick(); }")  # the release has landed
    webui_server.note_alert(on_screen, "done")
    assert on_screen in webui_server.session_alerts(), "a minimized webUI counted as opened"

    # 回到前台：声明恢复，用户又在看着它 —— 提醒收回去（铃停）。
    page.evaluate(
        """() => {
             Object.defineProperty(document, 'hidden', { value: false, configurable: true });
             Object.defineProperty(document, 'hasFocus', { value: () => true, configurable: true });
             document.dispatchEvent(new Event('visibilitychange'));
           }"""
    )
    assert _wait(lambda: on_screen not in webui_server.session_alerts()), (
        "coming back to a session did not clear its alert"
    )
