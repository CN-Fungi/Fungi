"""“Start a room” page: this machine hosts the hub."""

import secrets

from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QShortcut,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    InfoBar,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    ToolButton,
)

from . import net
from .const import SETTINGS_APP, SETTINGS_ORG
from .widgets import _copy, _copy_button, _row


class HostPage(QWidget):
    """发起房间：GUI 进程内直启 hub，随后显示 IP / Token / WebUI 入口。"""

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("hostPage")
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.room = None
        self._token = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("发起房间")
        root.addWidget(title)

        self.name_edit = LineEdit()
        self.name_edit.setFixedWidth(360)
        self.name_edit.setText(
            str(self.settings.value("last_host_name", "") or "") or net.default_host_name()
        )
        self.name_edit.setPlaceholderText("本机主机名（房间内的 wire 身份）")
        self.name_edit.setToolTip(
            "开房前：回车＝发起房间；开房后该身份固定（地址/文件名/对面信使都以它为准），改名需先离开房间"
        )
        root.addWidget(_row("主机名", self.name_edit))

        self.nick_edit = LineEdit()
        self.nick_edit.setFixedWidth(360)
        self.nick_edit.setText(str(self.settings.value("last_nick", "") or ""))
        self.nick_edit.setPlaceholderText("你的昵称（中文/emoji 均可，留空用主机名）")
        self.nick_edit.setToolTip(
            "开房前：回车＝发起房间；开房后：回车即时改名（对面立刻看到新昵称）"
        )
        root.addWidget(_row("昵称", self.nick_edit))

        # Enter in either field: launch when idle (both are read only at
        # _start), commit what a running room can take once it is up — the
        # nickname renames live, the wire name cannot move under it.
        self.name_edit.returnPressed.connect(self._apply_identity)
        self.nick_edit.returnPressed.connect(self._apply_identity)

        # token is an input, not a status readout: customize it before
        # launching, or edit it live while the room runs (hot-swap)
        self.token_edit = LineEdit()
        self.token_edit.setFixedWidth(360)
        self.token_edit.setPlaceholderText("留空则发起房间时自动生成")
        self.token_edit.setToolTip(
            "可自定义（字母/数字/-/_，1-64 位）。\n"
            "发起前修改：开房即用该 Token（未开房时按回车＝直接发起）；\n"
            "运行中修改：按回车（或移开焦点）即时热更新，"
            "已加入的好友需用新 Token 重新加入"
        )
        self.token_edit.editingFinished.connect(self._apply_token)
        # Enter in the token field: launch when the room is not up yet (there is
        # no live token to swap before launch), hot-swap once it runs. Focus-out
        # keeps meaning only "commit the token" — tabbing through must never
        # start a room.
        self.token_edit.returnPressed.connect(self._token_enter)
        # Remember the token: a fresh random one on every visit meant the user
        # had to re-send it to every friend (and 离开房间 regenerated it again).
        saved_token = str(self.settings.value("last_token", "") or "")
        self.token_edit.setText(
            saved_token if net._valid_token(saved_token) else secrets.token_urlsafe(12)
        )
        self.token_btn = _copy_button()
        self.token_btn.clicked.connect(lambda: _copy(self.token_edit.text(), window, "房间 Token"))
        self.token_row = _row("Token", self.token_edit, self.token_btn)
        root.addWidget(self.token_row)

        btn_row = QHBoxLayout()
        self.start_btn = PrimaryPushButton(FluentIcon.SHARE, "发起房间")
        self.start_btn.clicked.connect(self._start)
        self.leave_btn = PushButton(FluentIcon.CLOSE, "离开房间")
        self.leave_btn.clicked.connect(self._leave)
        btn_row.addWidget(self.start_btn, 1)
        btn_row.addWidget(self.leave_btn, 1)
        root.addLayout(btn_row)

        # -- status card (populated after launch) --
        self.ip_edit = LineEdit()
        self.ip_edit.setFixedWidth(360)
        self.ip_edit.setReadOnly(True)
        self.ip_btn = _copy_button()
        self.ip_btn.clicked.connect(lambda: _copy(self.ip_edit.text(), window, "房间 IP"))
        self.ip_refresh_btn = ToolButton(FluentIcon.SYNC)
        self.ip_refresh_btn.setToolTip(
            "刷新 IP（网络切换 / DHCP 续租后使用；房间绑定所有网卡，端口不变）"
        )
        self.ip_refresh_btn.clicked.connect(self._refresh_ip)
        self.webui_btn = PushButton(FluentIcon.GLOBE, "打开 WebUI")
        self.webui_btn.clicked.connect(self._open_webui)
        ip_buttons = QWidget()
        ip_box = QHBoxLayout(ip_buttons)
        ip_box.setContentsMargins(0, 0, 0, 0)
        ip_box.setSpacing(4)
        ip_box.addWidget(self.ip_refresh_btn)
        ip_box.addWidget(self.ip_btn)
        self.ip_row = _row("房间 IP", self.ip_edit, ip_buttons)
        self.webui_row = _row("WebUI", self.webui_btn)
        root.addWidget(self.ip_row)
        root.addWidget(self.webui_row)

        root.addStretch(1)

        self.status = BodyLabel(self._idle_status)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # page-scoped copy shortcut: Ctrl+C copies the room IP
        shortcut = QShortcut(QKeySequence("Ctrl+C"), self)
        shortcut.activated.connect(lambda: _copy(self.ip_edit.text(), window, "房间 IP"))

        self._set_started(False)

    def _set_started(self, started: bool) -> None:
        for row in (self.ip_row, self.webui_row):
            row.setVisible(started)
        self.start_btn.setEnabled(not started)
        self.leave_btn.setVisible(started)

    def _refresh_ip(self) -> None:
        """Re-detect the LAN IP: DHCP renewals / Wi-Fi switches change it while
        the room keeps running (the hub binds 0.0.0.0, so only the display ages)."""
        ip = net.lan_ip()
        changed = ip != self.ip_edit.text()
        self.ip_edit.setText(ip)
        if ip == "127.0.0.1":
            InfoBar.warning(
                "未检测到局域网",
                "当前 IP 显示为 127.0.0.1，请检查网络连接",
                duration=4000,
                parent=self.window_ref,
            )
        elif changed:
            InfoBar.success(
                "IP 已刷新", f"当前房间 IP：{ip}", duration=3000, parent=self.window_ref
            )
        else:
            InfoBar.info("IP 未变化", ip, duration=2000, parent=self.window_ref)

    def _apply_identity(self) -> None:
        """Enter in 主机名/昵称: start when idle, otherwise commit live.

        The nickname is presentation-only, so it hot-updates (the roster
        re-join refreshes what peers see). The wire name cannot move under a
        running room: addresses, the roster key, data/ file names and every
        peer's comm clone are keyed on it — say so instead of doing nothing.
        """
        if self.room is None:
            self._start()
            return
        host = self.name_edit.text().strip()
        wire, display = net._resolve_wire_name(host, self.nick_edit.text().strip())
        if wire != self.room.host:
            self.name_edit.setText(self.room.host)
            self.nick_edit.setText(self.room.display)
            InfoBar.warning(
                "主机名未更改",
                f"wire 身份（地址、文件名、对面信使都以「{self.room.host}」为准）"
                "在房间运行期间固定：想换名字请先离开房间",
                duration=6000,
                parent=self.window_ref,
            )
            return
        if display == self.room.display:
            InfoBar.info(
                "昵称未变化",
                f"当前昵称：{self.room.display or self.room.host}",
                duration=2000,
                parent=self.window_ref,
            )
            return
        applied = self.room.set_display(display)
        self.nick_edit.setText(applied)
        self.settings.setValue("last_nick", applied)
        InfoBar.success(
            "昵称已更新",
            f"对面看到的是「{applied or self.room.host}」，即时生效",
            duration=4000,
            parent=self.window_ref,
        )

    def _token_enter(self) -> None:
        """Enter in the Token field: launch when idle, otherwise hot-swap."""
        if self.room is None:
            self._start()
        else:
            self._apply_token()

    def _apply_token(self) -> None:
        """Commit a token edit. Before launch: no-op (validated at _start).
        While the room runs: hot-swap hub.token — every request re-reads it,
        so the change is live; joined peers must re-join with the new token."""
        if self.room is None:
            return
        token = self.token_edit.text().strip()
        if token == self._token:
            return
        if not net._valid_token(token):
            self.token_edit.setText(self._token)
            InfoBar.warning(
                "Token 未更改",
                "仅限字母、数字、- 和 _（Token 进 URL），1-64 位",
                duration=5000,
                parent=self.window_ref,
            )
            return
        self.room.hub.token = token
        self._token = token
        self.settings.setValue("last_token", token)
        InfoBar.success(
            "Token 已更新",
            "新 Token 即时生效；已加入的好友需用新 Token 重新加入",
            duration=5000,
            parent=self.window_ref,
        )

    _idle_status = (
        "尚未发起。\n"
        "· 端口从 8899 起自动向上寻找，加入方无需填 IP（同网段自动发现），只需 Token\n"
        "· 请确认加入方与本机在同一局域网（同一路由器）"
    )

    def _leave(self) -> None:
        if self.room is None:
            return
        self.room.stop()
        self.room = None
        self._set_started(False)
        self.window_ref.update_tray()
        self.ip_edit.clear()
        # The token stays as it is (and stays remembered): the next room reuses
        # it unless the user edits the field, so friends never have to re-type.
        self.status.setText(self._idle_status)
        InfoBar.info("已离开", "房间已停止", duration=2500, parent=self.window_ref)

    def _open_webui(self) -> None:
        if self.room is not None:
            self.room.open_webui()

    def _start(self) -> None:
        if self.room is not None:
            return
        host = self.name_edit.text().strip()
        display = self.nick_edit.text().strip()
        wire, display = net._resolve_wire_name(host, display)
        if wire != host:
            self.name_edit.setText(wire)
            self.nick_edit.setText(display)
            InfoBar.info(
                "已自动调整",
                f"wire 身份用「{wire}」（进地址和文件名，仅限 ASCII）；「{host}」留作昵称展示",
                duration=5000,
                parent=self.window_ref,
            )
            host = wire
        try:
            port = net.find_free_port()
        except OSError as exc:
            InfoBar.error("无可用端口", str(exc), duration=5000, parent=self.window_ref)
            return
        token = self.token_edit.text().strip() or secrets.token_urlsafe(12)
        if not net._valid_token(token):
            InfoBar.error(
                "Token 不合法",
                "仅限字母、数字、- 和 _（Token 进 URL），1-64 位",
                duration=5000,
                parent=self.window_ref,
            )
            return
        self._token = token
        self.room = net.start_server_room(host, display, self._token, port)
        self.ip_edit.setText(net.lan_ip())
        self.token_edit.setText(self._token)
        # Remember what this host launches with: next time the page opens (or
        # the next room starts) the same identity and token are waiting.
        self.settings.setValue("last_token", self._token)
        self.settings.setValue("last_nick", display)
        self.settings.setValue("last_host_name", host)
        self._set_started(True)
        self.window_ref.update_tray()
        self.status.setText(
            "房间运行中：关闭窗口会转入托盘后台，房间不会停。\n"
            f"· 端口 {port}（自动向上寻找）· 把 Token 发给好友即可加入（同网段自动发现）\n"
            "· Ctrl+C 复制房间 IP · 退出房间请点「离开房间」"
        )
        InfoBar.success(
            "房间已发起", f"{host} · {net.lan_ip()}:{port}", duration=3000, parent=self.window_ref
        )
