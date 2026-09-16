"""OpenAI-compatible SSE streaming client, stdlib only.

Compatible with DeepSeek's `reasoning_content` deltas and incremental
tool_call argument assembly (same protocol as the PowerShell original).
"""

import json
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import runlog

READ_TIMEOUT = 600  # socket inactivity timeout per read, seconds

DeltaCallback = Callable[[str, str], None]  # (kind, text) with kind in {"text", "reasoning"}
_reached: set[str] = set()  # endpoints that answered once: one line, not one per turn


@dataclass
class LLMResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)


class LLMError(Exception):
    """Raised for transport or non-200 responses; message is user-displayable."""


class LLMAbortedError(Exception):
    """Raised when the abort predicate fires mid-stream; carries the partial result."""

    def __init__(self, partial: LLMResult) -> None:
        super().__init__("Aborted by user")
        self.partial = partial


def _fail(endpoint: str, message: str) -> LLMError:
    """One failure, two audiences: the caller gets the error, the log gets the fact.

    "The agent says nothing" and "the model never answered" look identical from
    the outside; this is the line that tells them apart in a test report.
    """
    runlog.problem("model call to %s failed: %s", endpoint, message)
    return LLMError(message)


def _reachable(endpoint: str, model: str) -> None:
    if endpoint not in _reached:
        _reached.add(endpoint)
        runlog.note("model reachable: %s (%s)", endpoint, model)


def _apply_delta(tool_acc: dict[int, dict], delta: dict) -> None:
    """Accumulate one streamed delta into tool_call slots, keyed by index."""
    for tc in delta.get("tool_calls") or []:
        idx = int(tc.get("index", 0))
        slot = tool_acc.setdefault(
            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def _wire_messages(messages: list[dict]) -> list[dict]:
    """Messages as the provider should see them: `ts` is the transcript's own
    stamp (the WebUI hover label), not part of the protocol. Returns the list
    untouched when there is nothing to strip — the common case, and the reason
    this costs no copies.

    `reasoning` is ours too but has always shipped upstream, so it stays: this
    only keeps a stamp we invented today from inventing a new wire field.
    """
    if not any("ts" in m for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != "ts"} for m in messages]


def stream_chat(
    model: str,
    endpoint: str,
    api_key: str,
    messages: list[dict],
    tools: list[dict],
    on_delta: DeltaCallback | None = None,
    should_abort: Callable[[], bool] | None = None,
    max_tokens: int | None = None,
) -> LLMResult:
    """One streaming completion; raises LLMError on failure, LLMAbortedError
    (carrying the partial result) when `should_abort` fires mid-stream."""
    payload: dict = {
        "model": model,
        "messages": _wire_messages(messages),
        "tools": tools,
        "stream": True,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(request, timeout=READ_TIMEOUT)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        # 4xx debugging: the exact outgoing request lands next to the sessions
        # so a provider-side "Invalid API parameter" can be replayed offline.
        try:
            dump = Path(tempfile.gettempdir()) / "fungi-llm-error-payload.json"
            dump.write_text(
                json.dumps(
                    {"endpoint": endpoint, "payload": payload}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        raise _fail(endpoint, f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _fail(endpoint, f"Connection failed: {getattr(exc, 'reason', exc)}") from exc
    _reachable(endpoint, model)

    result = LLMResult()
    tool_acc: dict[int, dict] = {}
    finish_reason: str | None = None
    saw_done = False

    def _aborted() -> bool:
        return should_abort is not None and should_abort()

    if _aborted():
        raise LLMAbortedError(result)
    try:
        with resp:
            for raw_line in resp:
                if _aborted():
                    raise LLMAbortedError(result)
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    result.reasoning += reasoning
                    if on_delta:
                        on_delta("reasoning", reasoning)
                content = delta.get("content")
                if content:
                    result.content += content
                    if on_delta:
                        on_delta("text", content)
                if delta.get("tool_calls"):
                    _apply_delta(tool_acc, delta)
    except OSError as exc:
        raise _fail(endpoint, f"Stream interrupted: {exc}") from exc

    if finish_reason == "length" and not result.content and not result.tool_calls:
        raise _fail(
            endpoint,
            "Output token cap hit (finish_reason=length): the model's reasoning consumed "
            'the budget before producing a reply. Set "max_tokens" in config.json to raise it.',
        )
    result.tool_calls = [tool_acc[i] for i in sorted(tool_acc)]
    # Connection closed before the finish signal. A text-only partial is still
    # worth keeping (killing the turn loses it for nothing); a partial tool call
    # is NOT usable — its arguments are truncated.
    if finish_reason is None and not saw_done and (result.tool_calls or not result.content):
        raise _fail(
            endpoint,
            "Stream ended without a finish signal: the connection closed before the model "
            "finished generating (partial output only).",
        )
    return result
