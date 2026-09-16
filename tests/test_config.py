"""fungi/config.py: where the version lives, the two roots it resolves, and the
config file a hand-edit (or Explorer) can leave unreadable."""

import json
import tomllib
from pathlib import Path

from fungi import config


def test_local_version_reads_pyproject():
    # reads the real pyproject.toml; compare against the file, never a pinned
    # version (that broke the suite on every release bump)
    with Path("pyproject.toml").open("rb") as fh:
        expected = tomllib.load(fh)["project"]["version"]
    assert config.local_version() == expected


def test_local_version_frozen_uses_resource_root(monkeypatch, tmp_path):
    bundled = tmp_path / "pyproject.toml"
    bundled.write_text('[project]\nname = "fungi"\nversion = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(config, "RESOURCE_ROOT", bundled.parent)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "nowhere")
    assert config.local_version() == "9.9.9"


def test_a_path_pasted_from_explorer_is_read_anyway(tmp_path, capsys):
    """Explorer's "Copy as path" gives single backslashes, which JSON rejects.

    That must not cost the user their whole config (api key, model, switches) —
    it is repaired for reading, and said out loud once.
    """
    p = tmp_path / "config.json"
    p.write_text(
        '{"model": "m", "ghostworld": true, "ghostworld_dir": "C:\\Users\\me\\GhostWorld"}',
        encoding="utf-8",
    )

    cfg = config.load_config(p)

    assert cfg.model == "m", "one Windows path must not discard the rest of the file"
    assert cfg.ghostworld is True
    assert cfg.ghostworld_dir == "C:\\Users\\me\\GhostWorld", "the path is taken literally"
    err = capsys.readouterr().err
    assert "没转义" in err and str(p) in err
    # `load_config` runs on every tool call: the same complaint must not repeat.
    config.load_config(p)
    config.load_config(p)
    assert capsys.readouterr().err == "", "said once, not on every load"


def test_repair_leaves_real_escapes_alone(tmp_path, capsys):
    """Only the backslashes JSON rejects are doubled: \\n stays a newline."""
    p = tmp_path / "config.json"
    p.write_text(
        '{"courier_memory": "第一行\\n第二行", "ghostworld_dir": "C:\\Users\\me\\gw"}',
        encoding="utf-8",
    )

    cfg = config.load_config(p)

    assert cfg.courier_memory == "第一行\n第二行", "a real escape sequence must keep its meaning"
    assert cfg.ghostworld_dir == "C:\\Users\\me\\gw"
    capsys.readouterr()


def test_a_config_broken_beyond_repair_is_reported(tmp_path, capsys):
    """Nothing to repair: fall back to defaults, and say so instead of pretending."""
    p = tmp_path / "config.json"
    p.write_text('{"model": "m"', encoding="utf-8")  # unterminated

    cfg = config.load_config(p)

    assert cfg.model != "m"
    assert "不是合法 JSON" in capsys.readouterr().err


def test_a_windows_path_with_forward_slashes_is_accepted(tmp_path, capsys):
    """Also fine, and silent — a valid file has nothing to report."""
    p = tmp_path / "config.json"
    p.write_text(
        '{"ghostworld": true, "ghostworld_dir": "C:/Users/me/GhostWorld"}', encoding="utf-8"
    )

    cfg = config.load_config(p)

    assert cfg.ghostworld is True
    assert cfg.ghostworld_dir == "C:/Users/me/GhostWorld"
    assert capsys.readouterr().err == ""


def test_normalize_dir_takes_a_path_the_way_windows_hands_it_over():
    """右键"复制文件地址"给的是带引号、单反斜杠的字符串；手打的也是单反斜杠。"""
    assert config.normalize_dir("C:\\Users\\me\\GhostWorld") == ("C:/Users/me/GhostWorld", True)
    assert config.normalize_dir('"C:\\Users\\me\\GhostWorld"') == ("C:/Users/me/GhostWorld", True)
    assert config.normalize_dir("  C:/Users/me/GhostWorld  ") == ("C:/Users/me/GhostWorld", True)
    assert config.normalize_dir("C:/Users/me/GhostWorld") == ("C:/Users/me/GhostWorld", False)
    assert config.normalize_dir("") == ("", False)


def test_repair_config_file_rewrites_an_unreadable_file(tmp_path):
    """文件已经坏了的时候，打开设置页就该把它改回合法 JSON。"""
    p = tmp_path / "config.json"
    p.write_text(
        '{"api_key": "k", "ghostworld": true, "ghostworld_dir": "C:\\Users\\me\\GhostWorld",'
        ' "future_key": [1, 2]}',
        encoding="utf-8",
    )

    note = config.repair_config_file(p)

    assert note and "config.json" in note
    data = json.loads(p.read_text(encoding="utf-8"))  # 现在读得出来了
    assert data["ghostworld"] is True
    assert data["api_key"] == "k"
    assert data["ghostworld_dir"] == "C:/Users/me/GhostWorld", "路径顺手规范成正斜杠"
    assert data["future_key"] == [1, 2], "这一版不认识的键必须原样留着"
    assert config.repair_config_file(p) is None, "已经合法的文件不再动它"


def test_repair_config_file_leaves_files_it_cannot_fix(tmp_path):
    """不是那一类错误就别猜：少个括号的文件原样不动，报给用户就好。"""
    p = tmp_path / "config.json"
    p.write_text('{"model": "m"', encoding="utf-8")
    before = p.read_bytes()

    assert config.repair_config_file(p) is None

    assert p.read_bytes() == before


def test_repair_config_file_is_quiet_when_there_is_nothing_to_do(tmp_path):
    assert config.repair_config_file(tmp_path / "missing.json") is None
    good = tmp_path / "good.json"
    good.write_text('{"model": "m"}', encoding="utf-8")
    assert config.repair_config_file(good) is None
    assert json.loads(good.read_text(encoding="utf-8"))["model"] == "m"
