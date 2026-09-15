"""Friends / comm log / chat fallback / file transfer contract tests."""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pytest

from fungi.clone.base import RemoteTransport
from fungi.clone.comm import build_comm_clone
from fungi.clone.delegate import DelegateTools
from fungi.config import Config
from fungi.events import NullSink
from fungi.llm import LLMResult
from fungi.pending import PendingAsks
from fungi.protocol import Envelope

CFG = Config(api_key="k", endpoint="e", model="m")


def tool_call(name: str, args: dict, call_id: str = "t1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class ScriptedLLM:
    def __init__(self, results):
        self.results = list(results)

    def __call__(self, _messages, _tool_defs):
        return self.results.pop(0)


def _joined_room(room):
    hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    return hub, clients


# ── comm log ──


def test_comm_log_records_and_merges_directions(room):
    _hub, clients = _joined_room(room)
    a = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "hi beta"}
    )
    b = Envelope(
        src="beta:comm-alpha", dst="alpha:comm-beta", type="chat", body={"text": "hi alpha"}
    )
    clients["alpha"].send(a)
    clients["beta"].send(b)
    hub = room[0]
    rows = hub.commlog.read("alpha", "beta")
    assert [r["text"] for r in rows] == ["hi beta", "hi alpha"]
    assert rows[0]["src"] == "alpha:comm-beta"
    assert rows[0]["kind"] == "chat"


def test_peers_endpoint_excludes_self(room):
    _hub, clients = _joined_room(room)
    code, out = clients["alpha"].get("/api/peers?host=alpha&token=room-token")
    assert code == 200
    assert out["peers"] == [{"name": "beta", "display": ""}]  # display records, not bare names


# ── the turn's text is a report to this host's user, not a message ──


def test_the_turn_text_is_a_report_and_never_reaches_the_peer(room):
    """2026-09-10 user instruction (supersedes the 2026-09-03 fallback): what
    the counterpart hears comes from send_peer only. A turn's own text is the
    report to this host's user — shipping it as a message is what put private
    reports on the wire and had both couriers answering them for rounds."""
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM([LLMResult(content="files: public/a.txt, public/b.txt")])
    clone = build_comm_clone(
        "beta", "alpha", RemoteTransport(clients["beta"]), CFG, NullSink(), llm=fake
    )
    chat = Envelope(
        src="alpha:comm-beta", dst="beta:comm-alpha", type="chat", body={"text": "what files?"}
    )
    clients["alpha"].send(chat)
    messages, _cursor = clients["beta"].poll_env("beta")
    clone.run_turn(messages[0])
    replies, _cursor = clients["alpha"].poll_env("alpha")
    chats = [e for e in replies if e.type == "chat"]
    assert not chats, f"a report escaped to the peer: {[c.body for c in chats]}"
    # …while the report itself is kept for this host's user (friend view)
    assert clone.history[-1]["content"] == "files: public/a.txt, public/b.txt"


def test_one_send_peer_sends_exactly_one_message(room):
    _hub, clients = _joined_room(room)
    fake = ScriptedLLM(
        [
            LLMResult(content="", tool_calls=[tool_call("send_peer", {"text": "explicit"})]),
            LLMResult(content="explicit"),
        ]
    )
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
    chats = [e for e in replies if e.type == "chat"]
    assert [c.body["text"] for c in chats] == ["explicit"]


# ── transfer staging + authorization (hub API) ──


def test_transfer_stage_download_and_host_guard(room):
    hub, clients = _joined_room(room)
    clients["alpha"].fs("write", "public/report.txt", content="payload-123")
    code, out = clients["alpha"].post(
        "/api/transfer",
        {
            "token": "room-token",
            "host": "alpha",
            "path": "public/report.txt",
            "name": "report.txt",
            "to": "beta",
        },
    )
    assert code == 200 and out["ok"]
    assert out["size"] == len("payload-123")

    base = f"http://127.0.0.1:{hub.port}"
    url_ok = f"{base}/api/transfer?id={out['id']}&host=beta&token=room-token"
    with urllib.request.urlopen(url_ok, timeout=10) as resp:
        assert resp.read() == b"payload-123"

    url_bad = f"{base}/api/transfer?id={out['id']}&host=alpha&token=room-token"
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url_bad, timeout=10).read()


def test_transfer_rejects_unknown_and_self(room):
    _hub, clients = _joined_room(room)
    clients["alpha"].fs("write", "public/x.txt", content="x")
    code, out = clients["alpha"].post(
        "/api/transfer",
        {
            "token": "room-token",
            "host": "alpha",
            "path": "public/x.txt",
            "name": "x.txt",
            "to": "ghost",
        },
    )
    assert code == 400 and "unknown host" in out["error"]
    code, out = clients["alpha"].post(
        "/api/transfer",
        {
            "token": "room-token",
            "host": "alpha",
            "path": "public/x.txt",
            "name": "x.txt",
            "to": "alpha",
        },
    )
    assert code == 400


def test_transfer_path_guard_blocks_foreign_home(room):
    _hub, clients = _joined_room(room)
    _code, _out = clients["beta"].post(
        "/api/transfer",
        {
            "token": "room-token",
            "host": "beta",
            "path": "homes/alpha/secret.md",
            "name": "secret.md",
            "to": "alpha",
        },
    )
    assert _code == 400  # store guard: non-owner read needs consent


# ── receiving side: consent -> download -> inbox ──


def _stage(hub_clients, host="alpha", path="public/report.txt", name="report.txt", to="beta"):
    _hub, clients = hub_clients
    clients[host].fs("write", path, content="payload-123")
    code, out = clients[host].post(
        "/api/transfer",
        {"token": "room-token", "host": host, "path": path, "name": name, "to": to},
    )
    assert code == 200, out
    return out


def _answer_ask(clone, clients, value):
    """Wait for the consent ask in beta's buffer, answer it, dispatch the answer."""
    ask = None
    deadline = time.monotonic() + 5.0
    while ask is None and time.monotonic() < deadline:
        msgs, _cursor = clients["beta"].poll_env("beta")
        for m in msgs:
            if m.type == "ask":
                ask = m
        if ask is None:
            time.sleep(0.05)
    assert ask is not None, "consent ask never reached the receiving host"
    clients["beta"].send(
        Envelope(
            src="beta:local",
            dst="beta:comm-alpha",
            type="answer",
            body={"value": value},
            reply_to=ask.id,
        )
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        msgs, _cursor = clients["beta"].poll_env("beta")
        for m in msgs:
            if m.type == "answer" and m.reply_to == ask.id:
                clone.dispatch(m)
                return
        time.sleep(0.05)


def test_receive_transfer_accepted_lands_in_inbox(room, tmp_path):
    _hub, clients = _joined_room(room)
    staged = _stage((room[0], clients))
    clone = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        ask_timeout_s=5,
        inbox_dir=tmp_path / "inbox",
    )
    env = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="transfer",
        body={
            "id": staged["id"],
            "name": staged["name"],
            "size": staged["size"],
            "reason": "you asked for it",
            "from": "alpha:comm-beta",
        },
    )
    result = {}
    t = threading.Thread(target=lambda: result.update(clone.on_transfer(env)))
    t.start()
    _answer_ask(clone, clients, "yes")
    t.join(timeout=10)
    assert result.get("ok") is True, result
    assert (tmp_path / "inbox" / "alpha" / "report.txt").read_text() == "payload-123"


def test_receive_transfer_declined(room, tmp_path):
    _hub, clients = _joined_room(room)
    staged = _stage((room[0], clients))
    clone = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        ask_timeout_s=5,
        inbox_dir=tmp_path / "inbox",
    )
    env = Envelope(
        src="alpha:comm-beta",
        dst="beta:comm-alpha",
        type="transfer",
        body={
            "id": staged["id"],
            "name": staged["name"],
            "size": staged["size"],
            "reason": "",
            "from": "alpha:comm-beta",
        },
    )
    result = {}
    t = threading.Thread(target=lambda: result.update(clone.on_transfer(env)))
    t.start()
    _answer_ask(clone, clients, "no")
    t.join(timeout=10)
    assert result.get("ok") is False
    assert "declined" in result.get("error", "")
    assert not (tmp_path / "inbox" / "alpha").exists()


# ── local clone send_file (2026-09-04: the user-facing clone had no send path) ──


def test_local_send_file_delivers_via_receiver_consent(room, tmp_path):
    _hub, clients = _joined_room(room)
    src = tmp_path / "photo.png"
    src.write_bytes(b"PNG-bytes")
    tools = DelegateTools(
        "alpha:local",
        RemoteTransport(clients["alpha"]),
        PendingAsks(),
        lambda: ["beta"],
        timeout_s=10.0,
    )
    comm = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        ask_timeout_s=10,
        inbox_dir=tmp_path / "inbox",
    )
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(
            out=tools.send_file({"host": "beta", "path": str(src), "reason": "here you go"})
        )
    )
    worker.start()
    transfer = None
    deadline = time.monotonic() + 5
    while transfer is None and time.monotonic() < deadline:
        msgs, _cursor = clients["beta"].poll_env("beta")
        for m in msgs:
            if m.type == "transfer":
                transfer = m
        if transfer is None:
            time.sleep(0.05)
    assert transfer is not None, "transfer envelope never reached beta"
    receiver = threading.Thread(target=lambda: comm.run_turn(transfer))
    receiver.start()
    _answer_ask(comm, clients, "yes")
    receiver.join(timeout=10)
    # the local clone's poll loop would dispatch this; do it manually here
    resolved = False
    deadline = time.monotonic() + 5
    while not resolved and time.monotonic() < deadline:
        msgs, _cursor = clients["alpha"].poll_env("alpha")
        for m in msgs:
            if m.type == "result" and m.reply_to:
                tools.pending.resolve(m.reply_to, m.body)
                resolved = True
        if not resolved:
            time.sleep(0.05)
    worker.join(timeout=10)
    assert "DELIVERED" in result.get("out", ""), result
    assert (tmp_path / "inbox" / "alpha" / "photo.png").read_bytes() == b"PNG-bytes"


def test_local_send_file_zips_folders(room, tmp_path):
    _hub, clients = _joined_room(room)
    folder = tmp_path / "拾荒集"
    (folder / "raw").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha", encoding="utf-8")
    (folder / "raw" / "b.txt").write_text("beta", encoding="utf-8")
    tools = DelegateTools(
        "alpha:local",
        RemoteTransport(clients["alpha"]),
        PendingAsks(),
        lambda: ["beta"],
        timeout_s=10.0,
    )
    comm = build_comm_clone(
        "beta",
        "alpha",
        RemoteTransport(clients["beta"]),
        CFG,
        NullSink(),
        ask_timeout_s=10,
        inbox_dir=tmp_path / "inbox",
    )
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(out=tools.send_file({"host": "beta", "path": str(folder)}))
    )
    worker.start()
    transfer = None
    deadline = time.monotonic() + 5
    while transfer is None and time.monotonic() < deadline:
        msgs, _cursor = clients["beta"].poll_env("beta")
        for m in msgs:
            if m.type == "transfer":
                transfer = m
        if transfer is None:
            time.sleep(0.05)
    assert transfer is not None, "transfer envelope never reached beta"
    assert transfer.body["name"] == "拾荒集.zip"
    receiver = threading.Thread(target=lambda: comm.run_turn(transfer))
    receiver.start()
    _answer_ask(comm, clients, "yes")
    receiver.join(timeout=10)
    resolved = False
    deadline = time.monotonic() + 5
    while not resolved and time.monotonic() < deadline:
        msgs, _cursor = clients["alpha"].poll_env("alpha")
        for m in msgs:
            if m.type == "result" and m.reply_to:
                tools.pending.resolve(m.reply_to, m.body)
                resolved = True
        if not resolved:
            time.sleep(0.05)
    worker.join(timeout=10)
    assert "DELIVERED" in result.get("out", ""), result
    landed = tmp_path / "inbox" / "alpha" / "拾荒集.zip"
    with zipfile.ZipFile(landed) as zf:
        assert sorted(zf.namelist()) == ["拾荒集/a.txt", "拾荒集/raw/b.txt"]


def test_local_send_file_rejects_unknown_peer():
    tools = DelegateTools(
        "alpha:local",
        RemoteTransport(None),  # guard rejects before any transport use
        PendingAsks(),
        lambda: [],
        timeout_s=5,
    )
    out = tools.send_file({"host": "ghost", "path": "x.txt"})
    assert "unknown or offline peer" in out


def test_local_delegate_rejects_unknown_peer():
    tools = DelegateTools(
        "alpha:local",
        RemoteTransport(None),  # guard rejects before any transport use
        PendingAsks(),
        lambda: [],
        timeout_s=5,
    )
    out = tools.delegate({"host": "ghost", "goal": "g"})
    assert "unknown or offline peer" in out


def test_local_delegate_missing_args_lists_received_keys():
    tools = DelegateTools(
        "alpha:local", RemoteTransport(None), PendingAsks(), lambda: [], timeout_s=5
    )
    out = tools.delegate({"goal": "g", "context": "c"})
    assert "host" in out
    assert "['context', 'goal']" in out


def test_local_delegate_fails_fast_on_bounce():
    class BounceTransport:
        def send(self, _env):
            return {"ok": False, "status": "bounced"}

    tools = DelegateTools(
        "alpha:local", BounceTransport(), PendingAsks(), lambda: ["beta"], timeout_s=30
    )
    t0 = time.monotonic()
    out = tools.delegate({"host": "beta", "goal": "g"})
    assert "not reachable" in out
    assert time.monotonic() - t0 < 2


def test_local_delegate_wakes_on_abort():
    class OkTransport:
        def __init__(self):
            self.sent = []

        def send(self, env):
            self.sent.append(env)
            return {"ok": True, "status": "delivered"}

    transport = OkTransport()
    stop = threading.Event()
    tools = DelegateTools("alpha:local", transport, PendingAsks(), lambda: ["beta"], timeout_s=60)
    tools.abort_fn = stop.is_set
    out: list[str] = []

    def run():
        out.append(tools.delegate({"host": "beta", "goal": "g"}))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert transport.sent, "envelope never sent"
    assert "stopped" in out[0]
