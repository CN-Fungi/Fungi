"""Opening a file the way a double-click would, and making its window appear.

The WebUI page lives in a browser and cannot launch anything, so a card's two
actions (spec §73) are POSTs and this module does the shell's work:

* ``open``: hand the path to whatever the shell associates with it
  (``os.startfile``) — what a double-click does;
* ``reveal``: ``explorer.exe /select, <path>`` — the folder, file selected.

Windows gives the foreground to the process that owns it, or to the one that
just received the user's input; a background host is neither, because the click
happened in the browser. Measured on this box from a process holding no
foreground rights: neither action brought its window up, the taskbar only
flashed (skill ``windows-raise-window-from-background``). So both actions then
look for the window they made and raise it with ``AttachThreadInput``, and
report whether that worked — ``raised: False`` means "it opened, but you have
to go and get it".

Windows-only by design: Fungi ships as Fungi.exe. On any other host the
launcher has no answer at all, and the route reports that rather than silently
opening nothing.
"""

import ctypes
import os
import subprocess
import time
from pathlib import Path

_WAIT_S = 2.0  # a launched window gets this long to show up
_POLL_S = 0.1
_CONFIRM_S = 0.3  # the foreground flip is not always immediate; verify it
_SW_RESTORE = 9  # ShowWindow: a minimized window counts as not shown
_FOLDER_CLASS = "CabinetWClass"  # Explorer's folder window, and nothing else


def _foreground() -> int:
    """The window holding the foreground right now (0 = none)."""
    user32 = ctypes.windll.user32
    # HWND into a void*: the default c_int restype sign-extends any handle whose
    # low half has its top bit set, and then every comparison below is wrong.
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return int(user32.GetForegroundWindow() or 0)


def _visible_windows() -> list[int]:
    """Top-level windows that are showing, in z-order (topmost first)."""
    found: list[int] = []
    user32 = ctypes.windll.user32

    def _keep(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return 1

    # The callback wrapper must outlive the call — it does, as an argument.
    enum = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
    user32.EnumWindows(enum(_keep), 0)
    return found


def _title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    size = user32.GetWindowTextLengthW(hwnd)
    if size <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(size + 1)
    user32.GetWindowTextW(hwnd, buf, size + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _find_folder_window(folder: str) -> int:
    """The Explorer window showing `folder`, or 0.

    A title is either the bare folder name or, with "display the full path in
    the title bar" on, the whole path — a substring match covers both. The
    class check keeps a browser tab that happens to mention the folder out of
    it, which is the one real failure mode of matching on titles alone.
    """
    want = folder.strip().lower()
    if not want:
        return 0
    for hwnd in _visible_windows():
        if _window_class(hwnd) == _FOLDER_CLASS and want in _title(hwnd).lower():
            return hwnd
    return 0


def _wait_for(match):
    """The first window `match` likes, within `_WAIT_S`, or 0."""
    deadline = time.monotonic() + _WAIT_S
    while True:
        hwnd = match()
        if hwnd:
            return hwnd
        if time.monotonic() >= deadline:
            return 0
        time.sleep(_POLL_S)


def _raise(hwnd: int) -> bool:
    """Put `hwnd` in front; True only if it really got there.

    `SetForegroundWindow` is denied to a process that is neither the foreground
    one nor the one that just got the input — which is exactly what this server
    is. Attaching this thread's input to the foreground thread's first is the
    measured way through, and the read-back afterwards is what keeps `raised`
    honest: the denial is silent, so the foreground itself is the only judge.
    """
    user32 = ctypes.windll.user32
    if _foreground() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    mine = ctypes.windll.kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(theirs) and theirs != mine
    try:
        if attached:
            user32.AttachThreadInput(mine, theirs, True)
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(mine, theirs, False)
    deadline = time.monotonic() + _CONFIRM_S
    while time.monotonic() < deadline:
        if _foreground() == hwnd:
            return True
        time.sleep(0.05)
    return False


def open_with_default(path: Path) -> dict:
    """Double-click by proxy: start `path`'s association and bring it up."""
    if not hasattr(os, "startfile"):  # non-Windows: nothing to answer with
        raise OSError("opening files is only wired up on Windows")
    before = set(_visible_windows())
    was = _foreground()
    os.startfile(str(path))
    hwnd = _wait_for(lambda: next((h for h in _visible_windows() if h not in before), 0))
    if not hwnd:
        # Nothing new appeared: the association reused a window (Chrome opens a
        # tab, Word a document in the running instance). Whether it came forward
        # on its own is the only evidence left, and it is worth reporting.
        return {"raised": _foreground() != was}
    return {"raised": _raise(hwnd)}


def reveal_in_folder(path: Path) -> dict:
    """Show `path` selected in its folder, and bring that folder forward."""
    folder = path.parent.name or str(path.parent)
    subprocess.Popen(
        ["explorer.exe", "/select,", str(path)],
        creationflags=subprocess.CREATE_NO_WINDOW,  # never flash a console
    )
    hwnd = _wait_for(lambda: _find_folder_window(folder))
    return {"raised": _raise(hwnd) if hwnd else False}
