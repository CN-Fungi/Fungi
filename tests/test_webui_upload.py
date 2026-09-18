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
