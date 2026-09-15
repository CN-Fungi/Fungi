"""Video tool: subprocess wiring against a fake VidSense checkout (no torch)."""

import base64
import subprocess
import textwrap
from pathlib import Path

import pytest

from fungi.agent import _tool_content
from fungi.tools import TOOLS, dispatch, tool_defs
from fungi.tools import video as video_mod
from fungi.tools.files import ImageRead
from fungi.tools.video import tool_video

FAKE_CARD = {
    "video_id": "clip",
    "duration": 12.0,
    "fps_sampled": 1.0,
    "scenes": [{"start": 0.0, "end": 12.0}],
    "keyframes": [
        {"id": 0, "t": 2.0, "segment_id": 0, "caption": ""},
        {"id": 1, "t": 9.0, "segment_id": 1, "caption": ""},
    ],
    "segments": [
        {
            "id": 0,
            "start": 0.0,
            "end": 5.0,
            "text": "hello world",
            "speaker": None,
            "words": [],
            "frame_id": 2,
            "scene_index": 0,
        },
        {
            "id": 1,
            "start": 5.0,
            "end": 12.0,
            "text": "goodbye",
            "speaker": "host",
            "words": [],
            "frame_id": 9,
            "scene_index": 0,
        },
    ],
    "embeddings": [],
}

FAKE_CLI = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    args = sys.argv[1:]
    if "--no-api" not in args:
        print("unexpected args", file=sys.stderr); raise SystemExit(1)
    video = Path([a for a in args if not a.startswith("-")][0])
    jdir = Path("output/json"); jdir.mkdir(parents=True, exist_ok=True)
    (jdir / f"{video.stem}_eventcard.json").write_text(json.dumps(CARD), encoding="utf-8")
    print("ok")
""")

# every component _video_ready() checks, all present
_ALL_READY = {
    "ffmpeg": True,
    "huggingface_hub": True,
    "torch": True,
    "transformers": True,
    "faster_whisper": True,
    "opencv": True,
    "CLIP": True,
    "whisper": True,
}


@pytest.fixture()
def vidsense_env(tmp_path, monkeypatch):
    """Fake vidsense/ package at PROJECT_ROOT (cwd of the subprocess); the
    vendored real package is never imported - the fake CLI writes the card."""
    root = tmp_path
    (root / "vidsense").mkdir()
    (root / "vidsense" / "__init__.py").write_text("", encoding="utf-8")
    (root / "vidsense" / "cli.py").write_text(
        "CARD = " + repr(FAKE_CARD) + "\n" + FAKE_CLI, encoding="utf-8"
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00fake mp4")
    monkeypatch.setattr("fungi.tools.video.PROJECT_ROOT", root)
    # these tests fake a working VidSense checkout; pretend the whole runtime
    # (libs + ffmpeg + HF weights) is in place (dedicated tests below cover gaps)
    monkeypatch.setattr(
        "fungi.tools.video._video_ready",
        lambda: dict(_ALL_READY),
    )

    def fake_extract(video_path, timestamps, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, _t in enumerate(timestamps):
            dest = out_dir / f"kf{i:02d}.jpg"
            dest.write_bytes(b"fakejpeg%d" % i)
            paths.append(dest)
        return paths

    monkeypatch.setattr("fungi.tools.video._extract_keyframes", fake_extract)
    return root, video


def test_video_returns_transcript_plus_keyframe_pixels(vidsense_env):
    _root, video = vidsense_env
    out = tool_video(str(video))
    assert isinstance(out, ImageRead)
    assert "[    0.0-    5.0] hello world" in out
    assert "[host] goodbye" in out
    assert len(out.data_urls) == 2
    assert base64.b64decode(out.data_urls[0].partition("base64,")[2]) == b"fakejpeg0"
    assert out.data_url == out.data_urls[0]


def test_video_multi_image_tool_content_upgrade(vidsense_env):
    _root, video = vidsense_env
    out = tool_video(str(video))
    parts = _tool_content(out)
    assert parts[0] == {"type": "text", "text": str(out)}
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    assert _tool_content("plain text") == "plain text"


def test_video_missing_file(vidsense_env):
    root, _video = vidsense_env
    out = tool_video(str(root / "nope.mp4"))
    assert out.startswith("ERROR: File not found")


def test_video_surfaces_vidsense_stderr(vidsense_env):
    root, video = vidsense_env
    # a fake CLI that always fails: stderr tail must reach the agent
    (root / "vidsense" / "cli.py").write_text(
        'raise SystemExit("boom: no ffmpeg")', encoding="utf-8"
    )
    out = tool_video(str(video))
    assert out.startswith("ERROR: VidSense failed")
    assert "boom" in out


def test_video_cjk_path_runs_on_ascii_copy(vidsense_env, tmp_path, monkeypatch):
    """ffmpeg fails to decode inputs whose path contains CJK characters on
    this box: the tool must hand the subprocess an ASCII-named copy while the
    result still names the original file."""
    import fungi.tools.video as vt

    _root, video = vidsense_env
    cjk = tmp_path / "中文视频.mp4"
    cjk.write_bytes(video.read_bytes())
    argvs = []
    real_popen = vt.subprocess.Popen

    def spy(argv, **kw):
        argvs.append(argv)
        return real_popen(argv, **kw)

    monkeypatch.setattr(vt.subprocess, "Popen", spy)
    out = tool_video(str(cjk))
    assert isinstance(out, ImageRead)
    assert "[    0.0-    5.0] hello world" in out
    assert "中文视频" in str(out)  # the user-facing name survives the copy
    assert len(argvs) == 1
    video_arg = argvs[0][3]
    assert video_arg.isascii() and video_arg.endswith(".mp4")
    assert str(cjk) not in video_arg


def test_video_registered_and_dispatchable():
    names = {d["function"]["name"] for d in tool_defs()}
    assert "video" in names
    assert dispatch("video", {}).startswith("ERROR: Missing required argument")


def test_video_missing_model_refuses_and_points_to_downloader(vidsense_env, monkeypatch):
    """The tool never downloads on demand: missing weights -> guidance ERROR."""
    _root, video = vidsense_env
    ready = dict(_ALL_READY) | {"whisper": False}
    monkeypatch.setattr("fungi.tools.video._video_ready", lambda: ready)
    out = tool_video(str(video))
    assert out.startswith("ERROR: video not ready")
    assert "download_video_models.py" in out and "whisper" in out


def test_video_missing_non_healable_offers_opt_in_setup_recipe(vidsense_env, monkeypatch):
    """torch/ffmpeg missing: opt-in recipe (user agrees first), pip + mirror,
    no model-download step when weights are all cached."""
    _root, video = vidsense_env
    ready = dict(_ALL_READY) | {"torch": False, "ffmpeg": False}
    monkeypatch.setattr("fungi.tools.video._video_ready", lambda: ready)
    out = tool_video(str(video))
    assert out.startswith("ERROR: video not ready, missing: ffmpeg, torch")
    assert "ONLY after the user agrees" in out  # 不强制装
    assert "static-ffmpeg" in out  # ffmpeg 经 pip 配齐
    assert "tuna.tsinghua.edu.cn" in out  # 镜像
    assert "download_video_models" not in out  # 权重都在: 无脚本步骤
    assert "faster-whisper" not in out  # 没缺就不装


def test_model_cached_reads_hf_snapshot_layout(tmp_path, monkeypatch):
    """Cache check: env-driven root, models--*--* snapshot dirs, size floor."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(video_mod, "_MODEL_MIN_BYTES", 8)
    slug = "models--openai--clip-vit-base-patch32"
    snap = tmp_path / slug / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    assert not video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
    (snap / "model.safetensors").write_bytes(b"x" * 16)
    assert video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
    # below the size floor (truncated download) -> treated as missing
    (snap / "model.safetensors").write_bytes(b"x" * 4)
    assert not video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))
    # absent cache root -> missing, never raises
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "nope"))
    assert not video_mod._model_cached("openai/clip-vit-base-patch32", ("model.safetensors",))


def test_video_demo_path_runs_full_pipeline(vidsense_env, monkeypatch):
    """path "demo" = built-in self-test: routes the generated clip through the
    normal subprocess pipeline, so one successful call proves readiness."""
    _root, video = vidsense_env
    monkeypatch.setattr("fungi.tools.video._demo_video", lambda: video)
    out = tool_video("demo")
    assert isinstance(out, ImageRead) and "hello world" in out


def test_demo_video_generates_once_then_caches(tmp_path, monkeypatch):
    """_demo_video: ffmpeg lavfi generation is cached under PROJECT_ROOT/data."""
    monkeypatch.setattr("fungi.tools.video.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("fungi.tools.video.shutil.which", lambda _name: "C:/ffmpeg/ffmpeg.exe")
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake mp4")

    monkeypatch.setattr("fungi.tools.video.subprocess.run", fake_run)
    first = video_mod._demo_video()
    second = video_mod._demo_video()
    assert first == second == tmp_path / "data" / "demo_video.mp4"
    assert len(calls) == 1 and calls[0][0] == "C:/ffmpeg/ffmpeg.exe"
    assert any("testsrc2" in a for a in calls[0]) and any("sine" in a for a in calls[0])


class _FakeVidsenseProc:
    """Stand-in for BOTH the main VidSense proc (communicate always times out)
    and the taskkill child that _kill_tree spawns via subprocess.run (run()
    uses it as a context manager and calls poll() afterwards)."""

    returncode = 0

    def __init__(self, cmd, killed=None):
        self.pid = 4242
        self.cmd = cmd
        self.args = cmd
        self.killed = killed if killed is not None else []
        self._main = "taskkill" not in cmd

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def communicate(self, *_args, **_kw):
        if self._main:
            raise subprocess.TimeoutExpired(cmd="vidsense", timeout=1)
        return "", ""

    def poll(self):
        return 0

    def kill(self):
        self.killed.append(self.pid)


def test_video_cancelled_midrun_kills_process_tree(vidsense_env, monkeypatch):
    """should_abort -> VidSense subprocess tree killed within a poll tick."""
    _root, video = vidsense_env
    spawned = []
    killed = []

    def fake_popen(cmd, **_kw):
        spawned.append(cmd)
        return _FakeVidsenseProc(cmd, killed)

    monkeypatch.setattr("fungi.tools.video.subprocess.Popen", fake_popen)
    monkeypatch.setattr("fungi.tools.video.time.monotonic", lambda: 0.0)
    out = tool_video(str(video), should_abort=lambda: True)
    assert out == "ERROR: cancelled by user"
    assert spawned[0][1:3] == ["-m", "vidsense.cli"] and killed == [4242]


def test_video_still_times_out_when_not_aborted(vidsense_env, monkeypatch):
    _root, video = vidsense_env
    ticks = iter([0.0] * 3 + [video_mod._VIDEO_TIMEOUT_S + 1])
    monkeypatch.setattr("fungi.tools.video.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        "fungi.tools.video.subprocess.Popen", lambda cmd, **_kw: _FakeVidsenseProc(cmd)
    )
    out = tool_video(str(video))
    assert out.startswith("ERROR: VidSense timed out")


def test_dispatch_injects_should_abort_only_when_accepted(monkeypatch):
    """Tools declaring `should_abort` get the predicate; others stay untouched."""

    captured = {}

    def fake_video(path, should_abort=None):
        captured["abort"] = should_abort
        captured["path"] = path
        return "ok"

    monkeypatch.setitem(TOOLS, "video", {"schema": TOOLS["video"]["schema"], "fn": fake_video})
    out = dispatch("video", {"path": "x"}, should_abort=lambda: True)
    assert out == "ok" and captured["abort"]() is True

    captured_plain = {}

    def fake_read(path):
        captured_plain["called"] = True
        captured_plain["path"] = path
        return "text"

    monkeypatch.setitem(TOOLS, "read", {"schema": TOOLS["read"]["schema"], "fn": fake_read})
    assert dispatch("read", {"path": "x"}, should_abort=lambda: True) == "text"
    assert captured_plain == {"called": True, "path": "x"}
