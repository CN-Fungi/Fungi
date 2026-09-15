"""“Join a room” page: this machine joins someone else's hub."""

import threading

from PyQt5.QtCore import QSettings, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
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
from .widgets import _row


class JoinPage(QWidget):
    """加入房间：房间 IP 自动发现（全网段扫描），无需手填；Token 即身份。"""

    join_done = pyqtSignal(object)  # (ip, port) tuple or None; emitted from scan thread
    discover_done = pyqtSignal(object)  # (ip, port) tuple or None; refresh/autofill path

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("joinPage")
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.room = None
        # What the live room was joined with: the fields are compared against
        # these so Enter can tell "you changed something" from "same as joined".
        self._joined_ip = ""
        self._joined_token = ""
        self.join_done.connect(self._finish_join)
        self.discover_done.connect(self._fill_ip)

        root = QVBoxLayout(self)
        root.setContentsMargins(48, 32, 48, 32)
        root.setSpacing(14)

        title = SubtitleLabel("加入房间")
        root.addWidget(title)

        self.ip_edit = LineEdit()
        self.ip_edit.setPlaceholderText("留空 = 自动扫描局域网（无需手填）")
        self.ip_edit.setFixedWidth(360)
        self.ip_refresh_btn = ToolButton(FluentIcon.SYNC)
        self.ip_refresh_btn.setToolTip("重新扫描局域网，自动填入房间 IP（网络变化后使用）")
        self.ip_refresh_btn.clicked.connect(self._refresh_ip)
        ip_buttons = QWidget()
        ip_box = QHBoxLayout(ip_buttons)
        ip_box.setContentsMargins(0, 0, 0, 0)
        ip_box.setSpacing(4)
        ip_box.addWidget(self.ip_refresh_btn)
        self.ip_row = _row("房主 IP", self.ip_edit, ip_buttons)
        root.addWidget(self.ip_row)

        self.token_edit = LineEdit()
        self.token_edit.setFixedWidth(360)
        self.token_edit.setPlaceholderText("房主发给你的 Token")
        self.token_edit.setToolTip(
            "加入前：回车＝加入房间；加入后房主换了 Token，在这里按回车即可更新（会先校验）"
        )
        self.token_edit.setFixedWidth(360)
        root.addWidget(_row("Token", self.token_edit))

        self.nick_edit = LineEdit()
        self.nick_edit.setFixedWidth(360)
        self.nick_edit.setPlaceholderText("你的昵称（中文/emoji 均可，仅用于展示）")
        self.nick_edit.setToolTip(
            "加入前：回车＝加入房间；加入后：回车即时改名（对面立刻看到新昵称）"
        )
        self.nick_edit.setFixedWidth(360)
        root.addWidget(_row("昵称", self.nick_edit))

        self.name_edit = LineEdit()
        self.name_edit.setFixedWidth(360)
        self.name_edit.setText(net.default_host_name())
        self.name_edit.setPlaceholderText("本机主机名（wire 身份，一般不用改）")
        self.name_edit.setToolTip("加入后该身份固定（对面按它记你），改名需先离开房间")
        root.addWidget(_row("主机名", self.name_edit))

        # Enter in any field: join when idle, commit what a running room can
        # take once joined (昵称 live, Token verified hot-swap) — the hub
        # address and the wire name define the room, so they need a fresh join.
        for edit in (self.ip_edit, self.token_edit, self.nick_edit, self.name_edit):
            edit.returnPressed.connect(self._enter)

        btn_row = QHBoxLayout()
        self.join_btn = PrimaryPushButton(FluentIcon.CONNECT, "加入房间")
        self.join_btn.clicked.connect(self._join)
        self.leave_btn = PushButton(FluentIcon.CLOSE, "离开房间")
        self.leave_btn.clicked.connect(self._leave)
        btn_row.addWidget(self.join_btn, 1)
        btn_row.addWidget(self.leave_btn, 1)
        root.addLayout(btn_row)
        self.leave_btn.setVisible(False)

        self.webui_btn = PushButton(FluentIcon.GLOBE, "打开 WebUI")
        self.webui_btn.clicked.connect(self._open_webui)
        self.webui_row = _row("WebUI", self.webui_btn)
        root.addWidget(self.webui_row)
        self.webui_row.setVisible(False)

        root.addStretch(1)

        self.status = BodyLabel(
            "加入成功后可点「打开 WebUI」聊天；关闭窗口会转入托盘后台，房间不停。"
        )
        root.addWidget(self.status)

        self._restore()

    def _restore(self) -> None:
        last_ip = self.settings.value("last_ip", "")
        last_token = self.settings.value("last_token", "")
        last_nick = self.settings.value("last_nick", "")
        if last_ip:
            self.ip_edit.setText(str(last_ip))
        if last_token:
            self.token_edit.setText(str(last_token))
        if last_nick:
            self.nick_edit.setText(str(last_nick))

    def _join(self) -> None:
        # A disabled button IS the in-flight flag: without this guard, Enter
        # during a scan starts a second discovery thread and double-emits
        # join_done.
        if self.room is not None or not self.join_btn.isEnabled():
            return
        ip = self.ip_edit.text().strip()
        token = self.token_edit.text().strip()
        nick = self.nick_edit.text().strip()
        host = self.name_edit.text().strip()
        if not token:
            InfoBar.warning(
                "缺少 Token", "请向房主索要房间 Token", duration=3000, parent=self.window_ref
            )
            return
        wire, nick = net._resolve_wire_name(host, nick)
        if wire != host:
            self.name_edit.setText(wire)
            self.nick_edit.setText(nick)
            InfoBar.info(
                "已自动调整",
                f"wire 身份用「{wire}」（进地址和文件名，仅限 ASCII）；「{host}」留作昵称展示",
                duration=5000,
                parent=self.window_ref,
            )
            host = wire
        self.join_btn.setEnabled(False)
        self.status.setText(
            f"正在扫描局域网（{net.lan_ip().rsplit('.', 1)[0]}.*，房间端口 8899 起）…"
            if not ip
            else f"正在扫描 {ip} 的房间端口（8899 起）…"
        )

        def scan():
            try:
                if not ip:
                    found = net.discover_room(token)
                    if found is None:
                        self.join_done.emit(None)
                        return
                    auto_ip, port = found
                    self.discover_done.emit((auto_ip, port))  # visible autofill
                else:
                    port = net.probe_room_port(ip, token)
            except Exception:  # network hiccup: report as "not found"
                self.join_done.emit(None)
                return
            if not port:  # scan found nothing: the None path reports it plainly
                self.join_done.emit(None)
                return
            self.join_done.emit((ip or auto_ip, port))

        threading.Thread(target=scan, name="room-discovery", daemon=True).start()
        self._pending = (token, nick, host)

    def _enter(self) -> None:
        """Enter in a join field: join when idle, otherwise commit live.

        While joined, only two things can change under the room: the nickname
        (roster re-join) and the Token (the host may rotate it; adopt + verify
        by heartbeat). The hub address and the wire name are the room and the
        identity themselves — changing them means leaving and joining again.
        """
        if self.room is None:
            self._join()
            return
        # No join_btn guard here: the button stays disabled for the whole
        # session once joined (its job moved to 离开房间), so gating on it would
        # make every live update a no-op. A scan in flight cannot coincide with
        # a live room — _join owns that window and guards itself.
        notes: list[str] = []
        blocked: list[str] = []

        host_in = self.name_edit.text().strip()
        if host_in and host_in != self.room.host:
            self.name_edit.setText(self.room.host)
            blocked.append(f"主机名（对面按「{self.room.host}」记你）在房间运行期间固定")

        nick = self.nick_edit.text().strip()
        if nick != self.room.display:
            applied = self.room.set_display(nick)
            self.nick_edit.setText(applied)
            self.settings.setValue("last_nick", applied)
            notes.append(f"昵称已更新为「{applied or self.room.host}」")

        token = self.token_edit.text().strip()
        if token and token != self._joined_token:
            swap = getattr(self.room, "set_token", None)
            if swap is None:  # role without a client connection to re-auth
                blocked.append("Token 要等重新加入才能改")
            elif swap(token):
                self._joined_token = token
                self.settings.setValue("last_token", token)
                notes.append("Token 已更新并校验通过")
            else:
                self.token_edit.setText(self._joined_token)
                blocked.append("Token 未通过校验（房主那边没生效？），已还原")

        ip_in = self.ip_edit.text().strip()
        if ip_in and ip_in != self._joined_ip:
            self.ip_edit.setText(self._joined_ip)
            blocked.append("换房主 IP 等于换房间：先「离开房间」再重新加入")

        if blocked:
            InfoBar.warning(
                "有字段不能即时改", "；".join(blocked), duration=6000, parent=self.window_ref
            )
        if notes:
            InfoBar.success("已更新", "；".join(notes), duration=4000, parent=self.window_ref)
        elif not blocked:
            InfoBar.info("没有改动", "当前设置与房间一致", duration=2000, parent=self.window_ref)

    def _refresh_ip(self) -> None:
        """Re-run subnet discovery and auto-fill the room IP (networks change)."""
        token = self.token_edit.text().strip()
        if not token:
            InfoBar.warning(
                "缺少 Token", "自动发现需要房间 Token", duration=3000, parent=self.window_ref
            )
            return
        self.ip_refresh_btn.setEnabled(False)
        self.status.setText(
            f"正在扫描局域网（{net.lan_ip().rsplit('.', 1)[0]}.*，房间端口 8899 起）…"
        )

        def scan():
            try:
                found = net.discover_room(token)
            except Exception:  # network hiccup: report as "not found"
                found = None
            self.discover_done.emit(found)

        threading.Thread(target=scan, name="ip-discovery", daemon=True).start()

    def _fill_ip(self, found) -> None:
        self.ip_refresh_btn.setEnabled(True)
        if found is None:
            self.status.setText(
                "局域网内没有找到接受该 Token 的房间（端口 8899 起）。\n"
                "请确认房主已发起、Token 正确、且在同一局域网。"
            )
            InfoBar.error(
                "未发现房间", "扫描范围内没有匹配的房间", duration=4000, parent=self.window_ref
            )
            return
        ip, _port = found
        self.ip_edit.setText(ip)
        self.status.setText("已自动填入房间 IP，点「加入房间」即可。")
        InfoBar.success("已自动发现房间", f"房间 IP：{ip}", duration=3000, parent=self.window_ref)

    def _open_webui(self) -> None:
        if self.room is not None:
            self.room.open_webui()

    def _leave(self) -> None:
        if self.room is None:
            return
        self.room.stop()  # sends leave to the hub
        self.room = None
        self._joined_ip = ""
        self._joined_token = ""
        self.leave_btn.setVisible(False)
        self.webui_row.setVisible(False)
        self.join_btn.setEnabled(True)
        self.window_ref.update_tray()
        self.status.setText("已离开房间。")
        InfoBar.info("已离开", "已从房间退出", duration=2500, parent=self.window_ref)

    def _finish_join(self, found) -> None:
        token, nick, host = self._pending
        if found is None:
            self.join_btn.setEnabled(True)  # scan failed: allow retrying
            self.status.setText(
                "局域网内没有找到接受该 Token 的房间（端口 8899 起）。\n"
                "请确认房主已发起、Token 正确、且在同一局域网。"
            )
            InfoBar.error(
                "未找到房间", "扫描范围内没有匹配的房间", duration=4000, parent=self.window_ref
            )
            return
        ip, port = found
        try:
            self.room = net.start_client_room(host, nick, f"http://{ip}:{port}", token)
        except Exception as exc:  # hub refused the join (left / restarted)
            self.join_btn.setEnabled(True)  # join refused: allow retrying
            self.status.setText(f"加入失败：{exc}")
            InfoBar.error("加入失败", str(exc), duration=5000, parent=self.window_ref)
            return
        self.leave_btn.setVisible(True)
        self.webui_row.setVisible(True)
        self.window_ref.update_tray()
        self._joined_ip = ip  # what Enter compares the fields against
        self._joined_token = token
        self.settings.setValue("last_ip", ip)
        self.settings.setValue("last_token", token)
        self.settings.setValue("last_nick", nick)
        self.status.setText(
            f"已加入 {ip}:{port}（昵称 {nick or host}）。\n"
            "关闭窗口会转入托盘后台，房间不会停；「打开 WebUI」进你自己的聊天界面，退出房间点「离开房间」。"
        )
        InfoBar.success(
            "已加入房间", f"{host} → {ip}:{port}", duration=3000, parent=self.window_ref
        )
