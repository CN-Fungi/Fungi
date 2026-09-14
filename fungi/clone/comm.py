"""Comm clone assembly: role prompt + CommTools + Clone."""

from pathlib import Path

from .. import config as config_mod
from .. import todos
from ..agent import FILE_OPS_RULE, Agent  # noqa: F401 (re-exported type)
from ..config import Config
from ..events import Sink
from ..pending import PendingAsks
from .base import Clone
from .tools_comm import CommTools

COMM_SYSTEM_PROMPT = """You are the comm Agent for host {host}, dedicated to the counterpart \
comm Agent on host {peer}.

Rules:
- You and your counterpart may collaborate autonomously; no user attention is needed for that.
- File work is restricted by the server-side guard: public/ is free for both sides; anything under \
homes/<host>/ needs the owning host user's consent (your own host's home too) — call confirm \
first, then use the fs tools (the granted consent is applied automatically). Authoring \
documentation or skill-like files under public/docs/ also needs consent — propose first, never \
self-publish knowledge bases unannounced.
- send_file moves a server-stored file onto the peer host's local disk; their user must accept it.
- Incoming chats appear as [peer] messages and are kept in your conversation history. send_peer is the \
ONLY thing that reaches your counterpart, so call it when a reply is warranted and never just to \
acknowledge.
- Your turn's own text is a report to YOUR host's user — it stays on this machine and never goes out. \
After every send_peer, report what you sent and what came back; raise anything the user has to decide. \
A report that says nothing still costs the user attention, so to leave no report at all end your turn \
with the exact single line <<SILENT>> — prose "silence declarations" would themselves become a report; \
only the bare marker is silent.
- Your host can leave a 评价 — a note on one of your reports; it reaches you as a [评价] message — that \
is your own host talking to you, from the 评价 box under the report. It is between the two of you: \
never forward it to your counterpart and never mention it there. Take it as a correction — fix what it \
points at (a wrong clock time or place in the calendar, something you got wrong) and answer your host \
in your turn text as usual.
- When a date is in the air — in the broad sense: meeting up, a call, a meal, a visit, any plan the two \
humans want to fix — help them settle it. Nail down the three things that make it real: the event, the \
time, the place. Ask your counterpart for what your own host does not know, propose concrete options \
instead of volleying "when suits you" back and forth, and do not let the plan rest on a detail nobody \
agreed to. Write the settled plan into your host's calendar (the todo tool) with its clock time, and \
put to your own host only the questions that are genuinely theirs to answer.
- Read what the turn actually needs: a file the user or your counterpart named, or one you are already \
working on. Do not rummage through your host's files for clues about a task nobody asked for, and never \
build a plan on a document you merely stumbled across — if you cannot find out who asked for something, \
say exactly that.
- Use inquire only when your own host's user must decide something — never to confirm a plan you \
invented. It does not block you: the question goes to their card and their answer comes back later \
as a [主人的答复] message on a turn of its own (with the question attached). Do not wait for it, do \
not ask it again, and keep serving your counterpart in the meantime.
- When given a [TASK], do exactly what the goal says and answer strictly in the reply format; report \
failure as specified instead of improvising.
"""

SILENT_REPLY = "<<SILENT>>"  # a comm turn ending with exactly this leaves no report


def build_comm_clone(
    host: str,
    peer: str,
    transport,
    cfg: Config,
    sink: Sink,
    llm=None,
    ask_timeout_s: float = 1800.0,
    poll_timeout: float = 5.0,
    system_prompt: str | None = None,
    inbox_dir: Path | None = None,
    on_turn_end=None,
    on_direct=None,
) -> Clone:
    addr = f"{host}:comm-{peer}"
    pending = PendingAsks()
    comm_tools = CommTools(addr, transport, pending, ask_timeout_s, inbox_dir=inbox_dir)
    base_prompt = system_prompt or COMM_SYSTEM_PROMPT.format(host=host, peer=peer)

    def courier_prompt() -> str:
        """Message-courier prompt, re-read per turn: the GUI memory entry
        (config.courier_memory) and the calendar (todos.upcoming) apply to
        the next incoming chat, live."""
        # Read THROUGH the module: the courier re-reads the config every turn,
        # so a GUI edit applies to the next incoming message (and a caller that
        # swaps `config.load_config` — tests do — is honoured).
        memory = (config_mod.load_config().courier_memory or "").strip()
        entries = todos.upcoming()
        calendar = ""
        if entries:
            calendar = (
                "\n本机主人的日历待办（GUI 日历/todo 工具写入，回答涉及日程时据此代为说明）:\n"
                + "\n".join(f"- {d}: {'; '.join(items)}" for d, items in entries)
                + "\n"
            )
        if not memory and not calendar:
            return base_prompt + "\n" + todos.RULES + FILE_OPS_RULE
        return (
            base_prompt
            + "\n本机主人的长期备忘（用户在 GUI 里写给你的背景记忆，回答时可用它代为说明或转达）:\n"
            + memory + "\n"
            + calendar
            + "\n" + todos.RULES
            + FILE_OPS_RULE
        )

    return Clone(
        addr,
        transport,
        cfg,
        sink,
        tools={**comm_tools.bound(), **todos.bound()},
        system_prompt=courier_prompt,
        llm=llm,
        poll_timeout=poll_timeout,
        pending=pending,
        on_transfer=comm_tools.receive_transfer,
        subagents=False,  # a courier relays; it does not fan out (2026-09-10)
        on_turn_end=on_turn_end,
        on_direct=on_direct,
        # No native base tools: file work only via the guarded fs tools; the
        # spawned workers inherit exactly that surface (spec 6.1).
        tool_names=frozenset(),
        child_tool_names=frozenset(),
        child_extra_tools=comm_tools.fs_bound(),
    )
