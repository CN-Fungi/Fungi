"""Configuration loading: config.json first, environment variables override."""

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# User-data base: next to the exe when frozen (PyInstaller), repo root in dev.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
# Bundled read-only resources (web/); PyInstaller unpacks them to _MEIPASS.
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))

DEFAULT_API_KEY = "sk-your-key-here"
DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-pro"


def _pyproject() -> Path:
    """Source layout: repo root. Frozen: the copy bundled next to web/."""
    for cand in (PROJECT_ROOT / "pyproject.toml", RESOURCE_ROOT / "pyproject.toml"):
        if cand.is_file():
            return cand
    raise FileNotFoundError("pyproject.toml not found (bundled copy missing?)")


def local_version() -> str:
    """Version string from pyproject.toml, e.g. "0.5.0" (the only place it lives)."""
    with _pyproject().open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


@dataclass
class Config:
    api_key: str = DEFAULT_API_KEY
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    # Per-layer model override: layer number (1/2/3) -> model name.
    # Layers without an entry fall back to `model` (see model_for).
    layer_models: dict[int, str] = field(default_factory=dict)
    system_prompt: str | None = None
    # MCP servers (stdio): name -> {command, args?, env?}
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    # File transfer: per-file size cap and the local landing directory.
    max_file_mb: int = 200
    inbox_dir: str = ""  # empty -> PROJECT_ROOT / "inbox"
    # Presentation nickname shown to friends; never used on the wire.
    display: str = ""
    # >0: send max_tokens on every completion (0 = provider default). Rationale:
    # reasoning-heavy turns can die at the provider's output cap with an empty reply.
    max_tokens: int = 0
    # Experimental private diary (data/diary/): attach the diary tool and
    # inject the diary history into the L1 prompt. Off = the feature is absent.
    diary: bool = False
    # Courier (信使): when on, the local comm clone wakes on peer chats and
    # transfer consents to restate them for the user. Off = peer messages,
    # transfer consent cards and mail reach the user directly (zero agent
    # cost); default on.
    courier: bool = True
    # Message-courier standing memory (消息信使记忆): free text the user keeps
    # in the GUI (" weekdays I'm in class, answer for me and note anything
    # urgent"). Injected into every comm-clone chat turn's system prompt.
    courier_memory: str = ""
    # GUI ringtone for incoming mail: ring while a friend's message sits
    # unread (the tray icon flashes either way). `ring_tone` is one of the ids
    # in fungi/gui/ring.py::TONES — validated there, so an unknown id falls
    # back to the default instead of failing here.
    ring: bool = True
    ring_tone: str = "dingdong"
    # Screen control (spec §35): when on, the user-facing local agent gets the
    # `screen` tool (see this machine's desktop, click/paste/type on it). Off =
    # the tool is never attached, so the model cannot even see it.
    pc_control: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != DEFAULT_API_KEY

    def model_for(self, layer: int) -> str:
        """Model for one TriLayer layer: per-layer override if set, else `model`."""
        return self.layer_models.get(layer) or self.model


def load_config(path: Path | None = None) -> Config:
    """Load config from JSON file, then apply env overrides."""
    cfg = Config()
    source = path if path is not None else CONFIG_PATH
    if source.is_file():
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("api_key"):
            cfg.api_key = data["api_key"]
        if data.get("endpoint"):
            cfg.endpoint = data["endpoint"]
        if data.get("model"):
            cfg.model = data["model"]
        models = data.get("models")
        if isinstance(models, dict):
            cfg.layer_models = {
                int(k): str(v) for k, v in models.items() if str(k) in ("1", "2", "3") and v
            }
        if isinstance(data.get("mcp_servers"), dict):
            cfg.mcp_servers = {
                str(k): v for k, v in data["mcp_servers"].items() if isinstance(v, dict)
            }
        if data.get("max_file_mb"):
            cfg.max_file_mb = int(data["max_file_mb"])
        if data.get("inbox_dir"):
            cfg.inbox_dir = str(data["inbox_dir"])
        if data.get("display"):
            cfg.display = str(data["display"])
        if data.get("max_tokens"):
            cfg.max_tokens = int(data["max_tokens"])
        cfg.diary = bool(data.get("diary"))
        cfg.courier_memory = str(data.get("courier_memory") or "")
        cfg.courier = bool(data.get("courier", True))
        cfg.ring = bool(data.get("ring", True))
        cfg.ring_tone = str(data.get("ring_tone") or "dingdong")
        cfg.pc_control = bool(data.get("pc_control"))
    cfg.api_key = os.environ.get("OPENAI_API_KEY") or cfg.api_key
    cfg.endpoint = os.environ.get("OPENAI_ENDPOINT") or cfg.endpoint
    cfg.model = os.environ.get("OPENAI_MODEL") or cfg.model
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Persist api_key/endpoint/model (and system_prompt when set) to JSON."""
    target = path if path is not None else CONFIG_PATH
    data: dict[str, str | int] = {
        "api_key": cfg.api_key,
        "endpoint": cfg.endpoint,
        "model": cfg.model,
    }
    if cfg.max_tokens:
        data["max_tokens"] = cfg.max_tokens
    if cfg.layer_models:
        data["models"] = {str(k): cfg.layer_models[k] for k in sorted(cfg.layer_models)}
    if cfg.system_prompt:
        data["system_prompt"] = cfg.system_prompt
    if cfg.mcp_servers:
        data["mcp_servers"] = cfg.mcp_servers
    if cfg.max_file_mb != 200:
        data["max_file_mb"] = cfg.max_file_mb
    if cfg.inbox_dir:
        data["inbox_dir"] = cfg.inbox_dir
    if cfg.diary:
        data["diary"] = True
    if cfg.courier_memory:
        data["courier_memory"] = cfg.courier_memory
    if not cfg.courier:
        data["courier"] = False
    if not cfg.ring:
        data["ring"] = False
    if cfg.ring_tone != "dingdong":
        data["ring_tone"] = cfg.ring_tone
    if cfg.pc_control:
        data["pc_control"] = True
    if cfg.display:
        data["display"] = cfg.display
    target.write_text(json.dumps(data, indent=4), encoding="utf-8")
