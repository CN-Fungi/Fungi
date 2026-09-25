"""Tests for LLM delta assembly (offline)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fungi.llm import (
    LLMAbortedError,
    LLMError,
    LLMResult,
    _apply_delta,
    probe_model,
    stream_chat,
)


def test_tool_call_single_complete():
    acc: dict[int, dict] = {}
    _apply_delta(
        acc,
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path"'},
                }
            ]
        },
    )
    _apply_delta(acc, {"tool_calls": [{"index": 0, "function": {"arguments": ': "a.txt"}'}}]})
    assert acc[0]["id"] == "call1"
    assert acc[0]["function"]["name"] == "read"
    assert acc[0]["function"]["arguments"] == '{"path": "a.txt"}'


def test_tool_call_parallel_indexes():
    acc: dict[int, dict] = {}
    _apply_delta(
        acc,
        {
            "tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "read", "arguments": "{}"}},
                {"index": 1, "id": "b", "function": {"name": "glob", "arguments": ""}},
            ]
        },
    )
    _apply_delta(
        acc, {"tool_calls": [{"index": 1, "function": {"arguments": '{"pattern": "*.py"}'}}]}
    )
    assert acc[0]["function"]["name"] == "read"
    assert acc[1]["function"]["arguments"] == '{"pattern": "*.py"}'


def test_tool_call_missing_index_defaults_zero():
    acc: dict[int, dict] = {}
    _apply_delta(acc, {"tool_calls": [{"id": "x", "function": {"name": "bash", "arguments": ""}}]})
    assert acc[0]["function"]["name"] == "bash"


def test_delta_without_tool_calls_is_noop():
    acc: dict[int, dict] = {}
    _apply_delta(acc, {"content": "hello"})
    assert acc == {}


def test_llm_result_defaults():
    result = LLMResult()
    assert result.content == ""
    assert result.tool_calls == []
    assert result.reasoning == ""


def test_stream_chat_abort_mid_stream_carries_partial():
    """LLMAbortedError raised between SSE lines carries the partial content."""
    lines = [
        b'data: {"choices": [{"delta": {"content": "one"}}]}\n\n',
        b'data: {"choices": [{"delta": {"content": "two"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    deltas = {"n": 0}

    def on_delta(_kind, _text):
        deltas["n"] += 1

    def should_abort():
        return deltas["n"] >= 1

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for line in lines:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(LLMAbortedError) as excinfo:
            stream_chat(
                "m",
                f"http://127.0.0.1:{server.server_address[1]}/v1",
                "k",
                [],
                [],
                on_delta=on_delta,
                should_abort=should_abort,
            )
        assert excinfo.value.partial.content == "one"
    finally:
        server.shutdown()


def test_stream_chat_length_cap_with_empty_reply_raises():
    """finish_reason=length with no content/tool_calls -> LLMError naming the fix;
    max_tokens is forwarded into the request body when set."""
    lines = [
        b'data: {"choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": null}]}\n\n',
        b'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    bodies: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in lines:
                self.wfile.write(line)
                self.wfile.flush()

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        with pytest.raises(LLMError) as excinfo:
            stream_chat("m", url, "k", [{"role": "user", "content": "hi"}], [], max_tokens=123)
        assert "max_tokens" in str(excinfo.value)
        assert b'"max_tokens": 123' in bodies[0]
    finally:
        server.shutdown()


def test_stream_chat_keeps_the_transcript_stamp_off_the_wire():
    """`ts` is the WebUI's own per-row stamp (session transcripts carry it so
    hovering a message can say when it was sent). It must not ride along to the
    provider as an invented message field."""
    lines = [b"data: [DONE]\n\n"]
    bodies: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in lines:
                self.wfile.write(line)
                self.wfile.flush()

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        stamped = [{"role": "user", "content": "hi", "ts": 1700000000.0}]
        stream_chat("m", url, "k", stamped, [])
        assert b'"content": "hi"' in bodies[0]
        assert b'"ts"' not in bodies[0]
        assert stamped[0]["ts"] == 1700000000.0  # the caller's row keeps its stamp
    finally:
        server.shutdown()


def test_stream_chat_early_close_without_finish_signal_raises():
    """Server closing mid-generation (no finish_reason, no [DONE]) -> LLMError,
    never a silent empty reply."""
    lines = [
        b'data: {"choices": [{"delta": {"reasoning_content": "partial thought"}}]}\n\n',
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            # Eat the request body before answering: closing with it still in
            # the socket is an RST on Windows, and the client then loses this
            # stream to 10053 instead of reading it to its (missing) end —
            # the red this test used to show in full runs only, never alone.
            # The two siblings that capture the body already read it first.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for line in lines:
                self.wfile.write(line)
                self.wfile.flush()
            # no [DONE], no finish_reason: socket just closes

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        with pytest.raises(LLMError) as excinfo:
            stream_chat("m", url, "k", [{"role": "user", "content": "hi"}], [])
        assert "finish signal" in str(excinfo.value)
    finally:
        server.shutdown()


def test_probe_model_asks_cheaply_and_reports_the_served_name():
    """§66：切模型之后自动测一次调用。探针是**非流式**的一次极小补全——它要的是
    「这个模型答不答」，不是一轮对话；而且只有非流式才好上 15 秒的硬期限。"""
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            payload = json.dumps({"model": "served-name", "choices": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, detail = probe_model(
            "asked-name", f"http://127.0.0.1:{server.server_address[1]}/v1", "k"
        )
    finally:
        server.shutdown()

    assert (ok, detail) == (True, "served-name"), "provider 说它服务的是谁，也一并报上来"
    assert seen["model"] == "asked-name", "问的是要切过去的那个名字"
    assert seen["stream"] is False and "tools" not in seen, "不是一轮对话：不流式、不带工具"
    assert 0 < seen["max_tokens"] <= 16, "只要几个 token，别为一次测试花钱"


def test_probe_model_hands_back_the_providers_own_words():
    """失败时把 provider 的原话带回去：设置页要显示的就是它（401/404 的正文最有用）。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = b'{"error": {"message": "model not found"}}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok, detail = probe_model("m", f"http://127.0.0.1:{server.server_address[1]}/v1", "k")
    finally:
        server.shutdown()

    assert ok is False
    assert "404" in detail and "model not found" in detail


def test_probe_model_gives_up_on_an_endpoint_nobody_answers():
    """连不上也要有话说，而且必须**立刻**回来：那条路是用户最常见的失败（地址打错）。"""
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # 没人监听这个端口了

    ok, detail = probe_model("m", f"http://127.0.0.1:{dead_port}/v1", "k", timeout=2)

    assert ok is False and "Connection failed" in detail
