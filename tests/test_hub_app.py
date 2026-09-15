"""Hub integration: real HTTP server, two simulated hosts over urllib."""

import json
import socket
import threading
import urllib.request

from conftest import Client

from fungi.hub.app import Hub
from fungi.protocol import Envelope


def _send(client, src: str, dst: str, text: str, mid: str = "") -> dict:
    env = Envelope(src=src, dst=dst, type="chat", body={"text": text}, id=mid)
    _code, out = client.post("/api/send", {"token": client.token, "envelope": env.serialize()})
    return out


def test_join_and_peers(room):
    _hub, clients = room
    code, out = clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    assert code == 200 and out["new"] is True and out["peers"] == []
    code, out = clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    assert out["peers"] == ["alpha"]
    # rejoin is not an error and not "new"
    code, out = clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    assert out["new"] is False


def test_join_rejects_unsafe_host_names(room):
    _hub, clients = room
    for name in ("\U0001f602", "a/b", "a b", "-x", "x" * 33):
        code, out = clients["alpha"].post("/api/join", {"name": name, "token": "room-token"})
        assert code == 400 and "bad name" in out["error"]
    code, out = clients["alpha"].post("/api/join", {"name": "A-b_9", "token": "room-token"})
    assert code == 200 and out["ok"] is True


def test_join_carries_display_name(room):
    _hub, clients = room
    code, out = clients["alpha"].post(
        "/api/join", {"name": "alpha", "token": "room-token", "display": "\U0001f602阿法"}
    )
    assert code == 200 and out["roster"] == []  # emoji display is fine (presentation only)
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token", "display": "β"})
    code, out = clients["beta"].get("/api/peers?host=beta&token=room-token")
    assert out["peers"] == [{"name": "alpha", "display": "\U0001f602阿法"}]
    # re-join refreshes the nickname
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token", "display": "新"})
    _code, out = clients["beta"].get("/api/peers?host=beta&token=room-token")
    assert out["peers"] == [{"name": "alpha", "display": "新"}]
    # heartbeat carries the roster for client-side display caching
    _code, hb = clients["beta"].post("/api/heartbeat", {"name": "beta", "token": "room-token"})
    assert {"name": "alpha", "display": "新"} in hb["roster"]


def test_display_sanitized_not_rejected(room):
    _hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    clients["beta"].post(
        "/api/join",
        {"name": "beta", "token": "room-token", "display": "a\x01b\x7f x  y " + "z" * 80},
    )
    _code, out = clients["alpha"].get("/api/peers?host=alpha&token=room-token")
    assert out["peers"] == [{"name": "beta", "display": "ab x y " + "z" * (64 - 7)}]


def test_hub_binds_requested_port(room):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        fixed = probe.getsockname()[1]
    hub = Hub("srv", "room-token", room[0].store.root.parent, port=fixed)
    hub.start()
    try:
        assert hub.port == fixed
        req = urllib.request.Request(
            f"http://127.0.0.1:{fixed}/api/join",
            data=json.dumps({"name": "alpha", "token": "room-token"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert json.loads(resp.read())["ok"] is True
    finally:
        hub.stop()


def test_bad_token_rejected(room):
    _hub, clients = room
    code, _out = clients["alpha"].post("/api/join", {"name": "alpha", "token": "wrong"})
    assert code == 403
    code, _out = clients["alpha"].get("/api/poll?host=alpha&token=wrong")
    assert code == 403


def test_chat_via_relay_and_dedup(room):
    _hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    out = _send(clients["alpha"], "alpha:comm-beta", "beta:comm-alpha", "ping", mid="m1")
    assert out == {"ok": True, "status": "queued"}

    _code, polled = clients["beta"].poll_raw("beta")
    assert len(polled["messages"]) == 1
    msg = polled["messages"][0]
    assert msg["src"] == "alpha:comm-beta"
    assert msg["body"] == {"text": "ping"}
    # drained + dedup: replay the same id, poll again → nothing new
    _send(clients["alpha"], "alpha:comm-beta", "beta:comm-alpha", "ping", mid="m1")
    _code, polled = clients["beta"].poll_raw("beta", after=polled["cursor"])
    assert polled["messages"] == []


def test_unreachable_bounces_err_back(room):
    _hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    out = _send(clients["alpha"], "alpha:local", "ghost:local", "hi")
    assert out["ok"] is False
    _code, polled = clients["alpha"].poll_raw("alpha")
    assert polled["messages"][0]["type"] == "err"


def test_heartbeat_replays_pending_asks(room):
    hub, clients = room
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    _code, out = clients["beta"].post("/api/heartbeat", {"name": "beta", "token": "room-token"})
    assert out["pending_asks"] == []
    ask = hub.asks.open("beta", {"action": "write", "path": "homes/beta/x"})
    _code, out = clients["beta"].post("/api/heartbeat", {"name": "beta", "token": "room-token"})
    assert [a["ask_id"] for a in out["pending_asks"]] == [ask["ask_id"]]
    hub.asks.resolve(ask["ask_id"], value="yes")
    _code, out = clients["beta"].post("/api/heartbeat", {"name": "beta", "token": "room-token"})
    assert out["pending_asks"] == []


def test_fs_consent_gate_over_http(room):
    hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    base = {"token": "room-token", "host": "alpha"}

    code, out = clients["alpha"].post(
        "/api/fs/write", {**base, "path": "public/x.txt", "content": "v1"}
    )
    assert code == 200
    code, out = clients["alpha"].post("/api/fs/read", {**base, "path": "public/x.txt"})
    assert out["result"] == "v1"
    code, out = clients["alpha"].post(
        "/api/fs/edit", {**base, "path": "public/x.txt", "old_string": "v1", "new_string": "v2"}
    )
    assert "Edited" in out["result"]

    # beta's home: denied without consent, allowed with answered consent
    code, _out = clients["alpha"].post(
        "/api/fs/write", {**base, "path": "homes/beta/doc.md", "content": "x"}
    )
    assert code == 403
    ask = hub.asks.open("beta", {"action": "write"})
    hub.asks.resolve(ask["ask_id"], value="yes")
    code, _out = clients["alpha"].post(
        "/api/fs/write",
        {**base, "path": "homes/beta/doc.md", "content": "from alpha", "consent_id": ask["ask_id"]},
    )
    assert code == 200
    code, out = clients["beta"].post(
        "/api/fs/read", {"token": "room-token", "host": "beta", "path": "homes/beta/doc.md"}
    )
    assert out["result"] == "from alpha"

    # guard rejections surface as 403
    for path in ("sessions/s.json", "../x", "unknown/y"):
        code, _out = clients["alpha"].post("/api/fs/read", {**base, "path": path})
        assert code == 403


def test_sessions_api(room):
    _hub, clients = room
    code, _out = clients["alpha"].post(
        "/api/save",
        {
            "token": "room-token",
            "id": "s1",
            "title": "t",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert code == 200
    code, out = clients["alpha"].get("/api/sessions?token=room-token")
    assert out["sessions"][0]["id"] == "s1" and out["sessions"][0]["msgCount"] == 1
    code, out = clients["alpha"].get("/api/session?token=room-token&id=s1")
    assert out["messages"][0]["content"] == "hi"
    code, _out = clients["alpha"].post("/api/session/delete", {"token": "room-token", "id": "s1"})
    code, out = clients["alpha"].get("/api/sessions?token=room-token")
    assert out["sessions"] == []


def test_leave_stops_poll(room):
    _hub, clients = room
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    clients["beta"].post("/api/leave", {"name": "beta", "token": "room-token"})
    code, _out = clients["beta"].poll_raw("beta")
    assert code == 404


def test_reaper_removes_silent_hosts(room):
    hub, clients = room
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    done = threading.Event()

    def force_reap():
        hub.roster.heartbeat_timeout = 0.0
        for name in hub.roster.reap():
            hub.relay.drop_host(name)
        done.set()

    force_reap()
    assert done.is_set()
    code, _out = clients["beta"].poll_raw("beta")
    assert code == 404


def test_transfer_upload_roundtrip_and_guard(room, tmp_path):
    _hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    src = tmp_path / "report.bin"
    src.write_bytes(b"local-bytes-123")

    out = clients["alpha"].upload_transfer(str(src), "report.bin", "beta")
    assert out.get("ok") is True, out
    assert out["size"] == len("local-bytes-123")

    base = f"http://127.0.0.1:{room[0].port}"
    with urllib.request.urlopen(
        f"{base}/api/transfer?id={out['id']}&host=beta&token=room-token", timeout=10
    ) as resp:
        assert resp.read() == b"local-bytes-123"

    # unknown receiver and self-send are rejected
    out = clients["alpha"].upload_transfer(str(src), "report.bin", "ghost")
    assert "unknown host" in out.get("error", "")
    out = clients["alpha"].upload_transfer(str(src), "report.bin", "alpha")
    assert "error" in out


def test_transfer_upload_enforces_size_cap(tmp_path):
    hub = Hub("srv", "room-token", tmp_path, max_file_mb=0)  # cap = 1 MiB
    hub.start()
    try:
        client = Client(f"http://127.0.0.1:{hub.port}", "room-token", "alpha")
        client.post("/api/join", {"name": "alpha", "token": "room-token"})
        client.post("/api/join", {"name": "beta", "token": "room-token"})
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (1024 * 1024 + 1))
        out = client.upload_transfer(str(big), "big.bin", "beta")
        assert "too large" in out.get("error", "")
    finally:
        hub.stop()


def test_send_rejects_hosts_that_would_become_file_names(room):
    """The peer writes src/dst and the hub turns the host part into data/ file
    names (data/mail/<host>.jsonl, the comm log) plus a relay key: a host that
    is not a legal hostname must bounce before it reaches those joins."""
    hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    mail_root = hub.mail.root
    for dst in ("../../evil:mail", "a/b:local", "-x:mail", "x" * 40 + ":local"):
        code, out = clients["alpha"].post(
            "/api/send",
            {
                "token": "room-token",
                "envelope": Envelope(
                    src="alpha:local",
                    dst=dst,
                    type="mail",
                    body={"from": "alpha:human", "text": "hi"},
                ).serialize(),
            },
        )
        assert out.get("status") == "bounced", (dst, code, out)
    assert not (mail_root.is_dir() and list(mail_root.iterdir()))
