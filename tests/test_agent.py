"""Tests for the agent turn loop, driven by scripted (Fake) LLM completions."""

import fungi.agent as fungi_agent
from fungi.agent import Agent, BoundTool, wrap_reasoning_events
from fungi.config import Config
from fungi.events import FnSink
from fungi.llm import LLMAbortedError, LLMError, LLMResult


class FakeLLM:
    """Returns scripted LLMResults in order; records every request."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, messages, tool_defs):
        self.calls.append((list(messages), list(tool_defs)))
        return self.results.pop(0)


def make_agent(results, extra_tools=None):
    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    fake = FakeLLM(results)
    agent = Agent(
        Config(api_key="k", endpoint="e", model="m"), sink, extra_tools=extra_tools, llm=fake
    )
    return agent, fake, events


def tool_call(name, args_json, call_id="t1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args_json}}


def test_plain_text_turn():
    agent, _fake, events = make_agent([LLMResult(content="hello there")])
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)

    assert messages[0]["role"] == "system"  # system prompt injected
    assert messages[1]["role"] == "user"
    assert messages[2] == {"role": "assistant", "content": "hello there", "reasoning": ""}
    assert events == []  # no deltas from FakeLLM


def test_tool_round_then_answer(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("data", encoding="utf-8")
    results = [
        LLMResult(tool_calls=[tool_call("read", f'{{"path": "{target.as_posix()}"}}')]),
        LLMResult(content="the file says data"),
    ]
    agent, _fake, events = make_agent(results)
    messages = [{"role": "user", "content": "read it"}]
    agent.run(messages)

    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert "1:data" in messages[3]["content"]
    assert [e[0] for e in events] == ["tool", "tool_result"]
    assert messages[4]["content"] == "the file says data"


def test_tool_streaming_deltas_reach_sink():
    results = [
        LLMResult(content="let me check", tool_calls=[tool_call("bash", '{"command": "echo hi"}')]),
        LLMResult(content="done"),
    ]
    agent, _fake, events = make_agent(results)
    agent.run([{"role": "user", "content": "go"}])
    assert events[0][0] == "tool"
    assert events[0][1]["name"] == "bash"
    assert "hi" in events[1][1]["content"]


def test_extra_tool_dispatch():
    calls = []

    def spawn_fn(args):
        calls.append(args)
        return "child finished"

    spawn = BoundTool(
        schema={"type": "function", "function": {"name": "spawn", "parameters": {}}}, fn=spawn_fn
    )
    results = [
        LLMResult(tool_calls=[tool_call("spawn", '{"goal": "x"}')]),
        LLMResult(content="orchestrated"),
    ]
    agent, _fake, _events = make_agent(results, extra_tools={"spawn": spawn})
    messages = [{"role": "user", "content": "dispatch"}]
    agent.run(messages)

    assert calls == [{"goal": "x"}]
    assert messages[3]["content"] == "child finished"
    assert messages[4]["content"] == "orchestrated"


def test_extra_tool_exception_becomes_error_result():
    def bad_fn(_args):
        raise ValueError("boom")

    bad = BoundTool(
        schema={"type": "function", "function": {"name": "bad", "parameters": {}}}, fn=bad_fn
    )
    results = [LLMResult(tool_calls=[tool_call("bad", "{}")]), LLMResult(content="recovered")]
    agent, _fake, _events = make_agent(results, extra_tools={"bad": bad})
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)

    assert messages[3]["content"].startswith("ERROR: boom")
    assert messages[4]["content"] == "recovered"


def test_invalid_tool_args_json():
    """Parse failure surfaces a self-correcting error with the raw args —
    NOT a misleading "Required arguments" message from dispatch with {}."""
    results = [LLMResult(tool_calls=[tool_call("read", "{not json")]), LLMResult(content="ok")]
    agent, _fake, _events = make_agent(results)
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)
    assert "not valid JSON" in messages[3]["content"]
    assert "{not json" in messages[3]["content"]


def test_trailing_comma_args_are_repaired(tmp_path):
    """glm-style trailing comma in tool args: repaired, tool actually runs."""
    target = tmp_path / "f.txt"
    target.write_text("data", encoding="utf-8")
    results = [
        LLMResult(
            tool_calls=[tool_call("read", '{"path": "' + str(target).replace("\\", "\\\\") + '",}')]
        ),
        LLMResult(content="done"),
    ]
    agent, _fake, _events = make_agent(results)
    messages = [{"role": "user", "content": "read it"}]
    agent.run(messages)
    assert "data" in messages[3]["content"]
    assert messages[4]["content"] == "done"


def test_transient_llm_error_is_retried():
    """A dropped stream must not kill the turn: retry succeeds."""
    attempts = []

    def flaky(_messages, _tool_defs):
        attempts.append(1)
        if len(attempts) == 1:
            raise LLMError("Stream interrupted: [Errno 10054]")
        return LLMResult(content="recovered")

    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    agent = Agent(Config(api_key="k", endpoint="e", model="m"), sink, llm=flaky)
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)
    assert len(attempts) == 2
    assert messages[-1]["content"] == "recovered"
    assert not [t for t, _c in events if t == "error"]


def test_non_transient_llm_error_is_not_retried():
    attempts = []

    def failing(_messages, _tool_defs):
        attempts.append(1)
        raise LLMError("HTTP 400: bad request")

    sink = FnSink(lambda t, c: None)
    agent = Agent(Config(api_key="k", endpoint="e", model="m"), sink, llm=failing)
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)
    assert len(attempts) == 1
    assert messages[-1]["content"].startswith("(LLM error: HTTP 400")


def test_llm_error_reported_not_raised():
    class FailingLLM:
        def __call__(self, _messages, _tool_defs):
            raise LLMError("HTTP 500: sad")

    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    agent = Agent(Config(), sink, llm=FailingLLM())
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)

    assert ("error", "HTTP 500: sad") in events
    assert messages[-1]["content"].startswith("(LLM error")


def test_max_rounds_guard():
    endless = LLMResult(tool_calls=[tool_call("bash", '{"command": "echo x"}', call_id="loop")])
    agent, _fake, events = make_agent([endless] * fungi_agent.MAX_TOOL_ROUNDS)
    messages = [{"role": "user", "content": "go"}]
    agent.run(messages)

    assert any(t == "error" and "Max tool rounds" in str(c) for t, c in events)
    assert messages[-1]["content"].startswith("(Hit max tool rounds")


def test_tool_defs_include_extra_tools():
    spawn = BoundTool(schema={"type": "function", "function": {"name": "spawn"}}, fn=lambda _a: "")
    agent, _fake, _events = make_agent([], extra_tools={"spawn": spawn})
    names = [d["function"]["name"] for d in agent.tool_defs]
    assert "spawn" in names
    assert "read" in names


def test_session_messages_reused():
    """Preexisting system message (loaded session) is not duplicated; only the
    managed skills suffix is appended/staleness-replaced."""
    messages = [{"role": "system", "content": "custom"}, {"role": "user", "content": "hi"}]
    agent, _fake, _events = make_agent([LLMResult(content="ok")])
    agent.run(messages)
    assert sum(1 for m in messages if m["role"] == "system") == 1
    assert messages[0]["content"].startswith("custom")
    assert messages[0]["content"].count("\n\n## Skills\n") == 1

    messages[0]["content"] += "\n\n## Skills\n- stale: old\n"
    agent, _fake, _events = make_agent([LLMResult(content="ok")])
    agent.run(messages)
    assert messages[0]["content"].startswith("custom")
    assert "stale" not in messages[0]["content"]
    assert messages[0]["content"].count("\n\n## Skills\n") == 1


def test_wrap_reasoning_events_brackets_stream():
    events: list = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    on_delta, state = wrap_reasoning_events(sink)
    on_delta("reasoning", "a")
    on_delta("reasoning", "b")
    on_delta("text", "x")
    assert [t for t, _ in events] == [
        "reasoning_start",
        "reasoning",
        "reasoning",
        "reasoning_end",
        "text",
    ]
    assert state == {"started": True, "ended": True}


def test_wrap_reasoning_events_no_reasoning_no_events():
    events: list = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    on_delta, state = wrap_reasoning_events(sink)
    on_delta("text", "x")
    assert events == [("text", "x")]
    assert state == {"started": False, "ended": False}


def test_abort_before_round_appends_marker():
    """Abort flag set before the loop: no LLM call, marker appended."""
    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    fake = FakeLLM([LLMResult(content="never reached")])
    agent = Agent(Config(api_key="k"), sink, llm=fake, should_abort=lambda: True)
    messages = [{"role": "user", "content": "hi"}]
    agent.run(messages)

    assert fake.calls == []  # no LLM round ran
    assert ("error", "Aborted by user") in events
    assert messages[-1] == {"role": "assistant", "content": "(Aborted)"}


def test_abort_mid_stream_keeps_partial_reply():
    """LLMAbortedError from the LLM layer: partial content stays, marker appended."""
    partial = LLMResult(content="Roses are red,")

    class InterruptingLLM:
        def __call__(self, _messages, _tool_defs):
            raise LLMAbortedError(partial)

    events = []
    sink = FnSink(lambda t, c: events.append((t, c)))
    agent = Agent(Config(api_key="k"), sink, llm=InterruptingLLM())
    messages = [{"role": "user", "content": "poem"}]
    agent.run(messages)

    assert ("error", "Aborted by user") in events
    assert messages[-2] == {"role": "assistant", "content": "Roses are red,"}
    assert messages[-1] == {"role": "assistant", "content": "(Aborted)"}


def test_should_abort_passed_to_stream_chat(monkeypatch):
    """Default LLM path forwards the abort predicate to stream_chat."""

    def fake_stream(
        _model,
        _endpoint,
        _api_key,
        _messages,
        _tool_defs,
        _on_delta=None,
        should_abort=None,  # both are stream_chat kwargs: the fake must accept them
        max_tokens=None,
    ):
        seen.append(should_abort)
        return LLMResult(content="ok")

    seen: list = []
    monkeypatch.setattr(fungi_agent, "stream_chat", fake_stream)
    agent = Agent(Config(api_key="k"), FnSink(lambda _t, _c: None), should_abort=lambda: False)
    agent.run([{"role": "user", "content": "hi"}])
    assert len(seen) == 1 and callable(seen[0])


def test_parallel_batch_keeps_private_tools_off_the_stream():
    """`diary` joins the parallel tool set when the feature is on. The parallel
    path used to emit its card and result like any other tool — leaking the
    agent's inner life into the WebUI stream and the live tape, which the
    privacy contract forbids (the serial path always filtered it)."""
    inner = BoundTool(
        schema={"type": "function", "function": {"name": "diary", "parameters": {}}},
        fn=lambda _a: "today I felt seen",
    )
    shared = BoundTool(
        schema={"type": "function", "function": {"name": "todo", "parameters": {}}},
        fn=lambda _a: "listed",
    )
    agent, _fake, events = make_agent(
        [
            LLMResult(
                tool_calls=[
                    tool_call("diary", '{"action": "read"}', "d1"),
                    tool_call("todo", '{"action": "list"}', "t2"),
                ]
            ),
            LLMResult(content="done"),
        ],
        extra_tools={"diary": inner, "todo": shared},
    )
    agent.parallel_tools = frozenset({"diary", "todo"})
    agent.run([{"role": "user", "content": "go"}])

    offered = [c["name"] for kind, c in events if kind == "tool"]
    results = [c["content"] for kind, c in events if kind == "tool_result"]
    assert offered == ["todo"]  # the parallel batch still went through both
    assert all("felt seen" not in str(r) for r in results)


def test_a_bound_tool_can_hand_pixels_back_to_the_model():
    """The screen tool answers with an ImageRead (a summary plus data URLs).
    `_dispatch` used to str() every bound-tool result, which silently dropped the
    pictures the model was supposed to look at (spec §35.1)."""
    from fungi.tools.files import ImageRead

    shot = ImageRead("SCREEN 1x1", ["data:image/png;base64,AAAA"])
    bound = BoundTool(
        schema={"type": "function", "function": {"name": "screen", "parameters": {}}},
        fn=lambda _args, _call_id: shot,
        with_call_id=True,
    )
    agent, _fake, _events = make_agent(
        [
            LLMResult(tool_calls=[tool_call("screen", '{"action": "shot"}', "s1")]),
            LLMResult(content="I can see the desktop"),
        ],
        extra_tools={"screen": bound},
    )
    messages = [{"role": "user", "content": "look"}]
    agent.run(messages)

    content = messages[3]["content"]
    assert isinstance(content, list)  # upgraded to multimodal, not flattened
    assert content[0]["text"] == "SCREEN 1x1"
    assert content[1]["image_url"]["url"].startswith("data:image/png")


def test_public_messages_stamps_the_moment_a_row_lands():
    """The WebUI says when each message was sent, so the transcript boundary is
    where a row gets its `ts`: stamped once, never moved by the next save (a
    turn's end save must not restamp the user message its start save wrote)."""
    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo", "ts": 5.0},
    ]
    fungi_agent.public_messages(messages)
    assert "ts" not in messages[0]  # the system prompt is not a message row
    assert messages[1]["ts"] > 0
    assert messages[2]["ts"] == 5.0  # an already-stamped row keeps its moment
    stamped = messages[1]["ts"]
    fungi_agent.public_messages(messages)
    assert messages[1]["ts"] == stamped
