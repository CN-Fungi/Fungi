"""amail: hub mailbox, the amail tool, and courier-off direct delivery.

The mail contract the WebUI relies on: mail envelopes are consumed by the hub
(never relayed to clones), the mailbox survives offline readers, and courier-
off chat/transfer envelopes never wake the receiving agent.
"""

from fungi import config as config_mod
from fungi import room as room_mod
from fungi.clone.base import Clone
from fungi.clone.tools_comm import CommTools
from fungi.hub.app import Hub
from fungi.hub.mail import Mailbox
from fungi.pending import PendingAsks
from fungi.protocol import Envelope, deserialize

# ── Mailbox ──


def test_deliver_list_mark_read(tmp_path):
    box = Mailbox(tmp_path / "mail")
    out = box.deliver("alice", "bob:comm-alice", "你好", "正文")
    assert out["ok"]
    listed = box.list("alice")
    assert listed["unread"] == 1
    assert listed["mails"][0]["subject"] == "你好"
    assert listed["mails"][0]["read"] is False
    assert box.mark_read("alice", listed["mails"][0]["id"]) == {"ok": True}
    assert box.list("alice")["unread"] == 0


def test_deliver_unknown_host_and_boxes_are_independent(tmp_path):
    box = Mailbox(tmp_path / "mail")
    assert "error" in box.deliver("", "x", "s", "t")
    box.deliver("alice", "bob", "s", "t")
    assert box.list("bob")["mails"] == []


def test_box_rolls_off_oldest_past_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("fungi.hub.mail.MAX_BOX", 3)
    box = Mailbox(tmp_path / "mail")
    for i in range(5):
        box.deliver("a", "b", f"s{i}", "t")
    mails = box.list("a")["mails"]
    assert [m["subject"] for m in mails] == ["s2", "s3", "s4"]


def test_persistence_across_instances(tmp_path):
    Mailbox(tmp_path / "mail").deliver("a", "b", "s", "t")
    assert Mailbox(tmp_path / "mail").list("a")["unread"] == 1


def test_mail_envelope_survives_the_wire():
    """Regression: TYPES did not include "mail", so any mail envelope sent
    over the hub HTTP API (/api/send) was rejected with 400 bad type -- the
    in-process hub.send tests never exercised deserialization."""
    env = Envelope(
        src="alice:comm-bob",
        dst="bob:mail",
        type="mail",
        body={"from": "alice:human", "subject": "", "text": "hi"},
    )
    back = deserialize(env.serialize())
    assert back.type == "mail" and back.dst == "bob:mail" and back.body["text"] == "hi"


def test_deliver_pair_writes_both_ends_of_the_conversation(tmp_path):
    box = Mailbox(tmp_path / "mail")
    out = box.deliver_pair("alice:human", "bob", "", "周五交")
    assert out["ok"]
    got = box.list("bob")["mails"][0]
    assert got["peer"] == "alice" and got["mine"] is False and got["read"] is False
    sent = box.list("alice")["mails"][0]
    assert sent["peer"] == "bob" and sent["mine"] is True and sent["read"] is True
    assert box.list("bob")["unread"] == 1


def test_hub_consumes_mail_envelope_into_both_boxes(tmp_path):
    """Regression: parse_addr used to reject the "host:mail" role, so real
    hub delivery of the amail tool's envelope crashed with ProtocolError."""
    data_root = tmp_path / "data"
    hub = Hub(tmp_path, "tok", data_root, max_file_mb=10)
    hub.join("alice", "127.0.0.1:1")
    hub.join("bob", "127.0.0.1:2")
    out = hub.send(
        Envelope(
            src="alice:comm-bob",
            dst="bob:mail",
            type="mail",
            body={"from": "alice:human", "subject": "", "text": "hi"},
        )
    )
    assert out.get("ok") and out["status"] == "mailed"
    assert hub.mail.list("bob")["mails"][0]["body"] == "hi"
    assert hub.mail.list("alice")["mails"][0]["mine"] is True


# ── amail tool ──


def _comm_tools(tmp_path):
    sent = []

    class _Transport:
        def send(self, env):
            sent.append(env)
            return {"ok": True}

    tools = CommTools("alice:comm-bob", _Transport(), PendingAsks(), inbox_dir=tmp_path)
    return tools, sent


def test_amail_sends_mail_envelope(tmp_path):
    tools, sent = _comm_tools(tmp_path)
    out = tools.amail({"host": "bob", "subject": "计划", "body": "周五交"})
    assert out.startswith("MAILED")
    env = sent[0]
    assert env.type == "mail"
    assert env.dst == "bob:mail"
    assert env.body["subject"] == "计划"
    assert env.body["text"] == "周五交"


def test_amail_validation(tmp_path):
    tools, sent = _comm_tools(tmp_path)
    assert tools.amail({"host": "bob"}).startswith("ERROR")
    assert tools.amail({"host": "alice", "body": "self"}).startswith("ERROR")
    assert not sent


# ── courier-off direct delivery ──


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


def _clone(on_direct):
    cfg = config_mod.Config()
    return Clone(
        "alice:comm-bob", _FakeTransport(), cfg, sink=lambda *a, **k: None, on_direct=on_direct
    )


def test_dispatch_courier_off_direct_eats_chat():
    seen = []
    c = _clone(lambda env: seen.append(env) or True)
    c.dispatch(
        Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat", body={"text": "hello"})
    )
    assert len(seen) == 1
    assert c._work.empty()  # no LLM turn queued


def test_dispatch_courier_on_queues_turn():
    c = _clone(lambda env: False)
    c.dispatch(
        Envelope(src="bob:comm-alice", dst="alice:comm-bob", type="chat", body={"text": "hello"})
    )
    assert not c._work.empty()


def test_direct_transfer_answer_downloads_without_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(room_mod, "load_config", lambda: config_mod.Config(courier=False))
    inbox = tmp_path / "inbox"
    fake_transport = _FakeTransport()

    class _Rules:
        @staticmethod
        def allows(src):  # noqa: ARG004
            return False

    class _Local:
        transport = fake_transport

    class _Cards:
        @staticmethod
        def record(env):  # noqa: ARG004
            return True  # every ask reaches the card pipeline

    room = object.__new__(room_mod.RoomBase)
    room.host = "alice"
    room.cfg = config_mod.Config(inbox_dir=str(inbox))
    room.local_addr = "alice:local"
    room.rules = _Rules()
    room.cards = _Cards()
    room._direct_transfers = {}
    room._local = _Local()  # RoomBase.local reads this
    env = Envelope(
        src="bob:comm-alice",
        dst="alice:comm-bob",
        type="transfer",
        body={"id": "t1", "name": "doc.md", "size": 5, "reason": "hi", "from": "bob:comm-alice"},
    )
    assert room._courier_direct(env) is True
    assert env.id in room._direct_transfers  # ask reached the pipeline, awaiting user

    ask = Envelope(src=env.src, dst=room.local_addr, type="ask", id=env.id, body={})
    room._send_answer(ask, "yes")
    assert (inbox / "bob" / "doc.md").read_text() == "bytes"
    (ans,) = fake_transport.sent
    assert ans.type == "answer" and ans.reply_to == env.id
    assert ans.body["value"]["ok"] is True
    assert str(inbox / "bob") in ans.body["value"]["saved"]
