"""GUI launcher smoke: three pages construct offscreen; validation logic holds."""

import json
import os
import pathlib
import time
import uuid

import pytest

pytest.importorskip("qfluentwidgets", reason="PyQt6-Fluent-Widgets (qfluentwidgets) not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSharedMemory, Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtNetwork import QLocalSocket
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog

from fungi import gui
from fungi import server as webui_server
from fungi.gui import FungiGui, valid_host_name


@pytest.fixture(scope="module")
def window(qapp):
    # Unique pipe name: the user's real running Fungi owns the production
    # _GUI_IPC pipe (Windows serves clients from the OLDEST same-name server),
    # so a fixed name makes the second-launch test hit the wrong window.
    gui.app._GUI_IPC, _real = "fungi-gui-test-" + uuid.uuid4().hex[:8], gui.app._GUI_IPC
    win = FungiGui()
    yield win
    # close() only hides the window: without this the QLocalServer keeps the
    # fixed _GUI_IPC pipe alive, the NEXT fixture window's listen silently
    # fails, and the second-launch test talks to a stale unpatched window.
    win._ipc_server.close()
    gui.app._GUI_IPC = _real
    win.close()


class FakeRoom:
    def __init__(self):
        self.stopped = False
        self.webui_calls = 0

    def stop(self):
        self.stopped = True

    def open_webui(self, open_browser=True):
        self.webui_calls += 1  # the tray's click target (tests/test_gui.py tray cases)
        return "http://localhost:1"


@pytest.fixture(autouse=True)
def _fresh_pages(window):
    """Module-scoped window: reset mutable page state between tests."""
    yield
    window.host_page.room = None
    window.host_page.ip_edit.clear()
    window.host_page.token_edit.clear()
    window.host_page._set_started(False)
    window.join_page.room = None
    window.join_page.join_btn.setEnabled(True)
    window.join_page.leave_btn.setVisible(False)
    window.mobile_page._fw_prompted = False  # the auto-UAC fires once per page life


@pytest.fixture(autouse=True)
def _no_live_firewall_probe(monkeypatch):
    """Never shell out to PowerShell from a GUI test: it is slow, and the verdict
    depends on this machine's own rules. Each test stubs the verdict it needs."""
    from fungi.gui import firewall

    monkeypatch.setattr(firewall, "start_check", lambda program=None: None)
    # The blocked state raises UAC by itself now: no test may reach the real one.
    monkeypatch.setattr(firewall, "request_allow", lambda program=None: None)
    yield
    firewall.forget()


def test_three_pages_present(window):
    assert window.host_page.objectName() == "hostPage"
    assert window.join_page.objectName() == "joinPage"
    assert window.mobile_page.objectName() == "mobilePage"
    assert window.cfg_page.objectName() == "configPage"
    # status card hidden until the room is launched
    assert not window.host_page.ip_row.isVisibleTo(window.host_page)
    window.host_page._set_started(True)
    assert window.host_page.ip_row.isVisibleTo(window.host_page)
    assert not window.host_page.start_btn.isEnabled()
    window.host_page._set_started(False)


def test_mobile_page_keeps_the_url_when_the_qr_dependency_is_missing(window, monkeypatch):
    """2026-09-10 用户报告：二维码报错「缺少依赖 segno」。缺依赖时页面必须仍然可用——
    地址照填（手机手输也能进）+ 一键安装，而不是只留一句报错就返回。"""
    import sys

    from fungi.server import WEBUI_TOKEN

    page = window.mobile_page

    class FakeWebRoom:
        def open_webui(self, open_browser=True):
            return "http://localhost:12345"

    window.host_page.room = FakeWebRoom()
    monkeypatch.setitem(sys.modules, "segno", None)  # `import segno` -> ImportError
    try:
        page.refresh()
        assert page.url_edit.text() == f"http://{gui.lan_ip()}:12345/m?t={WEBUI_TOKEN}"
        assert "segno" in page.qr_label.text()
        assert page.qr_dep_btn.isVisibleTo(page)

        started: list[list[str]] = []

        class FakePopen:
            returncode = 0

            def __init__(self, cmd, **_kw):
                started.append(list(cmd))

            def poll(self):
                return 0

        monkeypatch.setattr(gui.subprocess, "Popen", FakePopen)
        page.qr_dep_btn.click()
        assert started and started[0][1:3] == ["-m", "pip"] and started[0][-1] == "segno"
    finally:
        page._dep_timer.stop()
        page._dep_proc = None
        window.host_page.room = None
        page.refresh()  # segno 仍在 monkeypatch 里：这里只清干净，隐藏断言放在有依赖的用例


def test_mobile_page_renders_qr_for_running_room(window):
    from fungi.server import WEBUI_TOKEN

    page = window.mobile_page
    # no room yet: placeholder text, empty URL field, no QR pixmap
    assert page.url_edit.text() == ""
    assert page.qr_label.pixmap() is None or page.qr_label.pixmap().isNull()

    class FakeWebRoom:
        def open_webui(self, open_browser=True):
            return "http://localhost:12345"

    window.host_page.room = FakeWebRoom()
    try:
        page.refresh()
        assert page.url_edit.text() == (f"http://{gui.lan_ip()}:12345/m?t={WEBUI_TOKEN}")
        pm = page.qr_label.pixmap()
        assert pm is not None and not pm.isNull()
        assert not page.qr_dep_btn.isVisibleTo(page)  # 依赖在 → 不给安装按钮
    finally:
        window.host_page.room = None
        page.refresh()
    assert page.url_edit.text() == ""


def test_settings_put_the_video_understanding_under_extensions(window):
    """用户定调（2026-09-12 / 2026-09-15）：视频理解不再是顶层「VidSense」段，
    而是末段「拓展」之下的小标题——判据是它可以独立存在（VidSense 是个独立项目）。"""
    page = window.cfg_page
    root = page.layout()

    def slot(target):
        """Row index holding `target`: a row is either a widget or a sub-layout."""
        for i in range(root.count()):
            item = root.itemAt(i)
            sub = item.layout()
            if item.widget() is target or (sub is not None and sub.indexOf(target) >= 0):
                return i
        return -1

    def heading(text):
        for i in range(root.count()):
            widget = root.itemAt(i).widget()
            if widget is not None and getattr(widget, "text", lambda: None)() == text:
                return i
        return -1

    experimental = heading("实验性")
    assert experimental > -1, "「实验性」大标题不见了"
    assert heading("VidSense") == -1, "视频理解又回到顶层了"
    assert slot(page.diary_switch) > experimental, "日记仍属实验性"

    extensions = heading("拓展")
    assert extensions > experimental, "「拓展」应当在「实验性」之后"
    assert slot(page.video_label) > extensions, "视频理解属于拓展（能独立存在）"
    assert slot(page.gw_switch) > extensions, "GhostWorld 同理"


def test_the_desktop_control_switch_sits_under_experimental_and_disarms_when_off(
    window, monkeypatch
):
    """设置页的桌面控制开关（spec §35.2）：即时写盘；关掉时立刻收回已经给出的授权。"""
    from fungi.gui import config as config_page
    from fungi.tools import screen

    saved = {}
    disarmed = []
    monkeypatch.setattr(
        config_page.config_mod, "save_config", lambda cfg: saved.update(pc_control=cfg.pc_control)
    )
    monkeypatch.setattr(screen, "disarm", lambda: disarmed.append(True))

    page = window.cfg_page
    root = page.layout()
    experimental = -1
    for i in range(root.count()):
        widget = root.itemAt(i).widget()
        if widget is not None and getattr(widget, "text", lambda: None)() == "实验性":
            experimental = i
    assert experimental > -1

    def slot(target):
        for i in range(root.count()):
            item = root.itemAt(i)
            sub = item.layout()
            if item.widget() is target or (sub is not None and sub.indexOf(target) >= 0):
                return i
        return -1

    assert slot(page.pc_switch) > experimental

    # The switch starts from whatever this machine's config says, so a single
    # setChecked() only fires when it is the other value: force a transition in
    # both directions instead. Nothing here may touch the real config.json —
    # save_config is patched, and the file bytes are compared to pin that.
    def config_bytes():
        path = pathlib.Path("config.json")
        return path.read_bytes() if path.is_file() else None

    before = config_bytes()
    if page.pc_switch.isChecked():
        page.pc_switch.setChecked(False)
        saved.clear()
        disarmed.clear()

    page.pc_switch.setChecked(True)
    assert saved["pc_control"] is True
    saved.clear()
    page.pc_switch.setChecked(False)
    assert saved["pc_control"] is False
    assert disarmed == [True]
    assert config_bytes() == before


def test_the_ghostworld_switch_sits_under_extensions_and_disarms_when_off(window, monkeypatch):
    """设置页的角色控制开关（spec §43）：即时写盘；关掉时立刻结束监视进程。"""
    from fungi.gui import config as config_page
    from fungi.tools import ghostworld

    saved = {}
    disarmed = []
    monkeypatch.setattr(
        config_page.config_mod,
        "save_config",
        lambda cfg: saved.update(ghostworld=cfg.ghostworld),
    )
    monkeypatch.setattr(ghostworld, "disarm", lambda: disarmed.append(True))

    page = window.cfg_page
    root = page.layout()
    extensions = -1
    for i in range(root.count()):
        widget = root.itemAt(i).widget()
        if widget is not None and getattr(widget, "text", lambda: None)() == "拓展":
            extensions = i
    assert extensions > -1, "「拓展」大标题不见了"

    def slot(target):
        for i in range(root.count()):
            item = root.itemAt(i)
            sub = item.layout()
            if item.widget() is target or (sub is not None and sub.indexOf(target) >= 0):
                return i
        return -1

    assert slot(page.gw_switch) > extensions

    # The switch starts from this machine's config, so one setChecked() only
    # fires on a real change: force a transition both ways. Nothing may touch
    # the real config.json — save_config is patched and the bytes are compared.
    def config_bytes():
        path = pathlib.Path("config.json")
        return path.read_bytes() if path.is_file() else None

    before = config_bytes()
    if page.gw_switch.isChecked():
        page.gw_switch.setChecked(False)
        saved.clear()
        disarmed.clear()

    page.gw_switch.setChecked(True)
    assert saved["ghostworld"] is True
    saved.clear()
    page.gw_switch.setChecked(False)
    assert saved["ghostworld"] is False
    assert disarmed == [True]
    assert config_bytes() == before


def test_the_ghostworld_dir_box_normalizes_a_pasted_path(window, monkeypatch):
    """游戏目录输入框：右键"复制文件地址"给的就是带引号、单反斜杠的字符串。

    它得照用，而且写进 config.json 的必须是合法 JSON —— 单反斜杠是非法转义，
    会让整份配置（连 api key）读不出来，所以回车时当场规范成正斜杠。
    """
    from fungi.gui import config as config_page

    saved = {}
    monkeypatch.setattr(
        config_page.config_mod,
        "save_config",
        lambda cfg: saved.update(dir=cfg.ghostworld_dir),
    )

    page = window.cfg_page
    root = page.layout()
    extensions = -1
    for i in range(root.count()):
        widget = root.itemAt(i).widget()
        if widget is not None and getattr(widget, "text", lambda: None)() == "拓展":
            extensions = i
    assert extensions > -1, "「拓展」大标题不见了"

    def slot(target, layout=None):
        """Top-level index of the item holding `target` — rows nest inside a holder."""
        layout = root if layout is None else layout
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is target:
                return i
            holder = item.widget()
            inner = item.layout() or (holder.layout() if holder is not None else None)
            if inner is not None and slot(target, inner) != -1:
                return i
        return -1

    assert slot(page.gw_dir_edit) > extensions, "游戏目录框要在「拓展」一节里"
    assert "\\" not in page.gw_dir_edit.text(), "框里显示的是规范化之后的值"

    page.gw_dir_edit.setText('"C:\\Users\\me\\GhostWorld"')  # 原样粘贴
    QTest.keyClick(page.gw_dir_edit, Qt.Key_Return)

    assert saved["dir"] == "C:/Users/me/GhostWorld", "写进去的是不含转义问题的形式"
    assert page.gw_dir_edit.text() == "C:/Users/me/GhostWorld", "框里显示的就是存下来的那份"


def test_opening_the_settings_page_repairs_an_unreadable_config(window):
    """读不出来的 config.json（单反斜杠那类）在进页面时就改写成合法 JSON。

    同时也钉住写盘只落在重定向后的路径上：真 config.json 一个字节都不许动
    （模块级窗口 fixture 先于函数级重定向建立，构造期写盘的写法会打穿它）。
    """
    from PyQt5.QtGui import QShowEvent

    from fungi.gui import config as config_page

    target = config_page.config_mod.CONFIG_PATH
    target.write_text(
        '{"api_key": "k", "ghostworld": true, "ghostworld_dir": "C:\\Users\\me\\GhostWorld"}',
        encoding="utf-8",
    )
    real = pathlib.Path("config.json")
    real_before = real.read_bytes() if real.is_file() else None

    window.cfg_page.showEvent(QShowEvent())

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["api_key"] == "k" and data["ghostworld"] is True
    assert data["ghostworld_dir"] == "C:/Users/me/GhostWorld"
    assert window.cfg_page.gw_dir_edit.text() == "C:/Users/me/GhostWorld", "框里跟着刷新"
    assert (real.read_bytes() if real.is_file() else None) == real_before, "真 config 不许被碰"


def test_the_bixian_row_sits_under_experimental_before_extensions(window):
    """Bixian是桌面控制的另一半（intent= 的挑号者），所以归「实验性」、在「拓展」之前
    （用户 2026-09-23 点名：设置页该有 bixian 的配置入口）。"""
    page = window.cfg_page
    root = page.layout()

    def slot(target):
        """Row index holding `target`: a widget, a sub-layout, or inside a `_row` holder."""
        for i in range(root.count()):
            item = root.itemAt(i)
            widget, sub = item.widget(), item.layout()
            if widget is target or (sub is not None and sub.indexOf(target) >= 0):
                return i
            holder = widget.layout() if widget is not None else None
            if holder is not None and holder.indexOf(target) >= 0:
                return i
        return -1

    def heading(text):
        for i in range(root.count()):
            widget = root.itemAt(i).widget()
            if widget is not None and getattr(widget, "text", lambda: None)() == text:
                return i
        return -1

    experimental, extensions = heading("实验性"), heading("拓展")
    assert experimental > -1 and extensions > experimental
    for field in (page.bixian_url, page.bixian_serve):
        assert experimental < slot(field) < extensions, "Bixian必须夹在实验性与拓展之间"


def test_the_bixian_row_prefills_and_writes_only_the_decider_block(window):
    """两格从盘里填、回车写盘；整块原样读写——手写的键（timeout）与 decider 之外的设置一个不动。"""
    from PyQt5.QtGui import QShowEvent

    from fungi.gui import config as config_page

    cfg = config_page.config_mod.load_config()
    cfg.decider = {"url": "http://127.0.0.1:9999", "timeout": 5}
    config_page.config_mod.save_config(cfg)
    page = window.cfg_page
    page.showEvent(QShowEvent())  # 真实进页路径：预填 + 状态行
    assert page.bixian_url.text() == "http://127.0.0.1:9999"
    assert "已保存" in page.bixian_status.text()

    target = config_page.config_mod.CONFIG_PATH
    before = json.loads(target.read_text(encoding="utf-8"))
    page.bixian_url.setText("http://127.0.0.1:8111")
    page.bixian_serve.setText(r"C:\Tools\Bixian\decider.py --start")
    page.bixian_url.returnPressed.emit()  # 回车即写盘，本页每个框同一条规矩

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["decider"] == {
        "url": "http://127.0.0.1:8111",
        "timeout": 5,
        "serve": ["C:/Tools/Bixian/decider.py", "--start"],
    }, "只碰地址与启动命令：手写的键原样留着，粘进来的路径换正斜杠"
    assert {k: v for k, v in data.items() if k != "decider"} == {
        k: v for k, v in before.items() if k != "decider"
    }, "decider 之外一个键都不许动"
    assert page.bixian_serve.text() == "C:/Tools/Bixian/decider.py --start", "框里 = 盘里"


def test_a_command_with_a_space_in_it_survives_a_second_save(window):
    """启动命令里带空格的路径必须加引号再拼回去：存两次切坏一次，服务就起不来了。"""
    from fungi.gui import config as config_page

    page = window.cfg_page
    page.bixian_url.setText("http://127.0.0.1:8111")
    page.bixian_serve.setText('python "C:/Program Files/Bixian/decider.py" --start')
    page._save_bixian()
    page._load_fields()  # 模拟关掉再打开设置页：两格重新从盘里填
    page._save_bixian()  # 第二次存盘：没改内容回车，也不许把它切碎

    data = json.loads(config_page.config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert data["decider"]["serve"] == [
        "python",
        "C:/Program Files/Bixian/decider.py",
        "--start",
    ]


def test_a_command_line_whose_quotes_never_close_is_refused(window):
    """引号不成对 → 拒写并说清楚，而不是存一个切错的 argv、埋在下次真正启动时爆。"""
    from fungi.gui import config as config_page

    target = config_page.config_mod.CONFIG_PATH
    before = target.read_bytes()
    page = window.cfg_page
    page.bixian_url.setText("http://127.0.0.1:8111")
    page.bixian_serve.setText('python "C:/broken')

    page._save_bixian()

    assert target.read_bytes() == before, "读不出来的命令一个字节都不许落盘"


def test_the_test_button_reports_whether_the_service_answers(window, monkeypatch):
    """「保存并测试」：先写盘，再问一次 /health，把事实翻成人话——连不上要说地址和原因。"""
    from fungi.gui import config as config_page
    from fungi.tools import screen

    page = window.cfg_page
    page.bixian_url.setText("http://127.0.0.1:8111")
    page.bixian_serve.setText("")

    monkeypatch.setattr(screen, "_decider_health", lambda *_a, **_k: None)
    page._test_bixian()
    assert "连不上" in page.bixian_status.text()
    assert "http://127.0.0.1:8111" in page.bixian_status.text()

    monkeypatch.setattr(
        screen, "_decider_health", lambda *_a, **_k: {"loaded": True, "model": "kev-4B"}
    )
    page._test_bixian()
    assert "连上" in page.bixian_status.text() and "kev-4B" in page.bixian_status.text()
    data = json.loads(config_page.config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert data["decider"]["url"] == "http://127.0.0.1:8111", "按钮测的就是框里那份（先存）"


def test_opening_the_page_never_asks_the_network(window, monkeypatch):
    """进页面的状态行只读盘：/health 最坏卡界面 3 秒，不许挂在打开设置页的路上。"""
    from PyQt5.QtGui import QShowEvent

    from fungi.gui import config as config_page
    from fungi.tools import screen

    cfg = config_page.config_mod.load_config()
    cfg.decider = {"url": "http://127.0.0.1:8111"}
    config_page.config_mod.save_config(cfg)
    monkeypatch.setattr(screen, "_decider_health", lambda *_a, **_k: pytest.fail("进页面就联网了"))

    window.cfg_page.showEvent(QShowEvent())

    assert window.cfg_page.bixian_url.text() == "http://127.0.0.1:8111"
    assert "已保存" in window.cfg_page.bixian_status.text()


def test_firewall_probe_parses_the_rule_count():
    from fungi.gui import firewall

    assert firewall.parse_count("1") is True
    assert firewall.parse_count("0\n") is False
    assert firewall.parse_count("2") is True  # python.exe owns two rules
    assert firewall.parse_count("Get-NetFirewallRule : Access is denied.\n0") is False
    assert firewall.parse_count("") is None
    assert firewall.parse_count("PowerShell is not recognized\n") is None


def test_firewall_allow_rule_targets_this_program_only():
    """交给 Windows 的放行脚本必须是「入站 / 允许 / 只认这个程序」。"""
    import base64

    from fungi.gui import firewall

    exe = r"C:\Program Files\Fungi\Fungi.exe"
    cmd = firewall.allow_command(exe)
    assert cmd[0].lower().startswith("powershell")
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    assert exe in script
    assert "-Direction Inbound -Action Allow" in script
    assert firewall.rule_name(exe) == "Fungi mobile WebUI (Fungi.exe)"


def test_mobile_page_offers_the_firewall_fix_when_this_program_is_blocked(window, monkeypatch):
    """2026-09-12 用户报告：exe 扫码进不去移动 WebUI——打包版从没被防火墙放行过
    （源码版 python.exe 早就放行）。页面要看得出来，并把放行做成一次点击。"""
    from fungi.gui import firewall

    page = window.mobile_page

    class FakeWebRoom:
        def open_webui(self, open_browser=True):
            return "http://localhost:12345"

    window.host_page.room = FakeWebRoom()
    monkeypatch.setattr(firewall, "supported", lambda: True)
    try:
        monkeypatch.setattr(firewall, "cached", lambda program=None: False)
        page.refresh()
        assert page.fw_btn.isVisibleTo(page)
        assert "防火墙" in page.fw_label.text()
        assert firewall.program_label() in page.fw_label.text()

        # 2026-09-12 用户：「放行之后整页安静，我看不出来有自检这回事」——状态常显。
        monkeypatch.setattr(firewall, "cached", lambda program=None: True)
        page.refresh()
        assert page.fw_label.isVisibleTo(page)
        assert "已放行" in page.fw_label.text()
        assert not page.fw_btn.isVisibleTo(page)

        asked: list[str] = []
        monkeypatch.setattr(firewall, "cached", lambda program=None: False)
        monkeypatch.setattr(
            firewall, "request_allow", lambda program=None: asked.append("runas") or None
        )
        page.refresh()
        page.fw_btn.click()
        assert asked == ["runas"]  # 点按钮 = 请 Windows 提权加规则
    finally:
        window.host_page.room = None
        page.refresh()
        assert not page.fw_btn.isVisibleTo(page)  # 没房间就不显示
        assert not page.fw_label.isVisibleTo(page)


def test_mobile_page_asks_windows_for_the_rule_by_itself_only_once(window, monkeypatch):
    """2026-09-12 用户追问「为啥不直接 UAC」：检测到被挡就自己提权，不必先点按钮；
    取消过就不再自动纠缠（按钮留着随时重试）。"""
    from fungi.gui import firewall

    page = window.mobile_page

    class FakeWebRoom:
        def open_webui(self, open_browser=True):
            return "http://localhost:12345"

    calls: list[str] = []
    window.host_page.room = FakeWebRoom()
    monkeypatch.setattr(firewall, "supported", lambda: True)
    monkeypatch.setattr(firewall, "cached", lambda program=None: False)
    monkeypatch.setattr(
        firewall, "request_allow", lambda program=None: calls.append("runas") or None
    )
    try:
        page.refresh()
        assert calls == ["runas"]
        page.refresh()  # 复检仍是被挡：不再自动弹第二次
        assert calls == ["runas"]
        page.fw_btn.click()
        assert calls == ["runas", "runas"]
    finally:
        window.host_page.room = None
        page.refresh()


def test_host_page_starts_server_in_process(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):
        started.append((host, display, token, port))
        return object()

    monkeypatch.setattr(gui.net, "start_server_room", fake_start)
    page.name_edit.setText("pc-alpha")
    page.nick_edit.setText("🌸花酱")
    page._start()
    host, display, token, port = started[0]
    assert host == "pc-alpha" and display == "🌸花酱"
    assert token and len(token) >= 16
    assert gui.GUI_PORT <= port < gui.GUI_PORT + gui.PORT_SCAN_LIMIT  # upward scan
    assert page.room is not None and not page.start_btn.isEnabled()
    page.room = None  # FungiGui.closeEvent must not stop a fake


def test_host_page_self_heals_pure_cjk_name(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):
        started.append((host, display))
        return FakeRoom()

    monkeypatch.setattr(gui.net, "start_server_room", fake_start)
    page.name_edit.setText("小新")  # pure CJK: nothing sanitizable
    page.nick_edit.setText("")
    page._start()
    wire = page.name_edit.text()
    assert gui.valid_host_name(wire) and wire != "小新"
    assert page.nick_edit.text() == "小新"  # the pretty input became the nickname
    assert started == [(wire, "小新")]
    page.room = None


def test_host_page_sanitizes_mixed_name(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(host, display, token, port):
        started.append((host, display))
        return FakeRoom()

    monkeypatch.setattr(gui.net, "start_server_room", fake_start)
    page.name_edit.setText("pc 阿新!")
    page.nick_edit.setText("🌸花酱")  # already set: must not be overwritten
    page._start()
    assert started == [("pc", "🌸花酱")]  # sanitized wire, nickname untouched
    assert page.name_edit.text() == "pc"
    page.room = None


def test_host_page_refreshes_ip(window, monkeypatch):
    page = window.host_page
    page.ip_edit.setText("192.168.1.10")
    monkeypatch.setattr(gui.net, "lan_ip", lambda: "10.0.0.9")
    page._refresh_ip()
    assert page.ip_edit.text() == "10.0.0.9"
    page._refresh_ip()  # unchanged: must not raise
    assert page.ip_edit.text() == "10.0.0.9"


def test_host_page_leave_stops_room(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    page._set_started(True)
    page.token_edit.setText("tok-keep-me")
    page._leave()
    assert room.stopped and page.room is None
    assert page.start_btn.isEnabled() and not page.leave_btn.isVisibleTo(page)
    assert page.ip_edit.text() == ""  # status card reset
    # 离开不再换 Token：下一个房间继续用同一个，好友不必重输
    assert page.token_edit.text() == "tok-keep-me"


def test_gui_remembers_the_host_identity_and_token(window):
    """发起房间页也要记住：Token 每次现场随机生成、昵称不记，用户每次进来都像换了个身份
    （2026-09-10 用户报告）。现在 token / 昵称 / 主机名都从 QSettings 还原。"""
    page = window.host_page
    page.settings.setValue("last_token", "tok-remembered")
    page.settings.setValue("last_nick", "花酱")
    page.settings.setValue("last_host_name", "OwO")
    fresh = gui.host.HostPage(window)  # 新实例＝下次打开
    try:
        assert fresh.token_edit.text() == "tok-remembered"
        assert fresh.nick_edit.text() == "花酱"
        assert fresh.name_edit.text() == "OwO"
    finally:
        fresh.deleteLater()
        for key in ("last_token", "last_nick", "last_host_name"):
            page.settings.remove(key)


def test_gui_settings_live_in_a_sandbox(window):
    """测试绝不能碰用户真正的 QSettings（token/昵称/IP 都在 `HKCU\\...\\FungiGUI`）：
    conftest 把两个页面都指到 test-only 的 app 名上。"""
    for page in (window.join_page, window.host_page):
        name = page.settings.fileName()
        assert name.endswith("FungiGUI-test"), name
        assert not name.endswith("\\FungiGUI"), name


def test_host_page_leave_without_room_is_noop(window):
    window.host_page._leave()  # must not raise


def test_host_page_start_uses_custom_token(window, monkeypatch):
    page = window.host_page
    started = []

    def fake_start(_host, _display, token, _port):
        started.append(token)
        return object()

    monkeypatch.setattr(gui.net, "start_server_room", fake_start)
    page.name_edit.setText("pc-alpha")
    page.token_edit.setText("my-token_1")
    page._start()
    assert started == ["my-token_1"]
    assert page.token_edit.text() == "my-token_1" and page._token == "my-token_1"
    page.room = None


def test_host_page_start_rejects_invalid_token(window):
    page = window.host_page
    page.name_edit.setText("pc-alpha")
    page.token_edit.setText("bad token 中文")  # token rides URLs: spaces/CJK invalid
    page._start()
    assert page.room is None and page.start_btn.isEnabled()


def test_host_page_token_hot_update(window):
    page = window.host_page

    class FakeHub:
        token = "old-token-12"

    page.room = FakeRoom()
    page.room.hub = FakeHub()
    page._token = "old-token-12"
    page.token_edit.setText("new-token-99")
    page._apply_token()
    assert page.room.hub.token == "new-token-99" and page._token == "new-token-99"
    # invalid edit reverts the field and keeps the live token
    page.token_edit.setText("不合法 token")
    page._apply_token()
    assert page.token_edit.text() == "new-token-99"
    assert page.room.hub.token == "new-token-99"
    page.room = None


def test_join_page_leave_stops_room(window):
    page = window.join_page
    room = FakeRoom()
    page.room = room
    page.leave_btn.setVisible(True)
    page._leave()  # stop() sends leave to the hub
    assert room.stopped and page.room is None
    assert page.join_btn.isEnabled() and not page.leave_btn.isVisibleTo(page)


def test_join_page_requires_token(window):
    page = window.join_page
    page.token_edit.setText("")
    before = page.settings.value("last_token", "")
    page._join()  # empty token: warns and returns without spawning
    assert page.settings.value("last_token", "") == before


def test_join_page_discovers_and_joins_in_process(window, monkeypatch):
    joined = []

    def fake_discover(token):
        assert token == "tok"
        return ("192.168.1.20", gui.GUI_PORT + 3)

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return FakeRoom()

    monkeypatch.setattr(gui.net, "discover_room", fake_discover)
    monkeypatch.setattr(gui.net, "start_client_room", fake_client)
    page = window.join_page
    page.ip_edit.clear()  # a real join on this box may have restored last_ip from QSettings
    page.token_edit.setText("tok")  # IP left empty: auto-discovery fills it
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()  # discovery runs on a thread; results arrive via signals
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert page.ip_edit.text() == "192.168.1.20"  # discovered IP visible in the field
    assert joined == [("pc-alpha", "🌸花酱", f"http://192.168.1.20:{gui.GUI_PORT + 3}", "tok")]
    assert page.settings.value("last_token") == "tok"


def test_join_page_uses_typed_ip(window, monkeypatch):
    joined = []

    def fake_probe(ip, token, start=gui.GUI_PORT, limit=gui.PORT_SCAN_LIMIT):
        assert ip == "192.168.1.20" and token == "tok"
        return gui.GUI_PORT + 3

    def fake_client(host, display, url, token):
        joined.append((host, display, url, token))
        return FakeRoom()

    monkeypatch.setattr(gui.net, "probe_room_port", fake_probe)
    monkeypatch.setattr(gui.net, "start_client_room", fake_client)
    page = window.join_page
    page.ip_edit.setText("192.168.1.20")  # typed/refilled IP: no subnet sweep
    page.token_edit.setText("tok")
    page.nick_edit.setText("🌸花酱")
    page.name_edit.setText("pc-alpha")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if joined:
            break
    assert joined == [("pc-alpha", "🌸花酱", "http://192.168.1.20:8902", "tok")]


def test_local_subnet_hosts_covers_self(monkeypatch):
    monkeypatch.setattr(gui.net, "lan_ip", lambda: "192.168.0.104")
    hosts = gui.local_subnet_hosts()
    assert len(hosts) == 254 and hosts[0] == "192.168.0.1" and "192.168.0.104" in hosts


def test_discover_room_finds_matching_host(monkeypatch):
    monkeypatch.setattr(gui.net, "local_subnet_hosts", lambda: ["10.0.0.1", "10.0.0.2"])
    monkeypatch.setattr(
        gui.net,
        "_port_open",
        lambda ip, port, timeout=gui.SWEEP_TIMEOUT: ((ip, port) == ("10.0.0.2", gui.GUI_PORT)),
    )
    monkeypatch.setattr(
        gui.net,
        "_room_accepts",
        lambda ip, port, token: (ip, port) == ("10.0.0.2", gui.GUI_PORT),
    )
    assert gui.discover_room("tok") == ("10.0.0.2", gui.GUI_PORT)


def test_discover_room_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(gui.net, "local_subnet_hosts", lambda: ["10.0.0.1"])
    monkeypatch.setattr(
        gui.net,
        "_port_open",
        lambda ip, port, timeout=gui.SWEEP_TIMEOUT: False,
    )
    assert gui.discover_room("tok") is None


def test_join_page_webui_button_lifecycle(window, monkeypatch):
    page = window.join_page
    rooms = []

    def fake_client(host, display, url, token):
        rooms.append(FakeRoom())
        return rooms[-1]

    monkeypatch.setattr(gui.net, "probe_room_port", lambda *a, **k: gui.GUI_PORT + 3)
    monkeypatch.setattr(gui.net, "start_client_room", fake_client)
    page.ip_edit.setText("192.168.1.20")
    page.token_edit.setText("tok")
    page._join()
    for _ in range(200):
        QApplication.processEvents()
        if rooms:
            break
    assert page.webui_row.isVisibleTo(page)  # joined: own WebUI is openable
    page._leave()
    assert rooms[0].stopped
    assert not page.webui_row.isVisibleTo(page)


def _settle(predicate, timeout_s: float = 5.0) -> bool:
    """Pump the Qt event loop until `predicate()` holds.

    Worker threads answer on a signal, so a *fixed number* of processEvents()
    calls can run out before the thread is even scheduled — that is how the scan
    tests flaked while the suite was busy (2026-09-19, seen once in a full run
    and green in isolation twice). Wall-clock deadline instead.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return bool(predicate())


def test_join_page_reports_scan_miss(window, monkeypatch):
    monkeypatch.setattr(gui.net, "discover_room", lambda *_a, **_k: None)
    monkeypatch.setattr(gui.net, "start_client_room", lambda *_: pytest.fail("must not join"))
    page = window.join_page
    page.ip_edit.clear()  # module-scoped window: drop any leftover from other tests
    page.token_edit.setText("tok")  # empty IP: full subnet discovery
    page.name_edit.setText("pc-alpha")
    page._join()
    assert _settle(lambda: page.join_btn.isEnabled()), "the scan never came back"
    assert page.room is None
    assert page.ip_edit.text() == ""  # nothing discovered, nothing filled
    assert "没有找到" in page.status.text()


def test_join_page_refresh_fills_ip(window, monkeypatch):
    page = window.join_page
    monkeypatch.setattr(gui.net, "discover_room", lambda *_a, **_k: ("10.0.0.9", gui.GUI_PORT))
    page.token_edit.setText("tok")
    page._refresh_ip()
    assert _settle(lambda: page.ip_edit.text() == "10.0.0.9"), "the refresh never filled the IP"
    assert page.ip_edit.text() == "10.0.0.9"
    assert page.ip_refresh_btn.isEnabled()


def test_close_parks_room_to_tray(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    window._tray = None
    window.close()
    assert not room.stopped  # close hides to tray; the room keeps running
    assert not window.isVisible()
    assert window._tray.isVisible()  # 托盘图标留着：那是回窗口与 WebUI 的路
    window._tray.hide()
    page.room = None


def test_close_parks_instead_of_closing_the_window(window):
    """2026-09-12 真机实测：关窗原来会把关闭事件交回 QWidget 的默认实现 → 事件被 accept →
    「最后一个窗口已关闭」触发 quitOnLastWindowClosed → **进程直接退出**（房间被杀、托盘图标
    同时被 hide 掉）。「关窗转托盘后台房间不停」要成立，事件必须保持 ignored、托盘必须留着。"""
    from PyQt5.QtGui import QCloseEvent

    page = window.host_page
    room = FakeRoom()
    page.room = room
    window._tray = None
    window.show()
    try:
        assert window.isVisible()
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()  # 吃下事件 = 窗口真关 = 应用退出
        assert not window.isVisible()  # 只是藏起来
        assert window._tray.isVisible()  # 回得去的路还在
        assert not room.stopped
    finally:
        window._tray.hide()
        page.room = None


def test_tray_menu_pulls_up_from_the_icon(window, monkeypatch):
    """托盘菜单要朝上开：光标就在任务栏上，DROP_DOWN 把菜单顶边锚在光标处、从上面滑下来
    （实测菜单底边会越过屏幕底部）。房间模式的托盘一直用 PULL_UP，GUI 的托盘漏了。
    """
    from PyQt5.QtWidgets import QSystemTrayIcon
    from qfluentwidgets import MenuAnimationType

    page = window.host_page
    page.room = FakeRoom()  # the tray lives exactly while a room runs
    window._tray = None
    window.update_tray()
    tray = window._tray
    seen = {}
    monkeypatch.setattr(tray._menu, "exec_", lambda *a, **k: seen.update(args=a))

    tray._on_activated(QSystemTrayIcon.Context)
    assert seen["args"][2] == MenuAnimationType.PULL_UP, seen
    tray.hide()
    page.room = None


def test_tray_icon_click_opens_the_webui(window):
    """2026-09-11 用户要求：「点击图标跳转 webUI（现在是启动器）」——点图标进 WebUI
    的好友视图，启动器改从菜单的「显示主界面」进（那条菜单项还在）。"""
    from PyQt5.QtWidgets import QSystemTrayIcon

    page = window.host_page
    room = FakeRoom()
    page.room = room
    window._tray = None
    window.update_tray()
    tray = window._tray
    tray._on_activated(QSystemTrayIcon.Trigger)
    assert room.webui_calls == 1
    tray.hide()
    page.room = None


def test_quit_from_tray_stops_rooms(window):
    page = window.host_page
    room = FakeRoom()
    page.room = room
    window.quit_from_tray()
    assert room.stopped and page.room is None


def test_valid_host_name_contract():
    assert valid_host_name("pc-alpha")
    assert not valid_host_name("不合法/名字")


def test_gui_singleton_guard():
    key = "FungiGuiSingletonTest"

    shared = QSharedMemory(key)
    assert shared.create(1)
    assert gui._singleton_taken(key)
    shared.detach()
    assert not gui._singleton_taken(key)


def test_second_launch_activates_existing_window(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "show_and_raise", lambda: calls.append(1))
    sock = QLocalSocket()
    sock.connectToServer(gui.app._GUI_IPC)
    assert sock.waitForConnected(1000)
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    for _ in range(200):
        QApplication.processEvents()
        if calls:
            break
    assert calls


def _ready(**overrides):
    return {
        "ffmpeg": True,
        "huggingface_hub": True,
        "torch": True,
        "transformers": True,
        "faster_whisper": True,
        "opencv": True,
        "CLIP": True,
        "whisper": True,
        **overrides,
    }


def test_config_page_video_models_all_present_disables_download(window, monkeypatch):
    """运行时全就绪 -> 按钮禁用, 状态行打勾 (不缺失禁用下载)."""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready())
    page._check_video_models()
    assert "✓" in page.video_status.text()
    assert "✗" not in page.video_status.text()
    assert not page.download_btn.isEnabled()


def test_config_page_video_models_missing_enables_download(window, monkeypatch):
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(CLIP=False))
    page._check_video_models()
    assert "✗" in page.video_status.text() and "CLIP" in page.video_status.text()
    assert page.download_btn.isEnabled()


def test_config_page_missing_dep_enables_download(window, monkeypatch):
    """模型都在但 huggingface_hub 没了 -> 状态行标 ✗, 按钮启用(可自愈)."""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(huggingface_hub=False))
    page._check_video_models()
    assert "huggingface_hub" in page.video_status.text()
    assert "✗" in page.video_status.text()
    assert page.download_btn.isEnabled()


def test_config_page_non_healable_missing_disables_download(window, monkeypatch):
    """torch/ffmpeg 缺失不可自愈: 状态行提示手动装, 按钮不给下载."""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(torch=False, ffmpeg=False))
    page._check_video_models()
    assert "torch" in page.video_status.text() and "手动安装" in page.video_status.text()
    assert not page.download_btn.isEnabled()


def test_config_page_download_runs_script_and_rechecks(window, monkeypatch):
    """点下载 -> Popen 脚本 + 轮询结束后自动复检并恢复按钮可用性。"""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(CLIP=False, whisper=False))
    monkeypatch.setattr("fungi.gui.config._hf_hub_missing", lambda: False)
    page._check_video_models()

    spawned = []

    class FakeProc:
        returncode = 0

        def poll(self):
            return 0  # "finished" on first tick

    monkeypatch.setattr(
        gui.subprocess,
        "Popen",
        lambda argv, **_kw: spawned.append(argv) or FakeProc(),
    )
    page._download_models()
    assert page._dl_proc is not None and not page.download_btn.isEnabled()
    assert len(spawned) == 1 and spawned[0][-1].endswith("download_video_models.py")

    # 下载结束后的复检, 两个模型都已就绪
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready())
    page._poll_download()
    assert page._dl_proc is None
    assert not page._dl_timer.isActive()
    assert "✓" in page.video_status.text()
    assert not page.download_btn.isEnabled()  # 全部就绪 -> 禁用


def test_config_page_download_installs_missing_dep_first(window, monkeypatch):
    """缺 huggingface_hub: 先 pip 装依赖, 成功后自动接下载脚本, 全程一次点击。"""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(CLIP=False, whisper=False))
    monkeypatch.setattr("fungi.gui.config._hf_hub_missing", lambda: True)

    spawned = []

    class FakeProc:
        returncode = 0

        def poll(self):
            return 0  # 每步首个轮询 tick 即"完成"

    monkeypatch.setattr(
        gui.subprocess,
        "Popen",
        lambda argv, **_kw: spawned.append(argv) or FakeProc(),
    )
    page._download_models()
    assert spawned[0][1:4] == ["-m", "pip", "install"] and "huggingface_hub" in spawned[0]
    assert "依赖" in page.video_status.text()
    page._poll_download()  # 依赖装完 -> 链到模型下载
    assert len(spawned) == 2 and spawned[1][-1].endswith("download_video_models.py")
    assert "VidSense" in page.video_status.text()
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready())
    page._poll_download()  # 模型下完 -> 复检就绪并禁用按钮
    assert not page._dl_timer.isActive()
    assert not page.download_btn.isEnabled()


def test_config_page_frozen_exe_falls_back_to_path_python(monkeypatch):
    """exe 冻结态没有内嵌解释器, 落到系统 PATH 上的 python。"""
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui.shutil, "which", lambda _name: "C:/Python/python.exe")
    page_gui = gui.ConfigPage.__new__(gui.ConfigPage)  # 不触 Qt: 只测纯函数
    assert page_gui._python_cmd() == "C:/Python/python.exe"


def test_config_page_frozen_exe_points_video_at_the_source_run(window, monkeypatch):
    """exe 冻结态下本地视频理解根本够不着（没有解释器、也看不见系统依赖）：
    状态行直接说清楚，别再给那个点了也没用的下载按钮。"""
    page = window.cfg_page
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(torch=False, CLIP=False))
    page._check_video_models()

    assert "python start.py" in page.video_status.text()
    assert not page.download_btn.isVisibleTo(page)


def test_config_page_download_btn_hidden_when_nothing_to_heal(window, monkeypatch):
    """用户定调: 没有可自愈缺失 -> 下载按钮整体隐藏 (不是灰着)。"""
    page = window.cfg_page
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready())
    page._check_video_models()
    assert not page.download_btn.isVisibleTo(page)
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(torch=False))
    page._check_video_models()
    assert not page.download_btn.isVisibleTo(page)  # 不可自愈 -> 也不给按钮
    monkeypatch.setattr("fungi.gui.config._video_ready", lambda: _ready(CLIP=False))
    page._check_video_models()
    assert page.download_btn.isVisibleTo(page)  # 有可自愈缺失 -> 出现


# ── 回车即更新（用户定调：输入框里按回车就该生效，不必回鼠标点按钮）──


def test_enter_starts_room_from_either_field(window, monkeypatch):
    """主机名 / 昵称里按回车 = 点「发起房间」（这两个值只在发起时读）。"""
    started = []

    def fake_start(host, display, token, port):
        started.append((host, display))
        return object()

    monkeypatch.setattr(gui.net, "start_server_room", fake_start)
    page = window.host_page
    page.name_edit.setText("pc-alpha")
    page.nick_edit.setText("花酱")
    QTest.keyClick(page.name_edit, Qt.Key_Return)
    assert started == [("pc-alpha", "花酱")]
    page.room = None  # release before the second launch
    QTest.keyClick(page.nick_edit, Qt.Key_Return)
    assert started == [("pc-alpha", "花酱"), ("pc-alpha", "花酱")]
    page.room = None


def test_enter_joins_room_and_ignores_a_press_mid_scan(window, monkeypatch):
    """Token 里按回车 = 点「加入房间」；扫描中再按一次不得重开一次扫描。"""
    scans = []

    def fake_discover(token):
        scans.append(token)

    monkeypatch.setattr(gui.net, "discover_room", fake_discover)
    monkeypatch.setattr(gui.net, "start_client_room", lambda *_: pytest.fail("must not join"))
    page = window.join_page
    page.ip_edit.clear()
    page.token_edit.setText("tok")
    page.join_btn.setEnabled(True)
    QTest.keyClick(page.token_edit, Qt.Key_Return)
    assert not page.join_btn.isEnabled()  # 禁用态即"扫描进行中"标志
    page._join()  # 第二次回车（扫描未回）：必须直接返回
    for _ in range(300):
        QApplication.processEvents()
        if page.join_btn.isEnabled():
            break
        time.sleep(0.01)
    assert scans == ["tok"]
    assert page.join_btn.isEnabled()


def test_enter_saves_config_from_any_field(window, monkeypatch):
    """设置页两个输入框：预填当前值（key 只露掩码），回车 = 保存，保存后回到当前值。

    模型那一格 2026-09-25（§66）起**不在**这条规矩里了 —— 用户：「原先的输入框从覆盖变成添加」。
    它的回车是「添加并切换」，当前用的是哪个由旁边那个下拉列表显示；那条契约由
    `test_the_model_row_picks_from_the_list_and_the_box_adds` 与
    `test_a_model_that_does_not_answer_is_reported_not_reverted` 钉。
    """
    from fungi.config import Config

    state = {
        "cfg": Config(
            api_key="sk-original-key",
            endpoint="https://orig.example/v1/chat/completions",
            model="orig-model",
        )
    }
    monkeypatch.setattr("fungi.config.load_config", lambda path=None: state["cfg"])
    monkeypatch.setattr("fungi.config.save_config", lambda c, path=None: state.update(cfg=c))
    page = window.cfg_page
    page._load_fields()
    # 已在用的值摆在框里；key 本身不上屏，占位符里是可辨认的掩码
    assert page.endpoint_edit.text() == "https://orig.example/v1/chat/completions"
    assert page.key_edit.text() == ""
    assert "sk-or…-key" in page.key_edit.placeholderText()
    # 模型不在输入框里（那是「添加」那一格）：正在用的那个在下拉列表里选中
    assert page.model_edit.text() == ""
    assert page.model_combo.currentText() == "orig-model"

    page.key_edit.setText("sk-enter-key-1234")
    QTest.keyClick(page.key_edit, Qt.Key_Return)
    assert state["cfg"].api_key == "sk-enter-key-1234"
    assert page.key_edit.text() == ""  # key 不留在屏幕上
    assert "sk-en…1234" in page.key_edit.placeholderText()

    page.endpoint_edit.setText("https://example.invalid/v1/chat/completions")
    QTest.keyClick(page.endpoint_edit, Qt.Key_Return)
    assert state["cfg"].endpoint == "https://example.invalid/v1/chat/completions"
    assert page.endpoint_edit.text() == "https://example.invalid/v1/chat/completions"

    # 空框 = 保持不变（老语义没变）
    page.endpoint_edit.clear()
    state["cfg"].api_key = "sk-untouched"
    page.key_edit.clear()
    QTest.keyClick(page.endpoint_edit, Qt.Key_Return)
    assert state["cfg"].endpoint == "https://example.invalid/v1/chat/completions"
    assert state["cfg"].api_key == "sk-untouched"
    assert page.endpoint_edit.text() == "https://example.invalid/v1/chat/completions"
    page._load_fields()  # 放开 patch 前先让框回到真配置


def test_ctrl_enter_saves_courier_memory(window, monkeypatch):
    """信使页记忆是多行文本：回车留给换行，Ctrl+Enter 才是保存。"""
    saved = []
    cfg = gui.load_config()
    monkeypatch.setattr("fungi.config.load_config", lambda path=None: cfg)
    monkeypatch.setattr("fungi.config.save_config", lambda c, path=None: saved.append(c))
    page = window.courier_page
    assert page.memory_save_sc.key() == QKeySequence("Ctrl+Return")
    page.memory_edit.setPlainText("工作日 8:00-17:00 在上课")
    page.memory_save_sc.activated.emit()
    assert saved[-1].courier_memory == "工作日 8:00-17:00 在上课"


def test_ctrl_enter_accepts_day_dialog():
    """日历录入框同理：Ctrl+Enter = 点「保存」，回车仍是换行（一行一条）。"""
    dlg = gui._DayDialog("2026-09-11", ["出去玩"], None)
    dlg.edit.setPlainText("出去玩\n去咖啡店")
    assert dlg.save_sc.key() == QKeySequence("Ctrl+Return")
    dlg.save_sc.activated.emit()
    assert dlg.result() == QDialog.Accepted
    assert dlg.items() == ["出去玩", "去咖啡店"]


def test_calendar_day_click_records_entries(window, monkeypatch):
    """日历录入走通到 todos：点某天 -> 对话框 -> 写入 -> 那天亮起。

    拆包后这页自己拿 todos（from .. import todos），所以打在 todos 模块上。
    """
    from fungi import todos as todos_mod

    page = window.courier_page
    iso = sorted(page._buttons)[0]
    saved, loaded = [], []

    class FakeDialog:
        def __init__(self, *_a, **_k):
            pass

        def exec_(self):
            return 1  # accepted

        def items(self):
            return ["六点老地方见"]

    monkeypatch.setattr(todos_mod, "set_day", lambda day, items: saved.append((day, items)))
    monkeypatch.setattr(todos_mod, "load", lambda: (loaded.append(1), {})[1])
    monkeypatch.setattr(gui.courier, "_DayDialog", FakeDialog)

    page._open_day(iso)
    assert saved == [(iso, ["六点老地方见"])]
    assert loaded, "the page must repaint after recording a day"


def test_enter_in_token_field_launches_the_room(window, monkeypatch):
    """发起房间页 Token 里按回车 = 发起房间（未开房时没有活动 token 可热更）。

    用户 2026-09-10 点名：这一格此前回车是静默空转。
    """
    started = []

    def fake_start(_host, _display, token, _port):
        started.append(token)
        return object()

    monkeypatch.setattr(gui.net, "start_server_room", fake_start)
    page = window.host_page
    page.room = None
    page._set_started(False)
    page.name_edit.setText("pc-alpha")
    page.token_edit.setText("probe-token-1")
    QTest.keyClick(page.token_edit, Qt.Key_Return)
    assert started == ["probe-token-1"]
    assert page.room is not None and not page.start_btn.isEnabled()
    page.room = None


def test_token_focus_out_never_launches_the_room(window, monkeypatch):
    """移开焦点只提交 token：切页面/点别处不许把房间开起来。"""
    monkeypatch.setattr(gui.net, "start_server_room", lambda *_: pytest.fail("must not start"))
    page = window.host_page
    page.room = None
    page.token_edit.setText("probe-token-2")
    page._apply_token()  # what editingFinished runs
    assert page.room is None


def _running_room(host="pc-alpha", display="花酱"):
    """A live room stub: records what Enter asks it to change."""

    class Room:
        calls: list = []

        def __init__(self):
            self.host = host
            self.display = display
            self.hub = type("Hub", (), {"token": "tok"})()

        def set_display(self, name):
            Room.calls.append(("display", name))
            self.display = name
            return name

        def set_token(self, token):
            Room.calls.append(("token", token))
            self.token = token
            return getattr(self, "_accept", True)

        def stop(self):
            pass

        def open_webui(self, open_browser=True):
            return "http://localhost:1"

    Room.calls = []
    return Room()


def test_enter_in_host_nickname_renames_the_running_room(window, monkeypatch):
    """用户要的：开房后昵称那格按回车＝即时改名（像 Token 热更一样）。"""
    monkeypatch.setattr(gui.net, "start_server_room", lambda *_: pytest.fail("must not relaunch"))
    page = window.host_page
    room = _running_room()
    page.room = room
    page._token = "tok"
    page.name_edit.setText("pc-alpha")
    page.nick_edit.setText("新昵称")
    QTest.keyClick(page.nick_edit, Qt.Key_Return)
    assert room.calls == [("display", "新昵称")]
    assert room.display == "新昵称"
    assert page.nick_edit.text() == "新昵称"
    page.room = None


def test_enter_in_host_wire_name_is_refused_while_running(window, monkeypatch):
    """wire 身份开房后固定：回车不许静默失败，也不许改成别的名字。"""
    monkeypatch.setattr(gui.net, "start_server_room", lambda *_: pytest.fail("must not relaunch"))
    page = window.host_page
    room = _running_room()
    page.room = room
    page.name_edit.setText("pc-beta")
    page.nick_edit.setText("花酱")
    QTest.keyClick(page.name_edit, Qt.Key_Return)
    assert room.calls == []  # nothing applied
    assert page.name_edit.text() == "pc-alpha"  # reverted to the live identity
    assert room.host == "pc-alpha"
    page.room = None


def test_join_page_enter_applies_nickname_and_verifies_token(window, monkeypatch):
    """加入页同理：昵称即时改；Token 热更走校验；房主 IP / 主机名要重新加入。"""
    page = window.join_page
    room = _running_room(host="pc-beta", display="旧昵称")
    page.room = room
    page._joined_ip = "192.168.1.20"
    page._joined_token = "tok-old"
    page.ip_edit.setText("192.168.1.99")  # a different hub = a different room
    page.name_edit.setText("pc-other")  # wire identity is fixed
    page.nick_edit.setText("新昵称")
    page.token_edit.setText("tok-new")
    QTest.keyClick(page.token_edit, Qt.Key_Return)
    assert ("display", "新昵称") in room.calls
    assert ("token", "tok-new") in room.calls
    assert room.token == "tok-new"
    assert page.nick_edit.text() == "新昵称"
    assert page.ip_edit.text() == "192.168.1.20"  # reverted: needs a fresh join
    assert page.name_edit.text() == "pc-beta"  # reverted: identity is fixed
    assert page._joined_token == "tok-new"  # future compares use the new one
    page.room = None


def test_join_page_rejected_token_is_rolled_back(window):
    """房主没换 Token（校验失败）→ 还原字段，别把房间带进 403。"""
    page = window.join_page
    room = _running_room(host="pc-beta", display="")
    room._accept = False
    page.room = room
    page._joined_token = "tok-old"
    page._joined_ip = "192.168.1.20"
    page.ip_edit.setText("192.168.1.20")
    page.name_edit.setText("pc-beta")
    page.nick_edit.setText("")
    page.token_edit.setText("tok-wrong")
    QTest.keyClick(page.token_edit, Qt.Key_Return)
    assert page.token_edit.text() == "tok-old"
    assert page._joined_token == "tok-old"
    page.room = None


# ── 来信提醒：铃声 + 托盘闪动 ──


class _UnreadRoom:
    """A stand-in room: the GUI reads exactly one field off it (last_unread)."""

    def __init__(self) -> None:
        self.stopped = False
        self.last_unread = 0

    def stop(self) -> None:
        self.stopped = True


class _RecordingRinger:
    def __init__(self) -> None:
        self.played: list[str] = []
        self.ringing = False

    def start(self, tone: str) -> None:
        self.played.append(tone)
        self.ringing = True

    def preview(self, tone: str) -> None:
        self.played.append("preview:" + tone)

    def stop(self) -> None:
        self.ringing = False


@pytest.fixture()
def ringing(window, monkeypatch):
    """A room with a driven unread count, and the ring isolated (no audio in a
    test run). Torn down so later tests see neither a room nor a tray."""
    cfg = gui.load_config()
    cfg.ring, cfg.ring_tone = True, "alert"
    gui.save_config(cfg)
    room, ringer = _UnreadRoom(), _RecordingRinger()
    monkeypatch.setattr(window, "_ringer", ringer)
    window.host_page.room = room
    window.update_tray()
    try:
        yield room, ringer
    finally:
        room.last_unread = 0
        # Session alerts (§61) live in a process-wide registry: a leaked one
        # would ring the next test's poll out of nowhere.
        webui_server.clear_alerts()
        window._poll_unread()
        window.host_page.room = None
        window.update_tray()


def test_unread_flashes_at_once_and_rings_after_the_grace(window, ringing):
    """未读即闪，铃声等一个宽限期：好友视图要 ~8 秒才把这一条标成已读
    （5s /comm-log 轮询 + 3s 邮箱轮询），没有宽限期就会为「你正看着的那条」响铃。"""
    room, ringer = ringing
    window._poll_unread()
    assert window._tray._alerting is False and ringer.played == []

    room.last_unread = 2
    window._poll_unread()
    assert window._tray._alerting is True  # 未读立刻闪
    assert ringer.played == []  # 铃声还没到

    window._unread_since -= gui.app.RING_GRACE_S + 1
    window._poll_unread()
    assert ringer.played == ["alert"]

    room.last_unread = 0
    window._poll_unread()
    assert window._tray._alerting is False
    assert ringer.ringing is False


def test_ring_off_still_flashes_the_tray(window, ringing):
    """关掉铃声只是不响：未读的视觉提示照样在。"""
    room, ringer = ringing
    cfg = gui.load_config()
    cfg.ring = False
    gui.save_config(cfg)

    room.last_unread = 1
    window._unread_since = time.monotonic() - gui.app.RING_GRACE_S - 1
    window._poll_unread()
    assert window._tray._alerting is True
    assert ringer.played == []


def test_a_tone_asks_both_backends_for_a_single_play(monkeypatch):
    """一次性播放（用户 2026-09-11：「铃声只响一次，但是图标保持闪动」）：Qt 后端的
    loop count 是 1，winsound 不带 SND_LOOP —— 试听与来信铃是同一种播放。"""
    from types import SimpleNamespace  # a QSoundEffect stand-in lives below

    from fungi.gui import ring as ring_mod

    loops: list[int] = []
    effect = SimpleNamespace(  # only the calls ring.py makes on QSoundEffect
        setSource=lambda url: None,
        setLoopCount=loops.append,
        setVolume=lambda volume: None,
        play=lambda: None,
        stop=lambda: None,
    )
    monkeypatch.setattr(ring_mod, "QSoundEffect", lambda: effect)
    ring_mod.Ringer().start("alert")
    assert loops == [1]

    winsound = pytest.importorskip("winsound")
    flags: list[int] = []
    monkeypatch.setattr(winsound, "PlaySound", lambda path, f=None: flags.append(f))
    fallback = ring_mod.Ringer()
    monkeypatch.setattr(fallback, "_ensure_effect", lambda: None)  # 没有多媒体插件那台机器
    fallback.start("alert")
    assert flags == [winsound.SND_FILENAME | winsound.SND_ASYNC]
    assert not flags[0] & winsound.SND_LOOP


def test_the_unread_poll_rings_once_not_once_per_second(window, monkeypatch):
    """铃声只发一次：`Ringer.ringing` 从 start() 一直到 stop() 都是 True —— 未读轮询
    每秒问一次，若它在 WAV 放完就变回 False，同一首会每秒重播。"""
    cfg = gui.load_config()
    cfg.ring, cfg.ring_tone = True, "alert"
    gui.save_config(cfg)
    plays: list[str] = []
    ringer = gui.Ringer()
    monkeypatch.setattr(ringer, "_play_qt", lambda path: plays.append(path) or True)
    monkeypatch.setattr(ringer, "_play_winsound", lambda path: False)
    monkeypatch.setattr(window, "_ringer", ringer)
    room = _UnreadRoom()
    window.host_page.room = room
    window.update_tray()
    try:
        room.last_unread = 1
        window._unread_since = time.monotonic() - gui.app.RING_GRACE_S - 1
        for _ in range(5):  # five polls: a real ring's worth of seconds
            window._poll_unread()
        assert plays == [gui.tone_path("alert")]
    finally:
        room.last_unread = 0
        window._poll_unread()
        window.host_page.room = None
        window.update_tray()


def test_a_session_alert_rings_at_once_and_stops_when_cleared(window, ringing):
    """§61：会话在等用户（答完了 / 出错了 / 正在提问）时响铃 —— 不吃邮件的宽限期。
    那个宽限期是替「你正看着的那条留言」挡的，而会话提醒在服务端就已经被页面自己的
    「我正在看它」声明挡过一次了；再等 10 秒，铃声就晚得没意义。"""
    _room, ringer = ringing
    window._poll_unread()
    assert window._tray._alerting is False and ringer.played == []

    webui_server.note_alert("sess-done", "done")
    window._poll_unread()  # no grace was spent: it rings on the first poll
    assert window._tray._alerting is True
    assert ringer.played == ["alert"]
    assert "会话" in window._tray.toolTip()

    # 用户点进去 = 页面向服务端声明「我正在看它」：铃声与闪动一起停。
    webui_server.mark_seen("sess-done", True)
    window._poll_unread()
    assert window._tray._alerting is False and ringer.ringing is False


def test_the_tray_says_which_kind_of_alert_it_is_flashing_for(window, ringing):
    """闪动的提示语要说清是什么在等：留言、会话，或两样都有（§25.2 的闪动 + §61）。"""
    room, _ringer = ringing
    room.last_unread = 1
    webui_server.note_alert("sess-ask", "ask")
    window._poll_unread()
    tip = window._tray.toolTip()
    assert "未读留言" in tip and "会话" in tip, tip

    room.last_unread = 0
    window._poll_unread()
    assert window._tray.toolTip() == "Fungi — 有会话在等你"


def test_ring_switch_hides_the_tone_picker_and_the_choice_is_saved(window, monkeypatch):
    """设置页：关掉就不显示铃声选择；换一个音色即时保存并试听一次。"""
    page = window.cfg_page
    page.ring_switch.setChecked(False)
    assert page.tone_row.isHidden()
    assert gui.load_config().ring is False

    page.ring_switch.setChecked(True)
    assert not page.tone_row.isHidden()

    preview = _RecordingRinger()
    monkeypatch.setattr(page, "_preview", preview)
    page.tone_combo.setCurrentIndex(3)
    assert gui.load_config().ring_tone == gui.TONE_IDS[3]
    assert preview.played == ["preview:" + gui.TONE_IDS[3]]


def test_the_audition_button_plays_the_tone_that_is_already_selected(window, monkeypatch):
    """2026-09-11 用户报告：「选中的铃声也要可以试听（现在不行）」——换选项才响的旧行为
    没法听当前那一首。按钮不动下拉框，直接听它。"""
    page = window.cfg_page
    page.ring_switch.setChecked(True)
    assert not page.tone_row.isHidden()  # 铃声开着时按钮才在屏幕上
    preview = _RecordingRinger()
    monkeypatch.setattr(page, "_preview", preview)

    selected = page.tone_combo.currentIndex()
    page.preview_btn.click()
    assert page.tone_combo.currentIndex() == selected  # 没动下拉框
    assert preview.played == ["preview:" + gui.TONE_IDS[selected]]
    assert gui.load_config().ring_tone == gui.TONE_IDS[selected]


def test_tray_icon_flashes_while_mail_is_unread(window, ringing):
    """闪动本身：图标在两版之间换，提示语说明为什么；菜单里没有铃声那一项
    （用户 2026-09-11：铃声一声就完，「停止铃声」多余）。"""
    tray = window._tray
    labels = [a.text() for a in tray._menu.actions()]
    assert "停止铃声" not in labels, labels
    tray.set_alert(True, "有未读留言")
    first = tray.icon().pixmap(64, 64).toImage()
    tray._flash_tick()
    second = tray.icon().pixmap(64, 64).toImage()
    assert first != second
    assert "未读" in tray.toolTip()
    tray.set_alert(False)
    assert tray.toolTip() == "Fungi"


def _wait_ui(pred, timeout_s: float = 4.0) -> bool:
    """探测跑在子线程、结果由 250ms 的定时器取走：等它，但不睡满。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


def _stub_probe(monkeypatch, result=(True, None)):
    """替掉真网络，只记下「问了哪个模型」；返回那个列表。"""
    from fungi.gui import config as config_page

    asked: list[str] = []

    def fake(model, endpoint, api_key, timeout=None):
        asked.append(model)
        return (result[0], result[1] if result[1] is not None else model)

    monkeypatch.setattr(config_page.llm_mod, "probe_model", fake)
    return asked


def _read_config(window):
    from fungi.gui import config as config_page

    return json.loads(config_page.config_mod.CONFIG_PATH.read_text(encoding="utf-8"))


def test_the_model_row_picks_from_the_list_and_the_box_adds(window, monkeypatch):
    """§66（用户 2026-09-25「添加模型下拉列表，下拉后选中哪个使用哪个 / 原先的输入框
    从覆盖变成添加，如果与之前模型不一样就新添 / 添加后自动测试模型调用」）。"""
    from PyQt5.QtGui import QShowEvent

    from fungi.gui import config as config_page

    cfg = config_page.config_mod.load_config()
    cfg.api_key, cfg.endpoint, cfg.model, cfg.model_list = "k", "e", "m1", ["m1"]
    config_page.config_mod.save_config(cfg)
    asked = _stub_probe(monkeypatch)

    page = window.cfg_page
    page.showEvent(QShowEvent())
    assert page.model_combo.currentText() == "m1", "下拉列表里就是正在用的那个"
    assert [page.model_combo.itemText(i) for i in range(page.model_combo.count())] == ["m1"]
    assert page.model_edit.text() == "", "框里不再印着当前模型：那是下拉列表的活儿"

    # 输入框回车 = 添加：进列表、切过去、自动测一次
    page.model_edit.setText("m2")
    page.model_edit.returnPressed.emit()
    assert _wait_ui(
        lambda: page.model_edit.text() == "" and page.model_combo.currentText() == "m2"
    ), "添加之后列表里出现这一项，并且就用它"
    assert _wait_ui(lambda: bool(asked)), "添加之后自动测了一次调用"
    assert asked == ["m2"]
    assert [page.model_combo.itemText(i) for i in range(page.model_combo.count())] == ["m2", "m1"]
    data = _read_config(window)
    assert (data["model"], data["model_list"]) == ("m2", ["m2", "m1"]), "旧的那个还在列表里"

    # 下拉列表里选回 m1：写盘（切过去）+ 又测一次
    page.model_combo.setCurrentIndex(1)
    assert _wait_ui(lambda: len(asked) == 2)
    assert asked[1] == "m1"
    data = _read_config(window)
    assert (data["model"], data["model_list"]) == ("m1", ["m1", "m2"])
    assert _wait_ui(lambda: "✓" in page.model_status.text()), "测试结果就写在这一行"

    # 同一个名字再来一次：不加第二行（列表长度不变）
    page.model_edit.setText("m2")
    page.model_edit.returnPressed.emit()
    assert _wait_ui(lambda: len(asked) == 3)
    assert [page.model_combo.itemText(i) for i in range(page.model_combo.count())] == ["m2", "m1"]
    assert len(_read_config(window)["model_list"]) == 2


def test_a_model_that_does_not_answer_is_reported_not_reverted(window, monkeypatch):
    """调不通就直说（✗ + 那句原话），但**不改回去**：用户可能就是要拿它当靶子试
    （错名字、临时挂掉的网关），替他把选择撤掉才是意外。"""
    from PyQt5.QtGui import QShowEvent

    from fungi.gui import config as config_page

    cfg = config_page.config_mod.load_config()
    cfg.api_key, cfg.endpoint, cfg.model, cfg.model_list = "k", "e", "m1", ["m1"]
    config_page.config_mod.save_config(cfg)
    _stub_probe(monkeypatch, result=(False, "HTTP 404: model not found"))

    page = window.cfg_page
    page.showEvent(QShowEvent())
    page.model_edit.setText("m-dead")
    page.model_edit.returnPressed.emit()

    assert _wait_ui(lambda: "✗" in page.model_status.text())
    assert "m-dead" in page.model_status.text()
    assert "model not found" in page.model_status.text(), "provider 的原话要留在屏上"
    assert _read_config(window)["model"] == "m-dead", "选择没被撤回去"
