"""Mobile file upload: multipart extraction, inbox landing, endpoint wiring."""

import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fungi import server as webui
from fungi.config import Config


def _multipart(fields: dict[str, bytes], boundary: str = "testboundary123") -> bytes:
    out = io.BytesIO()
    for name, data in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{name}.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue()


def test_extract_upload_roundtrips_binary_content():
    payload = b"\x00\x01\r\n\x00binary\xff"  # contains CRLF that must survive
    body = _multipart({"photo": payload})
    name, data = webui._extract_upload(body, b"testboundary123")
    assert name == "photo.bin"
    assert data == payload


def test_extract_upload_picks_the_file_part_not_text_fields():
    boundary = "b2"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\n'
        "hello\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
        "PNGDATA\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    assert webui._extract_upload(body, boundary.encode()) == ("a.png", b"PNGDATA")


def test_extract_upload_returns_none_without_file_part():
    body = b'--b\r\nContent-Disposition: form-data; name="note"\r\n\r\nhi\r\n--b--\r\n'
    assert webui._extract_upload(body, b"b") is None


@pytest.fixture()
def upload_env(tmp_path, monkeypatch):
    """WebUI server whose config lands uploads in tmp and caps size at 1 MiB."""
    inbox = tmp_path / "inbox"
    monkeypatch.setattr(
        webui,
        "load_config",
        lambda: Config(api_key="k", endpoint="e", model="m", inbox_dir=str(inbox), max_file_mb=1),
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


def _post_upload(url: str, body: bytes, boundary: str) -> urllib.request.Request:
    return urllib.request.Request(
        url + "/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )


def test_upload_lands_in_inbox_and_returns_absolute_path(upload_env):
    base, inbox = upload_env
    payload = b"hello from the phone"
    with urllib.request.urlopen(
        _post_upload(base, _multipart({"note": payload}), "testboundary123")
    ) as resp:
        out = json.loads(resp.read())
    assert out["ok"] is True and out["size"] == len(payload)
    saved = Path(out["path"])
    assert saved.parent == inbox
    assert saved.read_bytes() == payload
    assert saved.is_absolute()


def test_upload_numbers_colliding_names(upload_env):
    base, inbox = upload_env
    for _ in range(2):
        with urllib.request.urlopen(
            _post_upload(base, _multipart({"doc": b"x"}), "testboundary123")
        ) as resp:
            json.loads(resp.read())
    names = [p.name for p in inbox.iterdir()]
    assert "doc.bin" in names
    assert any(n.startswith("doc-") and n.endswith(".bin") for n in names)


def test_upload_without_file_part_is_rejected(upload_env):
    base, _ = upload_env
    body = b'--b\r\nContent-Disposition: form-data; name="note"\r\n\r\nhi\r\n--b--\r\n'
    req = urllib.request.Request(
        base + "/upload",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=b"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 400


def test_upload_over_max_file_mb_is_413(upload_env):
    base, inbox = upload_env  # cap = 1 MiB
    big = b"\x00" * (1024 * 1024 + 1)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(_post_upload(base, _multipart({"big": big}), "testboundary123"))
    assert err.value.code == 413
    assert not inbox.exists() or not any(inbox.iterdir())


def test_upload_rejects_non_multipart_bodies(upload_env):
    base, _ = upload_env
    req = urllib.request.Request(
        base + "/upload",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 400
