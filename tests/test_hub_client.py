"""The receiver's half of a delivery: ranged and resumable downloads (§50).

One connection does not fill a link, and a 1 GB file that dies at 90% is not
worth re-fetching from byte zero, so a delivery travels as several byte ranges
at once and a window that dies is resumed where it stopped. The landing rule is
unchanged (§49): nothing appears under the real name unless every byte is there.
"""

import http.server
import json
import pathlib
import threading
from http.server import ThreadingHTTPServer

import pytest

from fungi.hub import client as client_mod
from fungi.hub.client import HubClient, HubError, _windows

MiB = 1024 * 1024


class _Body:
    """A response body over bytes in memory: `read(n)`, then a dropped link."""

    def __init__(self, data: bytes, die_after: int | None = None, on_die=None):
        self._data = data
        self._at = 0
        self._die = die_after
        self._on_die = on_die

    def read(self, want: int) -> bytes:
        if self._die is not None and self._at >= self._die:
            if self._on_die is not None:
                self._on_die()
            raise OSError("the link went away")
        room = want if self._die is None else min(want, self._die - self._at)
        chunk = self._data[self._at : self._at + room]
        self._at += len(chunk)
        return chunk

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class _InMemoryHub(HubClient):
    """A HubClient whose hub is a byte string — window behaviour, no sockets.

    `die_after` kills the first range body that much in (a dropped link);
    `die_every` kills every one of them.
    """

    def __init__(self, payload: bytes, die_after: int | None = None, die_every: bool = False):
        super().__init__("http://127.0.0.1:9", "token", "beta")
        self.payload = payload
        self.die_after = die_after
        self.die_every = die_every
        self.asked: list[tuple[int, int]] = []
        self._died = False

    def _staged_size(self, transfer_id: str) -> int:
        return len(self.payload)

    def _open_window(self, transfer_id: str, first: int, last: int):
        self.asked.append((first, last))
        die = None
        if self.die_after is not None and (self.die_every or not self._died):
            die = self.die_after
        return _Body(self.payload[first : last + 1], die, self._died_now), True

    def _died_now(self) -> None:
        """The connection only dies when a body is actually read: the probe
        window is opened and dropped, and it must not spend the drop."""
        self._died = True


def _landing_dir(tmp_path) -> "pathlib.Path":
    """Where files land — tmp_path itself holds pytest's own fixtures."""
    folder = tmp_path / "inbox"
    folder.mkdir()
    return folder


def _payload(size: int) -> bytes:
    """Position-dependent bytes: a window written at the wrong offset shows up."""
    return bytes(range(256)) * (size // 256)


# ── how a delivery is cut up ──


def test_a_delivery_is_cut_into_windows_only_where_it_pays():
    """A window is a TCP connection and a resume point; a file too small to
    fill a few of them travels as one stream, which is also what a hub that
    does not do ranges gets."""
    assert _windows(12 * MiB, 4) == [(0, 4 * MiB), (4 * MiB, 8 * MiB), (8 * MiB, 12 * MiB)]
    assert _windows(32 * MiB, 2) == [(0, 16 * MiB), (16 * MiB, 32 * MiB)]
    assert _windows(3 * MiB, 4) == [(0, 3 * MiB)]
    # the cutting has to be exact even when the size does not divide evenly
    windows = _windows(10 * MiB - 7, 3)
    assert windows[0][0] == 0 and windows[-1][1] == 10 * MiB - 7
    assert all(end - start >= 4 * MiB for start, end in windows)
    assert [start for start, _end in windows] == [0, *(end for _s, end in windows[:-1])]


# ── the delivery ──


def test_a_delivery_over_several_windows_lands_byte_for_byte(tmp_path):
    land = _landing_dir(tmp_path)
    payload = _payload(12 * MiB)
    client = _InMemoryHub(payload)
    dest = land / "landed.bin"

    client.download_transfer("tid", dest)

    assert dest.read_bytes() == payload
    assert sorted(p.name for p in land.iterdir()) == ["landed.bin"], "a part file was left"
    # three windows were asked for, each ending at its own last byte
    assert {last for _first, last in client.asked} == {4 * MiB - 1, 8 * MiB - 1, 12 * MiB - 1}


def test_a_window_that_dies_resumes_where_it_stopped(tmp_path, monkeypatch):
    """The point of ranges: a dropped link costs a stretch of the file, not the
    file. The resumed request has to start at the byte the dead one reached."""
    monkeypatch.setattr(client_mod, "WINDOW_RETRY_WAIT_S", 0.01)
    land = _landing_dir(tmp_path)
    payload = _payload(12 * MiB)
    client = _InMemoryHub(payload, die_after=64 * 1024)
    dest = land / "landed.bin"

    client.download_transfer("tid", dest)

    assert dest.read_bytes() == payload
    assert sorted(p.name for p in land.iterdir()) == ["landed.bin"]
    assert (64 * 1024, 4 * MiB - 1) in client.asked, "the window restarted from zero"


def test_a_window_that_never_succeeds_lands_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "WINDOW_RETRY_WAIT_S", 0.01)
    land = _landing_dir(tmp_path)
    client = _InMemoryHub(_payload(12 * MiB), die_after=0, die_every=True)

    with pytest.raises(HubError):
        client.download_transfer("tid", land / "landed.bin")

    assert list(land.iterdir()) == [], "a half-fetched delivery was left behind"


# ── a hub from before this existed ──


def _old_hub(payload: bytes, with_progress: bool) -> ThreadingHTTPServer:
    """A hub that predates §50: it ignores Range and serves the whole file."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def _reply(self, body: bytes, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/api/transfer/progress"):
                if not with_progress:
                    self._reply(json.dumps({"error": "not found"}).encode(), 404)
                    return
                meta = {"ok": True, "sent": 0, "total": len(payload)}
                self._reply(json.dumps(meta).encode())
                return
            # no Accept-Ranges, no 206: whatever was asked for, here is the file
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("with_progress", [True, False])
def test_a_hub_that_ignores_ranges_still_delivers(tmp_path, with_progress):
    """Old hub, new receiver: the first range request comes back 200 with the
    whole file, and that answer *is* the single stream. A hub too old to know
    the size route never gets asked for ranges in the first place."""
    land = _landing_dir(tmp_path)
    payload = _payload(8 * MiB)
    server = _old_hub(payload, with_progress)
    try:
        client = HubClient(f"http://127.0.0.1:{server.server_port}", "token", "beta")
        dest = land / "landed.bin"
        client.download_transfer("tid", dest)
        assert dest.read_bytes() == payload
        assert sorted(p.name for p in land.iterdir()) == ["landed.bin"]
    finally:
        server.shutdown()
        server.server_close()
