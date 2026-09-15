"""Mobile access page: the LAN URL + QR code for the phone UI."""

import io
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    InfoBar,
    LineEdit,
    PushButton,
    SubtitleLabel,
)

from . import firewall
from .widgets import _copy, _copy_button, _row


class MobilePage(QWidget):
    """手机端：房间启动后生成移动版 WebUI 的局域网二维码，手机扫码即用。"""

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("mobilePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = SubtitleLabel("手机端")
        root.addWidget(title)

        hint = BodyLabel(
            "发起或加入房间后，用手机相机扫码即可在手机上打开移动版 WebUI。\n"
            "换网络后点「刷新二维码」。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setWordWrap(True)  # 缺依赖时那段说明要能读完，不能被裁掉
        self.qr_label.setMinimumSize(260, 260)
        root.addWidget(self.qr_label, 1)

        self.url_edit = LineEdit()
        self.url_edit.setReadOnly(True)
        copy_btn = _copy_button()
        copy_btn.clicked.connect(lambda: _copy(self.url_edit.text(), self.window_ref, "手机端地址"))
        root.addWidget(_row("手机端地址", self.url_edit, copy_btn))

        self.refresh_btn = PushButton(FluentIcon.SYNC, "刷新二维码")
        self.refresh_btn.clicked.connect(self.refresh)
        root.addWidget(self.refresh_btn)

        # Only visible when the QR cannot be drawn: a source install can fix
        # itself, a bundle cannot (mobile._install_qr_dep).
        self.qr_dep_btn = PushButton(FluentIcon.DOWNLOAD, "安装二维码依赖 segno")
        self.qr_dep_btn.clicked.connect(self._install_qr_dep)
        self.qr_dep_btn.hide()
        root.addWidget(self.qr_dep_btn)
        self._dep_proc: subprocess.Popen | None = None
        self._dep_timer = QTimer(self)
        self._dep_timer.setInterval(1000)
        self._dep_timer.timeout.connect(self._poll_qr_dep)

        # Windows Firewall allows inbound per program: a freshly extracted exe
        # has no rule of its own, so the phone silently times out. The state is
        # always on screen (a hidden warning reads as "no such feature"), and the
        # UAC fix fires by itself once per run — cancelling still leaves the
        # button for a retry.
        self._fw_prompted = False
        self.fw_label = BodyLabel("")
        self.fw_label.setWordWrap(True)
        self.fw_label.hide()
        root.addWidget(self.fw_label)

        self.fw_btn = PushButton(FluentIcon.WIFI, "放行防火墙（手机才能连）")
        self.fw_btn.clicked.connect(self._allow_firewall)
        self.fw_btn.hide()
        root.addWidget(self.fw_btn)

        self._fw_proc: subprocess.Popen | None = None
        self._fw_timer = QTimer(self)
        self._fw_timer.setInterval(500)
        self._fw_timer.timeout.connect(self._poll_firewall)

        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        # The room may have started (or left) since the last visit.
        self.refresh()

    def refresh(self) -> None:
        rooms = self.window_ref.rooms()
        if not rooms:
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText(
                "先在「发起房间」或「加入房间」页启动房间，再回到这里生成二维码。"
            )
            self.url_edit.clear()
            self._show_firewall(None)  # nothing to reach until a room runs
            return
        # The address is useful on its own (a phone can type it), so resolve it
        # first: the old order returned on a missing QR dependency before the
        # user ever saw the URL — the page then looked broken instead of helpful.
        webui_url = rooms[0].open_webui(open_browser=False)  # "http://localhost:PORT"
        port = urllib.parse.urlparse(webui_url).port
        from ..server import lan_payload  # noqa: PLC0415 (lazy: heavy module)

        self.url_edit.setText(lan_payload(port, loopback=True)["url"])
        self.url_edit.setCursorPosition(0)  # 长地址默认滚到尾部，读起来像只剩 token
        self._refresh_firewall()
        try:
            import segno  # noqa: PLC0415 (graceful degrade when not installed)
        except ImportError as exc:
            self.qr_label.setPixmap(QPixmap())
            if getattr(sys, "frozen", False):  # pip cannot repair a bundle
                self.qr_label.setText(
                    f"生成二维码失败：这个打包版缺少依赖 {exc.name}。"
                    "地址已填在上面（手机手输也能进）；请更新到最新版本。"
                )
                self.qr_dep_btn.setVisible(False)
            else:
                self.qr_label.setText(
                    f"生成二维码失败：缺少依赖 {exc.name}。地址已填在上面（手机手输也能进），"
                    f"点下面的按钮装好 {exc.name} 即可出码。"
                )
                self.qr_dep_btn.setVisible(True)
            return
        self.qr_dep_btn.setVisible(False)
        mobile_url = self.url_edit.text()

        buf = io.BytesIO()
        segno.make(mobile_url, error="m").save(
            buf, kind="png", scale=8, border=2, dark="#1f1f1f", light="#ffffff"
        )
        pm = QPixmap()
        pm.loadFromData(buf.getvalue())
        self.qr_label.setText("")
        self.qr_label.setPixmap(
            pm.scaled(
                self.qr_label.width(),
                self.qr_label.height(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def _install_qr_dep(self) -> None:
        """One-click repair for a source install: pip in its own console, then
        re-render (the VidSense page's pattern, one package wide)."""
        if self._dep_proc is not None:
            return
        self.qr_label.setText("正在安装 segno…（进度见弹出的控制台，装完自动出码）")
        self.qr_dep_btn.setEnabled(False)
        try:
            self._dep_proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "segno"],
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
            )
        except OSError as exc:
            self._dep_proc = None
            self.qr_dep_btn.setEnabled(True)
            InfoBar.error("安装失败", str(exc), duration=4000, parent=self.window_ref)
            return
        self._dep_timer.start()

    def _poll_qr_dep(self) -> None:
        if self._dep_proc is None or self._dep_proc.poll() is None:
            return
        code = self._dep_proc.returncode
        self._dep_proc = None
        self._dep_timer.stop()
        self.qr_dep_btn.setEnabled(True)
        if code == 0:
            InfoBar.success("依赖已安装", "二维码已重新生成", duration=2500, parent=self.window_ref)
        else:
            InfoBar.error(
                "安装失败",
                f"pip 退出码 {code}，详见其控制台窗口",
                duration=4000,
                parent=self.window_ref,
            )
        self.refresh()

    # ── Windows Firewall: the usual reason a phone cannot get in ──

    def _refresh_firewall(self) -> None:
        """Show the firewall state, probing Windows when the answer is not cached."""
        if not firewall.supported() or not self.window_ref.rooms():
            self._show_firewall(None)  # nothing to reach: stay quiet
            return
        known = firewall.cached()
        if known is not None:
            self._show_firewall(known)
            return
        if self._fw_proc is not None:
            return  # a probe is already in flight
        self._fw_proc = firewall.start_check()
        if self._fw_proc is not None:
            self._fw_timer.start()

    def _poll_firewall(self) -> None:
        if self._fw_proc is None or self._fw_proc.poll() is None:
            return
        proc, self._fw_proc = self._fw_proc, None
        self._fw_timer.stop()
        verdict = firewall.finish_check(proc)
        # The room may have been left while the probe ran: nothing to warn about.
        self._show_firewall(verdict if self.window_ref.rooms() else None)

    def _show_firewall(self, allowed: bool | None) -> None:
        """Three states: blocked (say why, offer the fix, ask UAC once), allowed
        (say so — silence would hide the feature), unknown (stay quiet)."""
        if allowed is None:  # no room, non-Windows, or an unreadable probe
            self.fw_label.setVisible(False)
            self.fw_btn.setVisible(False)
            return
        name = firewall.program_label()
        if allowed:
            self.fw_label.setText(f"Windows 防火墙已放行 {name} 的入站连接，手机可以直接连。")
            self.fw_btn.setVisible(False)
        else:
            self.fw_label.setText(
                f"手机连不上多半是这个原因：Windows 防火墙还没有放行 {name} 的入站连接"
                "（源码版早就放行过 python.exe，打包版通常没人放行）。"
            )
            self.fw_btn.setVisible(True)
            if not self._fw_prompted:  # ask Windows once; a cancelled prompt is final
                self._fw_prompted = True
                self._allow_firewall()
        self.fw_label.setVisible(True)

    def _allow_firewall(self) -> None:
        error = firewall.request_allow()
        if error is not None:
            InfoBar.warning("没有放行", error, duration=4000, parent=self.window_ref)
            return
        InfoBar.success(
            "已请求放行",
            "在系统弹窗里同意后手机就能连；本页稍后自动复检",
            duration=3000,
            parent=self.window_ref,
        )
        QTimer.singleShot(2000, self._refresh_firewall)  # 授权之后再问一次
