"""Human direct sends from the friend view (composer -> /comm-send).

The contract: the envelope bypasses the local courier entirely and lands on
the peer's comm clone address with explicit human attribution; how it is
received is decided by the PEER's courier switch (on -> courier relays it
faithfully; off -> straight into their transcript / consent cards, no agent).
The transcript write path must merge, never replace, and stay lock-protected.
"""

import itertools
import json
import threading
import time
from pathlib import Path

from fungi import config as config_mod
from fungi import room as room_mod
from fungi.clone.base import Clone, LocalTransport
from fungi.events import NullSink
from fungi.hub.mail import Mailbox
from fungi.hub.relay import Relay
from fungi.llm import LLMResult
from fungi.protocol import Envelope
from fungi.room import RoomClient, RoomServer
from fungi.session import SessionStore


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, env):
        self.sent.append(env)

    def poll(self, after, timeout):
        return [], after

    def fs(self, op, path, **kw):
        return {"error": "no hub"}

    def download_transfer(self, transfer_id, dest):
        dest.write_text("bytes")


class _FakeClone:
    def __init__(self, addr, transport):
        self.addr = addr
        self.transport = transport


class _Rules:
    @staticmethod
    def allows(src):  # noqa: ARG004
        return False


def _room(tmp_path):
    room = object.__new__(room_mod.RoomBase)
    room.host = "alice"
    room.display = "小爱"
    room.cfg = config_mod.Config()
    room.local_addr = "alice:local"
    room._guard = threading.Lock()
    room._comm_locks = {}
    room._comm_store = SessionStore(tmp_path / "comm-sessions")
    room._live_tapes = {}
    room._live_lock = threading.Lock()
    room._direct_transfers = {}
    room.xfer_jobs = room_mod.TransferJobs()
    room.rules = _Rules()
    return room


def _wire_clone(room, peer="bob"):
    transport = _FakeTransport()
    room._clones = {peer: _FakeClone(f"alice:comm-{peer}", transport)}
    return transport


def test_comm_send_human_text_is_a_mail_envelope(tmp_path):
    room = _room(tmp_path)
    transport = _wire_clone(room)
    out = room.comm_send_human("bob", text="  自由留言  ")
    assert out == {"ok": True, "kind": "chat"}
    (env,) = transport.sent
    assert env.type == "mail"
    assert env.src == "alice:comm-bob"
    assert env.dst == "bob:mail"
    assert env.body["from"] == "alice:human"
    assert env.body["text"] == "自由留言"
    # Text no longer lands in the comm transcript (it renders from the mail store)
    assert (room._comm_store.load("comm-bob") or {}).get("messages") in (None, [])


def test_comm_send_human_transfer_stages_and_sends(tmp_path):
    room = _room(tmp_path)
    transport = _wire_clone(room)
    src = tmp_path / "plan.txt"
    src.write_text("hello")
    transport.upload_transfer = lambda path, name, to_host, progress=None: {
        "id": "t9",
        "name": name,
        "size": src.stat().st_size,
    }
    out = room.comm_send_human("bob", file_path=str(src))
    assert out == {"ok": True, "kind": "transfer", "name": "plan.txt"}
    (env,) = transport.sent
    assert env.type == "transfer"
    assert env.dst == "bob:comm-alice"
    assert env.body["id"] == "t9"
    assert env.body["from_human"] is True
    assert env.body["from"] == "alice:local"


def test_comm_send_human_without_clone_fails(tmp_path):
    room = _room(tmp_path)
    room._clones = {}
    assert "error" in room.comm_send_human("ghost", text="hi")
    assert "error" in room.comm_send_human("bob", file_path=str(tmp_path / "nope.txt"))


def test_direct_download_lands_in_repo_root_even_from_foreign_cwd(tmp_path, monkeypatch):
    """Relative Path("inbox") broke when the WebUI HTTP thread ran from another
    cwd; the fallback must be PROJECT_ROOT/inbox/<src_host>/."""
    monkeypatch.chdir(tmp_path)  # cwd is NOT the repo root
    room = _room(tmp_path)
    staged = {}

    def _download(tid, dest):
        staged[tid] = dest
        dest.write_bytes(b"payload")

    room._local = type(
        "L",
        (),
        {
            "transport": type(
                "T",
                (),
                {
                    "download_transfer": staticmethod(_download),
                    "discard_transfer": staticmethod(lambda tid: staged.pop(tid, None)),
                },
            )()
        },
    )()
    env = Envelope(
        id="t1",
        src="bob:comm-alice",
        dst="alice:local",
        type="transfer",
        body={"id": "t1", "name": "报告.txt", "size": 7},
    )
    out = room._direct_download(env, "bob")
    assert out["ok"] is True
    saved = Path(out["saved"])
    try:
        assert saved == config_mod.PROJECT_ROOT / "inbox" / "bob" / saved.name
        assert saved.parent == config_mod.PROJECT_ROOT / "inbox" / "bob"
        assert saved.read_bytes() == b"payload"
        assert "t1" not in staged  # hub staged copy discarded
    finally:
        saved.unlink(missing_ok=True)


def test_courier_off_plain_chat_keeps_clone_prefix_and_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    room = _room(tmp_path)
    room._comm_store.save(
        "comm-bob",
        "comm: bob",
        [
            {"role": "user", "content": "[bob:comm-alice] earlier"},
        ],
    )
    chat = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat", body={"text": "hello"})
    assert room._courier_direct(chat) is True
    msgs = room._comm_store.load("comm-bob")["messages"]
    assert len(msgs) == 2
    assert msgs[1]["content"] == "[bob:comm-alice] hello"
    assert "sender" not in msgs[1]


def test_courier_off_human_transfer_card_names_the_human(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    room = _room(tmp_path)

    class _Cards:
        @staticmethod
        def record(env):
            room.last_ask_body = env.body
            return True

    room.cards = _Cards()
    env = Envelope(
        src="bob:comm-alice",
        dst="alice:comm-bob",
        type="transfer",
        body={
            "id": "t1",
            "name": "doc.md",
            "size": 5,
            "reason": "hi",
            "from": "bob:comm-alice",
            "from_human": True,
            "sender_name": "阿宝",
        },
    )
    assert room._courier_direct(env) is True
    assert "来自 阿宝 的用户" in room.last_ask_body["question"]


def test_courier_on_render_input_attributes_the_human():
    cfg = config_mod.Config()
    clone = Clone("alice:comm-bob", _FakeTransport(), cfg, sink=lambda *a, **k: None)
    env = Envelope(
        src="bob:comm-alice",
        dst="alice:comm-bob",
        type="chat",
        body={"text": "在吗", "from_human": True, "sender_name": "阿宝"},
    )
    assert clone.render_input(env) == "[来自 阿宝 的用户] 在吗"
    plain = Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat", body={"text": "在吗"})
    assert clone.render_input(plain) == "[bob:comm-alice] 在吗"


def test_concurrent_transcript_writers_keep_every_message(tmp_path):
    room = _room(tmp_path)
    n = 40
    counter = itertools.count()

    def _writer():
        for _ in range(n // 4):
            room._append_comm_message("bob", {"role": "user", "content": f"m{next(counter)}"})

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    msgs = room._comm_store.load("comm-bob")["messages"]
    assert len(msgs) == n
    assert len({m["content"] for m in msgs}) == n  # no lost update


# ── end-to-end: two real rooms, human direct chat ──


class _Scripted:
    def __init__(self, results):
        self.results = list(results)

    def __call__(self, _messages, tool_defs):
        if self.results:
            return self.results.pop(0)
        return LLMResult(content="(idle)")


def _wait(fn, timeout_s=15.0):
    end = time.time() + timeout_s
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def _two_rooms(tmp_path, monkeypatch, courier):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=courier))
    llm_alpha = _Scripted([LLMResult(content="alpha idle")])
    llm_beta = _Scripted([LLMResult(content="beta here")])
    server = RoomServer(
        "alpha",
        config_mod.Config(api_key="k", endpoint="e", model="m"),
        NullSink(),
        "tok",
        tmp_path / "d1",
        llm=llm_alpha,
        rules_path=tmp_path / "r1.json",
    )
    server.start()
    client = RoomClient(
        "beta",
        config_mod.Config(api_key="k", endpoint="e", model="m"),
        NullSink(),
        f"http://127.0.0.1:{server.hub.port}",
        "tok",
        llm=llm_beta,
        sessions_dir=tmp_path / "cs",
        rules_path=tmp_path / "r2.json",
    )
    client.start()
    return server, client, llm_beta


def test_human_text_mail_lands_in_both_mailboxes_without_agent(tmp_path, monkeypatch):
    """Human friend-view text is unified with amail: both mailboxes get the
    record (peer unread, sender pre-read) and no agent ever wakes — the
    courier switch no longer decides text delivery."""
    server, client, llm_beta = _two_rooms(tmp_path, monkeypatch, courier=False)
    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert _wait(lambda: client._clones.get("alpha") is not None)
        assert server.comm_send_human("beta", text="人类直发")["ok"]
        assert _wait(lambda: server.hub.mail.list("beta")["mails"])
        sent = server.hub.mail.list("alpha")["mails"]
        got = server.hub.mail.list("beta")["mails"]
        assert sent and got
        assert sent[0]["mine"] is True and sent[0]["read"] is True and sent[0]["peer"] == "beta"
        assert got[0]["mine"] is False and got[0]["read"] is False and got[0]["peer"] == "alpha"
        assert got[0]["body"] == "人类直发"
        # friend-view payload carries the thread with this peer
        assert [m["body"] for m in server.webui_runtime().comm_log("beta")["mails"]] == ["人类直发"]
        # zero agent involvement on the receiving side
        assert llm_beta.results, "mail delivery must not wake the agent"
        assert not (client._comm_store.load("comm-alpha") or {}).get("messages")
    finally:
        client.stop()
        server.stop()


def test_courier_feedback_wakes_our_courier_and_never_the_peer(tmp_path, monkeypatch):
    """2026-09-11 user instruction: every report card carries a feedback box,
    and what it sends is between the owner and their OWN courier. Our courier
    runs a turn with it (that is how it fixes its own mistakes, calendar
    included) while the counterpart hears nothing and its agent never wakes."""
    server, client, llm_beta = _two_rooms(tmp_path, monkeypatch, courier=True)
    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert _wait(lambda: client._clones.get("alpha") is not None)
        assert server.comm_note_human("beta", "时间记错了，是 17:15 不是 17:30")["ok"]
        assert _wait(
            lambda: any(
                str(m.get("content") or "").startswith("[评价]")
                for m in (server._comm_store.load("comm-beta") or {}).get("messages") or []
            )
        )
        msgs = (server._comm_store.load("comm-beta") or {})["messages"]
        assert "[评价] 时间记错了，是 17:15 不是 17:30" in [m.get("content") for m in msgs]
        assert msgs[-1]["role"] == "assistant", msgs[-1]  # our courier answered it
        # nothing about it went out: no envelope, no mirror row, peer asleep
        assert server.hub.commlog.read("alpha", "beta") == []
        assert not (client._comm_store.load("comm-alpha") or {}).get("messages")
        assert llm_beta.results, "a note must never reach the counterpart's agent"
    finally:
        client.stop()
        server.stop()


def test_an_empty_note_never_wakes_the_courier(tmp_path, monkeypatch):
    """The box may be left empty (2026-09-11): an empty submit sends nothing
    and must not spend a turn."""
    server, client, _llm = _two_rooms(tmp_path, monkeypatch, courier=True)
    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert "error" in server.comm_note_human("beta", "   ")
        assert "error" in server.comm_note_human("ghost", "hi")  # no courier there
        time.sleep(0.4)
        assert not (server._comm_store.load("comm-beta") or {}).get("messages")
    finally:
        client.stop()
        server.stop()


def test_comm_log_mails_are_filtered_to_the_peer(tmp_path):
    room = _room(tmp_path)
    mailbox = Mailbox(tmp_path / "mail")
    mailbox.deliver("alice", "bob:human", "", "for bob", peer="bob")
    mailbox.deliver("alice", "carol:human", "", "for carol", peer="carol")
    room.hub = type(
        "H",
        (),
        {
            "commlog": type(
                "C",
                (),
                {
                    "read": staticmethod(lambda *_: []),
                },
            )(),
            "mail": mailbox,
        },
    )()
    rt = object.__new__(room_mod.RoomRuntime)
    rt.room = room
    out = rt.comm_log("bob")
    assert [m["body"] for m in out["mails"]] == ["for bob"]


# ── courier mail wake: a human text message must reach the receiving courier ──
#
# 470b07c decoupled text from the courier so delivery never needs an agent —
# but it also stopped the receiving agent from ever hearing about the message:
# the hub consumes mail envelopes, so the courier could not answer. The room
# now polls the mailbox and wakes the comm clone when the courier switch is on.


class _MailTransport:
    def __init__(self, mails):
        self.mails = mails

    def mail(self):
        return {"mails": list(self.mails)}


class _RecordingClone:
    addr = "alice:comm-bob"

    def __init__(self):
        self.envs = []

    def dispatch(self, env):
        self.envs.append(env)


def _mail_room(tmp_path, mails):
    room = _room(tmp_path)
    room._local = type("L", (), {"transport": _MailTransport(mails)})()
    clone = _RecordingClone()
    room._clones = {"bob": clone}
    return room, clone


def test_courier_on_wakes_once_for_unseen_mail(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=True))
    mails = [
        {"id": "m1", "from": "bob:human", "peer": "bob", "body": "在吗", "mine": False},
        {"id": "m2", "from": "alice:human", "peer": "bob", "body": "自己的", "mine": True},
    ]
    room, clone = _mail_room(tmp_path, mails)
    seen = set()
    room._mail_poll_once(seen)
    assert len(clone.envs) == 1  # the outbound copy (mine) must not wake anyone
    env = clone.envs[0]
    assert (env.type, env.dst, env.body["text"]) == ("chat", "alice:comm-bob", "在吗")
    assert env.body["from_human"] is True
    assert env.body["sender_name"] == "bob"
    room._mail_poll_once(seen)  # same mail again: no second wake
    assert len(clone.envs) == 1


def test_courier_off_mail_never_wakes_but_advances_the_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    mails = [{"id": "m1", "from": "bob:human", "peer": "bob", "body": "在吗", "mine": False}]
    room, clone = _mail_room(tmp_path, mails)
    seen = set()
    room._mail_poll_once(seen)
    assert clone.envs == []
    # The cursor advances even while off: flipping the switch on later must
    # not replay the whole backlog as fresh turns.
    assert seen == {"m1"}


def test_courier_on_human_mail_wakes_the_receiving_courier(tmp_path, monkeypatch):
    server, client, _llm_beta = _two_rooms(tmp_path, monkeypatch, courier=True)
    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert _wait(lambda: client._clones.get("alpha") is not None)
        assert server.comm_send_human("beta", text="在吗")["ok"]
        # The receiving courier wakes on the mailbox and its turn records the
        # human line into the comm transcript (friend view).
        assert _wait(lambda: (client._comm_store.load("comm-alpha") or {}).get("messages"))
        msgs = client._comm_store.load("comm-alpha")["messages"]
        assert any("在吗" in str(m.get("content")) for m in msgs)
        # the receiving courier sees it attributed to the SENDER host (alpha)
        assert any("来自 alpha 的用户" in str(m.get("content")) for m in msgs)
    finally:
        client.stop()
        server.stop()


def test_courier_wake_answers_the_human_end_to_end(tmp_path, monkeypatch):
    """Acceptance for the user-facing bug: with the courier ON, a human text
    message must come back answered. The answer rides send_peer — the only
    channel that reaches the counterpart (comm's turn text is a report to this
    host's user) — so it must show up in the SENDER's friend-view transcript,
    and the report itself must stay home."""
    server, client, _llm = _two_rooms(tmp_path, monkeypatch, courier=True)

    class _CourierLLM:
        """Answers through send_peer, then goes silent so the two couriers do
        not ping-pong (the real prompt says: never reply just to acknowledge)."""

        def __init__(self):
            self.calls = 0

        def __call__(self, _messages, _tool_defs):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(
                    content="已回复：收到，我在",
                    tool_calls=[
                        {
                            "id": "s1",
                            "type": "function",
                            "function": {
                                "name": "send_peer",
                                "arguments": json.dumps({"text": "收到，我在"}),
                            },
                        }
                    ],
                )
            return LLMResult(content="<<SILENT>>")

    try:
        assert _wait(lambda: server._clones.get("beta") is not None)
        assert _wait(lambda: client._clones.get("alpha") is not None)
        client._clones["alpha"].llm = _CourierLLM()
        assert server.comm_send_human("beta", text="在吗")["ok"]
        assert _wait(
            lambda: any(
                "收到，我在" in str(m.get("content"))
                for m in ((server._comm_store.load("comm-beta") or {}).get("messages") or [])
            ),
            timeout_s=20.0,
        )
        # …and the courier's own report never crossed the wire.
        rows = (server._comm_store.load("comm-beta") or {}).get("messages") or []
        assert not any("已回复：收到，我在" in str(m.get("content")) for m in rows), rows
    finally:
        client.stop()
        server.stop()


def test_local_transport_mail_reads_this_host_mailbox(tmp_path):
    """Server-role rooms read mail through LocalTransport, not the HTTP client:
    that path must resolve its own host (a lost `self.host` assignment made it
    raise, and the watch loop swallowed the exception silently)."""
    mailbox = Mailbox(tmp_path / "mail")
    mailbox.deliver("alpha", "beta:human", "", "hi", peer="beta")
    hub = type("H", (), {"mail": mailbox})()
    transport = LocalTransport(Relay("alpha"), "alpha:comm-beta", hub=hub)
    out = transport.mail()
    assert out["host"] == "alpha"
    assert [m["body"] for m in out["mails"]] == ["hi"]


def test_room_stop_actually_stops_comm_clones(tmp_path):
    """stop() cleared the clone dict before the loop, so remove_comm_clone's
    pop() returned None and clone.stop() never ran: the poll/worker threads
    kept draining envelopes after the room was gone."""
    room = _room(tmp_path)
    room._stop = threading.Event()
    room._local = None
    room._webui = None
    stopped: list[str] = []

    class _Clone:
        def stop(self) -> None:
            stopped.append("stopped")

    room._clones = {"bob": _Clone()}
    room.stop()
    assert stopped == ["stopped"]
    assert room._clones == {}


def test_a_card_answer_carries_its_question_back_to_the_courier(tmp_path):
    """信使的 inquire 不阻塞，答复晚于那一轮才到：问题要跟着答复回去，
    否则信使只看到一句「周六有空」而不知道在答什么（RoomBase._send_answer）。"""
    room = _room(tmp_path)
    transport = _wire_clone(room)
    room._local = _FakeClone("alice:local", transport)
    ask = Envelope(
        src="alice:comm-bob",
        dst="alice:local",
        type="ask",
        body={
            "from": "alice:comm-bob",
            "questions": [{"question": "周六见面吗？", "options": [], "allow_custom": True}],
        },
    )
    room._send_answer(ask, "周六有空")
    (env,) = transport.sent
    assert env.type == "answer" and env.dst == "alice:comm-bob"
    assert env.body["value"] == "周六有空"
    assert env.body["questions"][0]["question"] == "周六见面吗？"


def test_a_consent_verdict_stays_a_bare_value(tmp_path):
    """同意类的答复（confirm/收文件）不带上问题：它对应的是一个还在等的工具，
    迟到的裁决不该被当成一轮新对话（见 Clone._answer_turn）。"""
    room = _room(tmp_path)
    transport = _wire_clone(room)
    room._local = _FakeClone("alice:local", transport)
    ask = Envelope(
        src="alice:comm-bob",
        dst="alice:local",
        type="ask",
        body={
            "from": "alice:comm-bob",
            "action": "write",
            "path": "homes/alice/x.md",
            "question": "Allow write?",
        },
    )
    room._send_answer(ask, "yes")
    (env,) = transport.sent
    assert env.body == {"value": "yes"}


def test_comm_send_human_reports_the_hub_upload_into_the_job(tmp_path):
    """发文件时浏览器自己造的 job id 拿到真实的字节进度：桌面端和手机端用同一个
    进度条，而字节只在服务器上流动（见 fungi/xfer.py）。"""
    room = _room(tmp_path)
    transport = _wire_clone(room)
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 10)
    seen: list[tuple] = []

    def fake_upload(path, name, to_host, progress=None):
        seen.append((path, name, to_host))
        if progress is not None:
            progress(4, 10)
        return {"id": "t9", "name": name, "size": 10}

    transport.upload_transfer = fake_upload
    out = room.comm_send_human("bob", file_path=str(src), job="job-1")
    assert out == {"ok": True, "kind": "transfer", "name": "big.bin"}
    assert seen == [(str(src), "big.bin", "bob")]
    job = room.xfer_jobs.get("job-1")
    assert job["state"] == "done"
    assert job["done"] == job["total"] == 10
    assert job["name"] == "big.bin"


def test_a_failed_upload_marks_the_job(tmp_path):
    """上传失败要落在 job 上：进度条不能停在半路假装还在传。"""
    room = _room(tmp_path)
    transport = _wire_clone(room)
    src = tmp_path / "x.bin"
    src.write_bytes(b"x")
    transport.upload_transfer = lambda path, name, to_host, progress=None: {"error": "hub is down"}
    out = room.comm_send_human("bob", file_path=str(src), job="job-2")
    assert out == {"error": "hub is down"}
    job = room.xfer_jobs.get("job-2")
    assert job["state"] == "error" and "hub is down" in job["error"]


def test_transfer_progress_for_an_unknown_job_is_an_error(tmp_path):
    room = _room(tmp_path)
    assert "error" in room.xfer_jobs.get("nope")
