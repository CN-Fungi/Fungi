"""WebUI LAN gate: loopback passes, non-loopback needs the QR token; the
static shell (page/css/js) is exempt; /lan hands out the token only to
loopback callers."""

import json
import threading
import urllib.error
import urllib.request

from fungi.server import WEBUI_TOKEN, YesSirHandler, lan_payload, make_webui_server


def _fake_handler(client_ip: str, path: str) -> YesSirHandler:
    """Bare handler instance (no socket): enough for the pure _authorized logic."""
    h = YesSirHandler.__new__(YesSirHandler)
    h.client_address = (client_ip, 12345)
    h.path = path
    return h


def test_loopback_passes_without_token():
    assert YesSirHandler._authorized(_fake_handler("127.0.0.1", "/sessions")) is True


def test_lan_client_without_token_is_rejected():
    assert YesSirHandler._authorized(_fake_handler("192.168.1.7", "/sessions")) is False


def test_lan_client_with_wrong_token_is_rejected():
    assert YesSirHandler._authorized(_fake_handler("192.168.1.7", "/sessions?t=nope")) is False


def test_lan_client_with_qr_token_passes():
    assert (
        YesSirHandler._authorized(_fake_handler("192.168.1.7", f"/sessions?t={WEBUI_TOKEN}"))
        is True
    )


def test_static_shell_is_open_to_lan_clients():
    """The page/css/js shell carries no data and its sub-resource URLs cannot
    append ?t= — a phone must be able to load them without a token (then m.js
    shows the rescan overlay when the token is missing or wrong)."""
    for path in ("/m", "/m.css", "/m.js", "/app.js", "/vendor/gsap.min.js"):
        assert YesSirHandler._authorized(_fake_handler("192.168.1.7", path)) is True


def test_vendor_traversal_is_not_exempt():
    assert YesSirHandler._authorized(_fake_handler("192.168.1.7", "/vendor/../secret.txt")) is False


def test_upload_endpoint_is_token_gated():
    """Files land on the PC: a LAN client must present the QR token to upload."""
    assert YesSirHandler._authorized(_fake_handler("192.168.1.7", "/upload")) is False
    assert (
        YesSirHandler._authorized(_fake_handler("192.168.1.7", f"/upload?t={WEBUI_TOKEN}")) is True
    )


def test_lan_payload_hides_token_from_lan_callers():
    loopback = lan_payload(8900, loopback=True)
    assert loopback["port"] == 8900
    assert loopback["token"] == WEBUI_TOKEN
    assert loopback["url"] == f"http://{loopback['ip']}:8900/m?t={WEBUI_TOKEN}"

    remote = lan_payload(8900, loopback=False)
    assert "token" not in remote
    assert "url" not in remote
    assert remote["ip"] == loopback["ip"]


class _TouchRuntime:
    """/lan only needs the handler's touch() hook."""

    def touch(self) -> None:
        pass


def test_webui_binds_all_interfaces_and_lan_endpoint_works():
    server = make_webui_server(0, _TouchRuntime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert server.server_address[0] == "0.0.0.0"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/lan", timeout=5
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["port"] == server.server_address[1]
        assert payload["ip"] not in ("", None)
        assert payload["token"] == WEBUI_TOKEN
        assert payload["url"].endswith(f"/m?t={WEBUI_TOKEN}")
    finally:
        server.shutdown()
        server.server_close()


class _BoomRuntime(_TouchRuntime):
    """A route that blows up: the page must still get an answer."""

    def comm_send(self, data: dict) -> dict:
        raise ValueError("hub said no")


def test_a_route_that_raises_still_answers():
    """A dropped connection reads as a network failure in the browser — and
    Chromium silently re-sends the POST, which restarted the send-file modal's
    bar from zero (a send over max_file_mb did exactly that). So an exception
    becomes a 500 the page can print.
    """
    server = make_webui_server(0, _BoomRuntime())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/comm-send",
            data=json.dumps({"host": "beta", "file": "x"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 500
            payload = json.loads(exc.read().decode("utf-8"))
        assert payload == {"error": "ValueError: hub said no"}
    finally:
        server.shutdown()
        server.server_close()
