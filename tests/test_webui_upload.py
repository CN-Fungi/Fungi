"""Mobile file upload: the raw-bytes wire, inbox landing, streaming to disk.

The wire is raw bytes with the name in X-Fungi-Filename (spec §48) — the old
multipart body was read into memory whole, which is the only reason a size cap
ever existed. 4 MiB+ in one request must not cost that much Python heap.
"""

import json
import socket
import threading
import tracemalloc
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from fungi import server as webui
from fungi.config import Config


@pytest.fixture()
def upload_env(tmp_path, monkeypatch):
    """WebUI server whose config lands uploads in tmp (no size cap any more)."""
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(
        webui,
        "load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox)),
    )

    class _TouchRuntime(webui.WebUIRuntime):
        pass

    srv = webui.make_webui_server(0, _TouchRuntime())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", inbox
    finally:
        srv.shutdown()
        srv.server_close()


def _post(url: str, body: bytes, name: str | None = "photo.bin") -> urllib.request.Request:
    headers = {"Content-Type": "application/octet-stream"}
    if name is not None:
        headers["X-Fungi-Filename"] = urllib.parse.quote(name)
    return urllib.request.Request(url + "/upload", data=body, headers=headers, method="POST")


def test_upload_lands_in_inbox_byte_for_byte(upload_env):
    """NULs and CRLFs must survive: nothing frames the body any more."""
    base, inbox = upload_env
    payload = b"\x00\x01\r\n\x00binary\xff" * 100
    with urllib.request.urlopen(_post(base, payload)) as resp:
        out = json.loads(resp.read())
    assert out["ok"] is True and out["size"] == len(payload)
    saved = Path(out["path"])
    assert saved.parent == inbox and saved.is_absolute()
    assert saved.read_bytes() == payload


def test_upload_keeps_a_cjk_filename(upload_env):
    """The name rides percent-encoded in a header. A header line is latin-1,
    which is exactly what used to mangle 拾荒集.zip on the multipart wire."""
    base, inbox = upload_env
    with urllib.request.urlopen(_post(base, b"zipzip", name="拾荒集.zip")) as resp:
        out = json.loads(resp.read())
    assert out["name"] == "拾荒集.zip"
    assert (inbox / "拾荒集.zip").read_bytes() == b"zipzip"


def test_upload_numbers_colliding_names(upload_env):
    base, inbox = upload_env
    for _ in range(2):
        with urllib.request.urlopen(_post(base, b"x")) as resp:
            json.loads(resp.read())
    assert sorted(p.name for p in inbox.iterdir()) == ["photo-1.bin", "photo.bin"]


def test_upload_without_a_name_is_rejected(upload_env):
    base, _ = upload_env
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(_post(base, b"x", name=None))
    assert err.value.code == 400


def test_upload_with_an_empty_body_is_rejected(upload_env):
    base, inbox = upload_env
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(_post(base, b""))
    assert err.value.code == 400
    assert not inbox.exists() or not any(inbox.iterdir())


def test_a_truncated_upload_leaves_no_half_file(upload_env):
    """Content-Length is a promise; a body that stops early must not leave a
    file the phone believes it sent."""
    base, inbox = upload_env
    host, port = base.split("//")[1].split(":")
    sock = socket.create_connection((host, int(port)), timeout=5)
    try:
        sock.sendall(
            b"POST /upload HTTP/1.1\r\nHost: x\r\n"
            b"X-Fungi-Filename: half.bin\r\nContent-Length: 4096\r\n"
            b"Connection: close\r\n\r\n" + b"z" * 64
        )
        sock.shutdown(socket.SHUT_WR)  # stop sending: the promise is broken
        head = sock.recv(4096).decode("utf-8", "replace")
    finally:
        sock.close()
    assert "400" in head.splitlines()[0], head
    assert not inbox.exists() or not any(inbox.iterdir())


# ── the same file, in windows (§51) ──


def _window(base, sid, name, offset, size, body) -> urllib.request.Request:
    q = urllib.parse.urlencode({"sid": sid, "offset": offset, "size": size})
    return urllib.request.Request(
        base + "/upload?" + q,
        data=body,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Fungi-Filename": urllib.parse.quote(name),
        },
        method="POST",
    )


def _send(req) -> dict:
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _status(base, sid) -> dict:
    with urllib.request.urlopen(base + "/upload?sid=" + sid, timeout=30) as resp:
        return json.loads(resp.read())


def test_the_host_says_whether_it_takes_windows(upload_env):
    """The one question a page asks before cutting a file up. An older host has
    no GET on this route, and the page then sends one body as it always did."""
    base, inbox = upload_env
    with urllib.request.urlopen(base + "/upload", timeout=10) as resp:
        out = json.loads(resp.read())
    assert out["ok"] is True and out["parts"] is True and out["min_part"] > 0
    assert not inbox.exists() or not any(inbox.iterdir())


def test_windows_land_the_file_byte_for_byte(upload_env):
    """Windows arrive in whatever order the network gives them; the file lands
    when the last one is covered, not when the last one was sent."""
    base, inbox = upload_env
    payload = bytes(range(256)) * (3 * 1024)  # 768 KiB, position-dependent
    third = len(payload) // 3
    windows = [(0, third), (third, 2 * third), (2 * third, len(payload))]
    sid = "sess-out-of-order"

    for lo, hi in (windows[1], windows[0]):  # out of order, and still no file
        out = _send(_window(base, sid, "talk.bin", lo, len(payload), payload[lo:hi]))
        assert out["ok"] is True and out["done"] is False, out
        assert out["missing"], out
    assert [p.name for p in inbox.iterdir()] == [f"talk.bin.{sid}.part"]

    lo, hi = windows[2]
    out = _send(_window(base, sid, "talk.bin", lo, len(payload), payload[lo:hi]))
    assert out["done"] is True and out["size"] == len(payload), out
    assert Path(out["path"]).read_bytes() == payload
    assert [p.name for p in inbox.iterdir()] == ["talk.bin"], "a part file was left"


def test_a_window_sent_twice_is_not_counted_twice(upload_env):
    """A retry re-sends the same range: the count is a union, so the file is
    still the sender's — and the number the page reads never overshoots."""
    base, inbox = upload_env
    payload = b"abcdefgh" * 512
    half = len(payload) // 2
    sid = "sess-retry"
    first = _send(_window(base, sid, "retry.bin", 0, len(payload), payload[:half]))
    again = _send(_window(base, sid, "retry.bin", 0, len(payload), payload[:half]))
    assert (first["received"], again["received"]) == (half, half)
    assert again["missing"] == [[half, len(payload)]]
    assert [p.name for p in inbox.iterdir()] == [f"retry.bin.{sid}.part"]

    out = _send(_window(base, sid, "retry.bin", half, len(payload), payload[half:]))
    assert out["done"] is True
    assert Path(out["path"]).read_bytes() == payload
    assert [p.name for p in inbox.iterdir()] == ["retry.bin"]


def test_a_missing_window_never_reaches_the_real_name(upload_env):
    """The §49 rule, one hop earlier: what the phone is missing is a question
    the host answers, and until the answer is "nothing" there is no file."""
    base, inbox = upload_env
    payload = b"q" * 900
    sid = "sess-gap"
    _send(_window(base, sid, "half.bin", 0, len(payload), payload[:300]))
    _send(_window(base, sid, "half.bin", 600, len(payload), payload[600:]))

    state = _status(base, sid)
    assert (state["done"], state["received"], state["missing"]) == (False, 600, [[300, 600]])
    assert [p.name for p in inbox.iterdir()] == [f"half.bin.{sid}.part"]

    out = _send(_window(base, sid, "half.bin", 300, len(payload), payload[300:600]))
    assert out["done"] is True
    assert Path(out["path"]).read_bytes() == payload
    assert [p.name for p in inbox.iterdir()] == ["half.bin"]


def test_a_truncated_window_keeps_what_arrived_and_says_what_is_missing(upload_env):
    """The body stops early (the phone went away). The bytes that did arrive
    stay on disk and the page is told which range to send again — in one body
    there is nothing to resume, so that shape still deletes the part."""
    base, inbox = upload_env
    host, port = base.split("//")[1].split(":")
    sock = socket.create_connection((host, int(port)), timeout=5)
    try:
        sock.sendall(
            b"POST /upload?sid=sess-cut&offset=0&size=4096 HTTP/1.1\r\nHost: x\r\n"
            b"X-Fungi-Filename: cut.bin\r\nContent-Length: 4096\r\n"
            b"Connection: close\r\n\r\n" + b"z" * 64
        )
        sock.shutdown(socket.SHUT_WR)
        raw = b""
        while True:  # Connection: close, so read to the end of the reply
            chunk = sock.recv(8192)
            if not chunk:
                break
            raw += chunk
    finally:
        sock.close()
    text = raw.decode("utf-8", "replace")
    assert "400" in text.splitlines()[0], text
    body = json.loads(text.split("\r\n\r\n", 1)[1])
    assert body["missing"] == [[64, 4096]], body
    assert [p.name for p in inbox.iterdir()] == ["cut.bin.sess-cut.part"]

    out = _send(_window(base, "sess-cut", "cut.bin", 64, 4096, b"z" * (4096 - 64)))
    assert out["done"] is True
    assert Path(out["path"]).read_bytes() == b"z" * 4096


def test_the_name_is_chosen_when_the_file_lands(upload_env):
    """A file that takes that name while the bytes are in flight gets numbered
    instead of overwritten — the collision is settled at commit, not at start."""
    base, inbox = upload_env
    _send(_post(base, b"first"))  # photo.bin exists before the windowed upload
    payload = b"later" * 100
    sid = "sess-named"
    out = _send(_window(base, sid, "photo.bin", 0, len(payload), payload))
    assert out["done"] is True and out["name"] == "photo-1.bin", out
    assert Path(out["path"]).read_bytes() == payload
    assert sorted(p.name for p in inbox.iterdir()) == ["photo-1.bin", "photo.bin"]


def test_a_window_that_would_walk_past_the_end_is_refused(upload_env):
    base, inbox = upload_env
    payload = b"12345"
    for query in (
        {"sid": "s", "offset": 3, "size": 5},  # 3 + 5 bytes promised > 5
        {"sid": "s", "offset": 2, "size": 0},
        {"sid": "", "offset": 0, "size": 5},  # no id: it would become a file name
        {"sid": "../evil", "offset": 0, "size": 5},
    ):
        q = urllib.parse.urlencode(query)
        req = urllib.request.Request(
            base + "/upload?" + q,
            data=payload,
            headers={"X-Fungi-Filename": "x.bin"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 400, query
    assert not inbox.exists() or not any(inbox.iterdir())


def test_a_session_the_host_forgot_is_gone_not_guessed(upload_env, monkeypatch):
    """Swept as stale (a page that walked away): the part goes with it, and the
    status route says so instead of pretending the upload is still running."""
    base, inbox = upload_env
    payload = b"p" * 2048
    _send(_window(base, "sess-stale", "old.bin", 0, len(payload), payload[:1024]))
    assert [p.name for p in inbox.iterdir()] == ["old.bin.sess-stale.part"]

    monkeypatch.setattr(webui, "UPLOAD_TTL_S", 0.0)
    with pytest.raises(urllib.error.HTTPError) as err:
        _status(base, "sess-stale")
    assert err.value.code == 404
    assert list(inbox.iterdir()) == [], "the swept session left its part behind"


# ── the other direction: PC -> phone (§52) ──


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _share(tmp_path, name: str = "shared.bin", body: bytes = b"") -> Path:
    path = tmp_path / name
    path.write_bytes(body or bytes(range(256)) * 40)  # 10240 position-dependent bytes
    return path


def test_meta_answers_the_size_without_sending_bytes(upload_env, tmp_path):
    """What a phone needs to plan windows with: name and size, no payload."""
    base, _inbox = upload_env
    src = _share(tmp_path)
    status, _headers, body = _get(base + "/download?meta=1&path=" + urllib.parse.quote(str(src)))
    assert status == 200
    assert json.loads(body) == {"ok": True, "name": "shared.bin", "size": src.stat().st_size}


def test_a_download_serves_the_file_with_a_name_for_the_browser(upload_env, tmp_path):
    base, _inbox = upload_env
    src = _share(tmp_path)
    status, headers, body = _get(base + "/download?path=" + urllib.parse.quote(str(src)))
    assert status == 200
    assert body == src.read_bytes()
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Length"] == str(src.stat().st_size)
    assert headers["Content-Disposition"].startswith('attachment; filename="shared.bin"')


def test_a_download_honours_a_range_the_way_the_hub_does(upload_env, tmp_path):
    """One Range contract for both servers (§50, §52): 206 for the window asked
    for, clamped at the end, 416 when it starts past it, whole file otherwise."""
    base, _inbox = upload_env
    src = _share(tmp_path)
    payload = src.read_bytes()
    url = base + "/download?path=" + urllib.parse.quote(str(src))

    status, headers, body = _get(url, {"Range": "bytes=100-199"})
    assert (status, headers["Content-Range"], headers["Content-Length"]) == (
        206,
        f"bytes 100-199/{len(payload)}",
        "100",
    )
    assert body == payload[100:200]

    status, headers, body = _get(url, {"Range": f"bytes={len(payload) - 4}-"})
    assert (status, headers["Content-Range"], body) == (
        206,
        f"bytes {len(payload) - 4}-{len(payload) - 1}/{len(payload)}",
        payload[-4:],
    )

    status, headers, body = _get(url, {"Range": "bytes=99999-"})
    assert (status, headers["Content-Range"], body) == (416, f"bytes */{len(payload)}", b"")

    # a form this server does not promise is ignored, never guessed at
    for guess in ("bytes=-10", "bytes=0-9,20-29"):
        status, _headers, body = _get(url, {"Range": guess})
        assert (status, len(body)) == (200, len(payload)), guess


def test_a_download_of_something_that_is_not_a_file_is_404(upload_env, tmp_path):
    base, _inbox = upload_env
    folder = tmp_path / "a-folder"
    folder.mkdir()
    for target in (str(tmp_path / "nope.bin"), str(folder), "../escape"):
        status, _headers, body = _get(base + "/download?path=" + urllib.parse.quote(target))
        assert status == 404, target
        assert "error" in json.loads(body)


def test_a_big_upload_is_streamed_not_buffered(upload_env):
    """24 MiB through the socket must not cost 24 MiB of Python heap — the cap
    could only go because the body now goes to disk in 64 KiB chunks (spec §48)."""
    base, _ = upload_env
    body = b"\x5a" * (24 * 1024 * 1024)  # built before measuring: not our peak
    tracemalloc.start()
    try:
        with urllib.request.urlopen(_post(base, body), timeout=60) as resp:
            out = json.loads(resp.read())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out["size"] == len(body)
    assert peak < len(body) // 4, f"peak {peak} bytes for a {len(body)} byte body"
