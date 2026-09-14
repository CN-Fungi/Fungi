"""Local Agent assembly: the user-facing Agent with delegate/peers."""

from .. import tools as _tools
from ..agent import FILE_OPS_RULE
from ..config import Config
from ..events import Sink
from ..pending import PendingAsks
from ..tools.ask import make_ask_tool
from ..tools.mcp import mcp_extra_tools
from .base import Clone
from .delegate import DelegateTools

BASE_TOOL_NAMES = _tools.BASE_TOOL_NAMES

LOCAL_SYSTEM_PROMPT = """You are the local Agent on host {host}: the only Agent the user talks to.

Rules:
- Serve the user directly with your native tools for anything on this host.
- The user never talks to other hosts directly: delegate cross-host work with the delegate tool \
(check peers first), then relay the result back in your own words.
- To give a file on this machine to a peer's user, call send_file(host, path, reason) — path may \
be a file or a FOLDER (folders are zipped automatically); the receiving user must accept it. \
Never ask the peer's clone how to transfer files; send_file is the way.
- The shared store (`public/` for friend-shared files, `homes/<host>/` per host) {store_hint}
- inquire questions surface as system notifications with a WebUI card.
"""

STORE_LOCAL = (
    "lives under `data/` in the Fungi repo directory — your file tools already run from "
    "that directory, so look there (e.g. `data/public/...`) for shared files."
)
STORE_REMOTE = (
    "lives on the hub host's disk, NOT on this machine — reach it with the delegate tool, "
    "never with your local file tools."
)


def build_local_clone(
    host: str,
    transport,
    cfg: Config,
    sink: Sink,
    llm=None,
    peers_fn=None,
    ask_timeout_s: float = 1800.0,
    poll_timeout: float = 5.0,
    on_ask=None,
    system_prompt: str | None = None,
    local_store: bool = False,
) -> Clone:
    addr = f"{host}:local"
    pending = PendingAsks()
    tools = {"inquire": make_ask_tool(sink)}
    delegate_tools = None
    if peers_fn is not None:
        delegate_tools = DelegateTools(addr, transport, pending, peers_fn, ask_timeout_s)
        tools.update(delegate_tools.bound())
    # MCP servers from config.json reach the user-facing clone in room mode too
    # (room.py used to drop them entirely — "single-host concern" was wrong).
    tools.update(mcp_extra_tools(cfg.mcp_servers))
    # Room-mode WebUI turns are built from these clone tools (not
    # build_orchestrator) — the todo tool must live here to be visible.
    from .. import todos  # noqa: PLC0415 (deferred like the other extras)
    tools.update(todos.bound())
    prompt = (system_prompt or LOCAL_SYSTEM_PROMPT.format(
        host=host, store_hint=STORE_LOCAL if local_store else STORE_REMOTE
    )) + todos.RULES + FILE_OPS_RULE
    clone = Clone(
        addr,
        transport,
        cfg,
        sink,
        tools=tools,
        system_prompt=prompt,
        llm=llm,
        poll_timeout=poll_timeout,
        on_ask=on_ask,
        pending=pending,
        # YESIR native full toolset (spec 6.2); spawned workers inherit it.
        tool_names=BASE_TOOL_NAMES,
        child_tool_names=BASE_TOOL_NAMES,
        skill_save=True,
    )
    # Room runtime injects the per-turn abort predicate here (room.py) so a
    # stopped turn wakes a blocked delegate/send_file wait.
    clone.delegate_tools = delegate_tools
    return clone
