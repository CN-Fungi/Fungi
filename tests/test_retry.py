"""Tests for the /retry message sanitizer (Alt+R continue-from-context)."""

from fungi.server import repair_tool_gaps, sanitize_for_retry


def test_strips_trailing_llm_error_marker() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "(LLM error: HTTP 402: Insufficient Balance)"},
    ]
    out = sanitize_for_retry(msgs)
    assert out[-1] == {"role": "user", "content": "hi"}


def test_strips_max_rounds_marker() -> None:
    msgs = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "(Hit max tool rounds: 25.)"},
    ]
    assert len(sanitize_for_retry(msgs)) == 1


def test_strips_dangling_tool_calls_tail() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
    ]
    assert len(sanitize_for_retry(msgs)) == 1


def test_stops_at_tool_result_tail() -> None:
    """Died after tool results: continue from them, strip nothing."""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]
    assert sanitize_for_retry(msgs) == msgs


def test_keeps_normal_assistant_answer() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello!"},
    ]
    assert sanitize_for_retry(msgs) == msgs


def test_strips_multiple_synthetic_tail() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "assistant", "content": "(LLM error: Connection failed)"},
        {"role": "assistant", "content": "(LLM error: HTTP 429: rate limited)"},
    ]
    out = sanitize_for_retry(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"


def test_does_not_mutate_input() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "(LLM error: x)"}]
    sanitize_for_retry(msgs)
    assert len(msgs) == 2


def test_strips_abort_marker_keeps_partial_content() -> None:
    """Esc-interrupted turn: marker is stripped, partial reply stays for continuation."""
    msgs = [
        {"role": "user", "content": "write a poem"},
        {"role": "assistant", "content": "Roses are red,"},
        {"role": "assistant", "content": "(Aborted)"},
    ]
    out = sanitize_for_retry(msgs)
    assert out[-1] == {"role": "assistant", "content": "Roses are red,"}


def test_strips_bare_abort_marker() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "(Aborted)"},
    ]
    assert len(sanitize_for_retry(msgs)) == 1


# ---- repair_tool_gaps: dangling tool_calls must not brick the session ----


def test_repair_synthesizes_missing_tool_result() -> None:
    """Died between tool_calls and their results: unanswered calls get an
    explicit failure result (the API would otherwise 400 forever)."""
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "t1", "function": {"name": "read"}},
                {"id": "t2", "function": {"name": "bash"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    out = repair_tool_gaps(msgs)
    assert out[-1]["role"] == "tool"
    assert out[-1]["tool_call_id"] == "t2"
    assert "interrupted" in out[-1]["content"]


def test_repair_closes_gap_before_next_user_message() -> None:
    """New user message after a broken turn: the dangling call is answered
    first so the history stays API-valid."""
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {"id": "t1", "function": {"name": "inquire"}},
            ],
        },
        {"role": "user", "content": "hello?"},
    ]
    out = repair_tool_gaps(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "user"]
    assert out[2]["tool_call_id"] == "t1"


def test_repair_leaves_clean_history_alone() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "t1", "function": {"name": "read"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert repair_tool_gaps(msgs) == msgs
