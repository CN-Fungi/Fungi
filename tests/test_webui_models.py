"""The header's model dropdown (spec §66): the model readout became a picker.

用户 2026-09-25：「在左上角原来显示模型的位置也改成下拉列表」——原来那里只是一个
span，印着 `/model` 给的模型名；现在是个自绘下拉列表，选中哪个就用哪个。列表就是
config.json 的 `model_list`（设置页那个输入框往里加的那一份），所以两个界面不会各说各话。

同一个用户当天看过第一版之后又报：「风格和原来的不搭，没有动画效果」——第一版是原生
`<select>`（弹出层由系统画，吃不到这套 token，也没有开合过程），所以这里同时钉住三件事：
**能切**（写盘 + 自动测一次调用）、**长得像这套 UI**（背景/圆角来自 `--surface`/`--radius-sm`
这些 token）、**开合有过程**（逐帧采到中间帧，不是「啪」一下出现）。

切换 = 一次 `POST /model` + 一次极小的补全调用（这里打桩：测试要断言的正是**页面有没有
把两件事分开报**——切过去了，和它答没答）。两个壳共用 `FC.mountModelPicker`，所以两个都测。

Skips (never fails) where playwright is missing, so CI without a browser stays green.
"""

import json
import time

import pytest

from fungi import config as config_mod
from fungi import server as server_mod
from fungi.config import Config
from fungi.events import NullSink
from fungi.room import RoomServer
from fungi.server import WEBUI_TOKEN

pw_sync = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

CFG = Config(api_key="k", endpoint="http://127.0.0.1:1/v1", model="m1")


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def room(tmp_path_factory):
    root = tmp_path_factory.mktemp("models-room")
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


class Probe:
    """The stubbed probe: its standing answer, plus the names it was asked about.

    A mutable object rather than a module global: the failing test flips `ok`
    for itself only, so test order cannot decide what the next test sees.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ok = True
        self.detail: str | None = None  # None = the provider echoed the name back

    def __call__(self, model, endpoint, api_key, timeout=None):
        self.calls.append(model)
        return (self.ok, self.detail if self.detail is not None else model)


@pytest.fixture()
def asked(monkeypatch):
    """Seed the pickable list the way the settings page would, and stub the probe.

    `probe_model` is monkeypatched on `fungi.server` because that is where the
    route imported it — the browser talks to a server that must not reach the
    network in a test.
    """
    cfg = config_mod.load_config()
    cfg.api_key, cfg.endpoint, cfg.model = "k", "http://127.0.0.1:1/v1", "m1"
    cfg.model_list = ["m1", "m2"]
    cfg.model_providers = {}
    config_mod.save_config(cfg)

    probe = Probe()
    monkeypatch.setattr(server_mod, "probe_model", probe)
    return probe


def _open_page(browser, room, path="/"):
    url = f"{room.open_webui(False)}{path}?t={WEBUI_TOKEN}"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_function("() => typeof loadSessions === 'function'")
    # Same rule as the sibling modules: the seeded config says "configured", so the
    # modal should not be up — this is the belt to that braces.
    page.evaluate("() => document.getElementById('config-overlay')?.classList.remove('show')")
    return ctx, page


@pytest.fixture()
def page(browser, room, asked):
    ctx, pg = _open_page(browser, room)
    try:
        yield pg
    finally:
        ctx.close()


@pytest.fixture()
def mobile_page(browser, room, asked):
    ctx, pg = _open_page(browser, room, path="/m")
    try:
        yield pg
    finally:
        ctx.close()


def _picker(page) -> dict:
    """What the header says: the trigger's label/colour, and the floating panel."""
    return page.evaluate(
        """() => {
             const el = document.getElementById('model-select');
             if (!el) return null;
             const wrap = el.closest('.model-pick');
             const menu = document.querySelector('.model-menu');  // 挂在 body 上的浮层
             const rows = menu ? Array.from(menu.querySelectorAll('.model-item')) : [];
             const cs = menu ? getComputedStyle(menu) : null;
             const box = menu ? menu.getBoundingClientRect() : null;
             return {
               label: (el.textContent || '').trim(),
               cls: el.className, title: el.title,
               expanded: el.getAttribute('aria-expanded'),
               nested: !!wrap,
               menuPos: cs ? cs.position : null,
               menuOnScreen: box ? box.width > 40 && box.height > 10 : false,
               nativeSelects: document.querySelectorAll('select#model-select, #header select, #topbar select').length,
               menuHidden: menu ? menu.hidden : null,
               menuRole: menu ? menu.getAttribute('role') : null,
               menuBg: cs ? cs.backgroundColor : null,
               menuRadius: cs ? cs.borderRadius : null,
               caret: getComputedStyle(wrap.querySelector('.model-caret')).transform,
               rows: rows.map(r => r.dataset.model),
               currentRow: (rows.find(r => r.classList.contains('current')) || {}).dataset?.model || '',
               tickOn: rows.filter(r => parseFloat(getComputedStyle(r.querySelector('.model-item-tick')).opacity) > 0.5).length,
               legacy: !!document.getElementById('model-name'),
               status: (document.getElementById('status') || {}).textContent || '',
             };
           }"""
    )


def _pick(page, name: str) -> None:
    """Open the dropdown the way a user does and click a row.

    Only clicks the trigger when the panel is really shut: it is a toggle, and a test
    that clicks it while open would just close it again. `aria-expanded` is the state
    the control itself writes (synchronously); `hidden` only flips when the closing
    animation finishes, so it would lie to a fast second click.
    """
    if page.evaluate(
        "() => document.getElementById('model-select').getAttribute('aria-expanded') !== 'true'"
    ):
        page.click("#model-select")
    page.click(f'.model-item[data-model="{name}"]')


def test_the_header_is_a_dropdown_that_switches_the_model(page, asked):
    """桌面上：左上角那个模型是自绘下拉列表，选中一个 = 换过去 + 自动测一次调用。"""
    page.wait_for_function("() => document.querySelectorAll('.model-item').length")
    before = _picker(page)
    assert before["nested"] is True and before["menuHidden"] is True, "收着的时候面板不在屏上"
    assert before["label"] == "m1", "触发器上印的就是正在用的那个"
    assert before["rows"] == ["m1", "m2"], "列表就是 config.json 里那份"
    assert before["currentRow"] == "m1" and before["tickOn"] == 1, "正在用的那个带勾"
    assert before["menuRole"] == "listbox", "自绘面板而不是原生 select"
    assert before["menuPos"] == "fixed", (
        "浮层挂在 body 上：头部的 overflow 裁不到它（手机那条正是坑）"
    )
    assert before["nativeSelects"] == 0, "原生 <select> 已经不在页面上了"
    assert before["legacy"] is False, "原来那个只读的 span 也不在了"

    page.click("#model-select")
    assert page.evaluate("() => !document.querySelector('.model-menu').hidden"), "点开就摊开"
    assert _picker(page)["expanded"] == "true"
    assert _picker(page)["menuOnScreen"] is True, "摊开是真的在屏上（有尺寸），不是被裁成 0"
    _pick(page, "m2")

    assert _wait(lambda: asked.calls == ["m2"]), "选一个就自动测一次调用"
    assert _wait(lambda: "ok" in _picker(page)["cls"]), "测试通过：颜色留在触发器上"
    assert _wait(lambda: _picker(page)["menuHidden"] is True), "选完自己收起来"
    after = _picker(page)
    assert after["label"] == "m2", "触发器跟着换成新模型"
    assert "m2" in after["status"], "状态行也说了这次切换"

    saved = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert (saved["model"], saved["model_list"]) == ("m2", ["m2", "m1"]), "写盘了，旧的还在列表里"

    # 页面开着不动，列表也不会自己变回去（重新读一次 /model 仍是刚选的）
    assert page.evaluate("async () => (await (await fetch('/model')).json()).model") == "m2"


def test_the_dropdown_wears_this_uis_tokens_and_animates(page, asked):
    """用户 2026-09-25：「风格和原来的不搭，没有动画效果」。

    两条都钉住：面板的底色/圆角来自这一页的 token（`--surface`/`--radius-sm`，主题一换就跟着
    换——不是写死的颜色），并且开合真有中间帧（逐帧记 opacity，GSAP 那 200ms 里必须采到
    「既不是 0 也不是 1」的一帧；瞬时出现就是在骗人）。
    """
    page.wait_for_function("() => document.querySelectorAll('.model-item').length")
    look = page.evaluate(
        """() => {
             const cs = getComputedStyle(document.querySelector('.model-menu'));
             const probe = document.createElement('div');
             probe.style.color = getComputedStyle(document.documentElement)
               .getPropertyValue('--surface').trim();
             document.body.appendChild(probe);
             const surface = getComputedStyle(probe).color;
             probe.remove();
             return { bg: cs.backgroundColor, radius: cs.borderRadius,
                      surface,
                      tokenRadius: getComputedStyle(document.documentElement)
                        .getPropertyValue('--radius-sm').trim() };
           }"""
    )
    assert look["bg"] == look["surface"], "面板底色就是 --surface（换主题跟着换），不是写死的颜色"
    assert look["radius"] == look["tokenRadius"], "圆角就是 --radius-sm"

    # 开合逐帧：按下触发器后一路采到 400ms，间或采到中间透明度 = 真在动
    frames = page.evaluate(
        """async () => {
             const menu = document.querySelector('.model-menu');
             const seen = [];
             const t0 = performance.now();
             document.getElementById('model-select').click();
             while (performance.now() - t0 < 400) {
               seen.push(parseFloat(getComputedStyle(menu).opacity));
               await new Promise(r => requestAnimationFrame(r));
             }
             return { seen, caret: getComputedStyle(document.querySelector('.model-caret')).transform };
           }"""
    )
    mid = [o for o in frames["seen"] if 0 < o < 1]
    assert frames["seen"], "采到了帧"
    assert mid, f"开的那一下要有过程（采到这些透明度：{frames['seen'][:8]}）"
    assert frames["seen"][-1] > 0.9, "最后还是完全摊开"
    assert frames["caret"] not in ("none", "matrix(1, 0, 0, 1, 0, 0)"), "打开时箭头翻过去了"

    # 关回去也有过程，关完才从屏上撤掉
    shut = page.evaluate(
        """async () => {
             const menu = document.querySelector('.model-menu');
             const seen = [];
             const t0 = performance.now();
             document.getElementById('model-select').click();
             while (performance.now() - t0 < 400) {
               const opacity = getComputedStyle(menu).opacity;
               seen.push(menu.hidden ? null : parseFloat(opacity));
               await new Promise(r => requestAnimationFrame(r));
             }
             return { seen, hidden: menu.hidden };
           }"""
    )
    assert [o for o in shut["seen"] if o is not None and 0 < o < 1], "收的那一下也要有过程"
    assert shut["hidden"] is True, "收完才 hidden"


def test_a_model_that_does_not_answer_is_marked_but_still_used(page, asked):
    """调不通：页面给红标记 + provider 那句话，但选择**不撤回**（写盘的是用户选的那个）。"""
    asked.ok = False
    asked.detail = "HTTP 404: model not found"

    page.wait_for_function("() => document.querySelectorAll('.model-item').length")
    _pick(page, "m2")

    assert _wait(lambda: "bad" in _picker(page)["cls"]), "答不上来是红标记"
    shown = _picker(page)
    assert "m2" in shown["status"] and "model not found" in shown["status"]
    assert shown["title"], "完整那句留在 title 里，状态行被下一轮对话顶掉也还看得见"

    saved = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["model"] == "m2", "用户选的就是用户选的：不改回上一个"


def test_switching_a_model_brings_its_own_endpoint_and_key(page, asked):
    """§68（用户 2026-09-25「每次调用 200 以后，之后切换模型应该随之切换 url 和 key」）：
    一个列表里住两家时，切到谁就把谁那套 url+key 写进去；页面也报出落在哪台主机上。"""
    cfg = config_mod.load_config()
    cfg.model_providers = {
        "m1": {"endpoint": "http://127.0.0.1:1/v1", "api_key": "k"},
        "m2": {"endpoint": "https://other.example/v1/chat/completions", "api_key": "sk-other"},
    }
    config_mod.save_config(cfg)

    page.wait_for_function("() => document.querySelectorAll('.model-item').length")
    _pick(page, "m2")
    assert _wait(lambda: asked.calls == ["m2"])

    saved = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["endpoint"] == "https://other.example/v1/chat/completions", "端点跟着模型换了"
    assert saved["api_key"] == "sk-other", "密钥也跟着换了"
    # 等那句结果：POST 还没回来时状态行上是「Switching to m2…」，读到它就把话说早了（曾经偶发红）
    assert _wait(lambda: "other.example" in _picker(page)["title"], 6.0), (
        "结果里报出落在哪台主机上：换没换 url，界面自己说得清"
    )

    # 切回去：上一次那套（m1 在 asked 里种的那套）也得回得来——它被学过一次
    _pick(page, "m1")
    assert _wait(
        lambda: json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))["model"] == "m1"
    )
    back = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert back["endpoint"] == "http://127.0.0.1:1/v1" and back["api_key"] == "k"


def test_a_probe_that_answers_teaches_the_pair(page, asked):
    """§68 的「学」那一半（WebUI 这条同步路径）：这次调用回 200，就把它用的 url+key
    记在这个名字上；答不上来则什么都不记。"""
    page.wait_for_function("() => document.querySelectorAll('.model-item').length")
    _pick(page, "m2")
    assert _wait(lambda: asked.calls == ["m2"])

    saved = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["model_providers"] == {"m2": {"endpoint": "http://127.0.0.1:1/v1", "api_key": "k"}}

    asked.ok = False
    asked.detail = "HTTP 404: nope"
    asked.calls.clear()
    _pick(page, "m1")
    assert _wait(lambda: asked.calls == ["m1"])
    assert (
        "m1"
        not in json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))["model_providers"]
    )


def test_the_mobile_topbar_picks_models_the_same_way(mobile_page, asked):
    """手机壳（`/m`）：同一份 config、同一个共享实现，头部那个模型也是下拉列表。"""
    mobile_page.wait_for_function("() => document.querySelectorAll('.model-item').length")
    assert _picker(mobile_page)["rows"] == ["m1", "m2"]

    _pick(mobile_page, "m2")

    assert _wait(lambda: asked.calls == ["m2"])
    assert _picker(mobile_page)["label"] == "m2"
    assert json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))["model"] == "m2"
