"""Session alerts (§61): what a session wants while nobody is showing it.

An in-turn ask, a finished turn, a failed turn — each raises an alert unless a
page is *showing* that session (its claim is fresh, which is the "I am looking
at it" report the shells send every few seconds). The alert is what the GUI
rings on and what the sidebar's red dot carries; opening the session clears it.

Server-side only: the two shells and the claim/poll wire they drive live in
tests/test_webui_alerts.py (which needs a browser).
"""

import io
import json
import threading
import urllib.request

import pytest

from fungi import server as webui
from fungi import session as session_mod


class _Agent:
    """The minimum a turn hands around: run(), plus what _run_turn persists."""

    def __init__(self, run=None, asks=None):
        self._run = run or (lambda messages: None)
        self.asks = asks or []
        self.subagents = {}

    def run(self, messages):
        self._run(messages)


def _ask_record(status: str) -> dict:
    return {"id": "a1", "call_id": "c1", "ts": 0.0, "questions": [], "status": status}


@pytest.fixture()
def alerts_env(tmp_path, monkeypatch):
    """A WebUI server whose sessions stay in tmp, and whose registry starts clean.

    `script` is what a turn runs: swapped per test to say how the turn ends.
    """
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path / "sessions")
    webui.clear_alerts()

    class _Scripted(webui.WebUIRuntime):
        script = staticmethod(lambda sink, should_abort: _Agent())

        def build_agent(self, sink, should_abort):
            return self.script(sink, should_abort)

    runtime = _Scripted()
    srv = webui.make_webui_server(0, runtime)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", runtime
    finally:
        srv.shutdown()
        srv.server_close()
        webui.clear_alerts()


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def _post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def _chat(base: str, session_id: str, text: str) -> list[dict]:
    """Run one turn; the NDJSON events it streamed."""
    req = urllib.request.Request(
        base + "/chat",
        data=json.dumps({"sessionId": session_id, "message": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return [json.loads(line) for line in resp.read().decode().splitlines() if line]


def _entry(base: str, session_id: str) -> dict:
    """The session's own row in /sessions — where the red dot comes from."""
    rows = _get(base, "/sessions")["sessions"]
    return next(row for row in rows if row["id"] == session_id)


def _new_session(base: str) -> str:
    return _post(base, "/new", {})["id"]


def test_a_finished_turn_alerts_and_the_list_and_the_answer_carry_it(alerts_env):
    base, _runtime = alerts_env
    sid = _new_session(base)
    events = _chat(base, sid, "你好")
    assert events[-1]["type"] == "done"

    assert _entry(base, sid)["alert"] == "done"
    assert webui.session_alerts()[sid] == "done"

    # 页面声明「我正在看它」= 打开那个会话：红点掉、铃停，都在这一下。
    seen = _post(base, "/session/seen", {"id": sid, "visible": True})
    assert seen["alerts"] == {}
    assert _entry(base, sid)["alert"] is None


def test_a_turn_that_ends_with_an_unanswered_ask_says_ask(alerts_env):
    """回合结束了但卡上还挂着一个没人回答的问题：要提醒的是「回答我」，不是「答完了」。"""
    base, runtime = alerts_env
    runtime.script = lambda sink, should_abort: _Agent(asks=[_ask_record("timeout")])
    sid = _new_session(base)
    _chat(base, sid, "问我一件事")
    assert _entry(base, sid)["alert"] == "ask"


def test_an_answered_ask_does_not_turn_a_finished_turn_into_ask(alerts_env):
    base, runtime = alerts_env
    runtime.script = lambda sink, should_abort: _Agent(asks=[_ask_record("answered")])
    sid = _new_session(base)
    _chat(base, sid, "答过了")
    assert _entry(base, sid)["alert"] == "done"


def test_a_turn_that_blows_up_says_error(alerts_env):
    base, runtime = alerts_env

    def boom(sink, should_abort):
        def run(messages):
            raise RuntimeError("model said no")

        return _Agent(run=run)

    runtime.script = boom
    sid = _new_session(base)
    events = _chat(base, sid, "hi")
    assert any(ev["type"] == "error" for ev in events), events
    assert _entry(base, sid)["alert"] == "error"


def test_a_turn_the_user_stopped_rings_nothing(alerts_env):
    """Esc 的意思是「我在」，不是「提醒我去看看」（/stop 把它按掉了）。"""
    base, runtime = alerts_env

    def stop_it(sink, should_abort):
        def run(messages):
            # What /stop does to a running turn, from inside it.
            with webui._TURNS_LOCK:
                for event in webui._ACTIVE_TURNS.get(sink.session_id, ()):
                    event.set()

        return _Agent(run=run)

    runtime.script = stop_it
    sid = _new_session(base)
    events = _chat(base, sid, "停下")
    assert events[-1]["type"] == "done"
    assert webui.session_alerts() == {}


def test_an_ask_alerts_the_moment_it_is_raised():
    """ask 在 WebSink.emit 上就把提醒挂上了：回合那时还开着，可能一等就是
    ASK_TIMEOUT_S —— 等到回合结束再提醒，等于没提。"""

    class _Handler:  # WebSink only needs somewhere to write
        wfile = io.BytesIO()

    webui.clear_alerts()
    try:
        webui.WebSink(_Handler(), "s1").emit("ask", {"id": "a1", "questions": []})
        assert webui.session_alerts()["s1"] == "ask"
    finally:
        webui.clear_alerts()


def test_the_page_showing_the_session_keeps_it_quiet(alerts_env):
    """最小化不算点开（用户 2026-09-21）：页面不再声明之后，那份「我在看」会过期 ——
    提醒就是真的；用户回来看着它，提醒才消失。"""
    webui.mark_seen("s1", True)
    webui.note_alert("s1", "done")
    assert webui.session_alerts() == {}, "the user is looking at it right now"

    with webui._ALERT_LOCK:
        webui._SEEN["s1"] -= webui.SEEN_TTL_S + 1  # the page went quiet (hidden / gone)
    webui.note_alert("s1", "done")
    assert webui.session_alerts()["s1"] == "done"

    # 释放声明（切走 / 收起）不撤回提醒，只有「正在看」才撤。
    webui.mark_seen("s1", False)
    assert webui.session_alerts()["s1"] == "done"
    webui.mark_seen("s1", True)
    assert webui.session_alerts() == {}


def test_a_report_without_a_session_only_answers_with_the_map(alerts_env):
    """没有 claim 的那种心跳（页面开着，但在好友视图里）：既不改声明也不清提醒。"""
    webui.mark_seen("s1", True)
    webui.note_alert("s1", "done")
    with webui._ALERT_LOCK:
        webui._SEEN["s1"] -= webui.SEEN_TTL_S + 1
    webui.note_alert("s1", "done")
    assert "s1" in webui.session_alerts()

    assert list(webui.mark_seen("", True)) == ["s1"], "the answer is the alert map"
    assert "s1" in webui.session_alerts(), "and nothing was cleared"


def test_deleting_a_session_forgets_its_alert(alerts_env):
    base, _runtime = alerts_env
    sid = _new_session(base)
    webui.note_alert(sid, "done")
    assert sid in webui.session_alerts()

    req = urllib.request.Request(base + "/session?id=" + sid, method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert json.loads(resp.read())["ok"] is True
    assert webui.session_alerts() == {}
