"""The system-tray icon (menu, notifications, close-to-tray)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the window imports this module, so no runtime cycle
    from .app import FungiGui

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QSystemTrayIcon,
)
from qfluentwidgets import (
    Action,
    MenuAnimationType,
    SystemTrayMenu,
)

from .. import runlog
from ..tray import make_icon

FLASH_MS = 500  # unread-mail flash: half a second per phase, like a ringing icon


class _Tray(QSystemTrayIcon):
    """托盘：房间后台驻留期间提供 显示主界面 / 打开 WebUI / 退出（fluent 菜单）。

    来信未读时图标在两版之间闪动（见 set_alert）。铃声不在这里管：它一声就完
    （2026-09-11 用户：菜单里的「停止铃声」多余，撤掉）。
    """

    def __init__(self, window: "FungiGui"):
        super().__init__(make_icon())
        self._window = window
        self.setToolTip("Fungi")
        self._alerting = False
        self._flashed = False
        self._flash = QTimer(self)
        self._flash.setInterval(FLASH_MS)
        self._flash.timeout.connect(self._flash_tick)
        self._menu = SystemTrayMenu(title="Fungi")  # keep referenced: the tray does not own it
        self._menu.addAction(Action("显示主界面", triggered=window.show_and_raise))
        self._menu.addAction(Action("打开 WebUI", triggered=window.open_webui_from_tray))
        self._menu.addAction(Action("打开日志", triggered=runlog.open_folder))
        self._menu.addSeparator()
        self._menu.addAction(Action("退出", triggered=window.quit_from_tray))
        self.activated.connect(self._on_activated)

    # ── 未读闪动 ──

    def set_alert(self, on: bool) -> None:
        """Unread mail: flash the icon (and the tooltip says why)."""
        if on == self._alerting:
            return
        self._alerting = on
        self.setToolTip("Fungi — 有未读留言" if on else "Fungi")
        if on:
            self._flashed = False
            self._flash.start()
            self._flash_tick()
        else:
            self._flash.stop()
            self.setIcon(make_icon())

    def _flash_tick(self) -> None:
        self._flashed = not self._flashed
        self.setIcon(make_icon(badge=self._flashed))

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            # 点图标 = 直接进 WebUI 的好友视图（2026-09-11 用户要求）。启动器仍从
            # 菜单的「显示主界面」进得去——他说的只是「点图标」。
            self._window.open_webui_from_tray()
        elif reason == QSystemTrayIcon.Context:
            # PULL_UP, same as the room-mode tray (fungi/tray.py): the cursor is
            # at the screen bottom, so the default DROP_DOWN anchors the menu's
            # TOP edge there and slides it down over the taskbar. Pull-up anchors
            # the bottom edge at the cursor and rises from the icon.
            self._menu.exec_(QCursor.pos(), True, MenuAnimationType.PULL_UP)

    def notify(self, title: str, body: str) -> None:
        self.showMessage(title, body, QSystemTrayIcon.Information, 8000)
