"""Screen tool: look at this machine's desktop and, with the user's consent, act on it.

Design decisions live in docs/spec.md §35; the load-bearing ones:

- **Coordinates are never the model's.** `click` / `type` / `scroll` name a
  target from `targets` (the program-side a11y/OCR candidate list), so the pixel
  comes from the OS. Measured 2026-09-13: coordinates produced by the model miss
  by 15-68px (a coin flip on an 18px target); picking a candidate number hit 3/3,
  1px on the hard one.
- `config.json` `pc_control` (default off) is both the consent and the switch: on
  means the agent works the desktop directly, with no per-action prompt (user
  decision 2026-09-13). Turning it off — or leaving the room — ends it at once and
  releases every held key, because a stuck host Ctrl is a fault only a human can
  clear.
- Every input action verifies itself against program-side truth (a11y value /
  focus / window rect / frame diff) and asks the user after FAILURE_LIMIT
  strikes instead of clicking blind again.
- **触手可及**: opening a window goes through the application's *own* entry point on
  screen — its taskbar button, its tray icon, or its desktop icon — because that is
  the path the application listens to. `ShowWindow` is the fallback for an app with
  no entry at all, and its result is the "visible but asleep" window (spec §35.15).
- An elevated window (UAC secure desktop) stays out of reach: UIPI. That is the
  hard boundary and the natural human/machine line, not an implementation gap.

Local agent only (spec §35.3): `trilayer.build_orchestrator` and the room's
user-facing turn agent attach it; comm agents and spawned subagents never do.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import ctypes.wintypes as wt
import importlib
import importlib.util
import io
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageGrab, ImageStat

from fungi.agent import BoundTool
from fungi.config import Config
from fungi.events import Sink
from fungi.tools.ask import blocking_ask
from fungi.tools.files import ImageRead, image_data_url

FAILURE_LIMIT = 3  # strikes before the tool asks the user instead of retrying
MAX_CANDIDATES = 60
DIFF_THRESHOLD = 0.002  # fraction of changed pixels that counts as an effect
SETTLE_S = 0.30  # let the app repaint before the verifying frame
DOUBLE_CLICK_GAP_S = 0.06  # well inside the system's double-click time (default 0.5s)
MOVE_STEPS = 14  # intermediate points on the way to a click target — a glide, not a teleport
MOVE_DURATION_S = 0.22  # total travel time: a person's flick, and small against SETTLE_S
MOVE_TOLERANCE_PX = 2  # measured round-trip error of the 0..65535 space: 0px, -1px at a corner
LAUNCH_WAIT_S = 3.0  # a gesture that starts a process: its window is not up immediately
SHOT_MAX_DIM = 1568  # same vision sweet spot as files.IMAGE_MAX_DIM

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi = ctypes.WinDLL("gdi32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_u32.GetSystemMetrics.argtypes = [ctypes.c_int]
_u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_u32.GetSystemMetrics.restype = ctypes.c_int
_u32.GetWindowDC.argtypes = [wt.HWND]
_u32.GetWindowDC.restype = ctypes.c_void_p
_u32.ReleaseDC.argtypes = [wt.HWND, ctypes.c_void_p]
_u32.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_uint]
_u32.PrintWindow.restype = wt.BOOL
_u32.GetClipboardData.argtypes = [ctypes.c_uint]
_u32.GetClipboardData.restype = ctypes.c_void_p
_u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_u32.SetClipboardData.restype = ctypes.c_void_p
_gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.SelectObject.restype = ctypes.c_void_p
_gdi.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi.GetDIBits.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint,
]
_k32.OpenProcess.argtypes = [ctypes.c_uint, wt.BOOL, ctypes.c_uint]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_k32.GlobalAlloc.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [ctypes.c_void_p]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_k32.GlobalSize.argtypes = [ctypes.c_void_p]
_k32.GlobalSize.restype = ctypes.c_size_t

# Every one of these returns a window handle: left to ctypes' default (c_int) a
# handle above 0x7FFFFFFF comes back negative, and comparing it with a window id
# then silently fails. Declared here for the ones this module reads.
_u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
_u32.FindWindowW.restype = wt.HWND
_u32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
_u32.FindWindowExW.restype = wt.HWND
_u32.WindowFromPoint.argtypes = [wt.POINT]
_u32.WindowFromPoint.restype = wt.HWND
_u32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_u32.GetAncestor.restype = wt.HWND
_u32.GetForegroundWindow.restype = wt.HWND
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_u32.GetCursorPos.restype = wt.BOOL

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _ensure_dpi() -> None:
    """One-shot: make screen coordinates and captured pixels the same space.

    A DPI-unaware process (a CLI run, or the exe) reads GetSystemMetrics in
    logical units while ImageGrab returns physical ones — measured on this box
    2026-09-13: 1493x933 vs 2240x1400. Everything after this call is physical
    pixels, so a rect read from a11y is a pixel in the frame. Qt already sets
    per-monitor-v2 for the GUI, in which case this call just fails harmlessly.
    """
    if getattr(_ensure_dpi, "_done", False):
        return
    _ensure_dpi._done = True  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        _u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2


def _virtual_origin() -> tuple[int, int]:
    return (
        int(_u32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(_u32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
    )


def _virtual_size() -> tuple[int, int]:
    return (
        int(_u32.GetSystemMetrics(SM_CXVIRTUALSCREEN)) or 1,
        int(_u32.GetSystemMetrics(SM_CYVIRTUALSCREEN)) or 1,
    )


# ── window identity: hwnd + process, never a title match ────────────────────
@dataclass(frozen=True)
class Win:
    hwnd: int
    title: str
    cls: str
    rect: tuple[int, int, int, int]
    pid: int
    proc: str
    state: str = "normal"  # normal | minimized | hidden | untitled

    @property
    def size(self) -> str:
        return f"{self.rect[2] - self.rect[0]}x{self.rect[3] - self.rect[1]}"


def _window_text(hwnd: int) -> str:
    n = _u32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        _k32.CloseHandle(ctypes.c_void_p(handle))


def _keep_window(state: str, title: str, *, include_hidden: bool) -> bool:
    """Which top-level windows a listing shows.

    Default: what the user can see or un-minimize. Asked for: the tray-resident
    ones too (a hidden window with a title), plus the untitled system surfaces
    the taskbar is made of — those are how a tray-only application is reached.
    """
    if not include_hidden:
        return state != "hidden" and bool(title)
    return state == "normal" or bool(title)


def list_windows(include_hidden: bool = False) -> list[Win]:
    """Top-level windows in z-order.

    Default: what the user can see and name. `include_hidden=True` adds the
    windows that live in the notification area (a tray-only app keeps a hidden
    or minimized window with a title) and the untitled system windows the taskbar
    is made of — without which there is no way to hand the model an hwnd for
    either (spec §35.7).
    """
    _ensure_dpi()
    out: list[Win] = []
    callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        state = window_state(hwnd)
        title = _window_text(hwnd)
        if not _keep_window(state, title, include_hidden=include_hidden):
            return True
        rect = window_rect(hwnd)
        if rect is None:
            return True
        # A minimized window's GetWindowRect is its icon slot (-48000,-48000 measured
        # on this box): read the restored geometry *before* the size filter, or every
        # minimized window is thrown out as icon-sized plumbing — which made the
        # every-window view lose exactly the windows `restore` exists for, and made
        # `shell_wake` answer "the window is gone" for them (2026-09-13).
        if state == "minimized" and (restored := normal_rect(hwnd)) is not None:
            rect = restored
        # In the every-window view, skip icon-sized plumbing (1x1 bridges, driver
        # status windows): measured 59 rows -> 40 useful ones on this box.
        if include_hidden and (rect[2] - rect[0] < 40 or rect[3] - rect[1] < 40):
            return True
        pid = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append(
            Win(
                int(hwnd),
                title,
                _class_name(hwnd),
                rect,
                int(pid.value),
                _process_name(pid.value),
                state if title else "untitled",
            )
        )
        return True

    _u32.EnumWindows(callback(visit), 0)
    return out


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    # Every coordinate that leaves this module is physical, so the awareness
    # switch has to happen before the first read — not merely before the first
    # capture. Measured 2026-09-13: a rect read while the process was still
    # DPI-unaware came back 300,200 where the same window was at 450,300.
    _ensure_dpi()
    rect = wt.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    box = (rect.left, rect.top, rect.right, rect.bottom)
    return box if box[2] > box[0] and box[3] > box[1] else None


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", wt.POINT),
        ("ptMaxPosition", wt.POINT),
        ("rcNormalPosition", wt.RECT),
    ]


def window_state(hwnd: int) -> str:
    """normal | minimized | hidden | untitled.

    A minimized window is still 'visible' to IsWindowVisible and keeps its title
    — that is how a window pushed into the notification area looks from outside,
    and why the state has to be reported instead of guessed from visibility.
    """
    if not _u32.IsWindowVisible(hwnd):
        return "hidden"
    if _u32.IsIconic(hwnd):
        return "minimized"
    return "normal"


def normal_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's restored geometry — a minimized window's GetWindowRect is
    its icon slot (-48000,-48000 measured on this box 2026-09-13), which would
    make a captured frame a 356x59 smear."""
    _ensure_dpi()
    _u32.GetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if not _u32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return None
    rect = placement.rcNormalPosition
    box = (rect.left, rect.top, rect.right, rect.bottom)
    return box if box[2] > box[0] and box[3] > box[1] else None


def ensure_on_screen(hwnd: int) -> str:
    """Bring a minimized or tray-hidden window back, and say what it was.

    `SW_RESTORE` shows a hidden window too, so this is also the way to wake an
    application that lives only in the notification area (spec §35.7). Both
    `targets` and every input action need it: a minimized window's controls keep
    off-screen rectangles and cannot be clicked where they claim to be.
    """
    state = window_state(hwnd)
    if state == "normal":
        return state
    _u32.ShowWindow(hwnd, SW_RESTORE)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and window_state(hwnd) != "normal":
        time.sleep(0.1)
    time.sleep(0.15)  # let it repaint before anyone measures or captures it
    return state


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's client area in screen coordinates.

    This is where an application draws its own controls; the title-bar buttons sit
    outside it. That distinction is what decides whether a window needs the
    picture tiers at all (spec §35.1): a window whose only *actionable* a11y
    elements are its window buttons is a self-drawn surface.
    """
    _ensure_dpi()
    rect = wt.RECT()
    if not _u32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wt.POINT(0, 0)
    if not _u32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom)


def inside_client(hwnd: int, target: Target) -> bool:
    client = client_rect(hwnd)
    if client is None:
        return True  # unknown: do not pretend the tiers are needed
    centre_x, centre_y = target.center
    return client[0] <= centre_x <= client[2] and client[1] <= centre_y <= client[3]


def foreground_hwnd() -> int:
    return int(_u32.GetForegroundWindow() or 0)


def set_foreground(hwnd: int) -> bool:
    """Raise the target before clicking: otherwise the click lands on whatever
    covers it (a hit on the wrong window measured in the prototype)."""
    if foreground_hwnd() == hwnd:
        return True
    _u32.ShowWindow(hwnd, SW_RESTORE)
    _u32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return foreground_hwnd() == hwnd


# ── pixels: capture ────────────────────────────────────────────────────────
@dataclass
class Frame:
    """A captured bitmap plus the screen coordinate of its pixel (0,0)."""

    image: Image.Image
    origin: tuple[int, int]
    hwnd: int | None
    ts: float

    def screen_rect(self) -> tuple[int, int, int, int]:
        ox, oy = self.origin
        return (ox, oy, ox + self.image.width, oy + self.image.height)

    def to_local(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        ox, oy = self.origin
        return (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def grab_screen() -> Frame:
    """Whole virtual desktop. Below IMAGE_MAX_DIM on a typical box, so the model
    gets near-native pixels; still call the window shot for small text."""
    _ensure_dpi()
    img = ImageGrab.grab(all_screens=True).convert("RGB")
    return Frame(img, _virtual_origin(), None, time.time())


def _dpi_unaware(hwnd: int) -> bool:
    """True when the target's process is DPI-unaware.

    Windows then gives it a *scaled* physical rect while its client area still
    draws at 100%, and PrintWindow returns that unscaled content in the top-left
    of a full-size bitmap: half black, with every control rectangle off by the
    scale factor (measured on this box 2026-09-13, 2240px screen at 150%).
    """
    with contextlib.suppress(Exception):
        ctx = _u32.GetWindowDpiAwarenessContext(hwnd)
        return bool(_u32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-1)))  # UNAWARE
    return False


def _print_window(hwnd: int, rect: tuple[int, int, int, int]) -> Frame | None:
    """PrintWindow(PW_RENDERFULLCONTENT): captures a window that is behind
    another one without stealing focus (verified on Win11 Notepad, 2026-09-13)."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    hdc = _u32.GetWindowDC(hwnd)
    mem = _gdi.CreateCompatibleDC(hdc)
    bmp = _gdi.CreateCompatibleBitmap(hdc, width, height)
    old = _gdi.SelectObject(mem, bmp)
    try:
        if not _u32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
            _u32.PrintWindow(hwnd, mem, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        info = _BITMAPINFOHEADER(
            ctypes.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0
        )
        if not _gdi.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(info), 0):
            return None
        image = Image.frombytes("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
    finally:
        _gdi.SelectObject(mem, old)
        _gdi.DeleteObject(bmp)
        _gdi.DeleteDC(mem)
        _u32.ReleaseDC(hwnd, hdc)
    return Frame(image, (rect[0], rect[1]), hwnd, time.time())


def capture_problem(hwnd: int) -> str | None:
    """Why this window's pixels cannot be captured right now, or None.

    The one case that has no honest answer: a DPI-unaware process that is behind
    another window. PrintWindow then returns the content at 1:1 inside a
    scaled-size bitmap — 58% black padding with every control off by the scale
    factor (measured 2026-09-13) — and the picture cannot be trusted for picking
    targets. Raising the window without activating does not move the z-order (the
    shell ignores it; WindowFromPoint still reports the covering window), so the
    fix belongs to the caller: bring it to the front, which is what restore and
    every input action do.
    """
    if _dpi_unaware(hwnd) and foreground_hwnd() != hwnd:
        return (
            f"window 0x{hwnd:X} {_window_text(hwnd)!r} runs in a DPI-unaware process and is "
            "currently behind another window, where Windows hands back a scaled stub instead of "
            "its pixels. Bring it to the front first: action=restore "
            f"(hwnd={hwnd}) — that does it and is also how a minimized or tray-resident window "
            "comes back."
        )
    return None


def grab_window(hwnd: int) -> Frame | None:
    """A window's pixels, in the same coordinate space as its a11y rectangles.

    A DPI-unaware window is cropped out of the screen capture: that is the only
    path that gives the pixels at the scale the user sees. Checked callers ask
    `capture_problem` first, so the unanswerable case (unaware *and* covered)
    becomes an explicit error instead of a silently misaligned picture.
    """
    _ensure_dpi()
    rect = window_rect(hwnd)
    if rect is None:
        return None
    if _dpi_unaware(hwnd):
        if foreground_hwnd() != hwnd:
            return None
        screen = ImageGrab.grab(all_screens=True).convert("RGB")
        origin = _virtual_origin()
        box = (rect[0] - origin[0], rect[1] - origin[1], rect[2] - origin[0], rect[3] - origin[1])
        return Frame(screen.crop(box), (rect[0], rect[1]), hwnd, time.time())
    frame = _print_window(hwnd, rect)
    if frame is not None and foreground_hwnd() != hwnd:
        low, high = frame.image.convert("L").getextrema()
        if low == high:
            # A single-colour bitmap is not "an empty window": some renderers
            # (Chromium/Electron surfaces) hand PrintWindow nothing at all while
            # they sit behind another window. Passing that on would tell the model
            # "the panel is blank" — a wrong answer shaped like a real one, which
            # cost a user an hour on 2026-09-13.
            return None
    return frame


def _diff_ratio(before: Frame, after: Frame) -> float:
    """Fraction of pixels that changed — the 'did that do anything' signal when
    a control exposes no readable state. PIL does the diff in C; a pure-Python
    per-pixel loop over 1M pixels would cost more than the action itself."""
    if before.image.size != after.image.size:
        return 1.0
    width = min(before.image.width, after.image.width)
    height = min(before.image.height, after.image.height)
    left = before.image.crop((0, 0, width, height)).convert("L")
    right = after.image.crop((0, 0, width, height)).convert("L")
    diff = ImageChops.difference(left, right).point(lambda p: 255 if p > 24 else 0)
    return ImageStat.Stat(diff).mean[0] / 255.0


def _attach(summary: str, frame: Frame) -> ImageRead:
    buf = io.BytesIO()
    frame.image.save(buf, "PNG")
    url, mime, dims = image_data_url(".png", buf.getvalue())
    if url is None:
        return ImageRead(f"{summary}\n(frame captured but could not be encoded)", [])
    return ImageRead(f"{summary}\n[frame attached: {dims}, {mime}]", [url])


INK_CONTRAST = 8  # a pixel this much darker than its neighbourhood counts as ink
INK_CLOSE = 9  # closing kernel: the strokes of one glyph become one blob
MIN_SIDE, MAX_SIDE = 8, 900  # the prototype's candidate size window (pixels)


def _visual_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Binarise *inside the program* and hand back boxes — never the binary image.

    The prototype measured why (2026-09-13): a thresholded picture sent to the
    model cost 21-296px of error and 3x the latency, while the same threshold fed
    to the *algorithm* produced 94 candidates on a real desktop, 75 of them
    carrying text of their own. So the threshold stops here and the model only
    ever chooses between numbers.

    Pillow does the pixels (BoxBlur / MaxFilter / MinFilter / point run in C), the
    components are run-length + union-find, and OpenCV stays out of the install.
    """
    grey = image.convert("L")
    # adaptive threshold (mean of a ~21px neighbourhood, C=8) built from C-speed
    # parts: ink is a pixel at least INK_CONTRAST levels darker than its area
    local = grey.filter(ImageFilter.BoxBlur(10))
    ink = ImageChops.subtract(local, grey).point(lambda v: 255 if v >= INK_CONTRAST else 0)
    # close it, so the strokes of one glyph become one blob
    ink = ink.filter(ImageFilter.MaxFilter(INK_CLOSE)).filter(ImageFilter.MinFilter(INK_CLOSE))
    # Filter *before* merging: the window's own border is a connected ring whose
    # bounding box is the whole frame, and merging first let it swallow every real
    # candidate (measured on the canvas probe, 2026-09-13). A box that spans the
    # frame is chrome or background, never a control.
    width, height = image.size
    return _merge_boxes(
        [
            box
            for box in _components(ink)
            if MIN_SIDE <= box[2] - box[0] <= MAX_SIDE
            and MIN_SIDE <= box[3] - box[1] <= MAX_SIDE
            and not (box[2] - box[0] >= 0.9 * width and box[3] - box[1] >= 0.9 * height)
        ]
    )


def _components(mask: Image.Image) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of a mask's connected components.

    Run-length per row plus union-find across rows: two runs that overlap in x are
    the same blob. Only runs are touched, so a 780x690 window costs a few hundred
    milliseconds in pure Python.
    """
    width, height = mask.size
    data = mask.tobytes()
    parent: dict[int, int] = {}
    runs: list[tuple[int, int, int, int]] = []  # (id, y, x0, x1)

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path compression
            parent[node], node = root, parent[node]
        return root

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    previous: list[tuple[int, int, int, int]] = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        current: list[tuple[int, int, int, int]] = []
        start = row.find(b"\xff")
        while start != -1:
            end = row.find(b"\x00", start)
            if end == -1:
                end = width
            run_id = len(parent)
            parent[run_id] = run_id
            current.append((run_id, y, start, end))
            start = row.find(b"\xff", end)
        for run_id, _y, x0, x1 in current:
            for other, _other_y, other_x0, other_x1 in previous:
                if x0 < other_x1 and other_x0 < x1:
                    union(run_id, other)
        runs.extend(current)
        previous = current
    boxes: dict[int, list[int]] = {}
    for run_id, y, x0, x1 in runs:
        box = boxes.setdefault(find(run_id), [x0, y, x1, y + 1])
        box[0], box[2] = min(box[0], x0), max(box[2], x1)
        box[1], box[3] = min(box[1], y), max(box[3], y + 1)
    return [tuple(box) for box in boxes.values()]


def _merge_boxes(
    boxes: list[tuple[int, int, int, int]], *, gap: int = 6
) -> list[tuple[int, int, int, int]]:
    """Fold the blobs that belong to one control together, drop the noise.

    A smiley is three blobs and a list row a dozen: anything within `gap` pixels
    is the same candidate. Icon-sized and window-sized boxes are not candidates —
    the prototype's 8..900 filter, which is also what keeps the frame border and
    the scrollbar out of the list.
    """
    merged = list(boxes)
    changed = True
    while changed:
        changed = False
        out: list[tuple[int, int, int, int]] = []
        for box in merged:
            for index, other in enumerate(out):
                if (
                    box[0] - gap <= other[2]
                    and other[0] - gap <= box[2]
                    and box[1] - gap <= other[3]
                    and other[1] - gap <= box[3]
                ):
                    out[index] = (
                        min(box[0], other[0]),
                        min(box[1], other[1]),
                        max(box[2], other[2]),
                        max(box[3], other[3]),
                    )
                    changed = True
                    break
            else:
                out.append(box)
        merged = out
    return merged


def _visual_targets(frame: Frame, start: int = 0, limit: int = 40) -> list[Target]:
    """The numbered candidates cut out of the picture, in screen coordinates."""
    origin_x, origin_y = frame.origin
    out: list[Target] = []
    for box in _visual_boxes(frame.image)[:limit]:
        out.append(
            Target(
                start + len(out) + 1,
                "",
                "",
                (origin_x + box[0], origin_y + box[1], origin_x + box[2], origin_y + box[3]),
                (),
                "visual",
            )
        )
    return out


def _mark_targets(frame: Frame, targets: list[Target]) -> Image:
    """Draw the numbers onto the frame: the model sees the picture it is choosing
    from, carrying the same numbers the listing printed."""
    marked = frame.image.copy()
    draw = ImageDraw.Draw(marked)
    for target in targets:
        left, top, right, bottom = frame.to_local(target.rect)
        draw.rectangle(
            (left, top, max(left + 1, right - 1), max(top + 1, bottom - 1)), outline=(255, 0, 0)
        )
        draw.text((left + 2, max(0, top + 1)), str(target.n), fill=(255, 255, 0))
    return marked


# ── a11y: UI Automation, the coordinate source that has no error ────────────
_thread_local = threading.local()


def _uia():
    """Per-thread UIA instance: COM objects are apartment-bound, and the agent
    runs tool calls on worker threads."""
    cached = getattr(_thread_local, "uia", None)
    if cached is not None:
        return cached
    try:
        import comtypes.client  # noqa: PLC0415 (heavy: only for desktop work)
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise RuntimeError(
            "screen control needs the comtypes package (UIAutomation); "
            "install it with: pip install comtypes"
        ) from exc
    comtypes.client.GetModule("UIAutomationCore.dll")
    # The type-library wrapper module is generated on the spot by GetModule, so
    # it is looked up by name instead of with an import statement.
    uia_client = importlib.import_module("comtypes.gen.UIAutomationClient")

    automation = comtypes.client.CreateObject(
        uia_client.CUIAutomation, interface=uia_client.IUIAutomation
    )
    _thread_local.uia = (automation, uia_client)
    return _thread_local.uia


def _pattern_labels(uia_client) -> tuple[tuple[int, str], ...]:
    return (
        (uia_client.UIA_InvokePatternId, "Invoke"),
        (uia_client.UIA_ValuePatternId, "Value"),
        (uia_client.UIA_SelectionItemPatternId, "Select"),
        (uia_client.UIA_ExpandCollapsePatternId, "ExpandCollapse"),
        (uia_client.UIA_ScrollItemPatternId, "ScrollItem"),
    )


def _child_of(walker, element):
    with contextlib.suppress(Exception):
        return walker.GetFirstChildElement(element)
    return None


def _sibling_of(walker, element):
    with contextlib.suppress(Exception):
        return walker.GetNextSiblingElement(element)
    return None


def _has_pattern(element, pattern_id: int) -> bool:
    try:
        return bool(element.GetCurrentPattern(pattern_id))
    except Exception:
        return False


@dataclass
class Target:
    """One selectable thing inside a window: the unit the model names instead of
    a coordinate."""

    n: int
    name: str
    cls: str
    rect: tuple[int, int, int, int]
    patterns: tuple[str, ...] = ()
    source: str = "a11y"

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def label(self) -> str:
        if self.source == "visual":
            kind, name = "[shape]", self.name or "(no text)"
        else:
            kind = f"[{'+'.join(self.patterns)}]" if self.patterns else "[text]"
            name = self.name or self.cls or "(unnamed)"
        number = f"#{self.n} " if self.n else ""  # a label carries no listing number
        return (
            f"{number}{kind} {name!r} cls={self.cls or '-'} rect={self.rect} centre={self.center}"
        )


def _scan(hwnd: int, limit: int = MAX_CANDIDATES) -> list[tuple[Target, Any]]:
    """Elements inside one window: (numbered target, live UIA element).

    Walks from the window element itself — never from the desktop root, which
    would mix in another window's controls (a wrong-click bug in the prototype)
    and cost an order of magnitude more time.
    """
    _ensure_dpi()
    automation, uia_client = _uia()
    try:
        window = automation.ElementFromHandle(hwnd)
    except Exception:
        return []
    if window is None:
        return []
    table = _pattern_labels(uia_client)
    walker = automation.RawViewWalker
    found: list[tuple[str, str, tuple[int, int, int, int], tuple[str, ...], Any]] = []

    def walk(element, depth: int) -> None:
        # UIA providers raise COM errors on windows that are minimized, closing
        # or gone ('invalid pointer' measured on a minimized window 2026-09-13):
        # a failed child lookup ends that branch, it never kills the scan.
        child = _child_of(walker, element)
        while child is not None and len(found) < limit * 6:
            try:
                rect = child.CurrentBoundingRectangle
                box = (rect.left, rect.top, rect.right, rect.bottom)
                if box[2] > box[0] and box[3] > box[1]:
                    patterns = tuple(label for pid, label in table if _has_pattern(child, pid))
                    name = child.CurrentName or ""
                    if name or patterns:
                        found.append((name, child.CurrentClassName or "", box, patterns, child))
            except Exception:
                pass
            if depth < 6:
                walk(child, depth + 1)
            child = _sibling_of(walker, child)

    walk(window, 0)
    found.sort(key=lambda row: (row[2][1], row[2][0]))
    return [
        (Target(n, name, cls, box, patterns), element)
        for n, (name, cls, box, patterns, element) in enumerate(found[:limit], 1)
    ]


def _focused() -> dict | None:
    """The focused element — program-side truth that a key or click landed.

    UIA answers in the calling process's DPI space, so this reads physical
    coordinates only after `_ensure_dpi` (see window_rect).
    """
    _ensure_dpi()
    try:
        automation, _uia_client = _uia()
        element = automation.GetFocusedElement()
        rect = element.CurrentBoundingRectangle
        return {
            "name": element.CurrentName or "",
            "cls": element.CurrentClassName or "",
            "rect": (rect.left, rect.top, rect.right, rect.bottom),
        }
    except Exception:
        return None


def _value_of(element, uia_client) -> str:
    try:
        pattern = element.GetCurrentPattern(uia_client.UIA_ValuePatternId)
        if pattern:
            return pattern.QueryInterface(uia_client.IUIAutomationValuePattern).CurrentValue or ""
    except Exception:
        pass
    return ""


def _read_back(hwnd: int, target: Target | None) -> tuple[str, str]:
    """(value, which element it came from) — the read-back half of 'type'."""
    pairs = _scan(hwnd, limit=MAX_CANDIDATES)
    _automation, uia_client = _uia()
    if target is not None:
        element = _match_element(pairs, target)
        if element is not None:
            return _value_of(element, uia_client), target.name or target.cls
    focused = _focused()
    if focused is not None:
        for cand, element in pairs:
            if cand.rect == focused["rect"]:
                value = _value_of(element, uia_client)
                if value:
                    return value, cand.name or cand.cls
    for cand, element in pairs:
        if "Value" in cand.patterns:
            return _value_of(element, uia_client), cand.name or cand.cls
    return "", ""


def _read_back_settled(hwnd: int, target: Target | None) -> tuple[str, str]:
    """`_read_back` with one retry: a single empty read also happens when the app
    is mid-repaint, and the read is what decides whether a paste is verified."""
    value, where = _read_back(hwnd, target)
    if value:
        return value, where
    time.sleep(0.25)
    return _read_back(hwnd, target)


def _match_element(pairs: list[tuple[Target, Any]], target: Target):
    """Re-find a candidate after the window may have moved: same name+class,
    nearest centre. Candidates are metadata, elements are per-call objects."""
    best = None
    best_distance = None
    for cand, element in pairs:
        if (not cand.name and not cand.cls) or cand.name != target.name or cand.cls != target.cls:
            continue  # nothing to identify it by
        cx, cy = cand.center
        tx, ty = target.center
        distance = abs(cx - tx) + abs(cy - ty)
        if best_distance is None or distance < best_distance:
            best, best_distance = element, distance
    return best


def _ocr_targets(frame: Frame, start: int = 0) -> list[Target]:
    """Optional fallback for windows with no usable a11y (pure canvas UI).

    rapidocr is a heavy extra (`pip install rapidocr-onnxruntime`), so its
    absence is reported rather than papered over. Boxes come back in frame
    pixels and are converted to screen coordinates with the frame origin — the
    prototype's two-point calibration problem does not exist here because the
    capture and the click share one coordinate space (see _ensure_dpi).
    """
    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        return []
    try:
        import numpy as np  # noqa: PLC0415
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
    except ImportError:
        return []
    engine = getattr(_thread_local, "ocr", None)
    if engine is None:
        engine = RapidOCR()
        _thread_local.ocr = engine
    result, _ = engine(np.array(frame.image.convert("RGB"))[:, :, ::-1])
    ox, oy = frame.origin
    out: list[Target] = []
    for item in result or []:
        points = np.array(item[0], dtype=float)
        left, top = int(points[:, 0].min()), int(points[:, 1].min())
        box = (
            ox + left,
            oy + top,
            ox + int(points[:, 0].max()),
            oy + int(points[:, 1].max()),
        )
        out.append(Target(start + len(out) + 1, str(item[1]), "", box, (), "ocr"))
    return out


# ── input injection ────────────────────────────────────────────────────────
class _MOUSEINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("pad", ctypes.c_byte * 32),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_: ClassVar[list[tuple[str, Any]]] = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}

# Keyboard vocabulary: the Ophio key_map's names and aliases, with 'delete'
# pointing at the real Del and the desktop-only keys (home/end/insert/pageup/
# pagedown/printscreen) added. pyautogui is not a Fungi dependency, so the table
# carries VK codes and injection goes through SendInput.
_KEY_VK: dict[str, int] = {
    "escape": 0x1B,
    "esc": 0x1B,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "space": 0x20,
    " ": 0x20,
    "capslock": 0x14,
    "up": 0x26,
    "arrowup": 0x26,
    "down": 0x28,
    "arrowdown": 0x28,
    "left": 0x25,
    "arrowleft": 0x25,
    "right": 0x27,
    "arrowright": 0x27,
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "option": 0x12,
    "win": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
    "command": 0x5B,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "insert": 0x2D,
    "ins": 0x2D,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "numpad_multiply": 0x6A,
    "numpad_add": 0x6B,
    "numpad_subtract": 0x6D,
    "numpad_decimal": 0x6E,
    "numpad_divide": 0x6F,
    **{chr(0x61 + i): 0x41 + i for i in range(26)},
    **{str(i): 0x30 + i for i in range(10)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
_EXTENDED_VK = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x6F}
)
_MODIFIER_VK = frozenset({0x10, 0x11, 0x12, 0x5B})
MODIFIER_NAMES = frozenset({"shift", "ctrl", "control", "alt", "win", "meta", "cmd", "command"})
_held: set[int] = set()


def resolve_key(name: str) -> int | None:
    """Key name (aliases and either case) → virtual-key code, else None."""
    text = str(name).strip()
    return _KEY_VK.get(text) or _KEY_VK.get(text.lower())


def _send(payload: _INPUT) -> int:
    return int(_u32.SendInput(1, ctypes.byref(payload), ctypes.sizeof(_INPUT)))


def _mouse_event(x: int, y: int, flags: int) -> int:
    return _send(_INPUT(INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(x, y, 0, flags, 0, None))))


def _norm_point(x: int, y: int) -> tuple[int, int]:
    """Physical pixel → SendInput's 0..65535 space over the whole virtual desktop.

    Not just the primary monitor: the virtual desk flags make the same numbers mean
    the same pixels on every display.
    """
    _ensure_dpi()
    ox, oy = _virtual_origin()
    width, height = _virtual_size()
    return (
        int((x - ox) * 65535 / max(1, width - 1)),
        int((y - oy) * 65535 / max(1, height - 1)),
    )


def cursor_pos() -> tuple[int, int] | None:
    """Where the pointer is, in physical pixels (None if the OS will not say)."""
    point = wt.POINT()
    return (point.x, point.y) if _u32.GetCursorPos(ctypes.byref(point)) else None


def _smoothstep(t: float) -> float:
    """0→1 with no jerk at either end: what makes a glide read as a hand and not a jerk."""
    return t * t * (3.0 - 2.0 * t)


def move_to(x: int, y: int, *, steps: int = MOVE_STEPS, duration: float = MOVE_DURATION_S) -> int:
    """Glide the pointer to physical (x, y); return the number of move events injected.

    A single absolute `MOUSEEVENTF_MOVE` teleports: the application sees one jump from
    wherever the pointer was straight onto the target. That is wrong for anything that
    tracks the pointer rather than just reading its position — hover states and tooltips
    never fire, a canvas or drag-style UI gets no intermediate coordinates, and a
    mis-aimed jump cannot be seen coming. So the travel is a short eased path (`MOVE_STEPS`
    points over `MOVE_DURATION_S`, smoothstep) whose last point is exactly the target.

    The pointer moves through the same `SendInput` channel as the clicks, never
    `SetCursorPos`: one injection path for everything this tool does, so an application
    that watches for injected input sees one continuous gesture.
    """
    move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    nx, ny = _norm_point(x, y)
    here = cursor_pos()
    if here is None or here == (x, y):
        # Nothing to travel: keep the position pinned with the one event a click needs.
        _mouse_event(nx, ny, move)
        return 1
    hx, hy = _norm_point(*here)
    sleep = duration / max(1, steps)
    for step in range(1, steps + 1):
        eased = _smoothstep(step / steps)
        # the last step is eased == 1.0, so it lands on (nx, ny) exactly — no extra event
        _mouse_event(int(hx + (nx - hx) * eased), int(hy + (ny - hy) * eased), move)
        if step < steps:
            time.sleep(sleep)
    return steps


def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> bool:
    """Absolute click(s) at physical screen coordinates, after gliding there.

    Normalized against the virtual desktop — SendInput's 0..65535 space covers
    every monitor, not just the primary one. The pointer travels (`move_to`) rather
    than teleports, so the application sees it arrive.

    `clicks=2` is the double-click: both press/release pairs land inside the
    system's double-click time, with no pointer movement between them, which is
    what makes the shell (and an app's own hit-testing) read it as one gesture
    rather than two clicks.
    """
    nx, ny = _norm_point(x, y)
    move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    down, up = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS["left"])
    moves = move_to(x, y)
    # Did it actually get there? A coordinate outside the virtual screen gets clamped by
    # the OS (measured 2026-09-14: x=-185 became 0), and then a press would land on
    # whatever is at the edge — a wrong click nobody asked for. No arrival, no press.
    landed = cursor_pos()
    if landed is None or max(abs(landed[0] - x), abs(landed[1] - y)) > MOVE_TOLERANCE_PX:
        return False
    injected = moves
    for index in range(clicks):
        if index:
            time.sleep(DOUBLE_CLICK_GAP_S)
        injected += _mouse_event(nx, ny, move | down)
        injected += _mouse_event(nx, ny, move | up)
    # the travel plus a press/release pair per click; a struct-size mistake makes
    # SendInput return 0 silently, which is what this number is here to catch
    return injected == moves + 2 * clicks


def _key_event(vk: int, *, down: bool) -> int:
    flags = 0 if down else KEYEVENTF_KEYUP
    if vk in _EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    result = _send(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, None))))
    if down:
        _held.add(vk)
    else:
        _held.discard(vk)
    return result


def send_keys(names: list[str]) -> tuple[list[str], str | None]:
    """Press a sequence; leading modifiers make it a combination.

    Every name is resolved *before* any injection: silently skipping an unknown
    key would turn "ctrl+s" into a bare Ctrl held down — the stuck-modifier
    fault this module's disarm exists to prevent.
    """
    codes: list[int] = []
    for name in names:
        vk = resolve_key(name)
        if vk is None:
            return [], f"unknown key name: {name!r} (see the key list in the tool description)"
        codes.append(vk)
    if not codes:
        return [], "no keys given"
    if len(codes) > 1 and all(code in _MODIFIER_VK for code in codes[:-1]):
        for code in codes[:-1]:
            _key_event(code, down=True)
        _key_event(codes[-1], down=True)
        _key_event(codes[-1], down=False)
        for code in reversed(codes[:-1]):
            _key_event(code, down=False)
    else:
        for code in codes:
            _key_event(code, down=True)
            _key_event(code, down=False)
    return [str(name) for name in names], None


def held_keys() -> list[str]:
    return sorted(f"0x{vk:02X}" for vk in _held)


def release_all_keys() -> list[str]:
    """Release every key this process may have left down. Called on every
    refusal path, on disarm, and at process exit: a host Ctrl stuck down is a
    fault the user cannot fix from inside Fungi."""
    stuck = [vk for vk in _held if vk in _MODIFIER_VK]
    for vk in stuck:
        with contextlib.suppress(Exception):
            _key_event(vk, down=False)
    _held.clear()
    return sorted(f"0x{vk:02X}" for vk in stuck)


# ── clipboard: how text gets in (and back out) ─────────────────────────────
CF_UNICODETEXT, CF_DIB, CF_DIBV5 = 13, 8, 17
_RESTORABLE_FORMATS = (CF_UNICODETEXT, CF_DIB, CF_DIBV5)
GMEM_MOVEABLE_ZEROINIT = 0x0042


def _clipboard_open() -> bool:
    for _ in range(8):
        if _u32.OpenClipboard(None):
            return True
        time.sleep(0.05)  # another process holds it for a few ms
    return False


def _clipboard_bytes(fmt: int) -> bytes | None:
    handle = _u32.GetClipboardData(fmt)
    if not handle:
        return None
    size = int(_k32.GlobalSize(ctypes.c_void_p(handle)))
    pointer = _k32.GlobalLock(ctypes.c_void_p(handle))
    if not pointer or not size:
        return None
    try:
        return ctypes.string_at(pointer, size)
    finally:
        _k32.GlobalUnlock(ctypes.c_void_p(handle))


def clipboard_text() -> str:
    if not _clipboard_open():
        return ""
    try:
        raw = _clipboard_bytes(CF_UNICODETEXT)
    finally:
        _u32.CloseClipboard()
    if not raw:
        return ""
    return raw.decode("utf-16-le", errors="replace").split("\x00")[0]


@dataclass
class ClipSnapshot:
    """What the clipboard held before a paste. Text and bitmaps are restored;
    anything else (a copied file list, say) is reported instead of guessed."""

    formats: dict[int, bytes] = field(default_factory=dict)
    other: bool = False


def snapshot_clipboard() -> ClipSnapshot:
    snap = ClipSnapshot()
    if not _clipboard_open():
        return ClipSnapshot(other=True)
    try:
        available = set()
        fmt = _u32.EnumClipboardFormats(0)
        while fmt:
            available.add(fmt)
            fmt = _u32.EnumClipboardFormats(fmt)
        for wanted in _RESTORABLE_FORMATS:
            if wanted in available:
                data = _clipboard_bytes(wanted)
                if data:
                    snap.formats[wanted] = data
        snap.other = bool(available - set(_RESTORABLE_FORMATS) - {1, 2, 3, 16, 7})
    finally:
        _u32.CloseClipboard()
    return snap


def _set_clipboard_bytes(fmt: int, data: bytes) -> bool:
    handle = _k32.GlobalAlloc(GMEM_MOVEABLE_ZEROINIT, len(data))
    if not handle:
        return False
    pointer = _k32.GlobalLock(ctypes.c_void_p(handle))
    if not pointer:
        return False
    ctypes.memmove(pointer, data, len(data))
    _k32.GlobalUnlock(ctypes.c_void_p(handle))
    return bool(_u32.SetClipboardData(fmt, ctypes.c_void_p(handle)))


def set_clipboard_text(text: str) -> bool:
    if not _clipboard_open():
        return False
    try:
        _u32.EmptyClipboard()
        return _set_clipboard_bytes(CF_UNICODETEXT, (text + "\x00").encode("utf-16-le"))
    finally:
        _u32.CloseClipboard()


def restore_clipboard(snap: ClipSnapshot) -> None:
    if not snap.formats:
        return
    if not _clipboard_open():
        return
    try:
        _u32.EmptyClipboard()
        for fmt, data in snap.formats.items():
            _set_clipboard_bytes(fmt, data)
    finally:
        _u32.CloseClipboard()


# ── the session: arming, candidates, two frames, strike counts ─────────────
@dataclass
class Session:
    """Everything the tool remembers between calls. Pixels are deliberately
    just the current and the previous frame (spec §35.4): enough to answer "did
    that change anything", never a screen recorder."""

    # No permission state lives here: the `pc_control` switch is the consent
    # (user decision 2026-09-13 — "the experimental switch means I already
    # allowed it"). What stays is bookkeeping that makes one action trustworthy:
    # which window is where, and which keys are down.
    candidates: dict[int, Target] = field(default_factory=dict)
    candidates_hwnd: int = 0
    candidates_rect: tuple[int, int, int, int] | None = None
    # What the model called a shape ("发送"), bound to the rectangle it saw it at
    # (spec §35.10). Numbers are per-listing; a rectangle survives a re-listing.
    labels: dict[str, Target] = field(default_factory=dict)
    labels_hwnd: int = 0
    labels_rect: tuple[int, int, int, int] | None = None
    frames: deque[Frame] = field(default_factory=lambda: deque(maxlen=2))
    failures: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def disarm(self) -> None:
        self.labels.clear()
        self.labels_hwnd = 0
        self.labels_rect = None
        self.frames.clear()

    def remember(self, frame: Frame) -> None:
        self.frames.append(frame)


_session = Session()


def disarm() -> None:
    """End the armed window; releases keys, drops frames, forgets candidates.
    Bound to the room's stop and the process exit, so a crash cannot leave the
    host's keyboard half-pressed.

    Nothing is announced: a tray toast pops over the very screen being driven and
    steals focus from it (user decision 2026-09-13 — spec §35.14)."""
    release_all_keys()
    _session.disarm()


atexit.register(disarm)


# ── target resolution: the model names one, the program locates it ─────────
def resolve_target(hwnd: int, args: dict, *, allow_ocr: bool = True) -> Target | str:
    """Turn `target=<n>` or `name=<text>` into a rectangle in screen space.

    Ambiguity is surfaced, never guessed: several matches come back as a list
    for the model to pick from (the prototype's "don't silently take the first"
    rule). A name with no match is `no_target`, with the a11y candidates listed.
    """
    number = args.get("target")
    name = str(args.get("name") or "").strip()
    if number is None and not name:
        return (
            "ERROR: pass target=<number from targets> or name=<visible text>. "
            "Call the targets action first to see the numbers."
        )
    pairs = _scan(hwnd)
    if number is not None:
        try:
            wanted = int(number)
        except (TypeError, ValueError):
            return f"ERROR: target must be a number, got {number!r}"
        if _session.candidates_hwnd == hwnd and wanted in _session.candidates:
            stored = _session.candidates[wanted]
            if stored.source != "a11y":
                # Its rectangle came out of the picture, not out of the tree, so
                # re-matching it against a11y elements picks a *different* control
                # (two empty-named elements matched each other on 2026-09-13 and the
                # click went to the wrong place). The picture's own frame origin
                # only holds while the window stays where it was.
                if _session.candidates_rect != window_rect(hwnd):
                    return (
                        "ERROR: the window moved or resized since that listing — run targets again "
                        f"(hwnd={hwnd}); those numbers belong to the picture they were cut from."
                    )
                return stored
            live = _match_element(pairs, stored)
            if live is not None:
                for cand, element in pairs:
                    if element is live:
                        return cand
            return (
                f"ERROR: target #{wanted} ({stored.name!r}) is not on screen any more — "
                "run targets again (the window changed since it was listed)."
            )
        return (
            f"ERROR: no candidate #{wanted} for this window. "
            "Call targets with this hwnd first; numbers do not survive a new listing."
        )
    lowered = name.casefold()

    # Match the same text the model reads in the listing: a control with no
    # accessible name is identified by its class ('Edit', 'ListBox'), and
    # matching only the empty name would make those unreachable by name.
    def key_of(cand: Target) -> str:
        return (cand.name or cand.cls).casefold()

    exact = [cand for cand, _ in pairs if key_of(cand) == lowered]
    if len(exact) == 1:
        return exact[0]  # a real control with that name outranks a label
    labelled = _find_label(hwnd, lowered)
    if isinstance(labelled, str):
        return labelled
    if isinstance(labelled, Target):
        return labelled
    loose = [cand for cand, _ in pairs if lowered in key_of(cand)]
    hits = exact or loose
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        listed = "\n".join(cand.label for cand in hits[:12])
        return f"ERROR: {len(hits)} controls match {name!r} — pick a number with target=\n{listed}"
    if allow_ocr:
        frame = grab_window(hwnd)
        if frame is not None:
            start = len(pairs)
            for cand in _ocr_targets(frame, start=start):
                if lowered in cand.name.casefold():
                    return cand
            if importlib.util.find_spec("rapidocr_onnxruntime") is None:
                return (
                    f"ERROR: no_target: nothing in this window is named {name!r}, and the OCR "
                    "fallback is not installed (pip install rapidocr-onnxruntime) — for a "
                    "canvas-drawn UI there is no a11y to fall back on."
                )
    listed = "\n".join(cand.label for cand, _ in pairs[:20])
    known = _session.labels if _session.labels_hwnd == hwnd else {}
    tail = f" Labels you set here: {', '.join(sorted(known))}." if known else ""
    return f"ERROR: no_target: nothing named {name!r} in this window.{tail} Candidates:\n{listed}"


# ── read-only actions ─────────────────────────────────────────────────────
def _windows_report(limit: int = 20, include_hidden: bool = False) -> str:
    windows = list_windows(include_hidden=include_hidden)
    if not windows:
        return "WINDOWS: none visible"
    front = foreground_hwnd()
    windowed = [win for win in windows if win.state != "untitled"]
    tray = [win for win in windows if win.cls == "Shell_TrayWnd"]
    if include_hidden:
        # On-screen windows first, then the ones hiding in the tray; biggest first
        # inside each group — the meaningful ones then survive the row cap.
        windows = sorted(
            windows,
            key=lambda win: (
                win.state != "normal",
                -((win.rect[2] - win.rect[0]) * (win.rect[3] - win.rect[1])),
            ),
        )
    lines = [
        f"WINDOWS ({len(windowed)} application windows"
        + (
            f", {len(windows)} rows incl. system/tray (helper windows under 40px skipped)"
            if include_hidden
            else ""
        )
        + "; hwnd is the identity, titles are not)"
    ]
    printed = windows[:limit]
    for win in printed:
        mark = " ← foreground" if win.hwnd == front else ""
        note = "" if win.state == "normal" else f" [{win.state}]"
        # An untitled row is a system surface: its class is the only identity the
        # model can reason about ('Shell_TrayWnd' == the taskbar).
        label = repr(win.title) if win.title else f"<no title, cls={win.cls}>"
        lines.append(
            f"  hwnd={win.hwnd} 0x{win.hwnd:X} {win.size} {win.proc or win.cls} {label}{note}{mark}"
        )
    # The hints name a row "above": only say that about a row that is actually in the
    # listing, because the row cap can cut the taskbar or the desktop off (both sort
    # last — the desktop is at the bottom of the z-order, the taskbar row is untitled).
    shown = {win.hwnd for win in printed}
    if tray and any(win.hwnd in shown for win in tray):
        lines.append(
            "  the taskbar/notification area is the Shell_TrayWnd row above: "
            "targets(hwnd=<it>) lists the tray icons, and a tray-only app usually also has its "
            "own [hidden] or [minimized] row here — targets(hwnd=<that one>) restores it."
        )
    desktop = _desktop_surface()
    if desktop in shown:
        lines.append(
            f"  the desktop is the 0x{desktop:X} row above: targets(hwnd={desktop}) lists the "
            "desktop icons (one row per icon), and double_click(hwnd=<it>, name=<icon text>) "
            "opens that one — this is how an application with no window, no taskbar button and no "
            "tray icon is started. Icons are covered by whatever window is on top, so that click "
            "is refused (and told why) until the desktop itself is showing."
        )
    if not include_hidden:
        lines.append(
            '  (hidden/tray windows are left out; call windows again with include="all" to see '
            "them plus the notification area)"
        )
    return "\n".join(lines)


def _action_windows(args: dict) -> str:
    include = str(args.get("include") or "visible").strip().lower()
    return _windows_report(limit=40 if include == "all" else 20, include_hidden=include == "all")


def _frame_for(hwnd: int | None) -> tuple[Frame | None, str]:
    if hwnd is None:
        return grab_screen(), "SCREEN"
    if not _u32.IsWindow(hwnd):
        return None, f"ERROR: no such window: 0x{hwnd:X}"
    frame = grab_window(hwnd)
    if frame is None:
        return None, f"ERROR: could not capture window 0x{hwnd:X}"
    return frame, "WINDOW"


def _action_shot(args: dict) -> str | ImageRead:
    hwnd = args.get("hwnd")
    hwnd = int(hwnd) if hwnd not in (None, "") else None
    if hwnd is not None and _u32.IsWindow(hwnd):
        was = window_state(hwnd)
        if was != "normal":
            return (
                f"ERROR: window 0x{hwnd:X} {_window_text(hwnd)!r} is {was} — that picture would "
                f"be its icon slot, not its UI. Use action=restore (hwnd={hwnd}) first; a "
                "whole-screen shot (no hwnd) still shows everything that is on screen."
            )
        problem = capture_problem(hwnd)
        if problem:
            return f"ERROR: {problem}"
    frame, kind = _frame_for(hwnd)
    if frame is None:
        return kind
    _session.remember(frame)
    title = ""
    if hwnd is not None:
        title = f" {_window_text(hwnd)!r}"
    summary = (
        f"{kind} {frame.image.width}x{frame.image.height} at {frame.origin}{title} "
        f"({'physical pixels; this tool sets DPI awareness' if hwnd is None else f'hwnd=0x{hwnd:X}'})"
    )
    if hwnd is None:
        summary += "\n" + _windows_report(limit=12)
        summary += (
            "\nFor small text, shot a single window: hwnd=<the one you care about>, or act "
            "directly with targets(hwnd=...)."
        )
    return _attach(summary, frame)


def _action_targets(args: dict) -> str | ImageRead:
    hwnd = args.get("hwnd")
    if hwnd in (None, ""):
        return "ERROR: targets needs hwnd=<window id from windows>"
    hwnd = int(hwnd)
    if not _u32.IsWindow(hwnd):
        return f"ERROR: no such window: 0x{hwnd:X}"
    was = window_state(hwnd)
    if was != "normal":
        return (
            f"ERROR: window 0x{hwnd:X} {_window_text(hwnd)!r} is {was} — its controls keep "
            "off-screen rectangles, so a listing here would be fiction. Use action=restore "
            f"(hwnd={hwnd}) to bring it back on screen first: that is also how an app sitting in "
            "the notification area comes back."
        )
    problem = capture_problem(hwnd)
    if problem:
        return f"ERROR: {problem}"
    pairs = _scan(hwnd)
    frame = grab_window(hwnd)
    if frame is None:
        return (
            f"ERROR: could not capture window 0x{hwnd:X} — its renderer keeps the pixels to "
            "itself while it sits behind another window. Bring it to the front with "
            f"action=restore (hwnd={hwnd}) and read it again."
        )
    _session.remember(frame)
    # Drop what cannot be addressed: no name and no class means the model has
    # nothing to say about it (Chromium/Electron's anonymous shells), and a
    # pattern-less whole-client-area box is only a temptation to click blind.
    targets = [cand for cand, _ in pairs if cand.name or cand.cls]
    note = ""
    # A self-drawn UI (WeChat 4.x renders every control into one surface element)
    # lists one candidate with no pattern: nothing to click by rectangle. Then the
    # picture takes over in the order spec §35.1 measured — a11y, then OCR text,
    # then the shapes the *program* cuts out of the pixels for the model to pick by
    # number. The binarised image itself never reaches the model (21-296px of error
    # and 3x the latency, measured).
    # A window whose only clickable a11y elements are its own window buttons (the
    # title bar) draws its controls itself: that is when the picture takes over.
    # "Clickable" needs a *name or class* to be addressable at all: Chromium/Electron
    # expose seven identical unnamed classless ScrollItem shells over the whole
    # client area (QQ measured 2026-09-13), which counted as actionable and so
    # silently switched the OCR and shape tiers off — the model got seven targets
    # it could neither name nor distinguish and no text at all.
    if not any(
        cand.patterns and (cand.name or cand.cls) and inside_client(hwnd, cand) for cand in targets
    ):
        found_a11y = targets
        ocr = _ocr_targets(frame, start=len(found_a11y))
        shapes = _visual_targets(frame, start=len(found_a11y) + len(ocr))
        # A shape sitting inside a text box is that box's own ink: keep the text
        # (addressable by name), drop the duplicate blob.
        shapes = [
            shape for shape in shapes if not any(_overlaps(shape.rect, text.rect) for text in ocr)
        ]
        # The window's own chrome is already a11y's job: what the tiers add is the
        # drawing area. Keeping the title bar here only made noise.
        ocr = [cand for cand in ocr if inside_client(hwnd, cand)]
        shapes = [cand for cand in shapes if inside_client(hwnd, cand)]
        targets = _renumber([*found_a11y, *ocr, *shapes])
        note = _candidate_note(found_a11y, ocr, shapes)
    if _session.labels_hwnd == hwnd and _session.labels_rect == window_rect(hwnd):
        for bound_name, bound in _session.labels.items():
            for cand in targets:
                if cand.rect == bound.rect:
                    cand.name = bound_name  # the semantic name the model gave it
    with _session.lock:
        _session.candidates = {cand.n: cand for cand in targets}
        _session.candidates_hwnd = hwnd
        _session.candidates_rect = window_rect(hwnd)
    title = _window_text(hwnd)
    head = f"TARGETS in hwnd=0x{hwnd:X} {title!r}{note} — pick one by number"
    listing = "\n".join(cand.label for cand in targets) or "  (none)"
    summary = f"{head}\n{listing}"
    if not targets:
        return summary
    marked = _mark_targets(frame, targets)
    buf = io.BytesIO()
    marked.save(buf, "PNG")
    url, mime, dims = image_data_url(".png", buf.getvalue())
    if url is None:
        return summary
    return ImageRead(f"{summary}\n[numbered frame attached: {dims}, {mime}]", [url])


def covering_window(point: tuple[int, int]) -> int:
    """The top-level window a click at this point would actually reach; 0 if none.

    The tool has two decisions that both rest on this one question — "may I click this
    rectangle" and "is that desktop icon still reachable" — and a rectangle existing in
    the a11y tree answers neither: measured 2026-09-14, the desktop's 微信 icon kept its
    coordinates while a terminal covered it, so a click there would have gone to the
    terminal.
    """
    at = int(_u32.WindowFromPoint(wt.POINT(point[0], point[1])) or 0)
    return int(_u32.GetAncestor(at, GA_ROOT) or 0) if at else 0


def target_problem(hwnd: int, target: Target) -> str | None:
    """Why this target must not be clicked — all three cases are measured ones.

    * Outside the window the caller asked to act on: the click lands on whatever
      is underneath it. A mis-resolved target put a real click on a desktop file
      on 2026-09-13.
    * Something else covers the point: the click would go to *that* window. Measured
      2026-09-14: with a terminal in front, the desktop's own 微信 icon still resolves
      (its rectangle is right there in the a11y tree) while `WindowFromPoint` at its
      centre returns the terminal — so "the rectangle exists" is not permission to
      click. The caller raises the window first, so for a normal window this only
      refuses targets that are genuinely covered.
    * A rectangle that is the whole window surface with no pattern at all: a
      self-drawn UI exposes exactly one such element (WeChat 4.x's
      MMUIRenderSubWindowHW), and its centre is not a control — it is a guess.
    """
    window = window_rect(hwnd)
    if window is None:
        return f"window 0x{hwnd:X} has no rectangle any more"
    left, top, right, bottom = window
    centre_x, centre_y = target.center
    if not (left <= centre_x <= right and top <= centre_y <= bottom):
        return (
            f"{target.label} sits outside window 0x{hwnd:X} "
            f"({left},{top},{right},{bottom}) — clicking there would hit whatever is underneath"
        )
    at = covering_window((centre_x, centre_y))
    if not at:
        return (
            f"{target.label} is at ({centre_x},{centre_y}), where no window answers at all — "
            "there is nothing to click there"
        )
    if at != hwnd:
        cover = _window_text(at) or _class_name(at) or f"0x{at:X}"
        return (
            f"{target.label} is covered: ({centre_x},{centre_y}) belongs to {cover!r} "
            f"(0x{at:X}), so the click would go there instead of to 0x{hwnd:X}. Move that "
            "window out of the way first (or click what you actually want on it)"
        )
    area = (target.rect[2] - target.rect[0]) * (target.rect[3] - target.rect[1])
    window_area = (right - left) * (bottom - top)
    if not target.patterns and window_area and area >= 0.75 * window_area:
        return (
            f"{target.label} is the whole window surface, not a control: this UI draws itself, so "
            "there is no rectangle to trust. Name the thing you want by its visible text instead "
            "(name=<text>; OCR reads the picture), or drive it with keys"
        )
    return None


LABEL_MAX = 24


def _find_label(hwnd: int, lowered: str) -> Target | str | None:
    """A shape the model named earlier, or why it cannot be used any more.

    Labels are bound to a rectangle rather than to a number: numbers are only
    valid inside one listing, while "the thing I called 发送" stays the thing at
    that rectangle for as long as the window does not move.
    """
    if not _session.labels or _session.labels_hwnd != hwnd:
        return None
    exact = [bound for name, bound in _session.labels.items() if name.casefold() == lowered]
    loose = [bound for name, bound in _session.labels.items() if lowered in name.casefold()]
    hits = exact or loose
    if not hits:
        return None
    if _session.labels_rect != window_rect(hwnd):
        return (
            "ERROR: the window moved or resized since you labelled that — the label's rectangle "
            f"belongs to the old picture. Run targets again (hwnd={hwnd}) and label it once more."
        )
    return hits[0]


def _renumber(targets: list[Target]) -> list[Target]:
    """Numbers are how the model refers to things: after dropping duplicates they
    must still run 1..N, or it will copy a number that is not in the listing."""
    for index, target in enumerate(targets, 1):
        target.n = index
    return targets


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int], iou: float = 0.3) -> bool:
    """IoU at or above `iou`, or one box inside the other — the prototype's merge
    rule (2026-09-13) for dropping a blob that is only the ink of a text box."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return False
    inter = (x1 - x0) * (y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(1, area_a + area_b - inter) >= iou or inter >= 0.9 * min(area_a, area_b)


def _candidate_note(found_a11y: list[Target], ocr: list[Target], shapes: list[Target]) -> str:
    """Say where the candidates came from: a listing that hides its origin invites
    the model to treat a cut-out shape as if it were a named control."""
    parts = ["a11y has nothing clickable here" if found_a11y else "no a11y at all"]
    if ocr:
        parts.append(f"{len(ocr)} text boxes read off the picture by OCR (found by name)")
    elif importlib.util.find_spec("rapidocr_onnxruntime") is None:
        parts.append("no OCR installed (pip install rapidocr-onnxruntime)")
    if shapes:
        parts.append(
            f"{len(shapes)} shapes the program cut out of the picture — no text, so pick them by "
            "number"
        )
    return " (" + "; ".join(parts) + ")"


def _action_label(args: dict) -> str:
    """Remember what the model calls a shape it picked off the picture (spec §35.10).

    Choosing *which* shape is the send button is the model's job — it is the one
    looking at the picture; making that choice stick is the program's. Without this
    the semantics lived only in the conversation, and the next listing handed back
    "(no text)" again.
    """
    hwnd = args.get("hwnd")
    if hwnd in (None, ""):
        return "ERROR: label needs hwnd=<the window the listing came from>"
    hwnd = int(hwnd)
    if not _u32.IsWindow(hwnd):
        return f"ERROR: no such window: 0x{hwnd:X}"
    text = " ".join(str(args.get("label") or "").split())[:LABEL_MAX]
    if not text:
        return "ERROR: label needs label=<the name to remember, e.g. 发送>"
    if args.get("target") is None:
        return "ERROR: label needs target=<the candidate number to name>"
    resolved = resolve_target(hwnd, {"target": args["target"]}, allow_ocr=False)
    if isinstance(resolved, str):
        return resolved
    with _session.lock:
        _session.labels[text] = Target(
            0, text, resolved.cls, resolved.rect, resolved.patterns, resolved.source
        )
        _session.labels_hwnd = hwnd
        _session.labels_rect = window_rect(hwnd)
        known = ", ".join(sorted(_session.labels))
    return (
        f"LABELLED {text!r} -> {resolved.label}\n"
        f"  click/type/scroll with name={text!r} find it while 0x{hwnd:X} stays where it is; "
        f"labels here: {known}"
    )


# ── input actions: armed, verified, and escalating ─────────────────────────
def _strike(key: str) -> int:
    with _session.lock:
        _session.failures[key] = _session.failures.get(key, 0) + 1
        return _session.failures[key]


def _clear_strikes(key: str) -> None:
    with _session.lock:
        _session.failures.pop(key, None)


def _escalate(sink, key: str, what: str, detail: str, should_abort, on_answer, call_id) -> str:
    status, value = blocking_ask(
        sink,
        [
            {
                "question": (
                    f"桌面动作连续 {FAILURE_LIMIT} 次没看到效果：{what}。\n{detail}\n"
                    "要我怎么继续？（例如：把窗口打开到前台 / 换一个控件 / 放弃这步）"
                ),
                "allow_custom": True,
            }
        ],
        should_abort=should_abort,
        on_answer=on_answer,
        call_id=call_id,
    )
    _clear_strikes(key)
    if status == "answered":
        return f"ESCALATED: asked the user after {FAILURE_LIMIT} failed attempts — USER: {value}"
    return (
        f"ESCALATED: {FAILURE_LIMIT} failed attempts at {what} and the user did not answer "
        f"({status}); stop retrying this target."
    )


def _guarded_input(hwnd: int) -> tuple[bool, str]:
    """The part of every input action that is not the action itself.

    Nothing here asks the user for permission: `config.json`'s `pc_control` switch
    *is* the consent (user decision 2026-09-13 — "the experimental switch means I
    already allowed it"). Nothing is announced either — a toast lands on top of the
    screen being driven (spec §35.14). What remains is what the tool owes the
    machine: it wakes a window that is minimized or hiding in the notification area
    before anything tries to measure or click it.
    """
    # A minimized window's controls sit at their icon coordinates and a hidden one
    # has no on-screen geometry at all: wake it before anything measures or clicks,
    # through the path that actually wakes its application (see wake_window).
    was = window_state(hwnd)
    wake_window(hwnd)
    if window_state(hwnd) != "normal":
        return False, (
            f"ERROR: 0x{hwnd:X} is still {window_state(hwnd)} after a wake attempt — an "
            "elevated window (or one on another virtual desktop) cannot be driven from here."
        )
    return True, (f"  restored from {was}" if was != "normal" else "")


def _action_click(args: dict, sink, should_abort, on_answer, call_id) -> str | ImageRead:
    return _click_once(args, sink, should_abort, on_answer, call_id, clicks=1)


def _action_double_click(args: dict, sink, should_abort, on_answer, call_id) -> str | ImageRead:
    """Open something with the gesture a person would use.

    Back in the tool face on the user's call (2026-09-13): `shell_open` only covers
    what we already know the path of, and an icon drawn on the desktop, inside a
    list, or in an app that lives in neither the taskbar nor the tray can only be
    opened by being double-clicked. It is also the one "open" that starts a *fresh*
    process — the single wake path that never yields the "visible but asleep" window
    (spec §35.13).
    """
    return _click_once(args, sink, should_abort, on_answer, call_id, clicks=2)


def _click_once(args, sink, should_abort, on_answer, call_id, *, clicks: int) -> str | ImageRead:
    hwnd = int(args["hwnd"])
    gesture = "double_click" if clicks == 2 else "click"
    # Resolve before asking for permission: a name that does not exist should come
    # back as no_target, not as a card the user answers for nothing. A window that
    # is not on screen cannot be measured first, so that case waits for the wake-up.
    # Fail fast only when the pixels are trustworthy: an OCR fallback for a
    # window that is minimized, hidden or covered cannot read anything anyway.
    ready = window_state(hwnd) == "normal" and capture_problem(hwnd) is None
    if ready:
        first = resolve_target(hwnd, args)
        if isinstance(first, str):
            return first
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    # Raise it before measuring: a click has to land in the window the caller
    # named, and for an unaware process that is also what makes its pixels
    # readable at all.
    raised = True if _is_shell_surface(hwnd) else set_foreground(hwnd)
    target = resolve_target(hwnd, args) if not ready else first
    if isinstance(target, str):
        return target
    problem = target_problem(hwnd, target)
    if problem:
        return f"ERROR: {problem}"
    known_windows = {w.hwnd for w in list_windows()} if clicks == 2 else set()
    before = grab_window(hwnd)
    injected = click_at(*target.center, button=str(args.get("button") or "left"), clicks=clicks)
    time.sleep(SETTLE_S)
    # A gesture meant to open something: what it opens is a *new* window, and that
    # may be the only visible effect — the window the double-click landed in is
    # allowed to look unchanged (spec §35.13).
    launched: list = []
    if clicks == 2:
        deadline = time.monotonic() + LAUNCH_WAIT_S
        while True:
            launched = [w for w in list_windows() if w.hwnd not in known_windows]
            if launched or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    after = grab_window(hwnd)
    if after is not None:
        _session.remember(after)
    focused = _focused()
    changed = _diff_ratio(before, after) if before is not None and after is not None else 0.0
    focus_hit = bool(focused) and target.name and target.name in focused.get("name", "")
    verified = injected and (changed > DIFF_THRESHOLD or focus_hit or bool(launched))
    key = f"{gesture}:{hwnd}:{target.label}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = f"目标 {target.label}；注入={injected}；画面变化={changed:.3%}；前台={raised}"
        return _escalate(
            sink, key, f"{gesture} {target.label}", detail, should_abort, on_answer, call_id
        )
    if focus_hit:
        effect = "focus matched"
    elif launched:
        effect = f"opened {_window_text(launched[0].hwnd)!r}"
    else:
        effect = f"frame changed {changed:.2%}"
    lines = [
        f"{gesture.upper()} {target.label} at {target.center} in hwnd=0x{hwnd:X} "
        f"({'injected' if injected else 'INJECTION FAILED'}){note}",
        f"  verify: {effect} "
        f"→ {'verified' if verified else 'unverified (no visible effect)'}"
        f"{'' if raised else ' · could not raise the window to the foreground!'}",
        "  control: on (pc_control switch; no prompt for this action)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after) if after is not None else summary


def _action_type(args: dict, sink, should_abort, on_answer, call_id) -> str | ImageRead:
    hwnd = int(args["hwnd"])
    text = str(args.get("text") or "")
    if not text:
        return "ERROR: type needs text=<the string to paste>"
    wants = args.get("target") is not None or bool(args.get("name"))
    awake = window_state(hwnd) == "normal"
    target: Target | None = None
    if wants and awake:
        resolved = resolve_target(hwnd, args)
        if isinstance(resolved, str):
            return resolved
        target = resolved
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    set_foreground(hwnd)
    if wants and not awake:  # raised after the arm: measure the controls only now
        resolved = resolve_target(hwnd, args)
        if isinstance(resolved, str):
            return resolved
        target = resolved
    if target is not None:
        _focus_target(hwnd, target)
    _before_value, where = _read_back_settled(hwnd, target)
    before_frame = grab_window(hwnd)
    snapshot = snapshot_clipboard()
    pasted = set_clipboard_text(text)
    if not pasted:
        return "ERROR: could not open the clipboard (another process is holding it)"
    _names, error = send_keys(["ctrl", "v"])
    if error:
        return f"ERROR: {error}"
    time.sleep(SETTLE_S)
    after_value, _where = _read_back_settled(hwnd, target)
    after_frame = grab_window(hwnd)
    if after_frame is not None:
        _session.remember(after_frame)
    restore_clipboard(snapshot)
    changed = _diff_ratio(before_frame, after_frame) if before_frame and after_frame else 0.0
    key = f"type:{hwnd}:{target.label if target else where}"
    verified = text in after_value or after_value.strip() == text.strip()
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = f"回读到 {after_value!r}；画面变化={changed:.3%}"
        return _escalate(
            sink, key, f"type into {where or hex(hwnd)}", detail, should_abort, on_answer, call_id
        )
    if verified:
        verdict = f"verified: read back {after_value!r} from {where or 'the focused control'}"
    elif changed > DIFF_THRESHOLD:
        verdict = f"unverified: the picture changed ({changed:.2%}) but this control exposes no readable value"
    else:
        verdict = "unverified: nothing changed on screen"
    clip = (
        ""
        if not snapshot.other
        else " · the clipboard also held a non-text format, which is not restored"
    )
    lines = [
        f"TYPE {len(text)} chars into hwnd=0x{hwnd:X}"
        + (f" target {target.label}" if target else " (current focus)")
        + note,
        f"  verify: {verdict}",
        f"  clipboard restored: {'yes' if snapshot.formats else 'nothing to restore'}{clip}",
        "  control: on (pc_control switch; no prompt for this action)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary


def _action_key(args: dict, sink, should_abort, on_answer, call_id) -> str | ImageRead:
    hwnd = int(args["hwnd"])
    names = [str(k) for k in args.get("keys") or []]
    if not names:
        return 'ERROR: key needs keys=[...] (e.g. ["ctrl","s"] or ["enter"])'
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    set_foreground(hwnd)
    before_focus = _focused()
    before_value, _where = _read_back(hwnd, None)
    before_frame = grab_window(hwnd)
    sent, error = send_keys(names)
    if error:
        return f"ERROR: {error}"
    time.sleep(SETTLE_S)
    after_focus = _focused()
    after_value, _where = _read_back(hwnd, None)
    after_frame = grab_window(hwnd)
    if after_frame is not None:
        _session.remember(after_frame)
    changed = _diff_ratio(before_frame, after_frame) if before_frame and after_frame else 0.0
    focus_moved = bool(before_focus) and bool(after_focus) and before_focus != after_focus
    value_changed = after_value != before_value
    verified = focus_moved or value_changed or changed > DIFF_THRESHOLD
    key = f"key:{hwnd}:{'+'.join(names)}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = f"焦点 {before_focus} → {after_focus}；画面变化={changed:.3%}"
        return _escalate(
            sink, key, f"key {'+'.join(names)}", detail, should_abort, on_answer, call_id
        )
    signal = (
        "focus moved"
        if focus_moved
        else ("control value changed" if value_changed else f"frame changed {changed:.2%}")
    )
    lines = [
        f"KEY {'+'.join(sent)} into hwnd=0x{hwnd:X}{note}",
        f"  verify: {signal} → {'verified' if verified else 'unverified (no visible effect)'}",
        f"  held keys after: {', '.join(held_keys()) or 'none'}",
        "  control: on (pc_control switch; no prompt for this action)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary


def _focus_target(hwnd: int, target: Target) -> bool:
    """a11y SetFocus: moves the input focus without touching the mouse or the
    z-order (the prototype's reason to prefer it over clicking a field)."""
    pairs = _scan(hwnd)
    element = _match_element(pairs, target)
    if element is None:
        return False
    try:
        element.SetFocus()
        return True
    except Exception:
        return False


def _action_scroll(args: dict, sink, should_abort, on_answer, call_id) -> str | ImageRead:
    hwnd = int(args["hwnd"])
    ready = window_state(hwnd) == "normal" and capture_problem(hwnd) is None
    if ready:
        first = resolve_target(hwnd, args)
        if isinstance(first, str):
            return first
    ok, note = _guarded_input(hwnd)
    if not ok:
        return note
    set_foreground(hwnd)
    target = resolve_target(hwnd, args) if not ready else first
    if isinstance(target, str):
        return target
    pairs = _scan(hwnd)
    element = _match_element(pairs, target)
    if element is None:
        return f"ERROR: {target.label} is no longer in the tree — run targets again"
    before_frame = grab_window(hwnd)
    _automation, uia_client = _uia()
    scrolled = False
    try:
        pattern = element.GetCurrentPattern(uia_client.UIA_ScrollItemPatternId)
        if pattern:
            pattern.QueryInterface(uia_client.IUIAutomationScrollItemPattern).ScrollIntoView()
            scrolled = True
    except Exception:
        scrolled = False
    time.sleep(SETTLE_S)
    rect = window_rect(hwnd)
    after_frame = grab_window(hwnd)
    if after_frame is not None:
        _session.remember(after_frame)
    changed = _diff_ratio(before_frame, after_frame) if before_frame and after_frame else 0.0
    verified = scrolled and changed > DIFF_THRESHOLD
    key = f"scroll:{hwnd}:{target.label}"
    if verified:
        _clear_strikes(key)
    elif _strike(key) >= FAILURE_LIMIT:
        detail = (
            f"ScrollItemPattern={'ok' if scrolled else 'unavailable'}；画面变化={changed:.3%}；"
            f"窗口 rect={rect}"
        )
        return _escalate(
            sink, key, f"scroll to {target.label}", detail, should_abort, on_answer, call_id
        )
    lines = [
        f"SCROLL to {target.label} in hwnd=0x{hwnd:X}{note}",
        f"  verify: ScrollItemPattern={'used' if scrolled else 'not available'} · "
        f"frame changed {changed:.2%} → {'verified' if verified else 'unverified'}",
        "  control: on (pc_control switch; no prompt for this action)",
    ]
    summary = "\n".join(lines)
    return _attach(summary, after_frame) if after_frame is not None else summary


TRAY_OVERFLOW_HINTS = ("显示隐藏的图标", "Show hidden icons", "显示隐藏的图标 ")
TRAY_FLYOUT_CLASSES = ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow")
SHELL_WAKE_S = (
    4.0  # the app's own wake path is asynchronous: a heavy tray app (WeChat) needs seconds
)
TRAY_FLYOUT_S = 2.0  # the overflow flyout: 0.12s to open, ~0.3s to fill (measured)
DESKTOP_CLASSES = ("Progman", "WorkerW")  # whichever of them hosts SHELLDLL_DefView
TRAY_BUTTON_PREFIX = "SystemTray."  # the notification strip, vs Taskbar.TaskListButton*
PINNED_HINTS = ("已固定", "Pinned")  # a pinned button is shown while the app is *not* running
GA_ROOT = 2


@dataclass(frozen=True)
class Entry:
    """A window's own door on screen: the icon or button a person would click.

    "触手可及" (at hand) is this tool's name for it (spec §35.15) — the application's
    entry point is on screen *now*, on the desktop, in the taskbar or in the tray, so
    opening the window can run the application's own path instead of moving the OS's
    idea of the window. Measured 2026-09-13: the OS path produced a window that was
    visible, foreground and screenshot-able, and swallowed every input (§35.12).

    `raise_first` is set for the tray strip only: that raise is what the pre-existing
    taskbar path did and it is measured working. A flyout is opened by us and is
    already in front, and the desktop is left alone — raising it is not what a person
    does before double-clicking an icon."""

    surface: int  # the window the entry is drawn in
    target: Target  # the rectangle to click
    where: str  # 桌面图标 | 任务栏按钮 | 托盘图标
    clicks: int  # a desktop icon opens on the second click
    label: str  # the row's own name, for the report
    raise_first: bool = False  # raise the surface before clicking (the tray strip only)


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def _norm(text: str) -> str:
    """Comparable form of a title or a row name: casefolded, zero-width characters
    removed.

    Measured on this box 2026-09-13: Edge's title is 'Fungi - 个人 - Microsoft\\u200b Edge'
    — a zero-width space between the two words — so the taskbar button
    'Microsoft Edge - 1 个运行窗口' shares no plain substring with it, and the window
    looked like it had no entry at all.
    """
    return text.translate(_ZERO_WIDTH).casefold()


def _app_tokens(win: Win) -> list[str]:
    """What a shell row for this application could be called.

    (the window title, the process stem). The taskbar names a button after the
    app's display name, not after its exe — measured on this box: '智能终端 - 1
    个运行窗口' for WindowsTerminal.exe — so the title is the better token, and the
    tray tooltip happens to carry it too (' QQ: 3754901636…').
    """
    tokens = [_norm(win.title).strip(), _norm(Path(win.proc).stem) if win.proc else ""]
    return [token for token in tokens if len(token) >= 2]


def _row_app_name(name: str) -> str:
    """A taskbar row's application name: '文件资源管理器 - 1 个运行窗口' → '文件资源管理器'.

    Windows 11 names a taskbar button after the app's display name and appends the
    window count; a desktop icon and a tray tooltip carry the bare name.
    """
    head, sep, tail = name.partition(" - ")
    if sep and ("运行窗口" in tail or tail.strip().isdigit()):
        return head.strip()
    return name.strip()


def _mentions(haystack: str, needle: str) -> bool:
    """Is `needle` a whole word of `haystack`?

    Whole words only, because the names are short: a desktop icon called 'OS'
    matched mid-word in 'Microsoft Edge' and in 'Task Host Window' — entries for
    applications that have nothing to do with it (measured 2026-09-13).
    """
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _shell_row(rows: list[Target], win: Win) -> Target | None:
    """The row in a shell surface that belongs to `win`.

    Both directions are needed, because a row and a window name the same application
    differently (measured 2026-09-13):

    * the row **contains** the window's title or process stem — 'QQ' on the desktop,
      'QQ: 3754901636' in the tray, 'Microsoft Edge - 1 个运行窗口' for a title of
      'Fungi - 个人 - Microsoft\u200b Edge';
    * the row's application name is **contained in** the window's title — the Explorer
      case that the first direction alone never matched: the button says
      '文件资源管理器 - 1 个运行窗口' while the window says 'Fungi - 文件资源管理器'.

    The second direction is only trusted where a row's name really is an application's
    name — a taskbar button or a desktop icon. A tray tooltip is free-form text chosen
    by the app, and measured on this box it can be a bare word that also appears in a
    foreign title: the tray icon 'Fungi' would otherwise claim the window titled
    'Fungi - 个人 - Microsoft Edge'.
    """
    names = [(_norm(cand.name), cand) for cand in rows]
    for token in _app_tokens(win):
        for name, cand in names:
            if _mentions(name, token):
                return cand
    title = _norm(win.title).strip()
    if title:
        for _, cand in names:
            if cand.cls.startswith(TRAY_BUTTON_PREFIX):
                continue
            app = _norm(_row_app_name(cand.name))
            if len(app) >= 2 and _mentions(title, app):
                return cand
    return None


def _entry_sized(cand: Target, surface: int) -> bool:
    """Is this row an icon/button, rather than the container it is drawn in?

    Both surfaces expose their own container as a row covering everything — the
    desktop's '桌面' SysListView32 is 2240x1400, the taskbar's frame is the whole bar —
    and clicking a container opens nothing (measured 2026-09-13).
    """
    box = window_rect(surface)
    area = (cand.rect[2] - cand.rect[0]) * (cand.rect[3] - cand.rect[1])
    if box is None:
        return True
    whole = (box[2] - box[0]) * (box[3] - box[1])
    return not whole or area * 4 < whole


def _surface_rows(surface: int) -> list[Target]:
    """The rows of a shell surface that could be an entry.

    Containers are out (see `_entry_sized`), and so is a pinned taskbar button:
    Windows shows that form only while the application is *not* running, so it can
    never be the way to a window we are already holding.
    """
    return [
        cand
        for cand, _ in _scan(surface, limit=80)
        if _entry_sized(cand, surface) and not any(hint in cand.name for hint in PINNED_HINTS)
    ]


def _desktop_surface() -> int:
    """The window the desktop icons are drawn in, or 0.

    Explorer draws them into a `SysListView32` under a `SHELLDLL_DefView`, hosted by
    `Progman` — or by a `WorkerW` when something else owns the wallpaper. Measured on
    this box 2026-09-13: Progman 0x10148 → SHELLDLL_DefView → SysListView32, whose rows
    are one per icon ('QQ', '微信', '学习', …) with the container row '桌面' on top.
    """
    for win in list_windows(include_hidden=True):
        if win.cls not in DESKTOP_CLASSES:
            continue
        if int(_u32.FindWindowExW(win.hwnd, 0, "SHELLDLL_DefView", None) or 0):
            return win.hwnd
    return 0


def _desktop_reachable(point: tuple[int, int]) -> bool:
    """Is the desktop icon still the thing at this point?

    Anything covering the desktop covers its icons with it, and a blind double-click
    would land on that window — the wrong-click class this tool refuses everywhere
    else. Measured 2026-09-14: with a browser maximized, `WindowFromPoint` at a desktop
    icon returned the browser's render host; with the desktop showing, it returned the
    desktop's own SysListView32 (root: Progman). Raising the desktop does not help:
    `set_foreground(Progman)` makes 'Program Manager' the foreground window and leaves
    the covering window exactly where it was.
    """
    desktop = _desktop_surface()
    return bool(desktop) and covering_window(point) == desktop


def _entry(surface: int, found: Target, where: str, clicks: int, *, raise_first=False) -> Entry:
    """One door on screen. The label is the row's own first line, trimmed — the tray
    writes its tooltip with a leading space (' QQ: 3754901636…')."""
    return Entry(surface, found, where, clicks, found.name.splitlines()[0].strip(), raise_first)


def _tray_flyout() -> Win | None:
    """The overflow flyout *while it is open*, or None.

    Measured on this box 2026-09-13: the island window is not created on demand — it
    already exists, hidden, before the arrow is ever clicked. So "it exists" says
    nothing; its state does ('hidden' → 'normal' 0.12s after the arrow click), and its
    rows appear a moment later still (12 icons read at 0.30s).
    """
    return next(
        (
            w
            for w in list_windows(include_hidden=True)
            if w.cls in TRAY_FLYOUT_CLASSES and w.state == "normal"
        ),
        None,
    )


def _close_tray_flyout() -> None:
    """Put the overflow flyout away again — but only when it is open: an Escape sent
    into a hidden flyout goes to whatever window has the focus instead."""
    if _tray_flyout() is not None:
        send_keys(["escape"])


def _tray_overflow_entry(win: Win, tray_rows: list[Target]) -> Entry | None:
    """Look behind the overflow chevron: that is where a tray-only app's icon lives.

    Opens the flyout, keeps looking until it is open *and* populated (both are later
    than the click), and closes it again when the application is not in there. Left
    open when it is — the caller clicks the icon it returns.
    """
    chevron = next(
        (c for c in tray_rows if any(h in c.name for h in TRAY_OVERFLOW_HINTS)), None
    )
    if chevron is None:
        return None
    # The arrow is a *toggle*, measured the hard way: an earlier attempt that left the
    # flyout open made the next arrow click close it, and the search then found nothing.
    # So: only click it when the flyout is actually closed.
    opened = _tray_flyout() is None
    if opened:
        tray = int(_u32.FindWindowW("Shell_TrayWnd", None) or 0)
        set_foreground(tray)
        click_at(*chevron.center)
    deadline = time.monotonic() + TRAY_FLYOUT_S
    while time.monotonic() < deadline:
        flyout = _tray_flyout()
        if flyout is not None:
            found = _shell_row(_surface_rows(flyout.hwnd), win)
            if found is not None:
                return _entry(flyout.hwnd, found, "托盘图标", 1)
        time.sleep(0.1)
    if opened:
        _close_tray_flyout()
    return None


def at_hand(win: Win) -> Entry | None:
    """The entry on screen that opens this window — 触手可及 — or None (spec §35.15).

    The order is the user's decision (2026-09-13): 桌面、任务栏、托盘 all count as at
    hand, but a taskbar button and a tray icon *activate* the window that is already
    running, while a desktop icon is a double-click that *launches* the application
    when it is not — so the desktop goes last.
    """
    tray = int(_u32.FindWindowW("Shell_TrayWnd", None) or 0)
    if tray:
        rows = _surface_rows(tray)
        found = _shell_row(rows, win)
        if found is not None:
            where = "托盘图标" if found.cls.startswith(TRAY_BUTTON_PREFIX) else "任务栏按钮"
            return _entry(tray, found, where, 1, raise_first=True)
        entry = _tray_overflow_entry(win, rows)
        if entry is not None:
            return entry
    desktop = _desktop_surface()
    if desktop:
        icon = _shell_row(_surface_rows(desktop), win)
        if icon is not None and _desktop_reachable(icon.center):
            return _entry(desktop, icon, "桌面图标", 2)
    return None


# Families that draw themselves and only wake input/a11y on their own activation
# path: Chromium/Electron (QQ), Qt (WeChat), and Qt's rendered surfaces.
SELF_DRAWN_CLASSES = ("Chrome_WidgetWin", "Chrome_RenderWidgetHost", "Qt5", "Qt6", "MMUIRender")


def shell_reason(hwnd: int, win: Win, *, just_woken: bool = False) -> str | None:
    """Why this window wants the shell path *first*, or None if the cheap wake is fine.

    Decided before anything is touched, from three signals measured on 2026-09-13:

    * it is not on screen (minimized or hidden): that is the application's own tray
      state, and `ShowWindow` cannot make the app *believe* it left the tray;
    * its a11y has nothing addressable at all — no name and no class anywhere,
      which is the QQ signature (7 anonymous ScrollItem shells);
    * its class is a self-drawn family (Chromium/Electron, Qt, MMUIRender): those
      render inside themselves and only attach input and accessibility when their
      own activation path runs.
    """
    state = window_state(hwnd)
    if state != "normal":
        return f"it is {state}: the application's own wake path is what ends that state"
    if not any(cand.name or cand.cls for cand, _ in _scan(hwnd, limit=40)):
        return "its a11y offers nothing addressable — the app is still asleep"
    if just_woken and win.cls.startswith(SELF_DRAWN_CLASSES):
        # Only for a window we just brought back: a healthy Chromium app (Edge) has
        # named controls and needs no help, while one that was sitting in the tray
        # keeps its renderer asleep even once it is visible again.
        return f"it draws itself ({win.cls}) and was just brought back"
    return None


def _is_shell_surface(hwnd: int) -> bool:
    """The desktop or the taskbar: the two windows that are never raised.

    `set_foreground` there measured 2026-09-14 as pure loss: on the desktop it moved the
    foreground to 'Program Manager' and uncovered not one icon, so it takes the user's
    focus and buys nothing. Clicks do not need the help either — the taskbar is topmost,
    and the desktop's icons are only clicked while `covering_window` already says the
    desktop is what is there.
    """
    return hwnd in (int(_u32.FindWindowW("Shell_TrayWnd", None) or 0), _desktop_surface())


def _no_entry_reason(win: Win) -> str:
    """Why nothing could be clicked: the windows an application has no entry in.

    A desktop icon that exists but is covered is worth naming — the application *does*
    have a door, it is just behind something right now, and a person would clear the
    screen before double-clicking it (spec §35.15)."""
    desktop = _desktop_surface()
    icon = _shell_row(_surface_rows(desktop), win) if desktop else None
    if icon is not None:
        return (
            f"its desktop icon {icon.name.splitlines()[0].strip()!r} is covered by another "
            "window — nothing to click while it is (bring the desktop to the front first)"
        )
    return "no taskbar button, no tray icon and no desktop icon to open it through"


def shell_wake(hwnd: int) -> str:
    """Open an application the way its own icon does — through the shell.

    `ShowWindow` + `SetForegroundWindow` only move the *operating system's* idea of
    the window: measured on this box (2026-09-13), a QQ window woken that way was
    the real foreground window, and yet every one of its windows ignored
    SendInput, batched SendInput, and even a directly posted WM_LBUTTONDOWN — while
    the same code drove a plain Win32 window (click confirmed by the app itself).
    The application still believed it was in the tray: no render surface attached,
    no a11y, no input.

    Clicking the icon in the taskbar or the notification area is what the user does,
    and it works because that click lands on *explorer*: the shell then delivers the
    application's own tray/activation callback, and the app runs its real
    "open my window" path. Measured the same afternoon: after that click the window
    went hidden -> normal and its a11y went from 7 anonymous shells to 20 elements
    with names.

    What gets clicked is whatever is 触手可及 (`at_hand`, spec §35.15): the taskbar
    button or tray icon when the app has one, else the desktop icon, double-clicked
    because a single click only selects it. Nothing at hand means nothing to click —
    the caller may still try `ShowWindow`, and should say out loud that the result is
    likely the "visible but asleep" window.
    """
    win = next((w for w in list_windows(include_hidden=True) if w.hwnd == hwnd), None)
    if win is None:
        return "the window is gone"
    if window_state(hwnd) == "normal" and foreground_hwnd() == hwnd:
        # Clicking the taskbar button of the window that is already in front *minimizes*
        # it — Windows toggles on that click. Nothing to open, so click nothing.
        return "it is already on screen and in front — nothing to open"
    entry = at_hand(win)
    if entry is None:
        _close_tray_flyout()
        return f"no entry at hand: {_no_entry_reason(win)}"
    if entry.raise_first:
        set_foreground(entry.surface)
    # A click on an entry can open a *different* window of the application, and for a
    # desktop icon that is the normal case. Measured 2026-09-13 on OneDrive: its tray
    # icon raised the 'Activity Center' while the window we were holding stayed a hidden
    # balloon host — so reporting only the held window would say "nothing happened" about
    # a click that plainly did something. The evidence is the window list, the same one
    # the double_click action uses (spec §35.13).
    before = {w.hwnd for w in list_windows()}
    click_at(*entry.target.center, clicks=entry.clicks)
    woke = _await_state(hwnd, "normal", SHELL_WAKE_S)
    opened = f"{entry.where} {entry.label!r}" + (" (double-click)" if entry.clicks == 2 else "")
    if woke:
        # A tray click leaves the overflow flyout open, and it has to stay that way: the
        # app's window often light-dismisses on any outside click, and clicking the arrow
        # to tidy up measured taking OneDrive's panel back down with it (2026-09-13). So
        # the flyout is reported, not cleaned up.
        note = " (the notification flyout is still open)" if _tray_flyout() is not None else ""
        return f"clicked its {opened} and it came up{note}"
    appeared = [w for w in list_windows() if w.hwnd not in before]
    if appeared:
        return f"clicked its {opened} and it opened {appeared[0].title!r} instead"
    _close_tray_flyout()
    return f"clicked its {opened} but it stayed hidden"


def _await_state(hwnd: int, want: str, timeout: float) -> bool:
    """Poll the window's own state until it matches — the app wakes on its own clock."""
    deadline = time.monotonic() + timeout
    while True:
        if window_state(hwnd) == want:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _named_a11y(hwnd: int) -> int:
    """How many addressable (named) controls the window's a11y offers right now."""
    return sum(1 for cand, _ in _scan(hwnd, limit=40) if cand.name)


def wake_window(hwnd: int, via: str = "auto") -> list[str]:
    """Get a window on screen, choosing the path the *application* needs.

    Returns the lines to report. `auto` reads the window before touching it (state,
    whether its a11y is addressable at all, whether it draws itself): for a
    tray-resident or self-drawn app the shell click runs **first and alone** —
    running `ShowWindow` first would only make a dead window visible, and it also
    destroys the very evidence the decision rests on. `ShowWindow` gets a turn only
    if the app still is not on screen afterwards.
    """
    win = next((w for w in list_windows(include_hidden=True) if w.hwnd == hwnd), None)
    if win is None:
        return ["  the window is gone"]
    notes: list[str] = []
    reason = shell_reason(hwnd, win) if via == "auto" else None
    if via == "shell" or reason is not None:
        if reason is not None:
            notes.append(f"  shell wake first: {reason}")
        notes.append(f"  shell wake: {shell_wake(hwnd)}")
        time.sleep(SETTLE_S)
        if window_state(hwnd) != "normal":
            # One clean retry: the first click can be eaten while the overflow flyout
            # is still closing.
            notes.append(f"  shell wake (retry): {shell_wake(hwnd)}")
            time.sleep(SETTLE_S)
    if window_state(hwnd) != "normal":
        restored = ensure_on_screen(hwnd)
        if restored != "normal" and window_state(hwnd) != "normal":
            # A window minimized by "show desktop" is held there by the shell, not by
            # the application: `ShowWindow(SW_RESTORE)` measured 2026-09-13 left five
            # of them minimized, while the taskbar button — the shell's own path —
            # brings them back. So the shell gets a turn whenever the API did not work.
            notes.append(f"  ShowWindow left it {restored}; shell path: {shell_wake(hwnd)}")
            time.sleep(SETTLE_S)
    if window_state(hwnd) != "normal":
        was = ensure_on_screen(hwnd)
        notes.append(
            f"  OS wake (ShowWindow): the shell path left it {was}"
            " — the application never ran its own wake path, so this window may look"
            " normal and still ignore every input (measured on QQ and WeChat, 2026-09-13)"
        )
        time.sleep(SETTLE_S)
    if not _is_shell_surface(hwnd):
        set_foreground(hwnd)
        time.sleep(SETTLE_S)
    return notes


def _action_restore(args: dict) -> str | ImageRead:
    """Bring a window back on screen, through the path the application needs.

    `via` defaults to `auto`: the shell entry (its taskbar button, or its tray icon
    behind the notification area's overflow) for a tray-resident or self-drawn app,
    `ShowWindow` otherwise — see `wake_window` and spec §35.12.
    """
    hwnd = int(args["hwnd"])
    via = str(args.get("via") or "auto").strip().lower()
    was = window_state(hwnd)
    notes = wake_window(hwnd, via)
    now = window_state(hwnd)
    frame = grab_window(hwnd) if now == "normal" else None
    if frame is not None:
        _session.remember(frame)
    summary = (
        f"RESTORE hwnd=0x{hwnd:X} {_window_text(hwnd)!r} → {now}"
        f" (was {was}, named controls: {_named_a11y(hwnd) if now == 'normal' else 0})\n"
        "  control: on (pc_control switch; no prompt for this action)"
    )
    if notes:
        summary += "\n" + "\n".join(notes)
    return _attach(summary, frame) if frame is not None else summary


_INPUT_ACTIONS = {
    "click": _action_click,
    "double_click": _action_double_click,
    "type": _action_type,
    "key": _action_key,
    "scroll": _action_scroll,
}


def _run(
    cfg: Config,
    sink: Sink,
    args: dict,
    *,
    call_id: str | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_answer: Callable[[dict], None] | None = None,
) -> str | ImageRead:
    action = str(args.get("action") or "").strip().lower()
    if not cfg.pc_control:
        return "ERROR: screen control is off (config.json pc_control=false)"
    if action == "windows":
        return _action_windows(args)
    if action == "shot":
        return _action_shot(args)
    if action == "targets":
        return _action_targets(args)
    if action == "label":
        return _action_label(args)
    handler = _INPUT_ACTIONS.get(action)
    if handler is None and action != "restore":
        return (
            f"ERROR: unknown action {action!r} — use shot, windows, targets, click, type, key, "
            "scroll or restore"
        )
    hwnd = args.get("hwnd")
    if hwnd in (None, ""):
        return f"ERROR: {action} needs hwnd=<window id from windows>; identity is never guessed"
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        return f"ERROR: hwnd must be a number, got {hwnd!r}"
    if not _u32.IsWindow(hwnd):
        return f"ERROR: no such window: 0x{hwnd:X}"
    try:
        if action == "restore":  # asks nothing, so it takes no ask plumbing
            return _action_restore(args)
        return handler(args, sink, should_abort, on_answer, call_id)
    finally:
        # Belt and braces: no injection path may leave a key down, including
        # the ones that raise or return early.
        release_all_keys()


SCHEMA = {
    "type": "function",
    "function": {
        "name": "screen",
        "description": (
            "Look at this machine's desktop and act on it. Read-only actions: "
            "`windows` (numbered window list, hwnd + title + process), `shot` (a "
            "picture of the whole screen, or of one window with hwnd=), `targets` "
            "(the controls inside one window, numbered, with the same numbers drawn "
            "on a picture). Input actions: `click`, `double_click` (the open "
            "gesture for an icon that has no path you know), `type` (pastes text), `key`, "
            "`scroll`, plus `restore` (bring a minimized or tray-resident window back on "
            "screen, then picture it) and `label` (give a shape you recognised a name, so the "
            "next listing shows it instead of '(no text)') — every one of them takes hwnd= and names its target as "
            "target=<number from targets> or name=<the control's visible text>. "
            "Coordinates are deliberately NOT accepted: the tool resolves the target "
            "itself from the accessibility tree (exact) or OCR, so a click always "
            "lands where the program says the control is, never where a model "
            "estimated. Workflow: windows → targets(hwnd) → click/type on a number; "
            "each action returns the window's new picture so you can check the "
            "effect yourself. Input actions ask nothing per action: the settings switch "
            "(config.json pc_control) is the consent, and turning it off stops the agent "
            "immediately. "
            "Minimized or tray-hidden windows: they keep off-screen geometry, so measure "
            'nothing until `restore` has run — `windows include="all"` is how you get their '
            "hwnd, and the taskbar row in that list is where tray icons themselves live. "
            "The desktop is a window as well (cls=Progman, titled 'Program Manager'): `targets` on "
            "it lists the desktop icons, and `double_click` on one starts that application — the "
            "way to open something that is running nowhere at all (no window, no taskbar button, "
            "no tray icon, so no hwnd to restore). Do not go looking for the exe on disk instead: "
            "the icons are the app's own entry point. While another window covers the icons that "
            "click is refused and the refusal names the window on top — bring the desktop up first "
            "(`key` with ['win','d'] shows it, the same keys bring the windows back), or act on the "
            "window that is actually covering it. "
            "`restore` opens a window through whatever is at hand (触手可及): the application's "
            "own entry point on screen — its taskbar button, its tray icon, or its desktop icon "
            "(double-clicked, so it goes last: a taskbar click activates the running window "
            "while a desktop icon may launch a new one). Only an application with no such entry "
            "is woken through the OS, and that path yields a window that looks normal and still "
            "ignores input — the result says so. "
            "A window that needs administrator rights cannot be driven from here "
            "(Windows blocks it). A UI drawn in pixels (Chromium/Electron apps such as QQ are "
            "exactly this: they expose anonymous shells and no readable controls) still has "
            "targets — the tool reads the text off the picture and cuts the icons out of it, "
            "numbered, so pick those by the number you can see on the attached frame rather than "
            "by name; names from OCR are approximate (a rare character can be misread), numbers "
            "are not. To type into such a window, click the box you mean first and then call type "
            "with no target: it pastes into whatever has the focus."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "windows",
                        "shot",
                        "targets",
                        "label",
                        "click",
                        "double_click",
                        "type",
                        "key",
                        "scroll",
                        "restore",
                    ],
                    "description": "What to do",
                },
                "hwnd": {
                    "type": "integer",
                    "description": (
                        "Window id from `windows`. Required for every input action and "
                        "for targets; omit on shot to capture the whole screen."
                    ),
                },
                "target": {
                    "type": "integer",
                    "description": "Candidate number from the last `targets` call for this window",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "The control's name or visible text instead of a number "
                        "(e.g. the button caption). Ambiguous matches come back as a list."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "type only: the text to paste at the focused control",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "key only: key names in order. Leading modifiers make it a "
                        'combination: ["ctrl","s"], ["enter"], ["alt","f4"]. '
                        "Names: letters/digits, enter, tab, escape, space, backspace, "
                        "delete, arrows (up/down/left/right), home/end/pageup/pagedown, "
                        "insert, ctrl/shift/alt/win, f1-f12, punctuation."
                    ),
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "description": "click/double_click: mouse button (default left)",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "label only: the semantic name to remember for a candidate, e.g. "
                        '"发送". It is bound to where that shape was, so click(name="发送") '
                        "keeps working across later listings — and an unlabelled picture shape has "
                        "no name at all, so label the ones you will need again."
                    ),
                },
                "via": {
                    "type": "string",
                    "enum": ["auto", "window", "shell"],
                    "description": (
                        "restore only. 'auto' (default) wakes the window and, if the application "
                        "still offers no named controls, opens it through what is at hand — its "
                        "taskbar button, tray icon or desktop icon; that is the shell path, the one "
                        "a tray-resident app (QQ and friends) actually responds to, instead of only "
                        "becoming visible. 'window' is the OS-level wake alone; 'shell' forces the "
                        "shell path."
                    ),
                },
                "include": {
                    "type": "string",
                    "enum": ["visible", "all"],
                    "description": (
                        "windows only: 'all' also lists windows that are minimized or hidden in "
                        "the notification area, plus the untitled system windows (the taskbar / "
                        "notification area itself) — the way to reach an app that only lives in "
                        "the tray. Default 'visible'."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


def bound(
    cfg: Config,
    sink: Sink,
    *,
    should_abort: Callable[[], bool] | None = None,
    on_answer: Callable[[dict], None] | None = None,
) -> dict[str, BoundTool]:
    """The `screen` tool for one local-agent turn (spec §35.3: local agent only)."""

    def screen(args: dict, call_id: str | None = None) -> str | ImageRead:
        return _run(
            cfg,
            sink,
            args,
            call_id=call_id,
            should_abort=should_abort,
            on_answer=on_answer,
        )

    return {"screen": BoundTool(schema=SCHEMA, fn=screen, with_call_id=True)}
