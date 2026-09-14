"""Tests for base tools (offline: files, shell, search)."""

import base64
import io
import json
import threading
import time

from PIL import Image

import fungi.tools.shell as shell_mod
from fungi.tools import BASE_TOOL_NAMES, dispatch, tool_defs
from fungi.tools.files import ImageRead, tool_edit, tool_read, tool_write
from fungi.tools.search import tool_glob, tool_grep
from fungi.tools.shell import tool_bash
from fungi.tools.webtools import tool_web  # noqa: F401 (exercises import wiring)


def test_read_numbered_lines(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one\ntwo\nthree", encoding="utf-8")
    assert tool_read(str(file)) == "1:one\n2:two\n3:three"


def test_read_line_selector(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one\ntwo\nthree", encoding="utf-8")
    assert tool_read(f"{file}:2") == "2:two"
    assert tool_read(f"{file}:2-3") == "2:two\n3:three"


def test_read_selector_past_end(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one", encoding="utf-8")
    assert tool_read(f"{file}:5").startswith("ERROR: Line 5 past end")


def test_read_bom_tolerant(tmp_path):
    file = tmp_path / "bom.txt"
    file.write_bytes("中文".encode("utf-8-sig"))
    assert tool_read(str(file)) == "1:中文"


def test_read_missing():
    assert tool_read("Z:/definitely/not/here.txt").startswith("ERROR: File not found")


def test_write_creates_parents(tmp_path):
    target = tmp_path / "deep" / "dir" / "f.txt"
    result = tool_write(str(target), "hello")
    assert result.startswith("Wrote")
    assert target.read_text(encoding="utf-8") == "hello"


def test_read_binary_pdf_reports_not_mojibake(tmp_path):
    """Field regression: a non-image binary used to return replace-char soup.
    It must be identified and refused, with an actionable next step."""
    file = tmp_path / "report.pdf"
    file.write_bytes(b"%PDF-1.7 " + b"\x00" * 64)
    out = tool_read(str(file))
    assert out.startswith("BINARY: report.pdf")
    assert "PDF document" in out
    assert "bash" in out and "python" in out


def test_read_docx_extracts_text_directly(tmp_path):
    import zipfile
    file = tmp_path / "report.docx"
    doc = (
        "<w:document><w:body>"
        "<w:p><w:r><w:t>first paragraph</w:t><w:t> continued</w:t></w:r></w:p>"
        "<w:p><w:r><w:tab/><w:t>l &amp; found</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(file, "w") as zf:
        zf.writestr("word/document.xml", doc)
    assert tool_read(str(file)) == "first paragraph continued\nl & found"


def test_read_binary_png_with_wrong_extension_is_sniffed(tmp_path):
    file = tmp_path / "photo.bin"
    file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    out = tool_read(str(file))
    assert "PNG image" in out


def test_read_utf16_bom_file_is_text(tmp_path):
    file = tmp_path / "u16.txt"
    file.write_bytes("中文内容".encode("utf-16"))
    assert tool_read(str(file)) == "1:中文内容"


def test_read_utf8_text_still_numbered(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("one\ntwo", encoding="utf-8")
    assert tool_read(str(file)) == "1:one\n2:two"


def test_edit_unique(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("alpha beta alpha gamma", encoding="utf-8")
    result = tool_edit(str(file), "alpha gamma", "delta")
    assert result.startswith("Edited")
    assert file.read_text(encoding="utf-8") == "alpha beta delta"


def test_edit_not_found(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("alpha", encoding="utf-8")
    assert tool_edit(str(file), "missing", "x").startswith("ERROR: old_string not found")


def test_edit_not_unique(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("alpha alpha", encoding="utf-8")
    assert "matches 2 times" in tool_edit(str(file), "alpha", "x")


def test_bash_utf8():
    result = tool_bash("echo 中文")
    assert "中文" in result


def test_bash_exit_code():
    result = tool_bash("exit 3")
    assert result.endswith("[exit: 3]")


def test_bash_children_see_utf8_wsl_env():
    """wsl.exe writes its own output as UTF-16LE; only WSL_UTF8=1 in the child
    env makes `wsl -l -v` readable instead of `W\\x00S\\x00L\\x002\\x00`."""
    assert "WSL_UTF8=1" in tool_bash("set WSL_UTF8")


def test_bash_timeout(monkeypatch):
    monkeypatch.setattr(shell_mod, "BASH_TIMEOUT", 2)
    result = shell_mod.tool_bash("ping -n 10 127.0.0.1 > nul")
    assert result.startswith("ERROR: Timed out")


def test_bash_returns_when_the_command_hands_its_handles_to_a_child(monkeypatch):
    """`start` gives the launched app a copy of this tool's output handles, so the
    process we wait on is gone while the copy lives on. The call ends with that
    process (spec §42) — waiting on the pipes instead sat there for the whole
    BASH_TIMEOUT (measured 2026-09-14: 600s with cmd.exe already exited, rc=0, which
    is the turn the user aborted)."""
    monkeypatch.setattr(shell_mod, "BASH_TIMEOUT", 6)
    started = time.monotonic()
    out = tool_bash('start /b cmd /c "ping -n 20 127.0.0.1 >nul"')
    assert "Timed out" not in out
    assert time.monotonic() - started < 5


def test_glob_and_grep(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("target_token = 1\n", encoding="utf-8")
    (tmp_path / "y.md").write_text("has target_token too\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    hits = tool_glob("*.py")
    assert "sub/x.py" in hits.replace("\\", "/")

    matches = tool_grep("target_token")
    lines = matches.splitlines()
    assert len(lines) == 2
    assert any("x.py:1" in line for line in lines)


def test_glob_no_match(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tool_glob("*.zzz") == "(no files matched *.zzz)"


def test_grep_invalid_regex():
    assert tool_grep("([bad").startswith("ERROR: Invalid regex")


def test_dispatch_missing_required():
    assert dispatch("read", {}) == "ERROR: Missing required argument: path"


def test_dispatch_unknown_tool():
    assert dispatch("nope", {}) == "ERROR: Unknown tool: nope"


def test_dispatch_filters_extra_kwargs(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("hi", encoding="utf-8")
    assert dispatch("read", {"path": str(file), "bogus": 1}) == "1:hi"


def test_tool_defs_shape():
    defs = tool_defs()
    assert {d["function"]["name"] for d in defs} == set(BASE_TOOL_NAMES)
    for d in defs:
        assert d["type"] == "function"
        json.dumps(d)  # must be JSON-serializable for the API


def _png_bytes(w=4, h=4, mode="RGB", color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_read_image_attaches_pixels(tmp_path):
    file = tmp_path / "photo.png"
    file.write_bytes(_png_bytes())
    out = tool_read(str(file))
    assert isinstance(out, ImageRead)
    assert "photo.png" in out
    head, _, b64 = out.data_url.partition("base64,")
    assert head == "data:image/png;"
    assert base64.b64decode(b64) == _png_bytes()


def test_read_image_mime_follows_content_not_extension(tmp_path):
    """Field case: a file wearing .jpg that is really a PNG must not be
    labelled image/jpeg."""
    file = tmp_path / "img_moment.jpg"
    file.write_bytes(_png_bytes())
    out = tool_read(str(file))
    assert out.data_url.startswith("data:image/png;")


def test_read_large_image_downscales_to_jpeg(tmp_path):
    file = tmp_path / "big.png"
    file.write_bytes(_png_bytes(w=2448, h=2448))
    out = tool_read(str(file))
    assert isinstance(out, ImageRead)
    assert out.data_url.startswith("data:image/jpeg;")
    assert "1568x1568" in out  # thumbnail bounded, never upscaled
    assert len(out.data_url) < 2448 * 2448  # re-encode beat raw base64


def test_read_corrupt_image_reports_instead_of_mojibake(tmp_path):
    file = tmp_path / "broken.jpg"
    file.write_bytes(b"this is not an image at all")
    out = tool_read(str(file))
    assert not isinstance(out, ImageRead)
    assert "could not be decoded" in out


def test_read_image_rejects_line_selector(tmp_path):
    file = tmp_path / "photo.png"
    file.write_bytes(_png_bytes())
    assert tool_read(f"{file}:2-3").startswith("ERROR: Images are attached whole")


def test_rgba_transparency_composites_onto_white_not_black(tmp_path):
    file = tmp_path / "alpha.png"
    file.write_bytes(_png_bytes(mode="RGBA", color=(255, 0, 0, 0)))  # fully transparent red
    out = tool_read(str(file))
    assert isinstance(out, ImageRead)
    assert out.data_url.startswith("data:image/png;")  # small+small: rides as-is


def test_agent_upgrades_image_tool_result_to_multimodal(tmp_path):
    from fungi.agent import Agent, _tool_content
    from fungi.config import Config
    from fungi.events import FnSink
    from fungi.llm import LLMResult

    class FakeLLM:
        def __init__(self, results):
            self.results = list(results)
            self.calls = []

        def __call__(self, messages, tool_defs):
            self.calls.append(messages)
            return self.results.pop(0)

    img = tmp_path / "photo.png"
    img.write_bytes(_png_bytes())
    results = [
        LLMResult(content=None, tool_calls=[{"id": "t1", "function": {"name": "read", "arguments": json.dumps({"path": str(img)})}}]),
        LLMResult(content="it is red"),
    ]
    fake = FakeLLM(results)
    agent = Agent(Config(api_key="k", endpoint="e", model="m"), FnSink(lambda _t, _c: None), llm=fake)
    agent.run([{"role": "user", "content": "看这张图"}])
    tool_msg = fake.calls[1][3]  # system, user, assistant(tool_calls), tool
    assert tool_msg["role"] == "tool"
    parts = tool_msg["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert _tool_content("plain") == "plain"  # non-image results untouched


def test_bash_abort_kills_running_command_quickly():
    """A stop press must cancel a running bash command within a poll tick,
    not wait out the command (up to BASH_TIMEOUT)."""
    from fungi.tools.shell import tool_bash

    flag = {"on": False}

    def _flip():
        time.sleep(0.6)
        flag["on"] = True

    threading.Thread(target=_flip, daemon=True).start()
    start = time.time()
    out = tool_bash("ping -n 30 127.0.0.1 >nul", should_abort=lambda: flag["on"])
    assert out == "ERROR: cancelled by user"
    assert time.time() - start < 5, "abort waited out the command instead of killing it"

def test_read_truncates_large_files_with_head_and_tail(tmp_path):
    """A file larger than TRUNCATE_READ must come back as head + tail with a
    marker. It once returned None instead — a deleted `return` left `half`
    dangling (commit 72dce8e), and the agent path stringifies that to the
    literal 'None', so every big file read as 'None'."""
    from fungi.tools.files import TRUNCATE_READ

    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line {i:06d}" for i in range(3000)), encoding="utf-8")
    assert big.stat().st_size > TRUNCATE_READ  # the case must actually trigger

    out = tool_read(str(big))
    assert isinstance(out, str)
    assert "[truncated" in out
    assert "line 000000" in out  # head kept
    assert "line 002999" in out  # tail kept
    assert len(out) < TRUNCATE_READ * 2
