"""WebUI i18n: the launcher's setting picks Chinese (default) or English, and
the dark-mode consent slider has to stay readable (user report, 2026-10-21).

The language is config.json's `ui_lang`, chosen on the launcher's settings page.
/lang.js is generated from `load_config()` per request and runs before
i18n.js, so these tests patch that reader instead of touching a user's file.
They drive a real Chromium and skip when it is missing, the same contract as
the other WebUI modules.
"""

import json
import urllib.request

import pytest

from fungi import config as config_mod
from fungi import session as session_mod
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="e", model="m")


class SilentLLM:
    def __call__(self, _messages, _tool_defs):
        return LLMResult(content="<<SILENT>>")


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    root = tmp_path_factory.mktemp("i18n-room")
    with pytest.MonkeyPatch.context() as mp:
        # The page loads /sessions, which ensures the transfer session: keep it
        # out of the developer's own data/sessions (see fungi-webui-and-sessions).
        mp.setattr(session_mod, "SESSIONS_DIR", root / "sessions")
        (root / "sessions").mkdir()
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


def _write_config(lang: str | None = None) -> None:
    """The launcher's own file (conftest already sandboxed CONFIG_PATH)."""
    payload = {"api_key": "k"}
    if lang:
        payload["ui_lang"] = lang
    config_mod.CONFIG_PATH.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def zh():
    """No ui_lang in config.json: the default is Chinese."""
    _write_config()


@pytest.fixture()
def english():
    _write_config("en")


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def _open(page, url):
    page.goto(url)
    page.wait_for_function("() => typeof loadSessions === 'function'")
    page.wait_for_timeout(300)
    page.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    page.evaluate("() => document.getElementById('rescan-overlay')?.classList.add('hide')")
    return page


def test_i18n_js_and_the_language_script_are_served(room, zh):
    base = room.open_webui(False)
    with urllib.request.urlopen(base + "/i18n.js") as r:
        assert r.status == 200
        assert "FUNGI_LANG" in r.read().decode("utf-8")
    with urllib.request.urlopen(base + "/lang.js") as r:
        assert r.status == 200
        assert 'window.FUNGI_LANG = "zh";' in r.read().decode("utf-8")


def test_desktop_defaults_to_chinese(browser, room, zh):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = _open(ctx.new_page(), f"{room.open_webui(False)}?t={WEBUI_TOKEN}")
    try:
        assert page.evaluate("document.documentElement.lang") == "zh-CN"
        assert page.inner_text("#btn-new-session") == "＋ 新建会话"
        # Empty box = retry (2026-10-31); typing flips it back to send.
        assert page.inner_text("#send") == "重试"
        page.fill("#input", "x")
        page.wait_for_timeout(100)
        assert page.inner_text("#send") == "发送"
        page.fill("#input", "")
        assert page.inner_text('#consent-slider [data-v="ask"]') == "询问"
    finally:
        ctx.close()


def test_desktop_follows_the_launcher_setting_in_english(browser, room, english):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = _open(ctx.new_page(), f"{room.open_webui(False)}?t={WEBUI_TOKEN}")
    try:
        assert page.evaluate("document.documentElement.lang") == "en"
        assert page.inner_text("#btn-new-session") == "+ New Session"
        assert page.get_attribute("#session-filter", "placeholder") == "Filter..."
        # Empty box = retry (2026-10-31); typing flips it back to send.
        assert page.inner_text("#send") == "Retry"
        page.fill("#input", "x")
        page.wait_for_timeout(100)
        assert page.inner_text("#send") == "Send"
        assert page.inner_text('#consent-slider [data-v="allow"]') == "Allow"
    finally:
        ctx.close()


def test_dark_consent_labels_have_light_text(browser, room, zh):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = _open(ctx.new_page(), f"{room.open_webui(False)}?t={WEBUI_TOKEN}")
    try:
        page.evaluate("""() => {
          document.documentElement.dataset.theme = 'dark';
          document.getElementById('consent-slider').dataset.mode = 'ask';
        }""")
        page.wait_for_timeout(400)  # the slider colors cross-fade (150 ms)
        inactive = page.evaluate(
            "getComputedStyle(document.querySelector('#consent-slider [data-v=\"allow\"]')).color"
        )
        active = page.evaluate(
            "getComputedStyle(document.querySelector('#consent-slider [data-v=\"ask\"]')).color"
        )
        # --text2 on the dark surface, and a near-white label on the deeper knob.
        assert inactive == "rgb(162, 168, 192)"
        assert active == "rgb(255, 251, 235)"
    finally:
        ctx.close()


def test_a_silent_agent_gets_a_line_instead_of_an_empty_bubble(browser, room, zh):
    """A turn whose reply is only `<<SILENT>>` reads as 「（Agent不说话）」 — the
    marker is control text and never reaches the screen."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = _open(ctx.new_page(), f"{room.open_webui(False)}?t={WEBUI_TOKEN}")
    try:
        page.fill("#input", "你好")
        page.click("#send")
        page.wait_for_function("() => !processing", timeout=20000)
        page.wait_for_timeout(400)  # done -> the stored transcript is reloaded
        texts = page.evaluate(
            "() => Array.from(document.querySelectorAll('#messages .msg'))"
            ".map(n => n.textContent.trim())"
        )
        assert "（Agent不说话）" in texts, texts
        assert not any("SILENT" in t for t in texts), texts
    finally:
        ctx.close()


def test_mobile_follows_the_launcher_setting(browser, room, zh):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    try:
        _open(page, f"{room.open_webui(False)}/m?t={WEBUI_TOKEN}")
        assert page.evaluate("document.documentElement.lang") == "zh-CN"
        assert page.inner_text("#btn-new-session") == "＋ 新会话"
        assert page.inner_text('#consent-seg [data-v="allow"]') == "允许"
        assert page.inner_text('#consent-seg [data-v="ask"]') == "询问"
    finally:
        ctx.close()
