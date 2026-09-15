"""Comm clone contract tests: FakeLLM drives task/chat/consent flows over a real hub."""

import json
import threading
import time

from fungi.clone.base import RemoteTransport
from fungi.clone.comm import SILENT_REPLY, build_comm_clone
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.protocol import Envelope, deserialize

CFG = Config(api_key="k", endpoint="e", model="m")


class ScriptedLLM:
    """Pops scripted LLMResults in order; records requested tool names."""

    def __init__(self, results: list[LLMResult]) -> None:
        self.results = list(results)
        self.requested: list[list[str]] = []

    def __call__(self, _messages: list[dict], tool_defs: list[dict]) -> LLMResult:
        self.requested.append([d["function"]["name"] for d in tool_defs])
        return self.results.pop(0)


def tool_call(name: str, args: dict, call_id: str = "t1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _joined_room(room):
    hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    return hub, clients


def test_comm_clone_tool_surface():
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    assert set(clone.tools) == {
        "send_peer",
        "send_file",
        "amail",
        "confirm",
        "inquire",
        "todo",
        "read_file",
        "write_file",
        "edit_file",
        "glob_files",
        "grep_files",
    }
    assert "host beta" in clone.resolved_prompt()
    assert "peer" in clone.resolved_prompt()


def test_task_produces_result_envelope(room):
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content="done: 42")])
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    task = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="task",
        body={"goal": "compute the answer", "reply_format": "number"},
    )
    clients["alpha"].send(task)
    messages, _cursor = clients["beta"].poll_env("beta")
    assert len(messages) == 1
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    assert len(replies) == 1
    out = replies[0]
    assert out.type == "result"
    assert out.src == "beta:comm-alpha"
    assert out.body == {"ok": True, "payload": "done: 42"}
    assert out.reply_to == task.id


def test_render_input_formats_task():
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    task = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="task",
        body={"goal": "g", "reply_format": "rf", "context": "ctx"},
    )
    text = clone.render_input(task)
    assert "[TASK from alpha:comm-beta]" in text
    assert "Goal: g" in text and "Reply format: rf" in text and "Context: ctx" in text


def test_send_peer_tool_sends_chat(room):
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM(
        [
            LLMResult(content="", tool_calls=[tool_call("send_peer", {"text": "hi"})]),
            LLMResult(content="sent"),
        ]
    )
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    task = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="task", body={"goal": "greet"}
    )
    clients["alpha"].send(task)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    chats = [e for e in replies if e.type == "chat" and e.src == "beta:comm-alpha"]
    assert chats and chats[0].body == {"text": "hi"}


def test_silent_marker_turn_delivers_nothing(room):
    """A turn ending with the bare SILENT_REPLY marker is true silence: the
    fallback auto-delivery must not ship prose 'silence declarations' back to
    the peer (the five-round mutual-silence loop of 2026-09-04)."""
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content=SILENT_REPLY)])
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    chat = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "ping"}
    )
    clients["alpha"].send(chat)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    chats = [e for e in replies if e.type == "chat" and e.src == "beta:comm-alpha"]
    assert not chats


def test_consent_flow_wakes_blocked_write(room):
    hub, clients = _joined_room(room)
    fake = ScriptedLLM(
        [
            LLMResult(
                content="",
                tool_calls=[
                    tool_call(
                        "confirm",
                        {
                            "host": "alpha",
                            "action": "write",
                            "path": "homes/alpha/notes.md",
                            "reason": "share meeting notes",
                        },
                        call_id="t1",
                    )
                ],
            ),
            LLMResult(
                content="",
                tool_calls=[
                    tool_call(
                        "write_file",
                        {"path": "homes/alpha/notes.md", "content": "shared"},
                        call_id="t2",
                    )
                ],
            ),
            LLMResult(content="written"),
        ]
    )
    clone = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        llm=fake,
        ask_timeout_s=5,
        poll_timeout=0.1,
    )

    task = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="task", body={"goal": "share"}
    )
    clients["alpha"].send(task)

    # alpha's "user" (local clone stand-in): answer every ask with yes
    stop = threading.Event()
    results: list[Envelope] = []

    def responder():
        after = 0
        while not stop.is_set():
            _code, out = clients["alpha"].poll_raw("alpha", after=after, timeout=0.2)
            after = out["cursor"]
            for raw in out["messages"]:
                if raw["type"] == "ask":
                    clients["alpha"].send(
                        Envelope(
                            src=raw["dst"],
                            dst=raw["src"],
                            type="answer",
                            body={"value": "yes"},
                            reply_to=raw["id"],
                        )
                    )
                else:
                    results.append(deserialize(raw))

    watcher = threading.Thread(target=responder)
    watcher.start()
    clone.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not any(r.type == "result" for r in results):
            time.sleep(0.05)
        assert any(r.type == "result" for r in results), "turn never completed"
        assert any("written" in r.body.get("payload", "") for r in results)
        # the consent ask reached the LLM, and the guarded write ran after "yes"
        assert "confirm" in fake.requested[0]
        assert "write_file" in fake.requested[1]
    finally:
        clone.stop()
        stop.set()
        watcher.join(timeout=3)

    # the consented write actually landed, and the ask is ANSWERED in the hub registry
    notes = hub.store.root / "homes" / "alpha" / "notes.md"
    assert notes.read_text(encoding="utf-8") == "shared"
    comm_tools = clone.tools["confirm"].fn.__self__
    record = hub.asks.get(comm_tools.consent_id)
    assert record is not None and record["status"] == "answered"


def test_turn_end_hook_receives_transcript(room):
    """The room's recorder gets the full turn transcript (system + history +
    new turn) for the friend view."""
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content="reply text")])
    seen: list[tuple[str, list[dict]]] = []
    clone = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        llm=fake,
        on_turn_end=lambda env_type, messages, agent: seen.append((env_type, list(messages))),
    )
    chat = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="chat",
        body={"text": "hello"},
    )
    clients["alpha"].send(chat)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    assert seen and seen[0][0] == "chat"
    transcript = seen[0][1]
    assert transcript[0]["role"] == "system"
    assert "[alpha:comm-beta]" in transcript[-2]["content"]
    assert transcript[-1]["content"] == "reply text"
    assert transcript[-1]["role"] == "assistant"


def test_courier_memory_injected_into_prompt(monkeypatch):
    """GUI 记忆注入契约：config.courier_memory 进入下一轮 chat 的 system prompt。"""
    from fungi import config as config_mod

    def fake_load(path=None):
        return Config(
            api_key="k", endpoint="e", model="m", courier_memory="工作日白天在上课，没空回消息"
        )

    monkeypatch.setattr(config_mod, "load_config", fake_load)
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    prompt = clone.resolved_prompt()
    assert "工作日白天在上课" in prompt
    # empty memory -> bare base prompt
    monkeypatch.setattr(config_mod, "load_config", lambda path=None: CFG)
    assert "工作日白天在上课" not in clone.resolved_prompt()


def test_courier_memory_config_roundtrip(tmp_path):
    """save_config 持久化 courier_memory，load_config 读回。"""
    from fungi.config import load_config, save_config

    path = tmp_path / "config.json"
    cfg = Config(api_key="k", endpoint="e", model="m", courier_memory="今天去上课了")
    save_config(cfg, path)
    assert load_config(path).courier_memory == "今天去上课了"
    # empty memory writes nothing
    save_config(Config(api_key="k", endpoint="e", model="m"), path)
    assert load_config(path).courier_memory == ""


def test_courier_calendar_injected(monkeypatch):
    """GUI 日历待办进入信使 prompt（overdue + 未来 21 天）。"""
    from fungi import config as config_mod
    from fungi import todos as todos_mod

    monkeypatch.setattr(config_mod, "load_config", lambda path=None: CFG)
    orig = todos_mod.load
    monkeypatch.setattr(todos_mod, "load", lambda path=None: orig())

    class FakeStore(dict):
        pass

    store = {"2026-09-10": ["下午上课"], "2026-09-24": ["面基"]}
    monkeypatch.setattr(todos_mod, "load", lambda path=None: store)
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    prompt = clone.resolved_prompt()
    assert "日历待办" in prompt and "下午上课" in prompt and "面基" in prompt
    monkeypatch.setattr(todos_mod, "load", lambda path=None: {})
    assert "日历待办" not in clone.resolved_prompt()
    # the calendar rules ride along even when there is nothing to show: they are
    # what keeps the courier from rewriting the host's entries.
    assert "user's calendar" in clone.resolved_prompt()


def _wait(predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _RecordingTransport:
    """The real transport plus a log of what the courier put on the wire."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.sent: list[Envelope] = []

    def send(self, env: Envelope) -> dict:
        self.sent.append(env)
        return self.inner.send(env)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_inquire_never_blocks_the_courier_and_the_answer_arrives_as_a_turn(room):
    """A courier that asks its owner must keep serving the peer meanwhile.

    One clone owns exactly one worker thread, so the blocking inquire parked
    every peer message behind the question for up to 30 minutes
    (2026-09-11 user report: 一旦用户没有及时回复 inquire，信使就卡死不动).
    The question now returns at once and the answer comes back as a turn of its
    own, carrying the question it answers.
    """
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM(
        [
            # turn 1: ask the owner, then report (the ask is left unanswered)
            LLMResult(
                content="",
                tool_calls=[tool_call("inquire", {"question": "周六见面吗？"}, "t1")],
            ),
            LLMResult(content="问过主人了，等他回"),
            # turn 2: a peer chat that arrives while the question is open
            LLMResult(content="", tool_calls=[tool_call("send_peer", {"text": "收到"}, "t2")]),
            LLMResult(content="转达了"),
            # turn 3: the owner's answer, as a turn of its own
            LLMResult(content="好，周六见"),
        ]
    )
    turns: list[tuple[str, list[dict]]] = []
    transport = _RecordingTransport(RemoteTransport(clients["beta"]))
    clone = build_comm_clone(
        "beta",
        "alpha",
        transport,
        CFG,
        NullSink(),
        llm=fake,
        poll_timeout=0.1,
        on_turn_end=lambda kind, messages, _agent: turns.append((kind, list(messages))),
    )
    clone.start()
    try:
        clients["alpha"].send(
            Envelope(
                src="alpha:comm-beta",
                dst="beta:comm-alpha",
                type="chat",
                body={"text": "周六有空吗"},
            )
        )
        # The turn ends while nobody has answered: the courier is not parked.
        assert _wait(lambda: len(turns) == 1), "the courier blocked on the unanswered ask"

        ask = next(e for e in transport.sent if e.type == "ask")
        assert ask.dst == "beta:local"  # its OWN host's card, not the peer's
        assert ask.body["questions"][0]["question"] == "周六见面吗？"
        asked = [m for m in turns[0][1] if m.get("role") == "tool"]
        assert any("ASKED" in str(m.get("content")) for m in asked), (
            "the tool did not return at once"
        )

        # A peer message that arrives while the question is still open is served.
        clients["alpha"].send(
            Envelope(
                src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "在吗"}
            )
        )
        assert _wait(lambda: len(turns) == 2), "the unanswered ask held the next peer chat"
        assert any(e.type == "chat" and e.body.get("text") == "收到" for e in transport.sent), (
            "the courier never reached the peer while the card waited"
        )

        # The owner answers (what RoomBase._send_answer sends): a new turn, with
        # the question carried along so the answer stands on its own.
        clients["beta"].send(
            Envelope(
                src="beta:local",
                dst="beta:comm-alpha",
                type="answer",
                body={"value": "周六有空", "questions": ask.body["questions"]},
                reply_to=ask.id,
            )
        )
        assert _wait(lambda: len(turns) == 3), "the answer never reached the courier"
        said = next(
            str(m.get("content"))
            for m in turns[2][1]
            if m.get("role") == "user" and str(m.get("content", "")).startswith("[主人的答复]")
        )
        assert "问: 周六见面吗？" in said and "答: 周六有空" in said
    finally:
        clone.stop()


def test_an_answer_without_questions_is_not_a_turn(room):
    """A late consent verdict (confirm/send_file timed out) must not become a
    chat turn: there is no question to interpret, and a bare "yes" would only
    confuse the courier."""
    clone = build_comm_clone("beta", "alpha", transport=None, cfg=CFG, sink=NullSink())
    clone.dispatch(
        Envelope(
            src="beta:local",
            dst="beta:comm-alpha",
            type="answer",
            body={"value": "yes"},
            reply_to="ask-1",
        )
    )
    assert clone._work.empty()
