"""Output rate in the header (§64): a tok/s readout left of the theme switch.

The number is the page's own count of streamed chunks over a sliding window, so
the test drives a real turn through the real WebUI stream: `stream_chat` is
replaced by a fake that streams 40 chunks at 20/s. It is sentinel-gated — the
courier's own calls (which share this stream) stay quiet and instant, the job
`SilentLLM` does in the sibling webui tests.

Skips (never fails) where playwright is missing, so CI without a browser stays
green.
"""

import re
import time

import pytest

from fungi import agent as agent_mod
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMAbortedError, LLMResult
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="e", model="m")

SENTINEL = "tok-rate-test"
CHUNKS = 40
CHUNK_S = 0.05  # 20 chunks/s: the reading has to land in the tens, not the units


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    """No `llm=` here: the agent has to reach `_default_llm`, which is the half
    that emits the deltas (an injected llm returns a whole result, no stream)."""
    root = tmp_path_factory.mktemp("rate-room")
    server = RoomServer("alpha", CFG, NullSink(), "tok", root / "d1", rules_path=root / "r1.json")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def fake_stream(monkeypatch):
    """A real streaming call minus the network: one chunk per `on_delta`."""

    def fake(
        model, endpoint, api_key, messages, tools, on_delta=None, should_abort=None, max_tokens=None
    ):
        if not any(SENTINEL in str(m.get("content") or "") for m in messages):
            return LLMResult(content="<<SILENT>>")  # courier chatter: quiet + instant
        text = ""
        for _ in range(CHUNKS):
            if should_abort and should_abort():
                raise LLMAbortedError(LLMResult(content=text))
            time.sleep(CHUNK_S)
            if on_delta:
                on_delta("text", "字")
            text += "字"
        return LLMResult(content=text)

    monkeypatch.setattr(agent_mod, "stream_chat", fake)


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
    page.wait_for_function("() => typeof loadSessions === 'function'")
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


def _reading(page) -> dict | None:
    """What the readout says, where it sits, and whether it is showing."""
    return page.evaluate(
        """() => {
             const el = document.getElementById('tok-rate');
             if (!el) return null;
             const box = el.getBoundingClientRect();
             const th = document.getElementById('theme-switch').getBoundingClientRect();
             return { text: el.textContent.trim(), on: el.classList.contains('on'),
                      opacity: getComputedStyle(el).opacity,
                      right: box.right, cy: box.top + box.height / 2,
                      themeLeft: th.left, themeCy: th.top + th.height / 2 };
           }"""
    )


def _rate(text: str) -> float:
    m = re.match(r"^(\d+(?:\.\d+)?) tok/s$", text)
    assert m, f"the readout is not a rate: {text!r}"
    return float(m.group(1))


def _warm(page) -> bool:
    """The readout is showing a number past the cold-start floor."""
    r = _reading(page)
    if not r or not r["on"]:
        return False
    m = re.match(r"^(\d+(?:\.\d+)?) tok/s$", r["text"])
    return bool(m) and float(m.group(1)) >= 8


def _burst(page, send_sel: str = "#send") -> dict:
    """Send the sentinel turn and come back once the number has warmed up."""
    page.evaluate("async () => { await loadSessions(); }")
    sid = page.evaluate("async () => (await (await fetch('/new', { method: 'POST' })).json()).id")
    page.evaluate("async (sid) => { await switchSession(sid); }", sid)
    page.fill("#input", SENTINEL + "：随便写点什么")
    page.click(send_sel)
    assert _wait(lambda: _warm(page), timeout_s=20), (
        "the readout never showed a rate while the model was streaming"
    )
    return _reading(page)


def test_the_desktop_reads_the_rate_left_of_the_theme_switch(page):
    """The whole user-facing contract: while the model writes, the header says how
    many tokens per second it is writing them; when it stops, the number goes."""
    page.evaluate("async () => { await loadSessions(); }")
    before = _reading(page)
    assert before is not None, "the header has no readout at all"
    assert before["on"] is False and before["opacity"] == "0", before  # 闲置时不该显影

    live = _burst(page)
    assert 8 <= _rate(live["text"]) <= 100, f"reading does not match a 20 chunk/s stream: {live}"
    assert live["right"] <= live["themeLeft"] + 1, f"not left of the theme switch: {live}"
    assert abs(live["cy"] - live["themeCy"]) < 12, f"not on the theme switch's row: {live}"

    page.wait_for_function("() => !processing", timeout=20000)
    assert _wait(lambda: (_reading(page) or {}).get("on") is False, timeout_s=6), (
        "the readout stayed on after the turn ended"
    )
    assert _wait(lambda: _reading(page)["opacity"] == "0", timeout_s=6)
    # The auto margin that holds the right edge moved onto the readout: a hidden
    # readout must still anchor the theme switch to the right of the header.
    assert _reading(page)["themeLeft"] > 600, "the theme switch drifted out of the right corner"


def test_the_phone_reads_it_too(mobile_page):
    """The phone shell is the same contract in its own header (m.html/m.css)."""
    live = _burst(mobile_page, send_sel="#btn-send")
    assert 8 <= _rate(live["text"]) <= 100, live
    assert live["right"] <= live["themeLeft"] + 1, f"not left of the theme switch: {live}"
    assert abs(live["cy"] - live["themeCy"]) < 12, f"not on the theme switch's row: {live}"
    mobile_page.wait_for_function("() => !processing", timeout=20000)
    assert _wait(lambda: (_reading(mobile_page) or {}).get("on") is False, timeout_s=6)
