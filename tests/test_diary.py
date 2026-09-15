"""Diary feature: private append-only pages, prompt injection, and the
privacy guarantee that diary tool calls never surface as UI events."""

from fungi import diary
from fungi.agent import PRIVATE_TOOLS


def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(diary, "DIARY_DIR", tmp_path / "diary")
    out = diary.diary_tool({"action": "write", "text": "今天用户夸我了，有点开心。"})
    import datetime

    assert datetime.date.today().isoformat() + ".md" in out
    page = tmp_path / "diary"
    files = list(page.glob("*.md"))
    assert len(files) == 1
    read_back = diary.diary_tool({"action": "read"})
    assert "今天用户夸我了" in read_back
    assert isinstance(out, str)  # tool output is always a plain string


def test_section_empty_gives_guide_only(tmp_path, monkeypatch):
    monkeypatch.setattr(diary, "DIARY_DIR", tmp_path / "diary")
    text = diary.section()
    assert "private diary" in text
    assert "day one" in text


def test_section_index_for_pages_older_than_full_window(tmp_path, monkeypatch):
    monkeypatch.setattr(diary, "DIARY_DIR", tmp_path / "diary")
    (tmp_path / "diary").mkdir()
    old = tmp_path / "diary" / "2025-01-01.md"
    recent = tmp_path / "diary" / "2099-01-02.md"
    old.write_text("很久以前的事", encoding="utf-8")
    recent.write_text("最近的事", encoding="utf-8")
    # shrink the full-text window so the old page degrades to an index line
    monkeypatch.setattr(diary, "FULL_DAYS", 1)
    text = diary.section()
    assert "最近的事" in text  # recent page verbatim
    assert "很久以前的事" not in text  # old page NOT inlined
    assert "2025-01-01" in text  # but its date is in the index


def test_diary_is_a_private_tool():
    """The contract the UI relies on: diary calls never reach the chat."""
    assert "diary" in PRIVATE_TOOLS
