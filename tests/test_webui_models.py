"""The header's model dropdown (spec §66): the model readout became a picker.

用户 2026-09-25：「在左上角原来显示模型的位置也改成下拉列表」——原来那里只是一个
span，印着 `/model` 给的模型名；现在是个 `<select>`，选中哪个就用哪个。列表就是
config.json 的 `model_list`（设置页那个输入框往里加的那一份），所以两个界面不会各说各话。

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
    """What the header says: the options, which one is selected, its colour class."""
    return page.evaluate(
        """() => {
             const el = document.getElementById('model-select');
             if (!el) return null;
             return { options: Array.from(el.options).map(o => o.value),
                      value: el.value, cls: el.className, title: el.title,
                      legacy: !!document.getElementById('model-name'),
                      status: (document.getElementById('status') || {}).textContent || '' };
           }"""
    )


def test_the_header_is_a_dropdown_that_switches_the_model(page, asked):
    """桌面上：左上角那个模型现在是下拉列表，选中一个 = 换过去 + 自动测一次调用。"""
    page.wait_for_function("() => document.getElementById('model-select').options.length")
    before = _picker(page)
    assert before["options"] == ["m1", "m2"], "列表就是 config.json 里那份"
    assert before["value"] == "m1", "开着的就是正在用的那个"
    assert before["legacy"] is False, "原来那个只读的 span 已经不在页面上了"

    page.select_option("#model-select", "m2")

    assert _wait(lambda: asked.calls == ["m2"]), "选一个就自动测一次调用"
    after = _picker(page)
    assert after["value"] == "m2"
    assert "ok" in after["cls"], "测试通过：颜色留在 select 上"
    assert "m2" in after["status"], "状态行也说了这次切换"

    saved = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert (saved["model"], saved["model_list"]) == ("m2", ["m2", "m1"]), "写盘了，旧的还在列表里"

    # 页面开着不动，列表也不会自己变回去（重新读一次 /model 仍是刚选的）
    assert page.evaluate("async () => (await (await fetch('/model')).json()).model") == "m2"


def test_a_model_that_does_not_answer_is_marked_but_still_used(page, asked):
    """调不通：页面给红标记 + provider 那句话，但选择**不撤回**（写盘的是用户选的那个）。"""
    asked.ok = False
    asked.detail = "HTTP 404: model not found"

    page.wait_for_function("() => document.getElementById('model-select').options.length")
    page.select_option("#model-select", "m2")

    assert _wait(lambda: "bad" in _picker(page)["cls"]), "答不上来是红标记"
    shown = _picker(page)
    assert "m2" in shown["status"] and "model not found" in shown["status"]
    assert shown["title"], "完整那句留在 title 里，状态行被下一轮对话顶掉也还看得见"

    saved = json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["model"] == "m2", "用户选的就是用户选的：不改回上一个"


def test_the_mobile_topbar_picks_models_the_same_way(mobile_page, asked):
    """手机壳（`/m`）：同一份 config、同一个共享实现，头部那个模型也是下拉列表。"""
    mobile_page.wait_for_function("() => document.getElementById('model-select').options.length")
    assert _picker(mobile_page)["options"] == ["m1", "m2"]

    mobile_page.select_option("#model-select", "m2")

    assert _wait(lambda: asked.calls == ["m2"])
    assert _picker(mobile_page)["value"] == "m2"
    assert json.loads(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))["model"] == "m2"
