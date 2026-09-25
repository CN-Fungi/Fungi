"""Hub integration: real HTTP server, two simulated hosts over urllib."""

import json
import socket
import threading
import urllib.error
import urllib.parse
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


def test_transfer_progress_counts_what_the_hub_handed_over(room, tmp_path):
    """§49: the hub is the one moving the last leg, so it is the only place that
    knows a delivery is progressing — and only the two ends may ask it."""
    _hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    src = tmp_path / "report.bin"
    src.write_bytes(b"x" * (3 * 1024 * 1024 + 7))  # past the 1 MiB report step
    out = clients["alpha"].upload_transfer(str(src), "report.bin", "beta")

    code, before = clients["alpha"].transfer_progress(out["id"])
    assert (code, before["sent"], before["total"]) == (200, 0, src.stat().st_size)

    dest = tmp_path / "landed.bin"
    clients["beta"].download_transfer(out["id"], dest)
    assert dest.read_bytes() == src.read_bytes()

    code, after = clients["beta"].transfer_progress(out["id"])
    assert code == 200
    assert after["sent"] == after["total"] == src.stat().st_size

    # a third host has no business watching someone else's transfer
    code, body = clients["srv"].transfer_progress(out["id"])
    assert code == 404 and "error" in body
    # and an unknown id is just unknown
    assert clients["beta"].transfer_progress("nope")[0] == 404


def test_transfer_discard_actually_drops_the_staged_copy(room, tmp_path):
    """The receiver's discard must drop the bytes. The client sends its token in
    the JSON body (HubClient._request); the route read it from the query, so the
    discard was always 403 and every delivered file kept a full staged copy on
    the hub's disk until restart — invisible, because both callers suppress the
    error."""
    hub, clients = room
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    src = tmp_path / "report.bin"
    src.write_bytes(b"round-trip")
    out = clients["alpha"].upload_transfer(str(src), "report.bin", "beta")
    dest = tmp_path / "landed.bin"
    clients["beta"].download_transfer(out["id"], dest)
    assert dest.read_bytes() == b"round-trip"

    # only the designated receiver may drop it, and only with the token
    assert clients["alpha"].discard_transfer(out["id"]) == {"ok": False}
    assert hub.transfers.fetchable(out["id"], "beta") is not None
    code, _ = clients["beta"].delete("/api/transfer", {"token": "nope", "id": out["id"]})
    assert code == 403

    assert clients["beta"].discard_transfer(out["id"]) == {"ok": True}
    assert not (hub.transfers.root / f"{out['id']}__report.bin").exists()
    assert hub.transfers.fetchable(out["id"], "beta") is None


def test_transfer_upload_stages_a_file_of_any_size(tmp_path):
    """No size cap any more (spec §48): staging is a 256 KiB-chunked stream to
    disk, so a file several chunks long lands whole and the record carries its
    real size."""
    hub = Hub("srv", "room-token", tmp_path)
    hub.start()
    try:
        client = Client(f"http://127.0.0.1:{hub.port}", "room-token", "alpha")
        client.post("/api/join", {"name": "alpha", "token": "room-token"})
        client.post("/api/join", {"name": "beta", "token": "room-token"})
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (3 * 1024 * 1024))
        out = client.upload_transfer(str(big), "big.bin", "beta")
        assert out["ok"] is True and out["size"] == big.stat().st_size
        staged = hub.transfers.root / f"{out['id']}__big.bin"
        assert staged.stat().st_size == big.stat().st_size
        assert staged.read_bytes() == big.read_bytes()
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


# ── ranged downloads (§50): several windows of one delivery at once ──


def _fetch(url: str, headers: dict | None = None) -> tuple:
    """GET, returning `(status, headers, body)` for answers that are not 200."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _staged(room, clients, tmp_path, payload: bytes, name: str = "report.bin") -> tuple[str, str]:
    """Upload `payload` alpha -> beta; returns the transfer id and its URL."""
    clients["alpha"].post("/api/join", {"name": "alpha", "token": "room-token"})
    clients["beta"].post("/api/join", {"name": "beta", "token": "room-token"})
    src = tmp_path / name
    src.write_bytes(payload)
    out = clients["alpha"].upload_transfer(str(src), name, "beta")
    assert out.get("ok") is True, out
    base = f"http://127.0.0.1:{room[0].port}"
    return out["id"], f"{base}/api/transfer?id={out['id']}&host=beta&token=room-token"


def test_a_range_request_serves_exactly_that_window(room, tmp_path):
    """A window is bytes, not a hint: 206, the window's own length, and the
    bytes at that offset — a receiver that assembles them out of order still
    ends up with the sender's file."""
    payload = bytes(range(256)) * 16  # 4096 position-dependent bytes
    _tid, url = _staged(room, room[1], tmp_path, payload)

    status, headers, body = _fetch(url, {"Range": "bytes=100-199"})
    assert status == 206
    assert headers["Content-Range"] == f"bytes 100-199/{len(payload)}"
    assert headers["Content-Length"] == "100"
    assert headers["Accept-Ranges"] == "bytes"
    assert body == payload[100:200]

    # open-ended, and clamped to the last byte like the RFC says
    status, headers, body = _fetch(url, {"Range": "bytes=4000-"})
    assert (status, headers["Content-Range"], body) == (
        206,
        f"bytes 4000-4095/{len(payload)}",
        payload[4000:],
    )
    status, headers, body = _fetch(url, {"Range": "bytes=4000-999999"})
    assert (status, headers["Content-Range"], len(body)) == (
        206,
        f"bytes 4000-4095/{len(payload)}",
        96,
    )

    # no range: the whole file, exactly as before §50 — and the hub says it
    # could have answered one
    status, headers, body = _fetch(url)
    assert (status, headers["Content-Length"], headers["Accept-Ranges"]) == (200, "4096", "bytes")
    assert body == payload


def test_a_range_past_the_end_is_416_and_serves_nothing(room, tmp_path):
    payload = bytes(range(256)) * 16
    _tid, url = _staged(room, room[1], tmp_path, payload)

    status, headers, body = _fetch(url, {"Range": "bytes=4096-"})
    assert status == 416
    assert headers["Content-Range"] == "bytes */4096"
    assert b"bytes" not in body  # the file's bytes, not our JSON saying so

    # ranges this server does not promise are ignored, never guessed at
    for guess in ("bytes=-100", "bytes=0-9,20-29", "items=0-9", "bytes=9-1"):
        status, headers, body = _fetch(url, {"Range": guess})
        assert status == 200, guess
        assert body == payload, guess


def test_a_range_is_only_for_the_two_ends_of_the_transfer(room, tmp_path):
    payload = b"private-bytes"
    _tid, url = _staged(room, room[1], tmp_path, payload)

    status, _headers, _body = _fetch(url.replace("host=beta", "host=srv"), {"Range": "bytes=0-3"})
    assert status == 404


def test_delivery_progress_is_the_union_of_the_windows_asked_for(room, tmp_path):
    """§50: with several windows in flight a running total would double-count a
    retried window and (being per-request) could walk backwards. The count the
    sender's page reads is the union of the ranges that really arrived."""
    payload = bytes(range(256)) * 16
    tid, url = _staged(room, room[1], tmp_path, payload)
    _hub, clients = room

    def delivered() -> int:
        code, out = clients["beta"].transfer_progress(tid)
        assert code == 200
        assert out["total"] == len(payload)
        assert 0 <= out["sent"] <= out["total"], out
        return out["sent"]

    assert delivered() == 0
    _fetch(url, {"Range": "bytes=0-1023"})
    assert delivered() == 1024
    _fetch(url, {"Range": "bytes=2048-4095"})  # out of order: windows finish when they finish
    assert delivered() == 1024 + 2048
    _fetch(url, {"Range": "bytes=0-2047"})  # a retry overlapping what already arrived
    assert delivered() == len(payload)
    # and the same window again is still the same file, not another 4096 bytes
    _fetch(url, {"Range": "bytes=0-2047"})
    assert delivered() == len(payload)


def test_a_real_delivery_over_several_windows_lands_whole(room, tmp_path):
    """The receiver's own code over real sockets (§50): one hub, four windows
    in flight, and the sender's page reading the union of what arrived."""
    from fungi.hub.client import HubClient

    payload = bytes(range(256)) * (12 * 1024 * 1024 // 256)  # three windows
    tid, _url = _staged(room, room[1], tmp_path, payload, name="big.bin")
    land = tmp_path / "inbox"
    land.mkdir()
    dest = land / "big.bin"

    receiver = HubClient(f"http://127.0.0.1:{room[0].port}", "room-token", "beta")
    receiver.download_transfer(tid, dest)

    assert dest.read_bytes() == payload
    assert [p.name for p in land.iterdir()] == ["big.bin"], "a part file was left"
    out = receiver.transfer_progress(tid)
    assert out["sent"] == out["total"] == len(payload)


# ── an upload that stops early is continued, not restarted (§62) ──


def _upload_raw(
    base: str, params: dict, body: bytes, token: str = "room-token"
) -> tuple[int, dict]:
    """One upload body, with whatever resume parameters the caller wants.

    `total` (the whole file's length) is a query parameter rather than the
    Content-Length, which is what lets a test hand over a *tail* — the shape a
    sender has when its connection died mid-file.
    """
    q = urllib.parse.urlencode({"token": token, **params})
    req = urllib.request.Request(
        f"{base}/api/transfer/upload?{q}",
        data=body,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _pending(base: str, name: str, size: int, head: str, to: str = "beta") -> dict:
    q = urllib.parse.urlencode(
        {"token": "room-token", "host": "alpha", "to": to, "name": name, "size": size, "head": head}
    )
    with urllib.request.urlopen(f"{base}/api/transfer/pending?{q}", timeout=10) as resp:
        return json.loads(resp.read())


def _head_of(payload: bytes) -> str:
    """What the sender sends: the digest of the first 64 KiB (the whole file when
    it is shorter) — the only thing that can say two same-named, same-sized files
    are the same file, since nothing on the wire carries a checksum."""
    import hashlib

    return hashlib.sha256(payload[: 64 * 1024]).hexdigest()


def test_an_upload_that_stops_early_keeps_a_partial_staging(room, tmp_path):
    """The sender's connection died 60% in. What arrived stays where it is and
    nothing partial is offered to the receiver — an announcement here is how a
    half file gets delivered."""
    hub, clients = room
    base = f"http://127.0.0.1:{hub.port}"
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    payload = bytes(range(256)) * (400 * 1024 // 256)  # 400 KiB
    cut = 240 * 1024

    code, out = _upload_raw(
        base,
        {"host": "alpha", "to": "beta", "name": "big.bin", "total": len(payload)},
        payload[:cut],
    )

    assert code == 400 and out["partial"] is True, out
    assert out["received"] == cut and out["total"] == len(payload)
    rec = hub.transfers.state(out["id"], "alpha")
    assert rec is not None and rec["partial"] is True and rec["size"] == cut
    staged = hub.transfers.root / f"{out['id']}__big.bin"
    assert staged.stat().st_size == cut and staged.read_bytes() == payload[:cut]
    # the receiver cannot fetch it, and a third host cannot even ask about it
    assert hub.transfers.fetchable(out["id"], "beta") is None
    assert hub.transfers.state(out["id"], "srv") is None


def test_the_sender_is_told_what_is_already_staged(room, tmp_path):
    """The question a sender asks before sending a byte: is this file already
    here? Name, length and the head digest are what make the answer trustworthy —
    a different file of the same size must not match."""
    hub, clients = room
    base = f"http://127.0.0.1:{hub.port}"
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    payload = bytes(range(256)) * (400 * 1024 // 256)
    cut = 240 * 1024
    _code, part = _upload_raw(
        base,
        {"host": "alpha", "to": "beta", "name": "big.bin", "total": len(payload)},
        payload[:cut],
    )

    found = _pending(base, "big.bin", len(payload), _head_of(payload))
    assert found == {
        "ok": True,
        "id": part["id"],
        "received": cut,
        "partial": True,
        "name": "big.bin",
    }

    # same name and length, different file
    other = bytes(reversed(payload))
    assert _pending(base, "big.bin", len(payload), _head_of(other))["ok"] is False
    # not what this hub holds at all
    assert _pending(base, "elsewhere.bin", len(payload), _head_of(payload))["ok"] is False
    # and a receiver that is not a known host gets a refusal, not a lookup
    assert _pending(base, "big.bin", len(payload), _head_of(payload), to="ghost")["ok"] is False


def test_a_resume_hands_over_the_rest_and_the_file_is_fetchable(room, tmp_path):
    """The whole leg: half the bytes arrived, the sender comes back with the
    other half, and the receiver gets one whole file."""
    hub, clients = room
    base = f"http://127.0.0.1:{hub.port}"
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    payload = bytes(range(256)) * (400 * 1024 // 256)
    cut = 240 * 1024
    _code, part = _upload_raw(
        base,
        {"host": "alpha", "to": "beta", "name": "big.bin", "total": len(payload)},
        payload[:cut],
    )

    code, out = _upload_raw(
        base,
        {
            "host": "alpha",
            "to": "beta",
            "name": "big.bin",
            "total": len(payload),
            "id": part["id"],
            "offset": cut,
        },
        payload[cut:],
    )

    assert code == 200 and out["ok"] is True, out
    assert out["id"] == part["id"], "the resume minted a second staging"
    assert out["size"] == len(payload) and out["partial"] is False
    assert _pending(base, "big.bin", len(payload), _head_of(payload))["partial"] is False

    dest = tmp_path / "landed.bin"
    clients["beta"].download_transfer(out["id"], dest)
    assert dest.read_bytes() == payload


def test_a_resume_at_the_wrong_offset_is_refused_and_touches_nothing(room, tmp_path):
    """A body that starts somewhere other than where the staging ends cannot be
    appended to it — the tail would land in the middle of the file. Refuse, and
    leave what is there alone so the next attempt can still continue it."""
    hub, clients = room
    base = f"http://127.0.0.1:{hub.port}"
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    payload = bytes(range(256)) * (400 * 1024 // 256)
    cut = 240 * 1024
    _code, part = _upload_raw(
        base,
        {"host": "alpha", "to": "beta", "name": "big.bin", "total": len(payload)},
        payload[:cut],
    )
    staged = hub.transfers.root / f"{part['id']}__big.bin"

    code, out = _upload_raw(
        base,
        {
            "host": "alpha",
            "to": "beta",
            "name": "big.bin",
            "total": len(payload),
            "id": part["id"],
            "offset": 100 * 1024,
        },
        payload[100 * 1024 :],
    )

    assert code == 409 and out.get("restart") is True, out
    assert staged.read_bytes() == payload[:cut], "a refused resume wrote into the staging"


def test_the_sender_does_not_send_a_file_the_hub_already_holds(room, tmp_path, monkeypatch):
    """A re-send of the same file is a *delivery* again, not a second upload: the
    hub still has the staging, and the receiver may simply not have taken it yet."""
    from fungi.hub.client import HubClient

    hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    src = tmp_path / "report.bin"
    src.write_bytes(bytes(range(256)) * (400 * 1024 // 256))
    sender = HubClient(f"http://127.0.0.1:{hub.port}", "room-token", "alpha")

    first = sender.upload_transfer(str(src), "report.bin", "beta")
    assert first["ok"] is True and first["size"] == src.stat().st_size

    def _never(*_args, **_kw):
        raise AssertionError("the file was uploaded a second time")

    monkeypatch.setattr(sender, "_post_upload", _never)
    again = sender.upload_transfer(str(src), "report.bin", "beta")

    assert again == {
        "ok": True,
        "id": first["id"],
        "name": "report.bin",
        "size": src.stat().st_size,
        "staged": True,
    }


def test_the_sender_finishes_a_staging_an_earlier_attempt_left(room, tmp_path, monkeypatch):
    """The other half of the same question: the hub holds *part* of the file, so
    the sender hands over the rest and nothing more."""
    from fungi.hub.client import HubClient

    hub, clients = room
    base = f"http://127.0.0.1:{hub.port}"
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    payload = bytes(range(256)) * (400 * 1024 // 256)
    cut = 240 * 1024
    src = tmp_path / "report.bin"
    src.write_bytes(payload)
    _code, part = _upload_raw(
        base,
        {"host": "alpha", "to": "beta", "name": "report.bin", "total": len(payload)},
        payload[:cut],
    )
    sender = HubClient(base, "room-token", "alpha")
    asked: list[tuple] = []
    real = sender._post_upload

    def _spy(*args, **kw):
        asked.append((args[4], args[5]))  # (offset, resume id)
        return real(*args, **kw)

    monkeypatch.setattr(sender, "_post_upload", _spy)
    out = sender.upload_transfer(str(src), "report.bin", "beta")

    assert out["ok"] is True and out["id"] == part["id"]
    assert asked == [(cut, part["id"])], asked
    assert (hub.transfers.root / f"{part['id']}__report.bin").read_bytes() == payload


def test_the_sender_starts_over_when_the_staging_moved_on(room, tmp_path, monkeypatch):
    """Between asking and sending, the staging can be gone (swept, or another
    attempt finished it). The tail it was about to send cannot be attached to
    anything — so it is sent from the top instead, once."""
    from fungi.hub.client import HubClient

    _hub, clients = room
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    src = tmp_path / "report.bin"
    src.write_bytes(b"x" * (400 * 1024))
    sender = HubClient(f"http://127.0.0.1:{_hub.port}", "room-token", "alpha")

    monkeypatch.setattr(
        sender,
        "_staged_already",
        lambda *_a, **_k: {"id": "gone-already", "received": 100, "partial": True},
    )
    asked: list[tuple] = []
    real = sender._post_upload
    monkeypatch.setattr(
        sender,
        "_post_upload",
        lambda *args, **kw: (asked.append((args[4], args[5])), real(*args, **kw))[1],
    )

    out = sender.upload_transfer(str(src), "report.bin", "beta")

    assert out["ok"] is True and out["size"] == src.stat().st_size
    assert asked == [(100, "gone-already"), (0, "")], asked


def test_no_completed_transfer_is_reported_for_a_partial_staging(room):
    """`/api/transfer` is the only route that hands bytes over, and it must not
    serve a staging the sender is still filling up — not even to its receiver."""
    hub, clients = room
    base = f"http://127.0.0.1:{hub.port}"
    for name in ("alpha", "beta"):
        clients[name].post("/api/join", {"name": name, "token": "room-token"})
    payload = b"y" * (400 * 1024)
    _code, part = _upload_raw(
        base,
        {"host": "alpha", "to": "beta", "name": "big.bin", "total": len(payload)},
        payload[: 200 * 1024],
    )

    status, _headers, body = _fetch(
        f"{base}/api/transfer?id={part['id']}&host=beta&token=room-token"
    )
    assert status == 404, body
    # the progress route still answers its two ends, and reports the whole length
    code, progress = clients["alpha"].transfer_progress(part["id"])
    assert (code, progress["sent"], progress["total"]) == (200, 0, len(payload))
