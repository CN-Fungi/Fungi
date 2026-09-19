"""Room mode tests: cards, consent rules, server/client assembly, WebUI routing."""

import json
import time
import urllib.request
from pathlib import Path

import pytest

from fungi.cards import AskCards
from fungi.config import Config
from fungi.consent_rules import ConsentRules
from fungi.events import NullSink
from fungi.hub.app import Hub
from fungi.protocol import Envelope
from fungi.room import RoomClient, RoomRuntime, RoomServer, merge_comm_history

CFG = Config(api_key="k", endpoint="e", model="m")  # assembly reads inbox_dir
LLM = object()


def _all_msgs(inbox) -> list:
    """Drain (cursor 0 = everything buffered) without relying on long-poll wait."""
    msgs, _cursor = inbox.after(0, 0.2)
    return msgs


def _wait(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── units ──


def test_ask_cards_record_take_and_dedup():
    cards = AskCards()
    a = Envelope(src="beta:comm-alpha", dst="alpha:local", type="ask", body={"question": "q"})
    assert cards.record(a) is True
    assert cards.record(a) is False  # duplicate
    assert [c["id"] for c in cards.pending()] == [a.id]
    taken = cards.take(a.id, "yes")
    assert taken is not None and taken.id == a.id
    assert cards.pending() == []
    assert cards.record(a) is False  # answered ids stay known (replay dedup)


def test_consent_modes_persist_and_legacy_migrates(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"always_allow": ["beta:comm-alpha"]}), encoding="utf-8")
    rules = ConsentRules(path)
    assert rules.mode_for("beta") == "allow"  # legacy grant migrated to a visible mode
    assert rules.allows("beta:comm-alpha") is True

    rules.set_mode("beta", "ask")
    assert rules.mode_for("beta") == "ask"
    assert rules.allows("beta:comm-alpha") is False
    reloaded = ConsentRules(path)
    assert reloaded.mode_for("beta") == "ask"
    assert "always_allow" not in json.loads(path.read_text(encoding="utf-8"))


# ── server role ──


@pytest.fixture()
def server_room(tmp_path):
    room = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "data",
        llm=LLM,
        rules_path=tmp_path / "rules.json",
    )
    room.start()
    yield room
    room.stop()


def _send_ask(hub, src, dst, body):
    env = Envelope(src=src, dst=dst, type="ask", body=body)
    hub.send(env)
    return env


def test_server_ask_becomes_card_and_answer_envelope_flows(server_room):
    room = server_room
    requester_inbox = room.hub.relay.register_local("alpha:comm-selftest")
    ask = _send_ask(
        room.hub,
        "alpha:comm-selftest",
        "alpha:local",
        {"question": "Allow write?", "from": "alpha:comm-selftest"},
    )
    assert _wait(room.cards.pending), "ask envelope never became a card"

    runtime: RoomRuntime = room.webui_runtime()
    pending = runtime.pending_asks()
    assert pending[0]["kind"] == "consent"
    assert pending[0]["from"] == "alpha:comm-selftest"
    # conv = the friend conversation the ask belongs to (agent suffix "selftest")
    assert pending[0]["conv"] == "selftest"

    assert runtime.route_answer(ask.id, "yes") is True
    msgs, _cursor = requester_inbox.after(0, 2.0)
    answers = [m for m in msgs if m.type == "answer" and m.reply_to == ask.id]
    assert answers and answers[0].body == {"value": "yes"}
    assert room.hub.asks.get(ask.id)["status"] == "answered"


def test_comm_history_merge_drops_the_abstention_marker():
    """`<<SILENT>>` is a delivery control token, and a browser renders the
    leftover marker as `<>` (HTML eats the unknown tag). It must not reach the
    stored transcript — and a row that carried real work keeps that work."""
    stored = [{"role": "user", "content": "早", "ts": 1.0}]
    fresh = [
        {"role": "user", "content": "早"},
        {"role": "assistant", "content": "<<SILENT>>"},
        {"role": "assistant", "content": "好 <<SILENT>>", "reasoning": "想好了"},
    ]
    merged = merge_comm_history(stored, fresh, ts=2.0)
    assert [m["content"] for m in merged] == ["早", "好"]
    assert merged[0]["ts"] == 1.0  # the stored row keeps the stamp it had
    assert "<<SILENT>>" not in json.dumps(merged)


def test_comm_history_merge_keeps_a_reply_with_its_question(server_room):
    """The clone's history copy of a turn is leaner than the stored one (no
    reasoning), which must not read as a new row: matching on whole
    dictionaries duplicated the courier's first reply and hoisted it above its
    own question (2026-09-10: two peer messages -> two courier turns)."""
    stored = [
        {"role": "user", "content": "在吗", "ts": 1.0},
        {"role": "assistant", "content": "在的", "reasoning": "想想要不要回", "ts": 1.5},
    ]
    fresh = [  # the clone's cumulative history: the same two rows, leaner
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
        {"role": "user", "content": "晚上吃火锅？"},
        {"role": "assistant", "content": "好"},
    ]
    merged = merge_comm_history(stored, fresh, ts=2.0)
    assert [m["role"] for m in merged] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in merged] == ["在吗", "在的", "晚上吃火锅？", "好"]
    assert merged[1]["reasoning"] == "想想要不要回"  # 展示用 payload 不丢
    assert [m["ts"] for m in merged] == [1.0, 1.5, 2.0, 2.0]


def test_comm_history_merge_keeps_the_tool_rows_above_the_dialogue_they_turn_belonged_to(
    server_room,
):
    """2026-09-11 用户报告：好友视图「对话沉底，工具和思考上浮」，刷新也不行。

    The clone's history carries the conversation (the peer's rows and the
    courier's report text); the transcript also holds what the agent wrote —
    its reasoning, its tool calls and their results. Matching the two lists
    position by position stopped at the first of those rows, decided the clone
    had forgotten everything, and re-appended the whole conversation at the
    end: everything the store already had (the tools and thinking) ended up on
    top of the dialogue, and it stayed wrong across a refresh."""
    stored = [
        {"role": "user", "content": "在吗", "ts": 1.0},
        {
            "role": "assistant",
            "content": None,
            "reasoning": "看看日历",
            "tool_calls": [{"id": "c1", "function": {"name": "todo"}}],
            "ts": 1.5,
        },
        {"role": "tool", "tool_call_id": "c1", "content": "09-12: 晚饭", "ts": 1.5},
        {"role": "assistant", "content": "在的", "ts": 1.5},
    ]
    fresh = [  # the clone's history: the same conversation, leaner, plus this turn
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
        {"role": "user", "content": "晚上吃啥"},
        {"role": "assistant", "content": "吃火锅"},
    ]
    merged = merge_comm_history(stored, fresh, ts=2.0)
    assert [(m["role"], m.get("content")) for m in merged] == [
        ("user", "在吗"),
        ("assistant", None),
        ("tool", "09-12: 晚饭"),
        ("assistant", "在的"),
        ("user", "晚上吃啥"),
        ("assistant", "吃火锅"),
    ]
    assert [m["ts"] for m in merged] == [1.0, 1.5, 1.5, 1.5, 2.0, 2.0]
    assert merged[1]["reasoning"] == "看看日历"  # the stored copy keeps its payload


def test_comm_history_merge_keeps_rows_the_clone_forgot(server_room):
    """A clone rebuilt after its peer dropped off the roster comes back with an
    empty history: the stored transcript must be carried forward, marker rows
    and all stripped on both sides so the alignment still lines up."""
    stored = [
        {"role": "user", "content": "早", "ts": 1.0},
        {"role": "assistant", "content": "<<SILENT>>", "ts": 1.5},
        {"role": "user", "content": "在吗", "ts": 2.0},
    ]
    assert merge_comm_history(stored, [], ts=3.0) == [
        {"role": "user", "content": "早", "ts": 1.0},
        {"role": "user", "content": "在吗", "ts": 2.0},
    ]


def test_answered_card_verdict_persists_to_comm_transcript(server_room):
    """A card ask's verdict must be filed into the friend conversation it
    belongs to, so the transcript keeps the decision after reload (the
    WebUI answered card would otherwise evaporate with the page)."""
    room = server_room
    room._comm_store.save("comm-selftest", "comm: selftest", [{"role": "user", "content": "hi"}])
    ask = _send_ask(
        room.hub,
        "alpha:comm-selftest",
        "alpha:local",
        {"question": "Allow write on homes/alpha/x?", "from": "alpha:comm-selftest"},
    )
    assert _wait(room.cards.pending), "ask envelope never became a card"
    runtime: RoomRuntime = room.webui_runtime()
    assert runtime.route_answer(ask.id, "yes") is True
    data = room._comm_store.load("comm-selftest")
    (rec,) = data["asks"]
    # card asks carry no tool-call id, but they do carry when they were answered
    # (the friend view slots them into the transcript timeline by that stamp)
    assert isinstance(rec.pop("ts"), float)
    assert rec == {
        "id": ask.id,
        "questions": [
            {"question": "Allow write on homes/alpha/x?", "options": [], "allow_custom": True}
        ],
        "answers": ["yes"],
        "status": "answered",
    }


def test_cross_host_card_verdict_has_no_local_transcript(server_room):
    """Guard asks raised on a remote host (conv == our own host) must not
    fabricate a local conversation — the raising turn lives elsewhere."""
    room = server_room
    room._comm_store.save("comm-beta", "comm: beta", [])
    ask = _send_ask(
        room.hub,
        "beta:comm-alpha",
        "alpha:local",
        {"question": "Allow write?", "from": "beta:comm-alpha"},
    )
    assert _wait(room.cards.pending), "ask envelope never became a card"
    runtime: RoomRuntime = room.webui_runtime()
    assert runtime.route_answer(ask.id, "no") is True
    assert room._comm_store.load("comm-beta")["asks"] == []


def test_room_turn_persists_answered_asks(server_room):
    """Room-mode turns must record completed inquire calls on the agent:
    the WebUI turn runner saves that bucket, and sessions replay answered
    cards from it. Room mode lost the on_answer wiring — asks stayed [] on
    every session file."""
    import threading

    from fungi.events import FnSink

    room = server_room
    runtime = room.webui_runtime()
    events: list[tuple] = []
    agent = runtime.build_agent(FnSink(lambda t, c: events.append((t, c))), lambda: False)

    out: dict = {}
    th = threading.Thread(
        target=lambda: out.update(reply=agent.extra_tools["inquire"].fn({"question": "proceed?"})),
        daemon=True,
    )
    th.start()

    def ask_id():
        asks = [c for t, c in events if t == "ask"]
        return asks[-1]["id"] if asks else None

    assert _wait(lambda: ask_id() is not None), "ask event never surfaced on the turn sink"
    assert runtime.route_answer(ask_id(), "yes") is True
    th.join(timeout=5.0)
    assert out["reply"].startswith("USER:"), f"ask tool never woke: {out}"
    assert [a["status"] for a in agent.asks] == ["answered"]
    assert agent.asks[0]["answers"] == "yes"


def test_server_allow_mode_silently_answers(server_room):
    room = server_room
    room.rules.set_mode("alpha", "allow")

    requester_inbox = room.hub.relay.register_local("alpha:comm-beta")
    _send_ask(
        room.hub,
        "alpha:comm-beta",
        "alpha:local",
        {
            "from": "alpha:comm-beta",
            "action": "write",
            "path": "homes/alpha/x.md",
            "reason": "r",
            "question": "Allow write?",
        },
    )
    assert room.cards.pending() == [], "always-allowed ask must not become a card"
    assert _wait(
        lambda: any(
            m.type == "answer" and m.body == {"value": "yes"} for m in _all_msgs(requester_inbox)
        )
    ), "auto-allowed ask was never answered yes"


def test_card_answer_is_one_shot_but_slider_mode_persists(server_room):
    room = server_room
    requester_inbox = room.hub.relay.register_local("alpha:comm-gamma")
    ask = _send_ask(
        room.hub,
        "alpha:comm-gamma",
        "alpha:local",
        {
            "from": "alpha:comm-gamma",
            "action": "write",
            "path": "homes/alpha/y.md",
            "reason": "r",
            "question": "Allow?",
        },
    )
    assert _wait(room.cards.pending)
    runtime = room.webui_runtime()
    assert runtime.route_answer(ask.id, "yes") is True
    assert room.rules.mode_for("alpha") == "ask"  # a one-shot card grants nothing

    # same-source ask right after: a new card, not a silent yes
    second = _send_ask(
        room.hub,
        "alpha:comm-gamma",
        "alpha:local",
        {
            "from": "alpha:comm-gamma",
            "action": "write",
            "path": "homes/alpha/z.md",
            "reason": "r",
            "question": "Allow again?",
        },
    )
    assert _wait(lambda: any(c["id"] == second.id for c in room.cards.pending()))

    # slider: allow mode makes future asks silent and reversible
    runtime.set_consent_mode("alpha", "allow")
    third = _send_ask(
        room.hub,
        "alpha:comm-gamma",
        "alpha:local",
        {
            "from": "alpha:comm-gamma",
            "action": "write",
            "path": "homes/alpha/w.md",
            "reason": "r",
            "question": "Allow a third time?",
        },
    )
    assert _wait(
        lambda: any(
            m.type == "answer" and m.reply_to == third.id and m.body == {"value": "yes"}
            for m in _all_msgs(requester_inbox)
        )
    ), "allow-mode ask was not auto-answered"
    assert not [c for c in room.cards.pending() if c["id"] == third.id]

    runtime.set_consent_mode("alpha", "ask")  # reversible: back to cards
    assert room.rules.mode_for("alpha") == "ask"


def test_consent_mode_endpoint_roundtrip(server_room):
    room = server_room
    url = room.open_webui(open_browser=False)
    d = json.loads(urllib.request.urlopen(url + "/consent-mode?host=alpha", timeout=5).read())
    assert d["mode"] == "ask"
    req = urllib.request.Request(
        url + "/consent-mode",
        data=json.dumps({"host": "alpha", "mode": "allow"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"] is True
    assert room.rules.mode_for("alpha") == "allow"
    assert room.webui_runtime().consent_mode("alpha") == "allow"


def test_server_roster_monitor_adds_and_removes_comm_clones(server_room):
    room = server_room
    room.hub.join("beta", "127.0.0.1")
    assert _wait(lambda: "beta" in room._clones, timeout_s=8.0), "monitor never spawned comm clone"

    room.hub.roster.leave("beta")
    assert _wait(lambda: "beta" not in room._clones, timeout_s=8.0), (
        "monitor never removed comm clone"
    )


def test_server_runtime_sessions_backed_by_hub_store(server_room):
    runtime = server_room.webui_runtime()
    sid = runtime.new_session_id()
    runtime.sessions_save(sid, "t", [{"role": "user", "content": "hi"}])
    assert [s["id"] for s in runtime.sessions_list()] == [sid]
    assert runtime.sessions_load(sid)["messages"][0]["content"] == "hi"
    assert runtime.new_session_prompt().startswith("You are the local Agent on host alpha")
    runtime.sessions_delete(sid)
    assert runtime.sessions_list() == []


def test_slider_keys_on_logical_requester_not_envelope_src(server_room):
    """Transfer receipts: the ask envelope's src is the receiving host's own
    comm clone; the friend's slider must still govern (body.from wins)."""
    room = server_room
    room.rules.set_mode("alpha", "allow")
    requester_inbox = room.hub.relay.host_buffer("beta")  # answer dst host = beta
    _send_ask(
        room.hub,
        "beta:comm-alpha",  # envelope src: our OWN comm clone (transfer path)
        "alpha:local",
        {
            "from": "alpha:comm-beta",  # logical requester: the friend
            "action": "receive file",
            "path": "report.txt",
            "reason": "r",
            "question": "Accept file?",
        },
    )
    assert _wait(
        lambda: any(
            m.type == "answer" and m.body == {"value": "yes"} for m in _all_msgs(requester_inbox)
        )
    ), "allow-mode transfer ask was not auto-answered"
    assert room.cards.pending() == []


def test_slider_does_not_auto_allow_generic_confirm(server_room):
    room = server_room
    room.rules.set_mode("alpha", "allow")
    requester_inbox = room.hub.relay.register_local("alpha:comm-beta")
    _send_ask(
        room.hub,
        "alpha:comm-beta",
        "alpha:local",
        {"from": "alpha:comm-beta", "questions": [{"question": "Pick one?", "options": []}]},
    )
    time.sleep(0.3)
    assert not any(m.type == "answer" for m in _all_msgs(requester_inbox))
    assert room.cards.pending(), "generic inquire must still raise a card"


# ── client role ──


@pytest.fixture()
def client_room(tmp_path):
    hub = Hub("alphaserver", "tok", tmp_path / "server-data")
    hub.start()
    room = RoomClient(
        "beta",
        CFG,
        NullSink(),
        f"http://127.0.0.1:{hub.port}",
        "tok",
        llm=LLM,
        rules_path=tmp_path / "rules.json",
        sessions_dir=tmp_path / "client-sessions",
    )
    room.start()
    yield hub, room
    room.stop()
    hub.stop()


def test_client_join_cards_and_heartbeat_replay(client_room):
    hub, room = client_room
    assert hub.roster.known("beta")

    # direct delivery: ask envelope reaches beta:local through the poller fanout
    ask = _send_ask(
        hub, "alpha:comm-beta", "beta:local", {"question": "q?", "from": "alpha:comm-beta"}
    )
    assert _wait(room.cards.pending), "poller fanout never delivered the ask"

    # heartbeat replay: same ask id again (from pending_asks) must dedup
    rec = hub.asks.get(ask.id)
    room._replay_ask({"ask_id": rec["ask_id"], "src": rec["src"], "payload": rec["payload"]})
    assert len(room.cards.pending()) == 1

    # a different pending ask replays as a new card
    second = _send_ask(
        hub, "alpha:comm-beta", "beta:local", {"question": "q2?", "from": "alpha:comm-beta"}
    )
    rec2 = hub.asks.get(second.id)
    room._replay_ask({"ask_id": rec2["ask_id"], "src": rec2["src"], "payload": rec2["payload"]})
    assert _wait(lambda: len(room.cards.pending()) == 2)  # comparison, not a bare call


def test_client_roster_diff_spawns_comm_clone(client_room):
    hub, room = client_room
    hub.join("gamma", "127.0.0.1")  # roster entry only; beta learns via heartbeat
    room._heartbeat_once()
    assert "gamma" in room._peers()
    assert "gamma" in room._clones, "heartbeat diff never spawned comm clone"


def test_client_runtime_answers_via_remote_transport(client_room):
    hub, room = client_room
    ask = _send_ask(
        hub, "alpha:comm-beta", "beta:local", {"question": "q?", "from": "alpha:comm-beta"}
    )
    assert _wait(room.cards.pending)
    runtime = room.webui_runtime()
    assert runtime.route_answer(ask.id, "no") is True
    assert _wait(lambda: (hub.asks.get(ask.id) or {}).get("status") == "denied", timeout_s=5.0), (
        "answer envelope never resolved the hub ask registry"
    )


def test_client_sessions_stay_off_the_hub_disk(client_room):
    """Client conversations must never land in the peer-operated hub store."""

    hub, room = client_room
    backend = room._sessions_backend()
    backend.save("s1", "t", [{"role": "user", "content": "private"}])
    assert backend.load("s1")["messages"][0]["content"] == "private"
    assert hub.store.sessions.list_sessions() == []


def test_comm_turn_transcript_recorded_for_friend_view(server_room):
    """_record_comm_turn persists per-peer transcripts; comm_log returns them
    (session-style payload for the friend view)."""
    room = server_room

    class FakeAgent:
        subagents = {
            "s1": {
                "id": "s1",
                "call_id": "c1",
                "layer": 2,
                "goal": "g",
                "reply_format": "",
                "status": "done",
                "events": [],
            }
        }
        asks = [{"id": "a1", "questions": [], "answers": ["x"], "status": "answered"}]

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[beta:comm-alpha] hi"},
        {"role": "assistant", "content": "hello", "reasoning": "r"},
    ]
    room._record_comm_turn("beta", "chat", msgs, FakeAgent)
    runtime = room.webui_runtime()
    d = runtime.comm_log("beta")
    # the store stamps every turn-carried message with `ts` (friend view: merge
    # with the timestamped ask/event/mail rows); the wire content is untouched
    assert [{k: v for k, v in m.items() if k != "ts"} for m in d["messages"]] == msgs
    assert all(isinstance(m["ts"], float) for m in d["messages"] if m["role"] != "system")
    assert d["subagents"][0]["id"] == "s1"
    assert d["asks"][0]["id"] == "a1"
    assert isinstance(d["events"], list)


def test_chat_turn_after_a_clone_rebuild_keeps_the_earlier_transcript(server_room):
    """A comm clone rebuilt after its peer dropped off the roster starts with
    an empty history: that turn's chat record must extend the stored
    transcript, not replace it. 2026-09-10 real-machine finding — replacing
    there threw every earlier turn away (and the disk copy with it)."""

    class FakeAgent:
        subagents: dict = {}
        asks: list = []

    room = server_room
    room._record_comm_turn(
        "beta",
        "chat",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[beta:comm-alpha] 早先的留言"},
            {"role": "assistant", "content": "早先的回复"},
        ],
        FakeAgent,
    )
    # rebuilt clone: its history only knows this turn
    room._record_comm_turn(
        "beta",
        "chat",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[beta:comm-alpha] 新留言"},
            {"role": "assistant", "content": "新回复"},
        ],
        FakeAgent,
    )

    msgs = room.webui_runtime().comm_log("beta")["messages"]
    assert [m["content"] for m in msgs] == [
        "sys",
        "[beta:comm-alpha] 早先的留言",
        "早先的回复",
        "[beta:comm-alpha] 新留言",
        "新回复",
    ]


def test_chat_turn_with_cumulative_history_does_not_duplicate(server_room):
    """The normal case: the clone's history already contains the stored
    transcript, so the stored copy is dropped and the history continues it —
    no duplicated rows, and the earlier rows keep their original ts."""

    class FakeAgent:
        subagents: dict = {}
        asks: list = []

    room = server_room
    first = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[beta:comm-alpha] one"},
        {"role": "assistant", "content": "two"},
    ]
    room._record_comm_turn("beta", "chat", first, FakeAgent)
    stamped = room.webui_runtime().comm_log("beta")["messages"]
    room._record_comm_turn(
        "beta",
        "chat",
        [
            *first,
            {"role": "user", "content": "[beta:comm-alpha] three"},
            {"role": "assistant", "content": "four"},
        ],
        FakeAgent,
    )

    msgs = room.webui_runtime().comm_log("beta")["messages"]
    assert [m["content"] for m in msgs] == [
        "sys",
        "[beta:comm-alpha] one",
        "two",
        "[beta:comm-alpha] three",
        "four",
    ]
    # the older rows kept the ts they were first stored with
    before = [m["ts"] for m in stamped if m["role"] != "system"]
    after = [m["ts"] for m in msgs if m["role"] != "system"]
    assert after[: len(before)] == before
    assert after[-1] >= after[0]  # and the timeline stays ordered


def test_comm_log_http_route_returns_full_payload(server_room):
    """Regression: the /comm-log route re-wrapped the already-dict payload as
    {"messages": {messages, events, ...}} — the frontend iterated an object
    as an array, threw into its swallowed catch, and the friend view stayed
    blank forever. The route must return comm_log's payload unchanged."""
    import json
    import threading
    import urllib.request

    from fungi.server import make_webui_server

    room = server_room

    class NoState:
        subagents = {}
        asks: list = []

    room._record_comm_turn(
        "beta",
        "chat",
        [{"role": "assistant", "content": "hello"}],
        NoState,
    )

    server = make_webui_server(0, room.webui_runtime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/comm-log?host=beta", timeout=5.0
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
    assert isinstance(payload["messages"], list)
    assert payload["messages"][-1]["content"] == "hello"
    assert isinstance(payload["events"], list)


def test_comm_task_turn_appends_after_chat_transcript(server_room):
    room = server_room

    class NoState:
        subagents = {}
        asks: list = []

    chat = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[beta] hi"},
        {"role": "assistant", "content": "hello"},
    ]
    room._record_comm_turn("beta", "chat", chat, NoState)
    task = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "[TASK from beta]\nGoal: g"},
        {"role": "assistant", "content": "done"},
    ]
    room._record_comm_turn("beta", "task", task, NoState)
    d = server_room.webui_runtime().comm_log("beta")
    roles = [m["role"] for m in d["messages"]]
    assert roles[0] == "system"
    assert d["messages"][3]["content"].startswith("[TASK from beta]")
    assert d["messages"][-1]["content"] == "done"


def _pong_llm(_messages, _tool_defs):
    from fungi.llm import LLMResult

    return LLMResult(content="pong")


def test_delegate_roundtrip_between_server_and_client(tmp_path):
    """REAL cross-host delegate: alpha:local -> hub -> beta comm clone turn ->
    result envelope back to alpha:local -> pending resolved. Guards against
    the 'delegate with correct args hangs forever' class of failure."""
    import threading

    from fungi.llm import LLMResult  # noqa: F401

    server = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=_pong_llm,
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta",
            CFG,
            NullSink(),
            f"http://127.0.0.1:{server.hub.port}",
            "tok",
            llm=_pong_llm,
            sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(
                lambda: "beta" in (server.local.delegate_tools.peers_fn() or []), timeout_s=10
            ), "beta never appeared in alpha's roster"
            out: list[str] = []

            def run():
                out.append(
                    server.local.delegate_tools.delegate({"host": "beta", "goal": "ping test"})
                )

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=25)
            assert not thread.is_alive(), "delegate never returned"
            assert out and "pong" in out[0], out
        finally:
            client.stop()
    finally:
        server.stop()


def test_a_delivered_transfer_lands_whole_and_drops_the_staged_copy(tmp_path):
    """The user's own flow, both roles: send a file to a peer that has the slider
    on allow -> it lands whole in inbox/<sender>/ -> the hub's staged copy is
    dropped. The discard used to be refused (token sent in the body, read from
    the query), so every delivery left a full copy on the hub's disk."""
    import hashlib

    inbox = tmp_path / "inbox"
    cfg = Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox))
    server = RoomServer(
        "alpha",
        cfg,
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=_pong_llm,
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta",
            cfg,
            NullSink(),
            f"http://127.0.0.1:{server.hub.port}",
            "tok",
            llm=_pong_llm,
            sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(
                lambda: "beta" in server._clones and "alpha" in client._clones, timeout_s=15
            ), "comm clones never appeared"
            # the friend-view slider on the RECEIVING side, one per direction
            client.rules.set_mode("alpha", "allow")
            server.rules.set_mode("beta", "allow")

            src = tmp_path / "payload.bin"
            src.write_bytes(bytes(range(256)) * 8192)  # 2 MiB, not compressible
            want = hashlib.sha256(src.read_bytes()).hexdigest()

            for sender, peer in ((server, "beta"), (client, "alpha")):
                out = sender.comm_send_human(peer, file_path=str(src))
                assert out.get("ok") is True, out
                landed = inbox / sender.host / "payload.bin"
                assert _wait(
                    lambda p=landed: p.is_file() and p.stat().st_size == src.stat().st_size,
                    timeout_s=30,
                ), f"never landed for {sender.host}"
                assert hashlib.sha256(landed.read_bytes()).hexdigest() == want
                assert _wait(
                    lambda: not list(server.hub.transfers.root.glob("*__payload.bin")), timeout_s=10
                ), "the hub still holds a staged copy after delivery"
        finally:
            client.stop()
    finally:
        server.stop()


def test_the_senders_job_shows_what_the_hub_handed_the_peer(tmp_path):
    """第二步的字节数（§49）：收件人点同意之前一个字节都不动，点了之后 hub 报它推了
    多少 —— 用户 2026-09-19 现场问「进度条为什么卡死、对面为什么是 .part」：卡死的
    那一步当时没有任何字节可看，而 .part 正是正在落地的文件。"""
    inbox = tmp_path / "inbox"
    cfg = Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox))
    server = RoomServer(
        "alpha",
        cfg,
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=_pong_llm,
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta",
            cfg,
            NullSink(),
            f"http://127.0.0.1:{server.hub.port}",
            "tok",
            llm=_pong_llm,
            sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(
                lambda: "beta" in server._clones and "alpha" in client._clones, timeout_s=15
            ), "comm clones never appeared"

            src = tmp_path / "notes.bin"
            src.write_bytes(b"z" * 200_000)
            out = server.comm_send_human("beta", file_path=str(src), job="job-progress")
            assert out.get("ok") is True, out

            # the card is still unanswered: everything is in the room, nothing
            # has moved to the peer yet, and the page can say so
            assert _wait(lambda: client.cards.pending()), "the peer never got a card"
            job = server.webui_runtime().transfer_progress("job-progress")
            assert job["state"] == "sent" and job["phase"] == "deliver"
            assert job["delivery_total"] == src.stat().st_size
            assert job["delivered"] == 0
            assert job["tid"], "the page needs the staged id to ask the hub"

            card = client.cards.pending()[0]
            assert client.webui_runtime().route_answer(card["id"], "yes") is True
            assert _wait(
                lambda: server.webui_runtime().transfer_progress("job-progress")["state"] == "done",
                timeout_s=30,
            ), "the delivery never landed"
            done = server.webui_runtime().transfer_progress("job-progress")
            assert Path(done["saved"]).read_bytes() == src.read_bytes()
        finally:
            client.stop()
    finally:
        server.stop()


def test_comm_turn_streams_live_events_to_friend_view(tmp_path):
    """While a comm clone turn is in flight, its events stream into the
    per-peer live tape (friend-view spectating); when the turn ends the
    tape clears and the durable transcript takes over."""
    import threading

    from fungi.llm import LLMResult

    release = threading.Event()
    calls = []

    def gated_llm(messages, _tools):
        if not calls:
            calls.append(1)
            return LLMResult(
                content="",
                tool_calls=[
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "send_peer", "arguments": '{"text": "hi"}'},
                    }
                ],
            )
        calls.append(1)
        release.wait(timeout=10)
        return LLMResult(content="done")

    server = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=gated_llm,
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    try:
        client = RoomClient(
            "beta",
            CFG,
            NullSink(),
            f"http://127.0.0.1:{server.hub.port}",
            "tok",
            llm=gated_llm,
            sessions_dir=tmp_path / "cs",
            rules_path=tmp_path / "r2.json",
        )
        client.start()
        try:
            assert _wait(
                lambda: "beta" in (server.local.delegate_tools.peers_fn() or []), timeout_s=10
            ), "beta never appeared in alpha's roster"
            out: list[str] = []
            th = threading.Thread(
                target=lambda: out.append(
                    server.local.delegate_tools.delegate({"host": "beta", "goal": "live test"})
                ),
                daemon=True,
            )
            th.start()
            rt = client.webui_runtime()
            assert _wait(
                lambda: any(e["kind"] == "tool" for e in rt.comm_log("alpha")["live"]),
                timeout_s=10,
            ), "live tape never saw the tool event"
            release.set()
            th.join(timeout=25)
            assert not th.is_alive(), "delegate never returned"
            assert _wait(lambda: rt.comm_log("alpha")["live"] == [], timeout_s=10), (
                "live tape not cleared after the turn"
            )
            data = client._comm_store.load("comm-alpha")
            assert data and data["messages"], "turn never reached the durable transcript"
        finally:
            client.stop()
    finally:
        server.stop()


# ── turn persistence vs refresh/delete races ──


@pytest.fixture()
def gated_room(tmp_path):
    """Room whose WebUI turns block inside the LLM until the test releases them."""
    import threading

    from fungi.llm import LLMResult

    gate = threading.Event()
    calls: list[list] = []

    def slow_llm(messages, _tools):
        calls.append(list(messages))
        gate.wait(timeout=10)
        return LLMResult(content=f"reply-{len(calls)}")

    room = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "data",
        llm=slow_llm,
        rules_path=tmp_path / "rules.json",
    )
    room.start()
    yield room, gate, calls
    room.stop()


def _webui_server(room):
    import threading

    from fungi.server import make_webui_server

    server = make_webui_server(0, room.webui_runtime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_turn_persists_user_message_while_streaming(gated_room):
    """Regression: mid-turn the session existed only in the client's memory —
    a refresh showed an empty list entry (or nothing) while the turn ran on.
    The user message must be on disk from the moment the turn starts."""
    import threading

    room, gate, _calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: _post(
                port, "/chat", {"message": "hello there", "sessionId": None}
            ).read(),
            daemon=True,
        )
        thread.start()
        assert _wait(
            lambda: any(s["title"] == "hello there" for s in room.webui_runtime().sessions_list())
        ), "session never appeared on disk while the turn was running"
        sid = room.webui_runtime().sessions_list()[0]["id"]
        stored = room.webui_runtime().sessions_load(sid)
        row = stored["messages"][-1]
        assert (row["role"], row["content"]) == ("user", "hello there")
        assert isinstance(row.get("ts"), float)  # stamped for the hover label
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_queued_turn_gets_status_line_and_keeps_prior_reply(gated_room):
    """Regression: a second /chat into a busy session silently blocked on the
    session lock (looked dead) and its pre-lock context snapshot dropped the
    running turn's reply on save. It must announce the wait, then continue
    from the full prior context."""
    import threading

    room, gate, calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]
        turn1 = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "first", "sessionId": sid}).read(),
            daemon=True,
        )
        turn1.start()
        assert _wait(lambda: len(calls) == 1), "first turn never reached the LLM"

        resp2 = _post(port, "/chat", {"message": "second", "sessionId": sid})
        first_line = json.loads(resp2.readline().decode("utf-8"))
        assert first_line["type"] == "status", f"no queue feedback, got {first_line}"
        gate.set()
        turn1.join(timeout=10)
        rest = resp2.read().decode("utf-8")
        assert '"done"' in rest, "queued turn never finished"

        messages = room.webui_runtime().sessions_load(sid)["messages"]
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert messages[2]["content"] == "reply-1", "prior turn's reply lost from context"
        assert messages[4]["content"] == "reply-2"
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_delete_running_session_leaves_it_deleted(gated_room):
    """Regression: deleting a mid-turn session unlinked the file, but the
    turn's exit-path save resurrected it — a ghost entry reappeared in the
    list when the run finished. Deleting must abort the turn and stay deleted."""

    import threading

    room, gate, _calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]
        turn = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "work", "sessionId": sid}).read(),
            daemon=True,
        )
        turn.start()
        assert _wait(
            lambda: len((room.webui_runtime().sessions_load(sid) or {}).get("messages", [])) >= 2
        ), "user message never reached disk while the turn ran"
        urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/session?id={sid}", method="DELETE"),
            timeout=10,
        ).read()
        assert room.webui_runtime().sessions_load(sid) is None
        gate.set()
        turn.join(timeout=10)
        assert not turn.is_alive()
        assert room.webui_runtime().sessions_load(sid) is None, "deleted session resurrected"
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def _read_events(port, sid, sink_lines):
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/events?sessionId={sid}", timeout=15)
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if line:
            sink_lines.append(json.loads(line))


def test_events_replays_recorded_events_then_done(server_room):
    """Seed tape: a reattaching client must receive the recorded events in
    order, then the done marker."""
    import threading

    from fungi.server import _TURN_TAPES

    room = server_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        _TURN_TAPES["replay-sid"] = [
            {"type": "text", "content": "partial answer"},
            {"type": "sessionId", "content": "replay-sid"},
            {"type": "done", "content": None},
        ]
        lines: list[dict] = []
        reader = threading.Thread(
            target=_read_events, args=(port, "replay-sid", lines), daemon=True
        )
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive(), "reader never saw the done marker"
        assert [ev["type"] for ev in lines] == ["text", "sessionId", "done"]
        assert lines[0]["content"] == "partial answer"
    finally:
        _TURN_TAPES.pop("replay-sid", None)
        server.shutdown()
        server.server_close()


def test_events_returns_done_when_nothing_runs(server_room):
    room = server_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        lines: list[dict] = []
        _read_events(port, "never-existed", lines)
        assert [ev["type"] for ev in lines] == ["done"]
    finally:
        server.shutdown()
        server.server_close()


def test_two_new_sessions_in_the_same_second_do_not_collide(server_room):
    """端到端：连按两次「新建」拿到的 id 必须不同——id 就是文件名，撞了等于丢掉一个会话
    （秒级时间戳，2026-09-10）。"""

    room = server_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        ids = []
        for _ in range(2):
            with _post(port, "/new", {}) as resp:
                ids.append(json.loads(resp.read())["id"])
        assert ids[0] != ids[1]
        listed = {
            s["id"]
            for s in json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=10).read()
            )["sessions"]
        }
        assert set(ids) <= listed, f"{ids} vs {sorted(listed)}"
    finally:
        server.shutdown()
        server.server_close()


def test_events_follows_running_turn_and_reports_running_flag(gated_room):
    """Refresh mid-turn: the reattached client must stay connected while the
    turn runs, then receive its tail events + done; /sessions must flag the
    session as running meanwhile."""
    import threading

    room, gate, calls = gated_room
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]
        turn = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "work", "sessionId": sid}).read(),
            daemon=True,
        )
        turn.start()
        assert _wait(lambda: len(calls) == 1), "turn never reached the LLM"
        sessions = {
            s["id"]: s
            for s in json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=10).read()
            )["sessions"]
        }
        assert sessions[sid]["running"] is True
        lines: list[dict] = []
        reader = threading.Thread(target=_read_events, args=(port, sid, lines), daemon=True)
        reader.start()
        gate.set()

        def flag() -> bool:
            payload = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=10).read()
            )
            return {s["id"]: s for s in payload["sessions"]}[sid]["running"]

        assert _wait(lambda: not flag()), "running flag stayed set after the turn finished"
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


def test_tape_grace_pop_does_not_kill_next_turns_tape(tmp_path, monkeypatch):
    """Regression: after a turn finished, the tape's grace-pop timer fired by
    session id alone. A NEW turn starting in the same session inside the grace
    window had its LIVE tape deleted mid-run — a mid-turn reload then received
    a bare done and silently lost the live view. The pop must only remove the
    sealed generation's tape."""
    import threading

    from fungi import server as fungi_server
    from fungi.events import NullSink

    monkeypatch.setattr(fungi_server, "_TAPE_GRACE_S", 0.2)
    turn2_gate = threading.Event()
    calls: list[list] = []

    def slow_llm(messages, _tools):
        calls.append(list(messages))
        if len(calls) >= 2:
            turn2_gate.wait(timeout=10)
        from fungi.llm import LLMResult

        return LLMResult(content=f"reply-{len(calls)}")

    room = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "data",
        llm=slow_llm,
        rules_path=tmp_path / "rules.json",
    )
    room.start()
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        with _post(port, "/new", {}) as resp:
            sid = json.loads(resp.read())["id"]

        t1 = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "first", "sessionId": sid}).read(),
            daemon=True,
        )
        t1.start()
        t1.join(timeout=15)
        assert not t1.is_alive()

        # Turn 2 starts inside turn 1's grace window and stays gated mid-LLM.
        t2 = threading.Thread(
            target=lambda: _post(port, "/chat", {"message": "second", "sessionId": sid}).read(),
            daemon=True,
        )
        t2.start()
        assert _wait(lambda: len(calls) >= 2, timeout_s=10), "turn 2 never reached the LLM"
        time.sleep(0.5)  # let turn 1's grace timer fire

        payload = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions", timeout=10).read()
        )
        rec = {s["id"]: s for s in payload["sessions"]}[sid]
        assert rec["running"], "live turn's tape was popped by the previous turn's grace timer"

        # The tape must stream (hold open or deliver events), never answer a
        # bare done while the turn is still running.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/events?sessionId={sid}", timeout=2.0
        ) as resp:
            try:
                first = json.loads(resp.readline().decode("utf-8"))
            except TimeoutError:  # stream held open for the live tape: correct
                first = {"type": "text"}
        assert first["type"] != "done", "reattach saw a bare done for a running turn"
    finally:
        turn2_gate.set()
        t2.join(timeout=15)
        server.shutdown()
        server.server_close()
        room.stop()


def test_background_spawn_report_reactivates_session(tmp_path):
    """Async spawn end-to-end: /chat returns dispatched at once, the finished
    subagent lands in the pending registry, and /resume injects its report as
    a new turn's input while updating the persisted spawn record."""
    import urllib.request

    from fungi.llm import LLMResult
    from fungi.server import _PENDING_SPAWNS

    orch_calls: list = []

    def routed_llm(messages, _tools):
        first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if first_user.startswith("## Goal"):
            return LLMResult(content="42")  # the child task agent
        orch_calls.append(1)
        if len(orch_calls) == 1:
            return LLMResult(
                tool_calls=[
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {
                            "name": "spawn",
                            "arguments": json.dumps(
                                {"goal": "count slowly", "reply_format": "a number"}
                            ),
                        },
                    }
                ]
            )
        return LLMResult(content="the answer is 42")

    room = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "data",
        llm=routed_llm,
        rules_path=tmp_path / "rules.json",
    )
    room.start()
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        body = (
            _post(port, "/chat", {"message": "fan out", "sessionId": None}).read().decode("utf-8")
        )
        sid = next(
            json.loads(line)["content"]
            for line in body.splitlines()
            if line and json.loads(line)["type"] == "sessionId"
        )
        assert sid
        assert _wait(lambda: len(_PENDING_SPAWNS.get(sid, [])) == 1), (
            "finished background spawn never reached the pending registry"
        )

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/spawn-pending?sessionId={sid}", timeout=10
        ) as resp:
            assert json.loads(resp.read())["pending"] == 1

        resume_body = _post(port, "/resume", {"sessionId": sid}).read().decode("utf-8")
        assert '"done"' in resume_body

        stored = room.webui_runtime().sessions_load(sid)
        report = next(
            m
            for m in stored["messages"]
            if m["role"] == "user" and m["content"].startswith("[background report]")
        )
        assert "42" in report["content"]
        rec = stored["subagents"][0]
        assert rec["status"] == "done" and rec["answer"] == "42"
        assert stored["messages"][-1]["content"] == "the answer is 42"

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/spawn-pending?sessionId={sid}", timeout=10
        ) as resp:
            assert json.loads(resp.read())["pending"] == 0  # drained by the resume
    finally:
        server.shutdown()
        server.server_close()
        room.stop()


def test_stop_discards_pending_reports_and_aborts_running_spawn(tmp_path):
    """Stop means stop: /stop clears the pending registry (no 3s-later
    re-activation with a [background report]) and a still-running background
    child finishes as aborted without re-adding itself."""
    import threading
    import urllib.request

    from fungi.llm import LLMResult
    from fungi.server import _PENDING_SPAWNS

    child_started = threading.Event()
    child_go = threading.Event()

    def routed_llm(messages, _tools):
        first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if first_user.startswith("## Goal"):
            child_started.set()
            child_go.wait(timeout=15)  # hold the child until the test stops it
            return LLMResult(content="42")
        return LLMResult(
            tool_calls=[
                {
                    "id": "t1",
                    "type": "function",
                    "function": {
                        "name": "spawn",
                        "arguments": json.dumps(
                            {"goal": "count slowly", "reply_format": "a number"}
                        ),
                    },
                }
            ]
        )

    room = RoomServer(
        "alpha",
        CFG,
        NullSink(),
        "tok",
        tmp_path / "data",
        llm=routed_llm,
        rules_path=tmp_path / "rules.json",
    )
    room.start()
    server = _webui_server(room)
    try:
        port = server.server_address[1]
        body = (
            _post(port, "/chat", {"message": "fan out", "sessionId": None}).read().decode("utf-8")
        )
        sid = next(
            json.loads(line)["content"]
            for line in body.splitlines()
            if line and json.loads(line)["type"] == "sessionId"
        )
        assert child_started.wait(timeout=10), "background child never started"

        stop_body = _post(port, "/stop", {"sessionId": sid}).read().decode("utf-8")
        assert json.loads(stop_body)["ok"] is True

        child_go.set()  # let the child finish; it must NOT re-add itself
        assert _wait(lambda: not _PENDING_SPAWNS.get(sid)), (
            "aborted background spawn re-registered itself after /stop"
        )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/spawn-pending?sessionId={sid}", timeout=10
        ) as resp:
            assert json.loads(resp.read())["pending"] == 0

        resume_body = _post(port, "/resume", {"sessionId": sid}).read().decode("utf-8")
        assert '"injected": 0' in resume_body  # nothing left to re-activate
    finally:
        server.shutdown()
        server.server_close()
        room.stop()


def test_server_display_rename_is_live(server_room):
    """开房后改昵称：roster 里立刻是新名字（对面 5s 轮询 /peers 就看到）。"""
    room = server_room
    assert room.set_display("  花 酱  ") == "花 酱"  # hub collapses whitespace
    assert room.display == "花 酱"
    assert room.hub.roster.display(room.host) == "花 酱"


def test_client_display_rename_reaches_the_hub_roster(client_room):
    """客户端改昵称走 re-join：hub 上的 roster 立即刷新（roster.join 会更新 display）。"""
    hub, room = client_room
    assert hub.roster.display("beta") in ("", "beta")
    assert room.set_display("阿宝") == "阿宝"
    assert hub.roster.display("beta") == "阿宝"
    # peers listing carries it too
    assert {"name": "beta", "display": "阿宝"} in hub.roster.entries("alphaserver")


def test_client_token_hot_swap_is_verified(client_room):
    """房主换了 Token：客户端热更并校验；错的 Token 必须还原，不能让房间静默 403。"""
    hub, room = client_room
    assert room.client.token == "tok"

    hub.token = "tok2"  # host rotated: the old token now earns 403
    assert room.set_token("tok2") is True
    assert room.client.token == "tok2"
    assert room.client.heartbeat()["ok"] is True  # the room works again

    assert room.set_token("nope") is False
    assert room.client.token == "tok2"  # rolled back: the verified one stays
    assert room.client.heartbeat()["ok"] is True
