"""Local clone contract tests: delegate rides the full clone loop; asks reach on_ask."""

import json
import threading
import time

from fungi.clone.base import RemoteTransport
from fungi.clone.local import build_local_clone
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.protocol import Envelope

CFG = Config(api_key="k", endpoint="e", model="m")


class RecordingLLM:
    """Pops scripted LLMResults in order; records every call's messages."""

    def __init__(self, results: list[LLMResult]) -> None:
        self.results = list(results)
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict], _tool_defs: list[dict]) -> LLMResult:
        self.calls.append(list(messages))
        return self.results.pop(0)


def tool_call(name: str, args: dict, call_id: str = "t1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _comm_stub(clients, stop: threading.Event, payload: str) -> None:
    """Stand-in for the remote comm clone: task envelope -> result envelope."""

    def loop():
        after = 0
        while not stop.is_set():
            messages, after = clients["beta"].poll_env("beta", after, 0.2)
            for env in messages:
                if env.type == "task":
                    clients["beta"].send(
                        Envelope(
                            src=env.dst,
                            dst=env.src,
                            type="result",
                            body={"ok": True, "payload": payload},
                            reply_to=env.id,
                        )
                    )

    threading.Thread(target=loop, daemon=True).start()


def _joined_room(room):
    hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    return hub, clients


def _wait(predicate, timeout_s: float, message: str):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def test_local_clone_tool_surface():
    clone = build_local_clone(
        "alpha", transport=None, cfg=CFG, sink=NullSink(), peers_fn=lambda: ["beta"]
    )
    assert set(clone.tools) == {"inquire", "delegate", "peers", "send_file", "todo"}
    assert "local Agent" in clone.system_prompt
    assert "host alpha" in clone.system_prompt
    assert "send_file" in clone.system_prompt
    assert "user's calendar" in clone.system_prompt


def test_the_file_ops_rule_reaches_every_prompt_that_can_touch_a_filesystem():
    """The user's order (2026-09-14): what is *at hand on screen* goes to the screen
    tool — opening a file included — while create/delete/rename/move go to the shell
    'in ANY place'. Both halves have to live in *every* prompt that has file tools,
    not just in the screen tool's description. These are all the assembly points: the
    shared base prompt (L1/orchestrator), the local agent, the courier, and the two
    child layers. The room/clone turn agents copy the local/clone prompt, so they
    inherit."""
    from fungi import trilayer
    from fungi.agent import FILE_OPS_RULE, SYSTEM_PROMPT
    from fungi.clone.comm import build_comm_clone

    keys = (
        "NEVER through a GUI",  # the CRUD half: filesystem changes, never a GUI
        "and that includes opening a file",  # at hand wins, and it outranks the path
        "Opening a file is not a file operation",  # ... so `start` is not the answer
    )

    local = build_local_clone(
        "alpha", transport=None, cfg=CFG, sink=NullSink(), peers_fn=lambda: ["beta"]
    )
    courier = build_comm_clone("alpha", "beta", transport=None, cfg=CFG, sink=NullSink())
    prompts = {
        "FILE_OPS_RULE": FILE_OPS_RULE,
        "SYSTEM_PROMPT": SYSTEM_PROMPT,
        "local": local.system_prompt,
        "L2_SYSTEM": trilayer.L2_SYSTEM,
        "L3_SYSTEM": trilayer.L3_SYSTEM,
        "courier": courier.resolved_prompt(),  # the courier's prompt is callable (per turn)
    }
    for name, prompt in prompts.items():
        for key in keys:
            assert key in prompt, f"{key!r} is missing from {name}"
    assert "SendToRecycleBin" in local.system_prompt  # the Recycle Bin caveat rides along


def test_local_clone_includes_mcp_tools(monkeypatch):
    """Room mode must surface configured MCP tools on the user-facing clone."""

    fake = {"mcp__demo__ping": object()}
    monkeypatch.setattr("fungi.clone.local.mcp_extra_tools", lambda _servers: fake)
    clone = build_local_clone(
        "alpha", transport=None, cfg=CFG, sink=NullSink(), peers_fn=lambda: []
    )
    assert clone.tools["mcp__demo__ping"] is fake["mcp__demo__ping"]


def test_local_clone_dispatches_incoming_ask_to_callback():
    seen: list[Envelope] = []
    clone = build_local_clone("alpha", transport=None, cfg=CFG, sink=NullSink(), on_ask=seen.append)
    ask = Envelope(
        src="beta:comm-alpha",
        dst="alpha:local",
        type="ask",
        body={"question": "Allow write?", "from": "beta:comm-alpha"},
    )
    clone.dispatch(ask)
    assert seen == [ask]


def test_delegate_roundtrip_via_full_loop(room):
    hub, clients = _joined_room(room)
    stop = threading.Event()
    _comm_stub(clients, stop, payload="42 items synced")
    fake = RecordingLLM(
        [
            LLMResult(
                content="",
                tool_calls=[
                    tool_call("delegate", {"host": "beta", "goal": "sync", "reply_format": "count"})
                ],
            ),
            LLMResult(content="done"),
        ]
    )
    local = build_local_clone(
        "alpha",
        RemoteTransport(clients["alpha"]),
        CFG,
        NullSink(),
        llm=fake,
        peers_fn=lambda: ["beta"],
        poll_timeout=0.1,
        ask_timeout_s=5,
    )
    local.start()
    try:
        hub.send(
            Envelope(src="srv:local", dst="alpha:local", type="chat", body={"text": "sync please"})
        )
        # chat turns never auto-reply (the Orchestrator chooses via send_peer);
        # the turn still runs and the exchange lands in the local history.
        _wait(
            lambda: (
                local.history
                and local.history[-1].get("role") == "assistant"
                and local.history[-1].get("content") == "done"
            ),
            10.0,
            "chat turn never completed into history",
        )
        # the delegate tool result (the remote payload) reached the agent conversation
        tool_results = [
            m.get("content", "") for call in fake.calls for m in call if m.get("role") == "tool"
        ]
        assert any("42 items synced" in c for c in tool_results), fake.calls
    finally:
        local.stop()
        stop.set()


def test_delegate_timeout_via_full_loop(room):
    hub, clients = _joined_room(room)
    fake = RecordingLLM(
        [
            LLMResult(
                content="",
                tool_calls=[tool_call("delegate", {"host": "beta", "goal": "nobody home"})],
            ),
            LLMResult(content="gave up"),
        ]
    )
    local = build_local_clone(
        "alpha",
        RemoteTransport(clients["alpha"]),
        CFG,
        NullSink(),
        llm=fake,
        peers_fn=lambda: ["beta"],
        poll_timeout=0.1,
        ask_timeout_s=5,
    )
    local.start()
    try:
        hub.send(
            Envelope(src="srv:local", dst="alpha:local", type="chat", body={"text": "try beta"})
        )
        _wait(lambda: len(fake.calls) >= 2, 10.0, "second LLM call never happened")
        tool_results = [
            m.get("content", "") for call in fake.calls for m in call if m.get("role") == "tool"
        ]
        assert any(c.startswith("FAIL:") for c in tool_results)
    finally:
        local.stop()
