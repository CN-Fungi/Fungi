"""Tests for the skill system: storage, tool, prompt section, readonly policy."""

import pytest

from fungi import skills
from fungi.agent import Agent
from fungi.clone.comm import build_comm_clone
from fungi.clone.local import build_local_clone
from fungi.config import Config
from fungi.events import FnSink
from fungi.llm import LLMResult
from fungi.trilayer import TriLayer


@pytest.fixture(autouse=True)
def _skills_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")


def test_seed_creates_meta_skill():
    assert not skills.SKILLS_DIR.exists()
    out = skills.load_all()
    assert [s.name for s in out] == ["writing-skills"]
    assert out[0].description
    assert (skills.SKILLS_DIR / "writing-skills" / "SKILL.md").is_file()
    # seeding is idempotent
    assert [s.name for s in skills.load_all()] == ["writing-skills"]


def test_save_read_roundtrip():
    assert skills.save("demo-recipe", "Use when baking bread.", "1. Mix.\n2. Bake.").startswith(
        "OK"
    )
    skill = skills.get("demo-recipe")
    assert skill is not None
    assert skill.description == "Use when baking bread."
    assert "2. Bake." in skill.body
    assert "demo-recipe: Use when baking bread." in skills.skill_tool({"action": "list"})
    assert "1. Mix." in skills.skill_tool({"action": "read", "name": "demo-recipe"})


def test_save_overwrites_same_name():
    skills.save("demo", "v1", "one")
    skills.save("demo", "v2", "two")
    assert skills.get("demo").body == "two"
    assert [s.name for s in skills.load_all()].count("demo") == 1


def test_save_validation_and_readonly():
    assert skills.save("Bad_Name", "d", "b").startswith("ERROR")
    assert skills.save("ok-name", "d", "  ").startswith("ERROR")
    assert skills.save("ok-name", "d", "x" * (skills.MAX_BODY + 1)).startswith("ERROR")
    ro = skills.bound(readonly=True)["skills"]
    assert ro.fn({"action": "list"}).startswith("- writing-skills")  # list still works
    assert ro.fn({"action": "read", "name": "writing-skills"}).startswith("# writing-skills")
    assert ro.fn({"action": "save", "name": "x", "body": "b"}).startswith(
        "ERROR: this agent may read skills"
    )


def test_section_lists_skills():
    skills.save("demo", "Trigger line.", "steps")
    section = skills.section()
    assert section.startswith("\n\n## Skills")
    assert "- demo: Trigger line." in section
    assert "- writing-skills:" in section


def test_clone_agent_gets_section_and_tool():
    sink = FnSink(lambda _t, _c: None)
    readonly = TriLayer(Config(api_key="k"), sink).build_clone_agent(
        sink, system_prompt="P", extra_tools={}
    )
    assert readonly.system_prompt.startswith("P")
    assert "## Skills" in readonly.system_prompt
    assert "skills" in readonly.extra_tools
    assert (
        readonly.extra_tools["skills"]
        .fn({"action": "save", "name": "x", "body": "b"})
        .startswith("ERROR")
    )

    writable = TriLayer(Config(api_key="k"), sink, skill_save=True).build_clone_agent(
        sink, system_prompt="P", extra_tools={}
    )
    assert (
        writable.extra_tools["skills"]
        .fn({"action": "save", "name": "x", "body": "b"})
        .startswith("OK")
    )
    assert (skills.SKILLS_DIR / "x" / "SKILL.md").is_file()


def test_orchestrator_includes_skills_tool():
    sink = FnSink(lambda _t, _c: None)
    agent = TriLayer(Config(api_key="k"), sink).build_orchestrator(sink)
    assert "## Skills" in agent.system_prompt
    assert (
        agent.extra_tools["skills"]
        .fn({"action": "save", "name": "o", "body": "b"})
        .startswith("OK")
    )


def test_local_clone_saves_comm_clone_readonly():
    cfg = Config(api_key="k")
    sink = FnSink(lambda _t, _c: None)
    local = build_local_clone("alpha", None, cfg, sink)
    assert local.skill_save is True
    comm = build_comm_clone("alpha", "beta", None, cfg, sink)
    assert comm.skill_save is False
    comm_agent = comm.build_agent()
    assert "## Skills" in comm_agent.system_prompt
    assert (
        comm_agent.extra_tools["skills"]
        .fn({"action": "save", "name": "y", "body": "b"})
        .startswith("ERROR")
    )


def test_agent_refreshes_stored_system_message():
    seen: list[list[dict]] = []

    def llm(messages, _tool_defs):
        seen.append(list(messages))
        return LLMResult(content="ok")

    agent = Agent(Config(api_key="k"), FnSink(lambda _t, _c: None), system_prompt="FRESH", llm=llm)
    agent.run([{"role": "system", "content": "STALE"}, {"role": "user", "content": "hi"}])
    # managed suffix: stored content kept, skills list appended fresh
    assert seen[0][0]["content"].startswith("STALE")
    assert seen[0][0]["content"].count("\n\n## Skills\n") == 1


def test_directory_skill_with_companion_script():
    assert skills.save("deploy-check", "Use before releasing.", "1. Run the script.").startswith(
        "OK"
    )
    d = skills.SKILLS_DIR / "deploy-check"
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")

    out = skills.skill_tool({"action": "read", "name": "deploy-check"})
    assert "scripts/check.py" in out, "companion file not surfaced in the doc read"
    assert "print('ok')" not in out  # doc read does not inline script bodies

    # path-confined companion read
    assert "print('ok')" in skills.skill_tool(
        {"action": "read", "name": "deploy-check", "path": "scripts/check.py"}
    )
    assert skills.skill_tool(
        {"action": "read", "name": "deploy-check", "path": "../secrets.txt"}
    ).startswith("ERROR")
    assert skills.skill_tool(
        {"action": "read", "name": "demo-recipe", "path": "scripts/check.py"}
    ).startswith("ERROR")  # skill without a directory


def test_legacy_flat_skills_still_read_and_shadowed():
    skills.load_all()  # seed the dir before placing a legacy flat file
    (skills.SKILLS_DIR / "old-style.md").write_text(
        "---\nname: old-style\ndescription: flat file skill\n---\nbody here\n", encoding="utf-8"
    )
    assert skills.get("old-style").description == "flat file skill"
    assert "- old-style: flat file skill" in skills.skill_tool({"action": "list"})

    # a directory with the same name shadows the flat file, no duplicate
    skills.save("old-style", "directory version", "new body")
    loaded = [s for s in skills.load_all() if s.name == "old-style"]
    assert len(loaded) == 1 and loaded[0].description == "directory version"
