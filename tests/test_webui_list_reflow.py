"""会话列表不再「重排」（2026-09-25 用户报告）。

用户原话：「现在每次切换会话也好，停止会话也好，就会触发视图重排。具体来说，每次新建会话，
会话会短暂停留在文件传输助手，然后再下移；每次切换会话，底部的会话则会渲染在好友列表之上。
以前没有这个问题，我怀疑是文件传输助手那次引入的」。

真机量的（`C:/tmp/scratch/fungi-reflow/` 的探针，21 个会话、列表可滚动）：
  * 一次纯重绘                = 130 次 childList 变更、20 帧里有行是 `position:absolute`
  * 切一次会话（滚动位置 150） = 260 次变更、22 帧绝对定位行、**scrollTop 被夹回 0**
  * 新建会话                  = 新行连着 20 帧停在**文件传输助手那一格**（top 109，它的终点是 147）
修复后同一次测量：纯重绘 0 次变更 / 切换 0 次变更且 scrollTop 150→150 / 新行第一帧就在 147。

两处根因（都在桌面上，手机壳 m.js 是另一条路，没有 Flip）：
  1. `renderSessionList` 每行无条件 `list.appendChild(row)`：21 行 = 130 次 DOM 搬动，
     连顺序没变的重绘也照搬，滚动容器上把 scrollTop 顶掉 —— 这就是「视图重排」。
     现在只搬位置不对的行，且顺序没变时连 Flip 都不进。
  2. `listFlip` 的 `absolute: true`：动画期间行被拿出文档流，列表内容高度当场塌掉（滚动位置
     被夹回）、绝对定位的行不再被 `overflow:auto` 裁剪（画到好友列表上）、而 mutate 里**新插入**
     的那一行在塌掉的流里排版（正好画在「文件传输助手」那格上）。行尺寸不变，transform 就够。

Skips (never fails) where playwright is missing, so CI without a browser stays green.
"""

import time

import pytest

from fungi.config import Config
from fungi.events import NullSink
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="http://127.0.0.1:1/v1", model="m1")
N_SESSIONS = 16  # enough rows that the list scrolls in a 620px-tall window

WATCHDOG = r"""
() => {
  const list = document.getElementById('session-list');
  window.__n = 0;
  window.__o && window.__o.disconnect();
  window.__o = new MutationObserver(rs => { for (const r of rs) if (r.type === 'childList') window.__n++; });
  window.__o.observe(list, {childList: true});
  window.__count = () => window.__n;
  window.__resetCount = () => { window.__n = 0; };
  window.__stamp = () => { Array.from(list.querySelectorAll('.session-row')).forEach((r, i) => { r.__probe = i; }); return 1; };
  window.__stamps = () => Array.from(list.querySelectorAll('.session-row')).filter(r => r.__probe !== undefined).length;
  window.__scroll = v => { if (v !== undefined) list.scrollTop = v; return Math.round(list.scrollTop); };
  window.__order = () => Array.from(list.querySelectorAll('.session-row')).map(r => r.dataset.sid);
  // Per-frame record: where every row paints and whether any is out of flow.
  window.__frames = [];
  window.__on = false;
  const snap = () => {
    const rows = Array.from(list.querySelectorAll('.session-row'));
    window.__frames.push({
      abs: rows.filter(r => getComputedStyle(r).position === 'absolute').length,
      tops: Object.fromEntries(rows.map(r => [r.dataset.sid, Math.round(r.getBoundingClientRect().top)])),
      order: rows.map(r => r.dataset.sid),
    });
  };
  const tick = () => { if (window.__on) snap(); requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  window.__start = () => { window.__frames = []; window.__on = true; };
  window.__stop = () => { window.__on = false; return window.__frames; };
}
"""


@pytest.fixture()
def room(tmp_path):
    # Per test, not per module: each test seeds its own N sessions and asserts on
    # exact row counts, so a shared directory would carry the previous test's rows
    # into this one (that is how the first version of this file timed out waiting
    # for "17 rows" on a list that already had 33).
    root = tmp_path / "room"
    server = RoomServer("alpha", CFG, NullSink(), "tok", root / "d1", rules_path=root / "r1.json")
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
    """A short window on purpose: the list has to be scrollable for the scroll
    position to be part of the report."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 620})
    pg = ctx.new_page()
    pg.goto(f"{room.open_webui(False)}/?t={WEBUI_TOKEN}")
    pg.wait_for_function("() => typeof loadSessions === 'function'")
    pg.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    for i in range(N_SESSIONS):
        sid = pg.evaluate("async () => (await (await fetch('/new', {method:'POST'})).json()).id")
        pg.evaluate(
            """async (a) => { await fetch('/save', {method:'POST',
                 headers:{'Content-Type':'application/json'},
                 body: JSON.stringify({id: a[0], title: a[1], messages: []})}); }""",
            [sid, f"会话{i:02d}"],
        )
    pg.evaluate("async () => { await loadSessions(); }")
    pg.wait_for_function(
        f"() => document.querySelectorAll('#session-list .session-row').length == {N_SESSIONS + 1}"
    )
    time.sleep(0.5)  # let the last list animation finish
    pg.evaluate(WATCHDOG)
    assert pg.evaluate(
        "() => { const l = document.getElementById('session-list'); return l.scrollHeight > l.clientHeight; }"
    ), "this test needs a scrollable list: that is where the old code reset scrollTop"
    try:
        yield pg
    finally:
        ctx.close()


def test_switching_a_session_leaves_the_list_dom_alone(page):
    """切会话只改「谁是当前会话」，列表一个节点都不该动（更不该把滚动位置顶掉）。"""
    page.evaluate("() => window.__stamp()")
    page.evaluate("() => window.__scroll(150)")
    page.evaluate("() => window.__resetCount()")
    before = page.evaluate("() => window.__order()")

    # element.click() in JS: Playwright's own click would scroll the row into view
    # and fake the very scroll change this test is about.
    page.evaluate("() => document.querySelector('#session-list .session-row:not(.active)').click()")
    page.wait_for_function(
        "() => document.querySelectorAll('#session-list .session-row.active').length === 1"
    )
    time.sleep(0.6)

    assert page.evaluate("() => window.__order()") == before, "顺序没变"
    assert page.evaluate("() => window.__count()") == 0, "切一次会话碰了列表的 DOM"
    assert page.evaluate("() => window.__stamps()") == len(before), (
        "行是同一批节点（被复用，不是重建）"
    )
    assert page.evaluate("() => window.__scroll()") == 150, "滚动位置被顶回顶上了"


def test_a_repaint_with_nothing_changed_touches_nothing(page):
    """「停止会话」那种重绘：顺序、标题、内容都没变 —— 0 次 DOM 变更（以前是每行搬一次）。"""
    page.evaluate("() => window.__resetCount()")
    page.evaluate("() => { renderSessionList(); }")
    time.sleep(0.5)
    assert page.evaluate("() => window.__count()") == 0


def test_a_new_session_lands_in_its_own_slot_not_on_the_transfer_session(page):
    """新建会话：新行**第一帧就在自己的位置上**，不是先在「文件传输助手」那格上停一会儿。"""
    before = page.evaluate("() => window.__order()")
    page.evaluate("() => window.__start()")
    page.evaluate("() => document.getElementById('btn-new-session').click()")
    page.wait_for_function(
        "(n) => document.querySelectorAll('#session-list .session-row').length === n + 1",
        arg=len(before),
    )
    time.sleep(0.8)
    frames = page.evaluate("() => window.__stop()")

    final_order = frames[-1]["order"]
    fresh = [s for s in final_order if s not in before]
    assert len(fresh) == 1, f"新建会话该只多一行，多了 {fresh}"
    new_id = fresh[0]
    final_top = frames[-1]["tops"][new_id]

    assert all(f["abs"] == 0 for f in frames), "动画期间有行被拿出文档流（absolute）——列表会塌"
    seen = [f["tops"].get(new_id) for f in frames if new_id in f["tops"]]
    assert seen and all(t == final_top for t in seen), (
        f"新行在自己的位置之前先停在了别处：{seen[:12]} 终点 {final_top}"
    )
    shuttle = [f["tops"].get("file-transfer") for f in frames if new_id in f["tops"]]
    assert not [t for t in seen if t in shuttle], "新行有一帧就画在「文件传输助手」那一格上"
