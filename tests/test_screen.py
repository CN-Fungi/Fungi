"""fungi/tools/screen.py: the desktop-control contract (spec §35).

Everything here runs without a desktop: the Win32 layer is monkeypatched, and
what is asserted is the behaviour the design promised — coordinates come from
the program, a window that is not on screen is never measured, permission is a
time-boxed arm that always ends in a disarm, and nothing is injected when a key
name is unknown.
"""

import time

import pytest
from PIL import Image, ImageDraw

from fungi.config import Config
from fungi.tools import screen
from fungi.tools.files import ImageRead


def _frame(size=(40, 30), colour=(255, 255, 255), origin=(1000, 500)):
    return screen.Frame(Image.new("RGB", size, colour), origin, 42, time.time())


def _cand(n, name, cls, rect, patterns=(), source="a11y"):
    return screen.Target(n, name, cls, rect, patterns, source)


class _U32:
    """Stand-in for the user32 handle: records what was asked of the OS."""

    def __init__(self, window=True, client=(1000, 500, 1400, 800)):
        self.window = window
        self.client = client
        self.shown = []

    def IsWindow(self, _hwnd):  # noqa: N802 (mimics user32)
        return self.window

    def ShowWindow(self, hwnd, cmd):  # noqa: N802 (mimics user32)
        self.shown.append((hwnd, cmd))
        return True

    def GetWindowRect(self, _hwnd, rect_ref):  # noqa: N802 (mimics user32)
        left, top, right, bottom = self.client
        rect_ref._obj.left, rect_ref._obj.top = left - 10, top - 30  # a title bar
        rect_ref._obj.right, rect_ref._obj.bottom = right + 10, bottom + 10
        return 1

    def GetClientRect(self, _hwnd, rect_ref):  # noqa: N802 (mimics user32)
        rect_ref._obj.right = self.client[2] - self.client[0]
        rect_ref._obj.bottom = self.client[3] - self.client[1]
        return 1

    def ClientToScreen(self, _hwnd, point_ref):  # noqa: N802 (mimics user32)
        point_ref._obj.x, point_ref._obj.y = self.client[0], self.client[1]
        return 1


@pytest.fixture(autouse=True)
def _clean_session():
    """Every test starts from a disarmed session, and leaves one behind."""
    screen.release_all_keys()
    screen._session.disarm()
    yield
    screen.release_all_keys()
    screen._session.disarm()


# ── the key table: resolve everything before injecting anything ─────────────
def test_key_names_resolve_through_their_aliases():
    assert (
        screen.resolve_key("return") == screen.resolve_key("ENTER") == screen.resolve_key("enter")
    )
    assert screen.resolve_key("DEL") == screen.resolve_key("delete") == 0x2E
    assert screen.resolve_key("cmd") == screen.resolve_key("win") == 0x5B
    assert screen.resolve_key("PgUp") == screen.resolve_key("pageup") == 0x21
    assert screen.resolve_key("nosuchkey") is None


def test_send_keys_injects_nothing_when_one_name_is_unknown(monkeypatch):
    """Resolve first, inject second: skipping 'ctrl' silently would press the
    bare key, and skipping the key after it would leave Ctrl held down."""
    events = []
    monkeypatch.setattr(screen, "_key_event", lambda vk, *, down: events.append((vk, down)))
    sent, error = screen.send_keys(["ctrl", "nosuchkey"])
    assert sent == [] and "unknown key" in error and events == []


def test_a_combination_presses_modifiers_around_the_key(monkeypatch):
    events = []
    monkeypatch.setattr(screen, "_key_event", lambda vk, *, down: events.append((vk, down)))
    sent, error = screen.send_keys(["ctrl", "shift", "s"])
    assert error is None and sent == ["ctrl", "shift", "s"]
    assert events == [
        (0x11, True),
        (0x10, True),
        (0x53, True),
        (0x53, False),
        (0x10, False),
        (0x11, False),
    ]


def test_release_all_keys_clears_whatever_is_still_held(monkeypatch):
    events = []
    monkeypatch.setattr(screen, "_key_event", lambda vk, *, down: events.append((vk, down)))
    monkeypatch.setattr(screen, "_held", {0x11, 0x10})
    released = screen.release_all_keys()
    assert events == [(0x10, False), (0x11, False)]
    assert released == ["0x10", "0x11"] and screen._held == set()


# ── the armed window (spec §35.2) ──────────────────────────────────────────
def test_disarm_releases_keys_and_forgets_frames(monkeypatch):
    released = []
    monkeypatch.setattr(screen, "release_all_keys", lambda: released.append(True) or ["0x11"])
    screen._session.labels = {"发送": _cand(0, "发送", "", (1, 2, 3, 4), (), "visual")}
    screen._session.remember(_frame())
    screen.disarm()
    assert released
    assert screen._session.labels == {} and len(screen._session.frames) == 0


def test_an_input_action_asks_nothing_and_pops_no_notice(monkeypatch):
    """Consent is the settings switch (user decision 2026-09-13): no card per
    action, and no toast either — a notification lands on top of the very screen
    being driven (spec §35.14)."""
    monkeypatch.setattr(screen, "blocking_ask", lambda *a, **k: pytest.fail("asked the user"))
    monkeypatch.setattr(screen, "ensure_on_screen", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "shell_wake", lambda hwnd: "no shell entry")
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(
        screen,
        "list_windows",
        lambda include_hidden=False: [
            screen.Win(42, "demo", "Notepad", (0, 0, 5, 5), 5, "notepad.exe", "normal")
        ],
    )
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "demo")

    first_ok, _note = screen._guarded_input(42)
    second_ok, _note = screen._guarded_input(42)
    assert first_ok and second_ok
    # The tool has no way to reach a tray any more: nothing can pop over the window
    # it is about to measure and click.
    assert not hasattr(screen, "set_notifier")


def test_a_window_that_stays_off_screen_is_refused(monkeypatch):
    monkeypatch.setattr(screen, "ensure_on_screen", lambda hwnd: "hidden")
    monkeypatch.setattr(screen, "shell_wake", lambda hwnd: "no shell entry")
    monkeypatch.setattr(
        screen,
        "list_windows",
        lambda include_hidden=False: [
            screen.Win(42, "demo", "Notepad", (0, 0, 5, 5), 5, "notepad.exe", "hidden")
        ],
    )
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "hidden")
    ok, note = screen._guarded_input(42)
    assert ok is False and "cannot be driven" in note


def test_a_listing_number_resolves_against_the_live_window(monkeypatch):
    element = object()
    pairs = [
        (_cand(1, "保存", "Button", (1000, 500, 1060, 530), ("Invoke",)), element),
        (_cand(2, "", "Edit", (1000, 540, 1100, 570), ("Value",)), object()),
    ]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    screen._session.candidates = {cand.n: cand for cand, _ in pairs}
    screen._session.candidates_hwnd = 42

    hit = screen.resolve_target(42, {"target": 1})
    assert isinstance(hit, screen.Target) and hit.name == "保存"

    stale = screen.resolve_target(42, {"target": 9})
    assert isinstance(stale, str) and "no candidate #9" in stale


def test_an_ambiguous_name_lists_the_candidates_instead_of_guessing(monkeypatch):
    pairs = [
        (_cand(1, "项目 01", "", (1000, 500, 1100, 520)), object()),
        (_cand(2, "项目 02", "", (1000, 520, 1100, 540)), object()),
    ]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    monkeypatch.setattr(screen, "_ocr_targets", lambda *a, **k: [])
    out = screen.resolve_target(42, {"name": "项目"})
    assert isinstance(out, str) and "2 controls match" in out and "#1 " in out and "#2 " in out


def test_a_control_without_an_accessible_name_is_found_by_its_class(monkeypatch):
    """The listing prints `cls` for a nameless control ('Edit' with name ''), so
    a model that reads that label has to be able to use it."""
    pairs = [(_cand(1, "", "Edit", (1000, 500, 1100, 520), ("Value",)), object())]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    hit = screen.resolve_target(42, {"name": "Edit"})
    assert isinstance(hit, screen.Target) and hit.cls == "Edit"


def test_an_unknown_name_is_no_target_with_the_candidate_list(monkeypatch):
    pairs = [(_cand(1, "保存", "Button", (1000, 500, 1060, 530), ("Invoke",)), object())]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: None)
    out = screen.resolve_target(42, {"name": "不存在的按钮"})
    assert isinstance(out, str) and out.startswith("ERROR: no_target") and "保存" in out


# ── a window that is not on screen is never measured or clicked ────────────
def test_the_default_listing_is_what_the_user_can_see():
    assert screen._keep_window("normal", "demo", include_hidden=False)
    assert screen._keep_window("minimized", "demo", include_hidden=False)
    assert not screen._keep_window("hidden", "tray-only", include_hidden=False)
    assert not screen._keep_window("normal", "", include_hidden=False)
    assert screen._keep_window("hidden", "tray-only", include_hidden=True)
    assert screen._keep_window("normal", "", include_hidden=True)
    assert not screen._keep_window("hidden", "", include_hidden=True)


def test_the_report_marks_state_and_names_the_taskbar(monkeypatch):
    wins = [
        screen.Win(11, "demo", "Notepad", (0, 0, 400, 300), 5, "notepad.exe", "normal"),
        screen.Win(12, "tray-only", "Tray", (0, 0, 200, 60), 6, "one.exe", "hidden"),
        screen.Win(13, "", "Shell_TrayWnd", (0, 0, 2240, 72), 7, "explorer.exe", "untitled"),
    ]
    monkeypatch.setattr(screen, "list_windows", lambda include_hidden=False: wins)
    monkeypatch.setattr(screen, "foreground_hwnd", lambda: 11)
    text = screen._windows_report(include_hidden=True)
    assert "[hidden]" in text and "Shell_TrayWnd" in text and "← foreground" in text
    assert "targets(hwnd=" in text  # the tray path is stated, not left to be guessed


def test_targets_refuses_a_minimized_window_and_points_at_restore(monkeypatch):
    monkeypatch.setattr(screen, "_u32", _U32())
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "minimized")
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "demo")
    out = screen._action_targets({"hwnd": 42})
    assert isinstance(out, str) and "is minimized" in out and "action=restore" in out


def test_a_covered_dpi_unaware_window_is_not_measured(monkeypatch):
    """PrintWindow hands back a scaled stub for those, so the honest answer is an
    error that names the way out — not a picture whose controls are 1.5x off."""
    monkeypatch.setattr(screen, "_dpi_unaware", lambda hwnd: True)
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "legacy")
    monkeypatch.setattr(screen, "foreground_hwnd", lambda: 99)
    problem = screen.capture_problem(7)
    assert problem and "DPI-unaware" in problem and "restore" in problem
    monkeypatch.setattr(screen, "foreground_hwnd", lambda: 7)
    assert screen.capture_problem(7) is None


def test_shell_rows_are_matched_by_the_application_s_own_names():
    """The taskbar names a button after the app's display name, not its exe
    ('智能终端 - 1 个运行窗口' for WindowsTerminal.exe), so the window title is the
    better token — and the tray tooltip carries it too."""
    win = screen.Win(1, "QQ", "Chrome_WidgetWin_1", (0, 0, 10, 10), 5, "QQ.exe", "normal")
    rows = [
        _cand(1, " Clash Verge 2.5.2", "", (0, 0, 5, 5)),
        _cand(2, " QQ: 3754901636", "", (0, 0, 5, 5)),
    ]
    hit = screen._shell_row(rows, win)
    assert hit is not None and "3754901636" in hit.name
    titled = screen.Win(
        2, "智能终端", "CASCADIA", (0, 0, 10, 10), 5, "WindowsTerminal.exe", "normal"
    )
    assert (
        screen._shell_row([_cand(1, "智能终端 - 1 个运行窗口", "", (0, 0, 5, 5))], titled)
        is not None
    )
    assert screen._shell_row([_cand(1, "无关的图标", "", (0, 0, 5, 5))], win) is None


def test_restore_reaches_for_the_shell_when_the_window_stays_asleep(monkeypatch):
    """A tray-resident app woken with ShowWindow alone offers no named controls
    (its renderer and a11y are still asleep); restore then does what the user does
    and clicks its shell entry (measured on QQ, 2026-09-13)."""
    monkeypatch.setattr(screen, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "QQ")
    monkeypatch.setattr(screen, "_named_a11y", lambda hwnd: 0)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: None)
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(
        screen,
        "list_windows",
        lambda include_hidden=False: [
            screen.Win(42, "QQ", "Chrome_WidgetWin_1", (0, 0, 10, 10), 5, "QQ.exe", "normal")
        ],
    )
    woken: list[int] = []
    monkeypatch.setattr(
        screen, "shell_wake", lambda hwnd: woken.append(hwnd) or "clicked its tray icon ' QQ'"
    )

    out = str(screen._action_restore({"hwnd": 42}))
    assert woken == [42]
    assert "shell wake first" in out and "tray icon" in out


def test_the_up_front_condition_names_why_the_shell_is_needed(monkeypatch):
    """Decided before anything is touched (user's ask, 2026-09-13): a tray-resident
    or self-drawn window goes to the shell path first, a healthy one does not."""
    qq = screen.Win(1, "QQ", "Chrome_WidgetWin_1", (0, 0, 10, 10), 5, "QQ.exe", "normal")
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    assert "nothing addressable" in screen.shell_reason(1, qq)

    monkeypatch.setattr(screen, "window_state", lambda hwnd: "hidden")
    assert "is hidden" in screen.shell_reason(1, qq)

    named = [(_cand(1, "保存", "Button", (0, 0, 5, 5), ("Invoke",)), object())]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: named)
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    assert screen.shell_reason(1, qq) is None  # alive and on screen: no help needed
    assert "just brought back" in screen.shell_reason(1, qq, just_woken=True)


def test_restore_leaves_an_awake_window_alone(monkeypatch):
    monkeypatch.setattr(screen, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "demo")
    monkeypatch.setattr(screen, "_named_a11y", lambda hwnd: 12)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: None)
    monkeypatch.setattr(screen, "shell_wake", lambda hwnd: pytest.fail("clicked the shell"))
    monkeypatch.setattr(
        screen,
        "_scan",
        lambda hwnd, limit=screen.MAX_CANDIDATES: [
            (_cand(1, "保存", "Button", (0, 0, 5, 5), ("Invoke",)), object())
        ],
    )
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "demo")

    out = str(screen._action_restore({"hwnd": 42}))
    assert "named controls: 12" in out and "shell wake" not in out


def test_restore_window_only_never_touches_the_shell(monkeypatch):
    monkeypatch.setattr(screen, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "demo")
    monkeypatch.setattr(screen, "_named_a11y", lambda hwnd: 0)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: None)
    monkeypatch.setattr(screen, "shell_wake", lambda hwnd: pytest.fail("clicked the shell"))
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])

    out = str(screen._action_restore({"hwnd": 42, "via": "window"}))
    assert "shell wake" not in out


def test_waking_a_window_reports_what_it_was(monkeypatch):
    states = iter(["minimized", "normal"])
    u32 = _U32()
    monkeypatch.setattr(screen, "_u32", u32)
    monkeypatch.setattr(screen, "window_state", lambda hwnd: next(states, "normal"))
    monkeypatch.setattr(screen.time, "sleep", lambda _s: None)
    assert screen.ensure_on_screen(11) == "minimized"
    assert u32.shown == [(11, screen.SW_RESTORE)]


# ── the result the model gets: a summary plus real pixels ──────────────────
def test_a_frame_comes_back_as_pixels_the_model_can_see():
    result = screen._attach("SCREEN 40x30", _frame())
    assert isinstance(result, ImageRead) and str(result).startswith("SCREEN 40x30")
    assert result.data_url.startswith("data:image/png;base64,")


def test_the_numbered_frame_marks_every_candidate():
    frame = _frame((60, 40))
    marked = screen._mark_targets(
        frame, [_cand(1, "保存", "Button", (1010, 510, 1030, 530), ("Invoke",))]
    )
    assert marked.size == frame.image.size
    assert marked.tobytes() != frame.image.tobytes()


def test_the_frame_diff_counts_pixels_not_promises():
    before = _frame((50, 50), (0, 0, 0))
    assert screen._diff_ratio(before, _frame((50, 50), (0, 0, 0))) == 0.0
    one_pixel = _frame((50, 50), (0, 0, 0)).image.copy()
    one_pixel.putpixel((5, 5), (255, 255, 255))
    assert 0 < screen._diff_ratio(before, screen.Frame(one_pixel, before.origin, 42, 0.0)) < 0.01
    assert screen._diff_ratio(before, _frame((60, 50))) == 1.0


def test_a_self_drawn_window_falls_back_to_ocr_text(monkeypatch):
    """WeChat 4.x exposes one pattern-less surface element and nothing else; the
    listing used to hand that back as the only target. Now the picture's text is
    offered instead (spec §35.1's a11y-then-OCR order)."""
    surface = _cand(1, "MMUIRenderSubWindow", "", (0, 0, 1200, 800), ())
    monkeypatch.setattr(
        screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [(surface, object())]
    )
    monkeypatch.setattr(
        screen,
        "_ocr_targets",
        lambda frame, start=0: [_cand(start + 1, "搜索", "", (10, 20, 90, 44), ())],
    )
    monkeypatch.setattr(screen, "_u32", _U32(client=(0, 0, 1200, 800)))
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((300, 200)))
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "微信")

    out = str(screen._action_targets({"hwnd": 42}))
    assert "OCR" in out and "搜索" in out
    assert screen._session.candidates[2].name == "搜索"


def test_the_whole_surface_of_a_self_drawn_window_is_not_clickable(monkeypatch):
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (0, 0, 1200, 800))
    surface = screen.Target(1, "MMUIRenderSubWindow", "", (0, 0, 1200, 800), ())
    problem = screen.target_problem(42, surface)
    assert problem and "whole window surface" in problem and "name=<text>" in problem

    editor = screen.Target(2, "文本编辑器", "RichEditD2DPT", (10, 10, 1180, 780), ("Value",))
    assert screen.target_problem(42, editor) is None  # a real control, even a big one
    small = screen.Target(3, "搜索", "", (100, 100, 200, 130), ())
    assert screen.target_problem(42, small) is None


def test_a_target_outside_its_window_is_refused(monkeypatch):
    """The case that put a click on a desktop file: a resolved rectangle that is
    not inside the window it was resolved for."""
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (1000, 500, 1400, 800))
    outside = screen.Target(1, "搜索", "", (100, 100, 200, 130), ())
    problem = screen.target_problem(42, outside)
    assert problem and "outside window" in problem and "underneath" in problem


def test_a_blank_capture_of_a_covered_window_is_not_an_answer(monkeypatch):
    """A single-colour bitmap from a covered Chromium surface must come back as
    "cannot read it", never as a picture that says the panel is empty — that wrong
    answer looks like a real one (2026-09-13)."""
    monkeypatch.setattr(screen, "_ensure_dpi", lambda: None)
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (0, 0, 100, 100))
    monkeypatch.setattr(screen, "_dpi_unaware", lambda hwnd: False)
    monkeypatch.setattr(
        screen, "_print_window", lambda hwnd, rect: _frame((100, 100), (32, 32, 32), (0, 0))
    )
    monkeypatch.setattr(screen, "foreground_hwnd", lambda: 99)
    assert screen.grab_window(42) is None

    monkeypatch.setattr(screen, "foreground_hwnd", lambda: 42)  # front: a black window is real
    assert screen.grab_window(42) is not None


def test_the_program_binarises_and_numbers_what_has_no_text():
    """The prototype's third tier (spec §35.1): the threshold happens inside the
    program and the model gets numbers — never the binary image, which cost it
    21-296px of error and 3x the latency when it was fed the picture."""
    image = Image.new("RGB", (300, 200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 160, 80), fill=(47, 111, 208))  # a drawn control, no text
    frame = screen.Frame(image, (1000, 500), 42, time.time())

    boxes = screen._visual_boxes(image)
    assert any(
        abs(box[0] - 40) <= 4 and abs(box[1] - 30) <= 4 and box[2] >= 158 for box in boxes
    ), boxes

    targets = screen._visual_targets(frame)
    assert targets and targets[0].source == "visual"
    assert "[shape]" in targets[0].label and "(no text)" in targets[0].label
    # screen coordinates, ready to click: the frame origin is added back
    assert targets[0].rect[0] >= 1000 and targets[0].rect[1] >= 500


def test_a11y_controls_keep_the_picture_tiers_out_of_the_way(monkeypatch):
    """Priority order, and its cost argument: when a11y has something clickable,
    neither OCR nor shape cutting runs at all."""
    pairs = [(_cand(1, "保存", "Button", (1000, 500, 1060, 530), ("Invoke",)), object())]
    called: list[str] = []
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    monkeypatch.setattr(screen, "_ocr_targets", lambda *a, **k: called.append("ocr") or [])
    monkeypatch.setattr(screen, "_visual_targets", lambda *a, **k: called.append("visual") or [])
    monkeypatch.setattr(screen, "_u32", _U32())
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((300, 200)))
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "demo")

    out = str(screen._action_targets({"hwnd": 42}))
    assert called == [] and "保存" in out and "[shape]" not in out


def test_unnamed_electron_shells_do_not_switch_the_picture_tiers_off(monkeypatch):
    """QQ (Electron) exposes seven identical unnamed, classless ScrollItem shells
    covering the whole client area. Counting those as "a11y already offers
    something" is why OCR never ran on it and the model saw no text at all."""
    shells = [
        (_cand(n, "", "", (504, 228, 1737, 1101), ("ScrollItem",)), object()) for n in range(1, 8)
    ]
    called: list[str] = []
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: shells)
    monkeypatch.setattr(
        screen,
        "_ocr_targets",
        lambda frame, start=0: called.append("ocr")
        or [_cand(start + 1, "九佾", "", (973, 316, 1031, 346))],
    )
    monkeypatch.setattr(screen, "_visual_targets", lambda *a, **k: [])
    monkeypatch.setattr(screen, "_u32", _U32(client=(504, 228, 1737, 1101)))
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(
        screen, "grab_window", lambda hwnd: _frame((1200, 870), (255, 255, 255), (504, 228))
    )
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "QQ")

    out = str(screen._action_targets({"hwnd": 42}))
    assert called == ["ocr"] and "九佾" in out
    # …and the anonymous shells themselves are not offered as targets
    assert "[ScrollItem]" not in out
    # …and a control with a class still counts as addressable, so nothing is added
    named = [(_cand(1, "", "Edit", (600, 300, 900, 340), ("Value",)), object())]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: named)
    called.clear()
    out = str(screen._action_targets({"hwnd": 42}))
    assert called == [] and "cls=Edit" in out


def test_a_shape_that_is_only_the_ink_of_a_text_box_is_dropped(monkeypatch):
    """The prototype merged these by IoU: a blob inside a text box is that text's
    own strokes, and listing it twice would offer two targets for one thing."""
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(
        screen,
        "_ocr_targets",
        lambda frame, start=0: [_cand(start + 1, "确定", "", (1000, 500, 1100, 540))],
    )
    monkeypatch.setattr(
        screen,
        "_visual_targets",
        lambda frame, start=0: [
            _cand(start + 1, "", "", (1002, 502, 1098, 538), (), "visual"),
            _cand(start + 2, "", "", (1200, 600, 1260, 660), (), "visual"),
        ],
    )
    monkeypatch.setattr(screen, "_u32", _U32())
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((400, 300)))
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "微信")

    out = str(screen._action_targets({"hwnd": 42}))
    assert "#1 [text] '确定'" in out
    assert out.count("[shape]") == 1
    assert "#2 [shape]" in out  # renumbered, so the listing has no gap
    assert screen._session.candidates[2].source == "visual"


def test_a_listing_says_where_its_candidates_came_from(monkeypatch):
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(
        screen,
        "_ocr_targets",
        lambda frame, start=0: [_cand(start + 1, "搜索", "", (1000, 500, 1060, 530))],
    )
    monkeypatch.setattr(
        screen,
        "_visual_targets",
        lambda frame, start=0: [_cand(start + 1, "", "", (1200, 600, 1260, 660), (), "visual")],
    )
    monkeypatch.setattr(screen, "_u32", _U32())
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((400, 300)))
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "微信")

    out = str(screen._action_targets({"hwnd": 42}))
    assert "no a11y at all" in out and "1 text boxes read off the picture by OCR" in out
    assert "1 shapes the program cut out of the picture" in out


def test_the_window_border_does_not_swallow_the_candidates():
    """The border is a connected ring whose bounding box is the whole frame; the
    prototype filtered by size first, and merging first let that ring eat every
    real candidate (measured on the canvas probe, 2026-09-13)."""
    image = Image.new("RGB", (400, 300), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 399, 299), outline=(0, 0, 0), width=2)  # the window border
    draw.rectangle((100, 100, 200, 150), fill=(40, 90, 190))  # what is actually there

    boxes = screen._visual_boxes(image)
    assert any(
        abs(box[0] - 100) <= 6 and abs(box[1] - 100) <= 6 and box[2] >= 195 for box in boxes
    ), boxes
    assert all(box[2] - box[0] < 0.9 * 400 or box[3] - box[1] < 0.9 * 300 for box in boxes)


def test_a_number_from_the_picture_keeps_its_own_rectangle(monkeypatch):
    """A shape or OCR candidate must not be re-matched against a11y elements: two
    elements with empty names matched each other and the click went to the wrong
    place (2026-09-13)."""
    shape = _cand(8, "", "", (1305, 261, 1461, 416), (), "visual")
    a11y = [(_cand(4, "", "", (852, 131, 2178, 173), ("Value",)), object())]
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: a11y)
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (852, 131, 2178, 173))
    screen._session.candidates = {8: shape}
    screen._session.candidates_hwnd = 42
    screen._session.candidates_rect = (852, 131, 2178, 173)

    hit = screen.resolve_target(42, {"target": 8})
    assert isinstance(hit, screen.Target) and hit.rect == (1305, 261, 1461, 416)


def test_a_number_from_the_picture_expires_when_the_window_moves(monkeypatch):
    """Those rectangles are screen coordinates cut out of one specific picture, so
    a window that moved makes them wrong rather than stale-but-usable."""
    shape = _cand(8, "", "", (1305, 261, 1461, 416), (), "visual")
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (852, 231, 2178, 273))
    screen._session.candidates = {8: shape}
    screen._session.candidates_hwnd = 42
    screen._session.candidates_rect = (852, 131, 2178, 173)

    out = screen.resolve_target(42, {"target": 8})
    assert isinstance(out, str) and "moved or resized" in out and "run targets again" in out


def test_an_element_without_a_name_or_class_is_never_matched():
    """Nothing identifies it, so any match would be a guess."""
    wanted = screen.Target(1, "", "", (10, 10, 50, 30), ("Value",))
    pairs = [(screen.Target(4, "", "", (100, 100, 200, 140), ("Value",)), "element")]
    assert screen._match_element(pairs, wanted) is None


def test_labelling_a_shape_makes_its_name_stick(monkeypatch):
    """The model decides which shape is the send button (it is the one looking at
    the picture); the program makes that choice stick, so the next listing shows
    发送 instead of '(no text)' (spec §35.10)."""
    shape = _cand(8, "", "", (1305, 261, 1461, 416), (), "visual")
    monkeypatch.setattr(screen, "_u32", _U32())
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (852, 131, 2178, 173))
    screen._session.candidates = {8: shape}
    screen._session.candidates_hwnd = 42
    screen._session.candidates_rect = (852, 131, 2178, 173)

    out = screen._action_label({"hwnd": 42, "target": 8, "label": " 发送 "})
    assert out.startswith("LABELLED '发送'") and "labels here: 发送" in out

    hit = screen.resolve_target(42, {"name": "发送"})
    assert isinstance(hit, screen.Target) and hit.rect == (1305, 261, 1461, 416)
    assert screen.resolve_target(42, {"name": "发"}) is not None  # substring works too


def test_a_label_stops_working_when_the_window_moves(monkeypatch):
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    screen._session.labels = {"发送": _cand(0, "发送", "", (1305, 261, 1461, 416), (), "visual")}
    screen._session.labels_hwnd = 42
    screen._session.labels_rect = (852, 131, 2178, 173)
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (852, 231, 2178, 273))

    out = screen.resolve_target(42, {"name": "发送"})
    assert isinstance(out, str) and "moved or resized since you labelled" in out


def test_labels_are_listed_back_when_a_name_matches_nothing(monkeypatch):
    """`no_target` is decided by the program, so it also has to say what the
    program does know — otherwise the model re-labels the same shape forever."""
    monkeypatch.setattr(
        screen,
        "_scan",
        lambda hwnd, limit=screen.MAX_CANDIDATES: [
            (_cand(1, "保存", "Button", (1000, 500, 1060, 530)), object())
        ],
    )
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: None)
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (1000, 500, 1400, 800))
    screen._session.labels = {"发送": _cand(0, "发送", "", (1010, 510, 1060, 530), (), "visual")}
    screen._session.labels_hwnd = 42
    screen._session.labels_rect = (1000, 500, 1400, 800)

    out = screen.resolve_target(42, {"name": "表情"})
    assert isinstance(out, str) and out.startswith("ERROR: no_target")
    assert "Labels you set here: 发送" in out


def test_a_real_control_outranks_a_label(monkeypatch):
    """A labelled shape must not shadow a control that a11y actually names."""
    real = _cand(3, "发送", "Button", (1000, 500, 1060, 530), ("Invoke",))
    monkeypatch.setattr(
        screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [(real, object())]
    )
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (1000, 500, 1400, 800))
    screen._session.labels = {"发送": _cand(0, "发送", "", (1200, 600, 1260, 660), (), "visual")}
    screen._session.labels_hwnd = 42
    screen._session.labels_rect = (1000, 500, 1400, 800)

    hit = screen.resolve_target(42, {"name": "发送"})
    assert isinstance(hit, screen.Target) and hit.cls == "Button"


def test_labels_die_with_the_session():
    screen._session.labels = {"发送": _cand(0, "发送", "", (1, 2, 3, 4), (), "visual")}
    screen._session.labels_hwnd = 42
    screen.disarm()
    assert screen._session.labels == {} and screen._session.labels_hwnd == 0


def test_the_numbered_listing_uses_the_label_again(monkeypatch):
    """Numbers are per-listing; the label is what survives one."""
    shape = _cand(8, "", "", (1305, 261, 1461, 416), (), "visual")
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: [])
    monkeypatch.setattr(screen, "_ocr_targets", lambda *a, **k: [])
    monkeypatch.setattr(screen, "_visual_targets", lambda *a, **k: [shape])
    monkeypatch.setattr(screen, "_u32", _U32(client=(852, 131, 2178, 1373)))
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(
        screen, "grab_window", lambda hwnd: _frame((1400, 1200), (255, 255, 255), (852, 131))
    )
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "微信")
    monkeypatch.setattr(
        screen,
        "window_rect",
        lambda hwnd: (842, 101, 2188, 1383),
    )
    screen._session.labels = {"发送": _cand(0, "发送", "", (1305, 261, 1461, 416), (), "visual")}
    screen._session.labels_hwnd = 42
    screen._session.labels_rect = (842, 101, 2188, 1383)

    out = str(screen._action_targets({"hwnd": 42}))
    assert "'发送'" in out and "[shape]" in out


# ── three strikes, then a question (spec §35.1) ────────────────────────────
def test_three_fruitless_attempts_ask_the_user_instead_of_retrying(monkeypatch):
    asked = []

    def fake_ask(_sink, questions, **_kw):
        asked.append(questions[0]["question"])
        return ("answered", "把它拉到前台吧")

    pairs = [(_cand(1, "clicks=0", "Static", (1000, 500, 1100, 520)), object())]
    monkeypatch.setattr(screen, "blocking_ask", fake_ask)
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    monkeypatch.setattr(screen, "click_at", lambda *a, **k: True)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((20, 20)))
    monkeypatch.setattr(screen, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(
        screen, "_focused", lambda: {"name": "", "cls": "Static", "rect": (0, 0, 0, 0)}
    )
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "ensure_on_screen", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (900, 400, 1200, 600))
    screen._session.candidates = {1: pairs[0][0]}
    screen._session.candidates_hwnd = 42

    results = [
        str(screen._action_click({"hwnd": 42, "target": 1}, None, None, None, None))
        for _ in range(3)
    ]
    assert results[0].count("unverified") == 1 and results[1].count("unverified") == 1
    assert results[2].startswith("ESCALATED") and "把它拉到前台吧" in results[2]
    assert len(asked) == 1 and "连续 3 次没看到效果" in asked[0]
    assert screen._session.failures == {}  # the counter resets once the user answered


def test_type_pastes_verifies_and_survives_its_own_side_effect(monkeypatch):
    """`type` pastes (never types), verifies by reading the control back, and must
    not fall over *after* the paste already happened: a leftover attribute made the
    tool report an error while the text was already sitting in the chat box
    (2026-09-13, found while sending a real message through QQ)."""
    monkeypatch.setattr(screen, "_guarded_input", lambda hwnd: (True, ""))
    monkeypatch.setattr(screen, "wake_window", lambda hwnd, via="auto": [])
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((200, 100)))
    monkeypatch.setattr(screen, "_read_back_settled", lambda hwnd, target: ("你好", "Edit"))
    monkeypatch.setattr(screen, "snapshot_clipboard", lambda: screen.ClipSnapshot())
    pasted: list[str] = []
    monkeypatch.setattr(screen, "set_clipboard_text", lambda text: pasted.append(text) or True)
    monkeypatch.setattr(screen, "restore_clipboard", lambda snap: None)
    monkeypatch.setattr(screen, "send_keys", lambda names: (names, None))

    out = str(screen._action_type({"hwnd": 42, "text": "你好"}, None, None, None, None))
    assert pasted == ["你好"]  # the paste is the side effect
    assert "verified: read back '你好'" in out


def test_double_click_reports_the_window_it_opened(monkeypatch):
    """The open gesture's visible effect is usually a *new* window, not a repaint of
    the one that was clicked — double-clicking a desktop icon launches an app, and
    that is the whole point of having the gesture back (user, 2026-09-13)."""
    pairs = [(_cand(1, "微信", "", (1000, 500, 1100, 520)), object())]
    before = [
        screen.Win(42, "桌面", "SysListView32", (0, 0, 2240, 1400), 5, "explorer.exe", "normal")
    ]
    after = [
        *before,
        screen.Win(777, "记事本", "Notepad", (10, 20, 300, 400), 9, "notepad.exe", "normal"),
    ]
    calls = []
    gestures: list = []

    def fake_windows(include_hidden=False):
        calls.append(1)
        return before if len(calls) == 1 else after

    monkeypatch.setattr(screen, "list_windows", fake_windows)
    monkeypatch.setattr(screen, "_scan", lambda hwnd, limit=screen.MAX_CANDIDATES: pairs)
    monkeypatch.setattr(
        screen, "click_at", lambda *a, **k: gestures.append(k.get("clicks")) or True
    )
    monkeypatch.setattr(screen, "grab_window", lambda hwnd: _frame((20, 20)))
    monkeypatch.setattr(screen, "set_foreground", lambda hwnd: True)
    monkeypatch.setattr(screen, "_focused", lambda: {"name": "", "cls": "Static", "rect": (0,) * 4})
    monkeypatch.setattr(screen, "window_state", lambda hwnd: "normal")
    monkeypatch.setattr(screen, "capture_problem", lambda hwnd: None)
    monkeypatch.setattr(screen, "_guarded_input", lambda hwnd: (True, ""))
    monkeypatch.setattr(screen, "window_rect", lambda hwnd: (900, 400, 1200, 600))
    monkeypatch.setattr(screen, "_window_text", lambda hwnd: "记事本" if hwnd == 777 else "桌面")
    screen._session.candidates = {1: pairs[0][0]}
    screen._session.candidates_hwnd = 42

    out = str(screen._action_double_click({"hwnd": 42, "target": 1}, None, None, None, None))
    assert gestures == [2]  # two press/release pairs reach the mouse, not one
    assert out.startswith("DOUBLE_CLICK") and "opened '记事本'" in out and "verified" in out


# ── the gate: the tool does not exist until the user turns it on ───────────
def test_the_tool_refuses_everything_until_pc_control_is_on():
    out = screen._run(Config(), None, {"action": "windows"})
    assert out.startswith("ERROR: screen control is off")


def test_an_unknown_action_says_what_is_available():
    cfg = Config()
    cfg.pc_control = True
    out = screen._run(cfg, None, {"action": "teleport"})
    assert "unknown action" in out and "restore" in out


def test_every_input_action_insists_on_a_window_id():
    cfg = Config()
    cfg.pc_control = True
    for action in ("click", "double_click", "type", "key", "scroll", "restore"):
        out = screen._run(cfg, None, {"action": action})
        assert "needs hwnd" in out, action


def test_the_tool_offers_the_actions_the_spec_promises():
    actions = screen.SCHEMA["function"]["parameters"]["properties"]["action"]["enum"]
    assert set(actions) == {
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
    }
    assert "target" in screen.SCHEMA["function"]["parameters"]["properties"]
    # no coordinates in the schema: the pixel is never the model's to choose
    assert "x" not in screen.SCHEMA["function"]["parameters"]["properties"]
