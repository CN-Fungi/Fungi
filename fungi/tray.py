"""System tray: runtime-drawn mushroom icon, fluent menu, and notifications.

Menu: open WebUI / open data directory / quit (qfluentwidgets SystemTrayMenu —
same fluent look and animations as the GUI; no extra dependency). Double-click
and message-click both open the WebUI; right-click pops the fluent menu.
"""

import webbrowser
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QCursor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import Action, MenuAnimationType, SystemTrayMenu

from . import runlog

_ACCENT = "#e07a5f"
_ALERT = "#e5484d"


def make_icon(size: int = 64, badge: bool = False) -> QIcon:
    """运行时绘制蘑菇图标（菌盖 + 菌柄 + 斑点），避免二进制资源文件。

    badge=True 在右上角加一枚红点：来信未读时托盘图标在两版之间闪动。
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # cap: upper half-circle
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_ACCENT))
    painter.drawPie(
        int(size * 0.10), int(size * 0.12), int(size * 0.80), int(size * 0.80), 0, 180 * 16
    )
    # stem
    painter.drawRoundedRect(
        int(size * 0.40),
        int(size * 0.48),
        int(size * 0.20),
        int(size * 0.40),
        int(size * 0.08),
        int(size * 0.08),
    )
    # cap spots (knocked out)
    painter.setBrush(QColor("#ffffff"))
    for cx, cy, r in ((0.32, 0.30, 0.06), (0.52, 0.22, 0.05), (0.66, 0.34, 0.055)):
        painter.drawEllipse(
            int(size * (cx - r)), int(size * (cy - r)), int(size * r * 2), int(size * r * 2)
        )
    if badge:
        # unread dot, top-right: white rim first so it reads on any taskbar
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(int(size * 0.60), int(size * 0.02), int(size * 0.38), int(size * 0.38))
        painter.setBrush(QColor(_ALERT))
        painter.drawEllipse(int(size * 0.64), int(size * 0.06), int(size * 0.30), int(size * 0.30))
    painter.end()
    return QIcon(pixmap)


class TrayController(QSystemTrayIcon):
    """托盘控制器：图标 + fluent 菜单 + 通知；动作回调由 room 装配层注入。

    PyQt5 end-to-end: the global qfluentwidgets install is the PyQt5 build
    (same binding gui.py rides), and mixing Qt bindings in one process crashes.
    """

    def __init__(self, on_open_webui=None, data_root: Path | None = None):
        super().__init__(make_icon())
        self._on_open_webui = on_open_webui or (lambda: None)
        self._data_root = data_root
        self.setToolTip("Fungi")
        menu = SystemTrayMenu(title="Fungi")
        menu.addAction(Action("打开 WebUI", triggered=self._on_open_webui))
        menu.addAction(Action("打开数据目录", triggered=self._open_data_dir))
        menu.addAction(Action("打开日志", triggered=runlog.open_folder))
        menu.addSeparator()
        menu.addAction(Action("退出", triggered=QApplication.quit))
        self._menu = menu  # keep referenced: the tray does not own it
        self.activated.connect(self._on_activated)
        # Qt delivers toast clicks via a dedicated signal, not an activation reason
        self.messageClicked.connect(self._on_open_webui)

    def _open_data_dir(self) -> None:
        if self._data_root is not None:
            webbrowser.open(self._data_root.as_uri())

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._on_open_webui()
        elif reason == QSystemTrayIcon.Context:
            # PULL_UP: qfluentwidgets anchors pos at the menu's BOTTOM edge and
            # animates upward — the tray cursor sits at the screen bottom, so
            # DROP_DOWN (top-edge anchor) always slid the menu down over it.
            self._menu.exec_(QCursor.pos(), True, MenuAnimationType.PULL_UP)

    # ── 通知 ──
    def notify(self, title: str, body: str) -> None:
        self.showMessage(title, body, QSystemTrayIcon.Information, 8000)
