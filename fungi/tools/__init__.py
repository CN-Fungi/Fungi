"""Tool registry: OpenAI function-calling schemas, dispatch, and per-layer whitelists.

Layer rules (see docs/spec.md 2.3):
- L1/L2: all base tools; `spawn` and `inquire` are attached by fungi.trilayer / fungi.tools.ask.
- L3:   read/write/edit/glob/grep/bash only (basic worker, no web, no dispatch).
"""

import inspect
from collections.abc import Callable

from fungi.tools.files import tool_edit, tool_read, tool_write
from fungi.tools.search import tool_glob, tool_grep
from fungi.tools.shell import tool_bash, tool_bash_kill, tool_bash_send, tool_bash_start
from fungi.tools.video import tool_video
from fungi.tools.webtools import tool_web, tool_web_search


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS: dict[str, dict] = {
    "read": {
        "schema": _schema(
            "read",
            "Read a file, numbered lines. Append `:N` for one line, `:N-M` for a "
            "range. Image files (png/jpg/jpeg/webp/gif/bmp) are returned as "
            "attached pictures visible to vision models instead of text.",
            {
                "path": {
                    "type": "string",
                    "description": "File path, optionally with :N or :N-M selector",
                }
            },
            ["path"],
        ),
        "fn": tool_read,
    },
    "video": {
        "schema": _schema(
            "video",
            "Understand a local video file: returns the timestamped transcript, "
            "scene structure, and keyframe images attached for vision models. "
            'path "demo" runs a built-in test clip - one successful call '
            "proves the whole pipeline (ffmpeg, VidSense, HF models) is ready; "
            "do NOT promise video analysis before a call has succeeded. If any "
            "runtime part is missing the tool returns an ERROR with setup "
            "guidance and never downloads on demand.",
            {
                "path": {
                    "type": "string",
                    "description": 'Video file path (mp4/mov/mkv…) or "demo" for the built-in test clip',
                }
            },
            ["path"],
        ),
        "fn": tool_video,
    },
    "write": {
        "schema": _schema(
            "write",
            "Create or overwrite a file.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        "fn": tool_write,
    },
    "edit": {
        "schema": _schema(
            "edit",
            "Replace old_string with new_string. old_string must match exactly and be unique.",
            {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            ["path", "old_string", "new_string"],
        ),
        "fn": tool_edit,
    },
    "bash": {
        "schema": _schema(
            "bash",
            "Run a command in cmd.exe (Windows). Timeout: 600 seconds. "
            "stdin is NUL: an interactive command (bare date / pause / a REPL) "
            "reads EOF and exits at once — it cannot wait for your input. "
            "Inner double quotes get eaten by cmd: use single quotes inside, e.g. "
            "date/time via: powershell -Command (Get-Date).ToString('yyyy-MM-dd dddd HH:mm') "
            '(output is GBK-encoded). Unix-style flags like date "+%Y" are NOT valid here.',
            {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
            },
            ["command"],
        ),
        "fn": tool_bash,
    },
    "bash_start": {
        "schema": _schema(
            "bash_start",
            "Start a command (cmd.exe) as a background session and return "
            "IMMEDIATELY with its session id and output so far. Use for "
            "REPL-style read->write interaction: dev servers, watch tasks, "
            "python/ssh/REPL prompts, multi-prompt installers. stdin defaults "
            "to NUL (an interactive prompt reads EOF and exits at once) — pass "
            "stdin_arg 'pipe' to feed input later via bash_send. Output keeps "
            "accumulating; each bash_send (or bash_start of a new session) "
            "returns everything printed since. Sessions are killed when their "
            "turn ends, on /stop, or after 10 idle minutes.",
            {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "stdin_arg": {
                    "type": "string",
                    "enum": ["nul", "pipe"],
                    "description": "'pipe' enables bash_send input (default 'nul')",
                },
            },
            ["command"],
        ),
        "fn": tool_bash_start,
    },
    "bash_send": {
        "schema": _schema(
            "bash_send",
            "Feed ONE line to a running bash_start session (started with "
            "stdin_arg 'pipe'), then get the output it produced (waits up to "
            "~2s). The newline is added for you; do not include it.",
            {
                "id": {"type": "string", "description": "Session id from bash_start"},
                "text": {"type": "string", "description": "One line to write to the process stdin"},
            },
            ["id"],
        ),
        "fn": tool_bash_send,
    },
    "bash_kill": {
        "schema": _schema(
            "bash_kill",
            "Kill a bash session's whole process tree (the session id is from "
            "bash_start). Sessions also die on their own when the turn ends.",
            {"id": {"type": "string", "description": "Session id from bash_start"}},
            ["id"],
        ),
        "fn": tool_bash_kill,
    },
    "glob": {
        "schema": _schema(
            "glob",
            "Find files by pattern (e.g. *.py). Returns paths newest-first.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Base directory (default: current)"},
            },
            ["pattern"],
        ),
        "fn": tool_glob,
    },
    "grep": {
        "schema": _schema(
            "grep",
            "Search files with a regex. Returns file:line:match.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "File or directory (default: current)"},
            },
            ["pattern"],
        ),
        "fn": tool_grep,
    },
    "web": {
        "schema": _schema(
            "web",
            "Fetch a web page and return its text content.",
            {"url": {"type": "string", "description": "Full URL to fetch"}},
            ["url"],
        ),
        "fn": tool_web,
    },
    "web_search": {
        "schema": _schema(
            "web_search",
            "Search the web via Brave Search. Returns titles, URLs, and snippets.",
            {"query": {"type": "string"}},
            ["query"],
        ),
        "fn": tool_web_search,
    },
}

BASE_TOOL_NAMES = frozenset(TOOLS)
L3_TOOL_NAMES = frozenset({"read", "write", "edit", "glob", "grep", "bash"})


def tool_defs(names: frozenset[str] | set[str] | None = None) -> list[dict]:
    """Function-calling defs for the given whitelist (default: all base tools)."""
    selected = names if names is not None else BASE_TOOL_NAMES
    return [TOOLS[name]["schema"] for name in TOOLS if name in selected]


def dispatch(name: str, args: dict, should_abort: Callable[[], bool] | None = None) -> str:
    tool = TOOLS.get(name)
    if tool is None:
        return f"ERROR: Unknown tool: {name}"
    for required in tool["schema"]["function"]["parameters"].get("required", []):
        if not args.get(required):
            return f"ERROR: Missing required argument: {required}"
    accepted = inspect.signature(tool["fn"]).parameters
    kwargs = {k: v for k, v in args.items() if k in accepted}
    # cooperative cancellation: tools that declare `should_abort` (long-running
    # ones like video) get the agent's abort predicate injected automatically
    if should_abort is not None and "should_abort" in accepted:
        kwargs["should_abort"] = should_abort
    try:
        result = tool["fn"](**kwargs)
    except (OSError, ValueError, TypeError) as exc:
        return f"ERROR: {exc}"
    # ImageRead (str subclass) must survive: str() would flatten it and the
    # agent loop would lose the pixels attached to the result.
    return result if isinstance(result, str) else str(result)
