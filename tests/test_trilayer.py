"""TriLayer orchestration tests, driven by a routing FakeLLM (no network)."""

import json
import time

import fungi.agent as fungi_agent
from fungi.agent import Agent
from fungi.config import Config
from fungi.events import FnSink
from fungi.llm import LLMResult
from fungi.tools import L3_TOOL_NAMES, tool_defs
from fungi.trilayer import (
    BACKGROUND_SCHEMA,
    MAX_SPAWNS_PER_TURN,
    SPAWN_SCHEMA,
    TaskSpec,
    TriLayer,
    task_brief,
)

CFG = Config(api_key="k", endpoint="e", model="m")


class RoutingFakeLLM:
    """Routes scripted results by role layer / goal marker in the message list."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[LLMResult]] = {}
        self.requested_tool_defs: list[list[str]] = []
        self.call_log: list[str] = []

    def route(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "Orchestrator" in system:
            return "L1"
        if "basic Worker" in system:
            return "L3"
        if "Task Agent" in system:
            return "L2:" + ("GOAL_B" if "GOAL_B" in user else "GOAL_A")
        return "L3"

    def __call__(self, messages: list[dict], tool_defs: list[dict]) -> LLMResult:
        key = self.route(messages)
        self.call_log.append(key)
        self.requested_tool_defs.append([d["function"]["name"] for d in tool_defs])
        return self.scripts[key].pop(0)


def tool_call(name: str, args_json: str, call_id: str = "t1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args_json}}


def spawn_call(call_id: str, goal: str, reply_format: str, layer: int = 2) -> dict:
    args = json.dumps({"goal": goal, "reply_format": reply_format, "layer": layer})
    return {"id": call_id, "type": "function", "function": {"name": "spawn", "arguments": args}}


def make_trilayer() -> tuple[TriLayer, list, RoutingFakeLLM]:
    events: list = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    fake = RoutingFakeLLM()
    results: list = []  # spawn_done capture: background reports land here
    tl = TriLayer(CFG, sink, llm=fake, spawn_done=results.append)
    tl._results = results
    return tl, events, fake


def _settle(tl: TriLayer, timeout: float = 10.0) -> None:
    """Wait until every background spawn thread has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with tl._lock:
            if tl._active == 0:
                return
        time.sleep(0.01)
    raise AssertionError("background spawns did not settle in time")


def test_full_chain_l1_spawns_l2_spawns_l3():
    tl, events, fake = make_trilayer()
    fake.scripts = {
        "L1": [
            LLMResult(tool_calls=[spawn_call("t1", "do GOAL_A", "one line summary")]),
            LLMResult(content="orchestration complete"),
        ],
        "L2:GOAL_A": [
            LLMResult(tool_calls=[spawn_call("t2", "GOAL_B basic step", "the number", layer=3)]),
            LLMResult(content="L2 done using worker output"),
        ],
        "L3": [LLMResult(content="the number is 42")],
    }
    orchestrator = tl.build_orchestrator(FnSink(lambda t, c: events.append((t, c))))
    messages = [{"role": "user", "content": "run the chain"}]
    orchestrator.run(messages)

    tool_results = [m["content"] for m in messages if m["role"] == "tool"]
    assert all(r.startswith("dispatched (id=") for r in tool_results)  # async: no inline answers
    _settle(tl)
    answers = {r["goal"]: r["answer"] for r in tl._results}
    assert answers["do GOAL_A"] == "L2 done using worker output"
    assert answers["GOAL_B basic step"] == "the number is 42"
    # Both bubbles are visible at top level: the L2 spawn and the nested L3 spawn.
    spawn_events = [c for t, c in events if t == "agent_spawn"]
    assert [e["layer"] for e in spawn_events] == [2, 3]
    statuses = [c["status"] for t, c in events if t == "agent_status"]
    assert statuses.count("done") == 2


def test_l3_toolset_is_restricted():
    tl, _events, fake = make_trilayer()
    fake.scripts = {
        "L1": [LLMResult(tool_calls=[spawn_call("t1", "GOAL_B step", "number")])],
        "L2:GOAL_B": [LLMResult(content="done directly")],
    }
    tl.bound_spawn(1).fn({"goal": "GOAL_B step", "reply_format": "number"})
    # L2's own request had spawn available; L3 never appears in this script, so
    # verify the whitelist directly from the tools registry instead.
    assert "spawn" not in L3_TOOL_NAMES
    assert "web_search" not in L3_TOOL_NAMES
    assert "inquire" not in L3_TOOL_NAMES
    assert {"read", "write", "edit", "glob", "grep", "bash"} == set(L3_TOOL_NAMES)


def test_l3_agent_gets_l3_prompt_and_no_spawn():
    """L3 child (spawned by L2) sees the L3 system prompt and no spawn tool."""
    tl, _events, fake = make_trilayer()
    fake.scripts = {
        "L1": [LLMResult(content="nothing to do")],
    }
    # Drive the internal path directly: L2 spawning L3.
    fake.scripts["L2:GOAL_A"] = [LLMResult(content="done")]
    tl.bound_spawn(2).fn({"goal": "GOAL_A", "reply_format": "done marker"})
    _settle(tl)
    assert fake.call_log == ["L3"]
    assert "spawn" not in fake.requested_tool_defs[0]


def test_json_contract_retry_then_success():
    tl, _events, fake = make_trilayer()
    fake.scripts = {
        "L1": [LLMResult(content="ok")],
        "L2:GOAL_A": [
            LLMResult(content="sorry, not json"),
            LLMResult(content='{"found": true}'),
        ],
    }
    tl.bound_spawn(1).fn({"goal": "GOAL_A", "reply_format": 'JSON {"found": bool}'})
    _settle(tl)
    assert tl._results[0]["answer"] == '{"found": true}'


def test_json_contract_fails_after_retries():
    tl, events, fake = make_trilayer()
    fake.scripts = {
        "L1": [LLMResult(content="noted the failure")],
        "L2:GOAL_A": [LLMResult(content="still not json")] * 3,
    }
    tl.bound_spawn(1).fn({"goal": "GOAL_A", "reply_format": 'JSON {"ok": bool}'})
    _settle(tl)
    answer = tl._results[0]["answer"]
    assert answer.startswith("FAIL: reply is not valid JSON")
    statuses = [c["status"] for t, c in events if t == "agent_status"]
    assert statuses[-1] == "failed"


def test_parallel_spawns_both_answered():
    tl, events, fake = make_trilayer()
    fake.scripts = {
        "L1": [
            LLMResult(
                tool_calls=[
                    spawn_call("t1", "task GOAL_A part", "reply A"),
                    spawn_call("t2", "task GOAL_B part", "reply B"),
                ]
            ),
            LLMResult(content="both done"),
        ],
        "L2:GOAL_A": [LLMResult(content="reply A")],
        "L2:GOAL_B": [LLMResult(content="reply B")],
    }
    orchestrator = tl.build_orchestrator(FnSink(lambda t, c: events.append((t, c))))
    messages = [{"role": "user", "content": "fan out"}]
    orchestrator.run(messages)

    tool_results = [m["content"] for m in messages if m["role"] == "tool"]
    assert all(r.startswith("dispatched (id=") for r in tool_results)
    _settle(tl)
    answers = {r["goal"]: r["answer"] for r in tl._results}
    assert answers["task GOAL_A part"] == "reply A"
    assert answers["task GOAL_B part"] == "reply B"
    assert len([c for t, c in events if t == "agent_spawn"]) == 2


def test_spawn_limit_enforced():
    tl, _events, _fake = make_trilayer()
    tl._active = MAX_SPAWNS_PER_TURN
    answer = tl.bound_spawn(1).fn({"goal": "x", "reply_format": "y"})
    assert answer.startswith("ERROR: spawn limit reached")


def test_depth_guard():
    tl, _events, _fake = make_trilayer()
    answer = tl.bound_spawn(3).fn({"goal": "x", "reply_format": "y"})
    assert answer.startswith("ERROR: You are at the deepest layer")


def test_missing_args():
    tl, _events, _fake = make_trilayer()
    assert tl.bound_spawn(1).fn({}).startswith("ERROR: Missing required argument")
    assert tl.bound_spawn(1).fn({"goal": "x"}).startswith("ERROR: Missing required argument")


def test_task_brief_contains_all_sections():
    spec = TaskSpec(
        id="a1", layer=2, goal="do X", reply_format="yes/no", context="ctx", constraints="c"
    )
    brief = task_brief(spec)
    assert "## Goal" in brief and "do X" in brief
    assert "## Reply format" in brief and "yes/no" in brief
    assert "## Context" in brief and "ctx" in brief
    assert "## Constraints" in brief and "c" in brief


def test_agent_without_tri_layer_untouched():
    """Base agents (no extra tools) keep working — spawn is opt-in."""
    events: list = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    agent = Agent(CFG, sink, llm=lambda _m, _t: LLMResult(content="plain"))
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)
    assert messages[-1]["content"] == "plain"


def test_spawn_records_history_with_call_id():
    tl, events, fake = make_trilayer()
    fake.scripts = {
        "L1": [LLMResult(content="ok")],
        "L2:GOAL_A": [
            LLMResult(tool_calls=[tool_call("bash", '{"command": "echo hi"}', call_id="c9")]),
            LLMResult(content="worker reply"),
        ],
    }
    tl.bound_spawn(1).fn({"goal": "GOAL_A", "reply_format": "worker reply"}, "call-xyz")
    _settle(tl)

    assert len(tl.subagents) == 1
    record = next(iter(tl.subagents.values()))
    assert record["call_id"] == "call-xyz"
    assert record["status"] == "done"
    kinds = [e["type"] for e in record["events"]]
    assert "tool" in kinds and "tool_result" in kinds
    spawn_events = [c for t, c in events if t == "agent_spawn"]
    assert spawn_events[0]["call_id"] == "call-xyz"


def test_parallel_spawns_record_distinct_histories():
    tl, _events, fake = make_trilayer()
    fake.scripts = {
        "L1": [LLMResult(content="ok")],
        "L2:GOAL_A": [LLMResult(content="reply A")],
        "L2:GOAL_B": [LLMResult(content="reply B")],
    }
    tl.bound_spawn(1).fn({"goal": "GOAL_A", "reply_format": "reply A"}, "call-a")
    tl.bound_spawn(1).fn({"goal": "GOAL_B", "reply_format": "reply B"}, "call-b")
    _settle(tl)

    assert len(tl.subagents) == 2
    call_ids = {r["call_id"] for r in tl.subagents.values()}
    assert call_ids == {"call-a", "call-b"}


def test_layer_models_route_to_stream_chat(monkeypatch):
    """Per-layer models reach the real LLM call: L1/L3 overrides, L2 falls back."""

    models_seen: list[str] = []

    def scripted_stream(
        model,
        _endpoint,
        _api_key,
        messages,
        _tool_defs,
        _on_delta=None,
        should_abort=None,  # both are stream_chat kwargs: the fake must accept them
        max_tokens=None,
    ):
        models_seen.append(model)
        system = messages[0]["content"]
        replied = any(m["role"] == "assistant" for m in messages)
        # L3's prompt mentions "Task Agent", so check the Worker marker first.
        if "basic Worker" in system:
            return LLMResult(content="the number is 42")
        if "Orchestrator" in system and not replied:
            return LLMResult(tool_calls=[spawn_call("t1", "GOAL_A step", "reply A")])
        if "Task Agent" in system and not replied:
            return LLMResult(tool_calls=[spawn_call("t2", "GOAL_B step", "the number", layer=3)])
        return LLMResult(content="done")

    monkeypatch.setattr(fungi_agent, "stream_chat", scripted_stream)
    cfg = Config(api_key="k", endpoint="e", model="base", layer_models={1: "big", 3: "small"})
    tl = TriLayer(cfg, FnSink(lambda _t, _c: None))
    orchestrator = tl.build_orchestrator(FnSink(lambda _t, _c: None))
    orchestrator.run([{"role": "user", "content": "run the chain"}])
    assert models_seen == ["big", "base", "small", "base", "big"]


def test_trilayer_propagates_should_abort():
    flag = {"on": False}
    tl = TriLayer(CFG, FnSink(lambda _t, _c: None), should_abort=lambda: flag["on"])
    orchestrator = tl.build_orchestrator(FnSink(lambda _t, _c: None))
    assert callable(orchestrator.should_abort) and orchestrator.should_abort() is False
    flag["on"] = True
    assert orchestrator.should_abort() is True


def test_every_tool_description_is_a_plain_string():
    """A tuple description (trailing comma inside parens) serializes to a JSON
    array, which strict providers (GLM 1210) reject as an invalid parameter -
    the whole turn 400s. Assert the type at the source for every schema."""
    schemas = [SPAWN_SCHEMA, BACKGROUND_SCHEMA, *tool_defs()]
    for s in schemas:
        assert isinstance(s["function"]["description"], str), s["function"]["name"]


def test_background_runs_command_directly_without_an_llm():
    """`background` executes the command on a worker thread - no subagent, no
    extra LLM round-trips - and reports the output via spawn_done."""

    def counting_llm(_messages, _tools):
        raise AssertionError("background must not invoke the LLM")

    got: list = []
    tl = TriLayer(
        CFG,
        FnSink(lambda _t, _c: None),
        llm=counting_llm,
        spawn_done=lambda rec: got.append(rec),
    )
    out = tl.bound_background(1).fn({"command": "echo bg-direct"})
    assert out.startswith("dispatched (id=")
    for _ in range(50):
        if got:
            break
        time.sleep(0.2)
    assert got and got[0]["status"] == "done"
    assert "bg-direct" in got[0]["answer"]


def test_bad_timeout_never_leaks_a_spawn_slot():
    """_spawn took its per-turn slot before validating the timeout, and the
    error return never gave it back: a few malformed calls exhausted the limit
    and the model could no longer dispatch anything that turn."""
    tl, _events, _fake = make_trilayer()
    outs = [
        tl.bound_spawn(1).fn({"goal": "g", "reply_format": "r", "timeout": "soon"})
        for _ in range(MAX_SPAWNS_PER_TURN + 2)
    ]
    assert all("timeout must be a number" in o for o in outs)
    assert not any("limit reached" in o for o in outs)
    assert tl._active == 0
