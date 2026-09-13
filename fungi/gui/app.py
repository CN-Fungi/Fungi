"""The window itself: page assembly, single-instance guard, entry point."""

import contextlib
import os
import sys
import time

from PyQt5.QtCore import QSharedMemory, Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (
    QApplication,
    QMessageBox,
    QScrollArea,
)
from qfluentwidgets import (
    FluentIcon,
    FluentWindow,
)

from .. import config as config_mod
from ..config import RESOURCE_ROOT
from ..tray import make_icon
from . import ring
from .config import ConfigPage
from .const import GUI_SCALE
from .courier import CourierPage
from .help import HelpPage
from .host import HostPage
from .join import JoinPage
from .mobile import MobilePage
from .trayicon import _Tray

_GUI_IPC = "FungiGuiIPC"  # named pipe: second launch -> running window shows itself
# Windows taskbar identity. Without it the taskbar groups the window under
# python.exe (source runs) and shows the exe's own icon, so the mushroom the
# tray draws never reaches the taskbar button (2026-09-11 user report).
_APP_ID = "Offblink.Fungi"
UNREAD_POLL_MS = 1000
# Ring only after unread mail has stayed unread this long. The friend view needs
# up to ~8 s to mark a thread read (its /comm-log poll is 5 s, the room's mailbox
# poll 3 s), so ringing at once would ring for a message the user is already
# looking at. The tray flash takes no grace: it is the unread indicator itself.
RING_GRACE_S = 10.0


class FungiGui(FluentWindow):
    def __init__(self):
        super().__init__()
        self.host_page = HostPage(self)
        self.join_page = JoinPage(self)
        self.mobile_page = MobilePage(self)
        self.courier_page = CourierPage(self)
        self.cfg_page = ConfigPage(self)
        self.help_page = HelpPage()
        # Every page rides a scroll area (help-page style): the window keeps
        # its compact size no matter what each page's content minimum is.
        self.addSubInterface(self._scroll(self.host_page, "hostScroll"), FluentIcon.HOME, "发起房间")
        self.addSubInterface(self._scroll(self.join_page, "joinScroll"), FluentIcon.PEOPLE, "加入房间")
        self.addSubInterface(self._scroll(self.mobile_page, "mobileScroll"), FluentIcon.QRCODE, "手机端")
        self.addSubInterface(self._scroll(self.courier_page, "courierScroll"), FluentIcon.CALENDAR, "信使")
        self.addSubInterface(self._scroll(self.cfg_page, "cfgScroll"), FluentIcon.SETTING, "设置")
        self.addSubInterface(self.help_page, FluentIcon.INFO, "帮助")
        self.resize(840, 540)  # compact default; pages scroll instead of stretching it
        self._tray: _Tray | None = None
        # 来信提醒：未读留言的托盘闪动 + 铃声（见 _poll_unread）。读的是房间自己
        # 的邮箱轮询结果，GUI 不再打一份到 hub。
        self._ringer = ring.Ringer()
        self._unread_since: float | None = None
        self._unread_timer = QTimer(self)
        self._unread_timer.setInterval(UNREAD_POLL_MS)
        self._unread_timer.timeout.connect(self._poll_unread)
        self._unread_timer.start()
        # single-instance IPC: a second launch asks this window to show itself
        QLocalServer.removeServer(_GUI_IPC)  # stale pipe from a hard crash
        self._ipc_server = QLocalServer(self)
        if self._ipc_server.listen(_GUI_IPC):
            self._ipc_server.newConnection.connect(self._on_ipc_connection)

    @staticmethod
    def _scroll(page, name: str) -> QScrollArea:
        box = QScrollArea()
        box.setObjectName(name)
        box.setWidgetResizable(True)
        box.setFrameShape(QScrollArea.NoFrame)
        box.setWidget(page)
        return box

    # ── tray / background lifecycle ──

    def rooms(self) -> list:
        """Live rooms across pages (a page holds at most one)."""
        return [
            page.room
            for page in (self.host_page, self.join_page)
            if getattr(page, "room", None) is not None
        ]

    def update_tray(self) -> None:
        """The tray icon lives exactly while a room runs (it IS the backend)."""
        if self.rooms():
            if self._tray is None:
                self._tray = _Tray(self)
            self._tray.show()
        elif self._tray is not None:
            self._tray.hide()

    # ── 来信：托盘闪动 + 铃声 ──

    def _poll_unread(self) -> None:
        """Ring and flash while a friend's message sits unread — the same event
        the WebUI's unread badge counts (RoomBase.last_unread, filled by the
        room's own mailbox poll), and opening that thread marks it read, which
        is what stops the ring."""
        rooms = self.rooms()
        unread = int(getattr(rooms[0], "last_unread", 0) or 0) if rooms else 0
        if unread <= 0:
            if self._unread_since is not None or self._ringer.ringing:
                # only when there was an alert to take down: with no room and
                # nothing ringing this poll must cost nothing (it runs every
                # second, for the whole life of the window)
                self._unread_since = None
                self._stop_alert()
            return
        now = time.monotonic()
        if self._unread_since is None:
            self._unread_since = now
        if self._tray is not None:
            self._tray.set_alert(True)  # unread flashes at once; only the tone waits
        if now - self._unread_since >= RING_GRACE_S:
            self._start_ring()

    def _start_ring(self) -> None:
        """Play the configured tone (config is read as a ring begins, so a
        settings change applies to the next message, not mid-ring)."""
        if self._ringer.ringing:
            return
        cfg = config_mod.load_config()
        if cfg.ring:
            self._ringer.start(cfg.ring_tone)

    def _stop_alert(self) -> None:
        if self._tray is not None:
            self._tray.set_alert(False)
        self._ringer.stop()

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_ipc_connection(self) -> None:
        """Second-launch signal: raise the running window (洞见-style)."""
        conn = self._ipc_server.nextPendingConnection()
        if conn is None:
            return
        conn.readAll()
        conn.disconnectFromServer()
        self.show_and_raise()

    def open_webui_from_tray(self) -> None:
        rooms = self.rooms()
        if rooms:
            rooms[0].open_webui()

    def quit_from_tray(self) -> None:
        """Real exit: stop rooms (proper leave envelopes), then quit."""
        self._stop_alert()  # a ring must not outlive the room it belongs to
        for page in (self.host_page, self.join_page):
            room = getattr(page, "room", None)
            if room is not None:
                room.stop()
                page.room = None
        if self._tray is not None:
            self._tray.hide()
        # Deferred: calling quit() inside the current dispatch races the
        # callback teardown (flaky silent exit / hard crash on Windows).
        QTimer.singleShot(0, QApplication.quit)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.rooms():
            # Closing the window parks the room in the tray; only the tray menu's
            # 退出 (or a page's 离开房间 button) actually stops it.
            #
            # The event must STAY ignored and must NOT be chained into QWidget's
            # own handler: the default implementation accepts the close, the last
            # window then counts as closed, and quitOnLastWindowClosed kills the
            # process — which stopped the room AND hid the tray icon, the exact
            # opposite of parking (2026-09-12 probe: tray_visible=False and
            # exec_() returned on its own right after the close).
            event.ignore()
            self.hide()
            if self._tray is None:
                self.update_tray()
            if self._tray is not None:
                self._tray.show()  # the way back has to stay visible
            return
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)


def _singleton_taken(key: str = "FungiGuiSingleton") -> bool:
    """True when another process already holds the GUI singleton slot."""

    shared = QSharedMemory(key)
    return shared.attach() or not shared.create(1)


def _activate_running_instance() -> bool:
    """Second launch: ask the running GUI to show itself. True when delivered."""
    sock = QLocalSocket()
    sock.connectToServer(_GUI_IPC)
    ok = sock.waitForConnected(500)
    if ok:
        sock.write(b"show")
        sock.waitForBytesWritten(200)
        sock.disconnectFromServer()
    return ok


def _set_windows_app_id() -> None:
    """Claim our own taskbar identity, before any window exists.

    Source runs otherwise inherit python.exe's icon and grouping; the call is
    Windows-only and harmless everywhere else (and if the shell32 entry point
    is missing, the app must still start).
    """
    if os.name != "nt":
        return
    with contextlib.suppress(Exception):
        import ctypes  # noqa: PLC0415 (Windows-only, and only for the taskbar)

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_ID)


def _app_icon() -> QIcon:
    """The mushroom: assets/fungi.ico — the same art the tray draws itself."""
    ico = RESOURCE_ROOT / "assets" / "fungi.ico"
    return QIcon(str(ico)) if ico.is_file() else make_icon()


def run_gui() -> int:
    # QT_SCALE_FACTOR grows fonts, widgets and the window together (must be set
    # before QApplication exists); AA_EnableHighDpiScaling lets Qt5 honor it.
    os.environ.setdefault("QT_SCALE_FACTOR", str(GUI_SCALE))
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("Fungi")
    app.setWindowIcon(_app_icon())  # window, Alt-Tab and the taskbar button
    # Single instance: a second launcher raises the running window instead
    # (IPC ping). Without the guard, two GUI windows (each able to host a
    # room) could coexist (2026-09-04 real-machine finding; tray-room mode
    # had the same guard).
    if _singleton_taken():
        if _activate_running_instance():
            print("Fungi GUI 已在运行：已唤起主界面。")
        else:
            QMessageBox.warning(None, "Fungi", "Fungi GUI 已在运行。")
        return 0
    win = FungiGui()
    win.show()
    return app.exec_()
