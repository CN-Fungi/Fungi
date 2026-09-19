"""Atomic landing + the writable-inbox rule (§49).

What the receiver's side owes the sender, and what the user's WinRAR found out
the hard way: a delivery that does not finish must not leave a half file under
the real name, and an install in Program Files must still have somewhere to put
an incoming file.
"""

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fungi import landing
from fungi.hub.client import HubClient


@pytest.fixture()
def land(tmp_path) -> Path:
    """The landing directory — tmp_path itself holds pytest's own fixtures."""
    folder = tmp_path / "inbox" / "alpha"
    folder.mkdir(parents=True)
    return folder


# ── the landing itself ──


def test_a_complete_delivery_lands_and_leaves_no_part_file(land):
    dest = land / "report.txt"
    with landing.atomic_landing(dest, expect=7) as fh:
        fh.write(b"payload")
    assert dest.read_bytes() == b"payload"
    assert [p.name for p in land.iterdir()] == ["report.txt"]


def test_a_short_delivery_lands_nothing(land):
    """The §49 case: 31.7% of an archive used to sit there wearing its name."""
    dest = land / "AndroidStudio.rar"
    with (
        pytest.raises(landing.TransferTruncatedError) as exc,
        landing.atomic_landing(dest, expect=1286) as fh,
    ):
        fh.write(b"x" * 400)
    assert (exc.value.written, exc.value.expected) == (400, 1286)
    assert not dest.exists(), "a truncated delivery wore the real name"
    assert list(land.iterdir()) == [], "the part file was left behind"


def test_a_dead_delivery_keeps_the_file_that_was_already_there(land):
    """A retry that dies must not cost the receiver the copy they already had."""
    dest = land / "report.rar"
    dest.write_bytes(b"the good copy")
    with pytest.raises(ConnectionResetError), landing.atomic_landing(dest, expect=1_000) as fh:
        fh.write(b"x" * 10)
        raise ConnectionResetError("the sender's room went away")
    assert dest.read_bytes() == b"the good copy"
    assert [p.name for p in land.iterdir()] == ["report.rar"]


def test_an_unknown_length_still_lands(land):
    """No Content-Length from the hub: land what arrived instead of refusing."""
    dest = land / "no-length.bin"
    with landing.atomic_landing(dest) as fh:
        fh.write(b"abc")
    assert dest.read_bytes() == b"abc"


# ── the transport that streams over HTTP ──


class _HalfwayHub(BaseHTTPRequestHandler):
    """Promises 1 MiB, sends 4 KiB, then drops the connection: the exact shape
    of a sender whose app is closed mid-download."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(1024 * 1024))
        self.end_headers()
        self.wfile.write(b"x" * 4096)
        self.wfile.flush()
        self.close_connection = True
        self.connection.close()


def test_an_aborted_http_delivery_leaves_nothing_behind(land):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HalfwayHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HubClient(f"http://127.0.0.1:{server.server_address[1]}", "t", "beta")
        dest = land / "half.rar"
        with pytest.raises((OSError, http.client.HTTPException)):
            client.download_transfer("whatever", dest)
        assert not dest.exists(), "an aborted delivery landed under the real name"
        assert list(land.iterdir()) == []
    finally:
        server.shutdown()
        server.server_close()


# ── the landing directory ──


def test_the_inbox_beside_the_program_is_preferred_when_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(landing, "PROJECT_ROOT", tmp_path)
    assert landing.inbox_root() == tmp_path / "inbox"


def test_a_read_only_program_folder_falls_back_to_the_user_profile(tmp_path, monkeypatch):
    """Program Files: `inbox` cannot be created there, which is the WinError 5
    that named `Program Files (x86)\\Fungi\\Fungi\\inbox` and killed every
    incoming transfer before §49."""
    program = tmp_path / "ProgramFiles"
    program.mkdir()
    (program / "inbox").write_text("a folder an ACL forbids looks like this")
    monkeypatch.setattr(landing, "PROJECT_ROOT", program)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    root = landing.inbox_root()
    assert root == tmp_path / "Local" / "Fungi" / "inbox"
    assert root.is_dir(), "the fallback has to exist before a file can land"


def test_a_configured_inbox_is_used_exactly_as_given(tmp_path):
    """The user's own choice is not second-guessed — even a bad one: the error
    they see then names their path, not ours."""
    chosen = tmp_path / "mine" / "incoming"
    assert landing.inbox_root(str(chosen)) == chosen
