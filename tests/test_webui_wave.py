"""The status line's waiting wave has to outlive the line's own rewrites.

motion.js drops three elastic bars (`.motion-wave`) into `#status` when a turn
starts, but every later event rewrote that line with `status.textContent = ...`,
which replaces **all** children — so the wave died at the first event of the
turn and only the opening "Thinking..." ever showed it (2026-09-28 user report:
「工具也增加等待动效，现在只有 thinking 有」). The fix routes every rewrite
through `setStatus()`, which swaps only the text nodes and leaves the wave
attached (detaching it would restart the CSS animation on every streamed chunk).

The test drives a real turn through the real WebUI stream — a scripted model
calls a tool — and records in-page every node the status line ever lost, plus
the text it showed while a wave was in it.

Skips (never fails) where playwright or its chromium is missing, so CI without
a browser stays green.
"""

import time

import pytest

from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="e", model="m")
SENTINEL = "wave-test"


class ToolFirstLLM:
    """Sentinel-gated: this test's own message earns a tool call, while the
    courier's chatter (which shares the same llm object) stays silent."""

    def __call__(self, messages, _tool_defs):
        if not any(SENTINEL in str(m.get("content") or "") for m in messages):
            return LLMResult(content="<<SILENT>>")
        if any(m.get("role") == "tool" for m in messages):
            return LLMResult(content="工具跑完了")
        return LLMResult(
            tool_calls=[
                {
                    "id": "call-wave",
                    "type": "function",
                    "function": {"name": "no-such-tool", "arguments": "{}"},
                }
            ]
        )


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    root = tmp_path_factory.mktemp("wave-room")
    server = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        root / "d1",
        llm=ToolFirstLLM(),
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
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        reduced_motion="no-preference",  # the wave must exist to be asserted
    )
    pg = ctx.new_page()
    pg.goto(url)
    pg.wait_for_function("() => typeof loadSessions === 'function'")
    pg.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    try:
        yield pg
    finally:
        ctx.close()


def test_the_wave_rides_through_a_whole_tool_turn(page):
    """One real turn that calls a tool: the wave goes in at turn start, the tool
    event rewrites the status line, and the wave must still be there."""
    assert page.evaluate("() => document.documentElement.classList.contains('motion-on')"), (
        "the motion module is collapsed: there is no wave to keep alive"
    )
    page.evaluate(
        """() => {
          window.__samples = [];  // [status text, wave present] per rewrite
          window.__gone = [];     // texts the line showed right after losing the wave
          const st = document.getElementById('status');
          new MutationObserver(muts => {
            const wave = !!st.querySelector('.motion-wave');
            for (const m of muts) {
              if (!wave && processing) {
                for (const n of m.removedNodes) {
                  if (n.nodeType === 1 && n.classList.contains('motion-wave')) {
                    window.__gone.push(st.textContent);
                  }
                }
              }
              for (const n of m.addedNodes) {
                if (n.nodeType === 3 && n.textContent) window.__samples.push([n.textContent, wave]);
              }
            }
          }).observe(st, { childList: true });
        }"""
    )

    page.evaluate("async () => { await loadSessions(); }")
    sid = page.evaluate("async () => (await (await fetch('/new', { method: 'POST' })).json()).id")
    page.evaluate("async (sid) => { await switchSession(sid); }", sid)
    page.fill("#input", SENTINEL + "：跑一个工具")
    page.click("#send")
    assert _wait(lambda: page.evaluate("() => !processing"), timeout_s=20), "the turn never ended"

    samples = page.evaluate("() => window.__samples")
    running = [s for s in samples if s[0].startswith("Running ")]
    assert running, f"the tool phase never rewrote the status line: {samples}"
    assert all(wave for _, wave in running), f"the wave was gone while a tool ran: {running}"
    assert page.evaluate("() => window.__gone") == [], (
        "the wave left the status line mid-turn (only waveOff may remove it)"
    )
