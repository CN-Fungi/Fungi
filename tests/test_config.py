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


def test_the_retired_max_file_mb_key_is_reported_not_swallowed(tmp_path, capsys):
    """Spec §48: transfers are not capped any more. A config still carrying the
    old key must say so once — someone who set it small to protect their disk
    cannot be left believing it still applies."""
    p = tmp_path / "config.json"
    p.write_text('{"model": "m", "max_file_mb": 20}', encoding="utf-8")
    config._warned.discard("max_file_mb")  # the once-per-process guard is global

    cfg = config.load_config(p)

    assert cfg.model == "m"
    assert not hasattr(cfg, "max_file_mb"), "the knob is gone, not merely ignored"
    said = capsys.readouterr().err
    assert "max_file_mb" in said and "废止" in said

    capsys.readouterr()  # drain: the next load must be quiet
    config.load_config(p)
    assert capsys.readouterr().err == "", "a warning per load would be noise"


def test_the_model_trio_comes_from_the_file_and_never_from_the_environment(tmp_path, monkeypatch):
    """Spec §65, 用户 2026-09-25 的裁决：模型三件套只读 config.json。

    这台机器的 User 级 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 是别的 agent 接小米 MiMo 的
    **通用名字**；以前 `load_config` 让 `OPENAI_API_KEY` 压过文件，于是 Fungi 拿别人的钥匙去敲
    config.json 里的 DeepSeek 端点 —— 401 里那把钥匙（`****sovy`）根本不在任何 config.json 里。
    现在：环境里有什么都不算数，配置只有一个来源。
    """
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "api_key": "sk-file-key",
                "endpoint": "https://file.example/v1",
                "model": "file-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-somebody-elses-key")
    monkeypatch.setenv("OPENAI_ENDPOINT", "https://somebody-elses.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "somebody-elses-model")

    cfg = config.load_config(p)

    assert (cfg.api_key, cfg.endpoint, cfg.model) == (
        "sk-file-key",
        "https://file.example/v1",
        "file-model",
    )


def test_the_model_list_keeps_every_name_the_box_ever_added(tmp_path):
    """§66（用户 2026-09-25「原先的输入框从覆盖变成添加，如果与之前模型不一样就新添」）：
    下拉列表是一份越长越长的备选，切换只是把它里面的某个名字挪到最前，谁都不丢。"""
    p = tmp_path / "config.json"
    config.save_config(config.Config(api_key="k", endpoint="e", model="m1"), p)
    # 只有一个名字时根本不写这个键：不去动用户的 config.json
    assert "model_list" not in json.loads(p.read_text(encoding="utf-8"))

    cfg = config.load_config(p)
    assert config.remember_model(cfg, "m2") is True, "新名字：算添了一行"
    config.save_config(cfg, p)
    assert json.loads(p.read_text(encoding="utf-8"))["model_list"] == ["m2", "m1"]

    cfg = config.load_config(p)
    assert (cfg.model, cfg.model_list) == ("m2", ["m2", "m1"])
    assert config.remember_model(cfg, "m2") is False, "同一个名字再来一次不加第二行"
    assert cfg.model_list == ["m2", "m1"], "顺序也不动"
    assert config.remember_model(cfg, "m1") is False, "切回旧的那个：它还在"
    assert (cfg.model, cfg.model_list) == ("m1", ["m1", "m2"])


def test_the_model_in_use_is_always_one_of_the_pickable_ones(tmp_path):
    """手改过的 config.json 只写了 model、没写 model_list：下拉列表不能开着是空的。"""
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"api_key": "k", "endpoint": "e", "model": "typed-by-hand"}),
        encoding="utf-8",
    )
    cfg = config.load_config(p)
    assert (cfg.model, cfg.model_list) == ("typed-by-hand", ["typed-by-hand"])


def test_a_hand_edited_list_does_not_get_two_identical_rows(tmp_path):
    """盘里手写重复项、空串，或者 model 不在列表里：读进来就是能给下拉列表用的那三行。"""
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "api_key": "k",
                "endpoint": "e",
                "model": "m2",
                "model_list": ["m1", "m1", "", "  ", "m2"],
            }
        ),
        encoding="utf-8",
    )
    cfg = config.load_config(p)
    assert cfg.model_list == ["m1", "m2"], "去重、去空、model 在最前"

    assert config.remember_model(cfg, "") is False, "空名字什么也不干"
    assert cfg.model_list == ["m1", "m2"]


def test_a_model_remembers_the_endpoint_and_key_it_answered_on(tmp_path):
    """§68（用户 2026-09-25「每次调用 200 以后，之后切换模型应该随之切换 url 和 key」）：
    一个列表里可以住两家——每个名字记着自己那套端点+密钥，切换时整套跟着走。"""
    p = tmp_path / "config.json"
    deepseek = ("https://api.deepseek.com/chat/completions", "sk-deepseek")
    mimo = ("https://api.xiaomimimo.com/v1/chat/completions", "sk-mimo")
    # 先落一次盘再读回来（真实路径：`load_config` 会把在用的那个模型放进列表，
    # 之后切换才谈得上「旧名字还在不在」）
    config.save_config(
        config.Config(api_key=deepseek[1], endpoint=deepseek[0], model="deepseek-x"), p
    )
    cfg = config.load_config(p)
    config.remember_provider(cfg, "deepseek-x", *deepseek)
    config.switch_model(cfg, "mimo-v2.6-flash")
    config.remember_provider(cfg, "mimo-v2.6-flash", *mimo)
    assert config.adopt_provider(cfg, "mimo-v2.6-flash") is True, "刚学会的那套现在就得上岗"
    config.save_config(cfg, p)

    assert json.loads(p.read_text(encoding="utf-8"))["model_providers"] == {
        "deepseek-x": {"endpoint": deepseek[0], "api_key": deepseek[1]},
        "mimo-v2.6-flash": {"endpoint": mimo[0], "api_key": mimo[1]},
    }

    # 关掉程序再打开：切回 deepseek 那个名字，url+key 自己回来（不用重新糊一遍）
    cfg = config.load_config(p)
    assert (cfg.model, cfg.endpoint, cfg.api_key) == ("mimo-v2.6-flash", *mimo)
    assert config.switch_model(cfg, "deepseek-x") is False, "老名字：不算新添一行"
    assert (cfg.model, cfg.endpoint, cfg.api_key) == ("deepseek-x", *deepseek)
    assert cfg.model_list == ["deepseek-x", "mimo-v2.6-flash"], "两个都还在列表里"


def test_switching_to_an_unknown_name_leaves_the_pair_alone(tmp_path):
    """没记过的新名字：端点+密钥保持现状（用户可能正打算手填一套），不许凭空指向别处。"""
    cfg = config.Config(api_key="sk-now", endpoint="https://now.example/v1", model="m1")
    assert config.provider_for(cfg, "m2") is None
    assert config.switch_model(cfg, "m2") is True
    assert (cfg.model, cfg.endpoint, cfg.api_key) == ("m2", "https://now.example/v1", "sk-now")

    assert config.remember_provider(cfg, "m2", "https://now.example/v1", "") is False, (
        "缺 key 不算一套"
    )
    assert config.remember_provider(cfg, "  ", "https://now.example/v1", "sk-x") is False, (
        "缺名字也不算"
    )
    assert cfg.model_providers == {}

    assert config.remember_provider(cfg, "m2", "https://now.example/v1", "sk-now") is True
    assert config.remember_provider(cfg, "m2", "https://now.example/v1", "sk-now") is False, (
        "一模一样不再动"
    )


def test_a_half_written_memory_row_is_dropped(tmp_path):
    """手改 config.json 只写了一半（缺 key、名字空、值不是 dict）：读进来就当没有这一条 ——
    留着它只会在下一次切换时把请求指向一个空的地址。"""
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "api_key": "k",
                "endpoint": "e",
                "model": "m1",
                "model_providers": {
                    "half": {"endpoint": "https://half.example/v1"},
                    "": {"endpoint": "https://x/v1", "api_key": "sk-x"},
                    "junk": "not-a-dict",
                    "good": {"endpoint": "https://good.example/v1", "api_key": "sk-good"},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = config.load_config(p)
    assert cfg.model_providers == {
        "good": {"endpoint": "https://good.example/v1", "api_key": "sk-good"}
    }
    assert config.adopt_provider(cfg, "half") is False, "半条记录不会被采用"
    assert (cfg.endpoint, cfg.api_key) == ("e", "k")
    assert config.adopt_provider(cfg, "good") is True
    assert (cfg.endpoint, cfg.api_key) == ("https://good.example/v1", "sk-good")
    assert config.adopt_provider(cfg, "good") is False, "已经在用的这一套：不用再写一遍"
