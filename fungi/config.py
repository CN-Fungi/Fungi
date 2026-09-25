"""Configuration loading: config.json is the one source for the model trio
(api_key / endpoint / model). Fungi's own environment hooks all carry the FUNGI_
prefix; the generic OPENAI_* names deliberately override nothing (spec §65)."""

import json
import os
import re
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


def _writable(folder: Path) -> bool:
    """Can this user actually create a file in `folder`?

    A probe file, not `os.access`: on Windows os.access answers about the
    read-only attribute and knows nothing about ACLs, so a Program Files
    folder looks writable and then raises WinError 5 on the first write.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def writable_dir(preferred: Path, name: str) -> Path:
    """`preferred` when this user can write there, else %LOCALAPPDATA%\\Fungi\\<name>.

    Unpacking a release into Program Files (or any admin-owned place) makes the
    program's own folder read-only. Everything that writes beside the exe needs
    somewhere to go instead — the log directory has fallen back this way since
    §45, and the file inbox needs the same rule or every incoming transfer dies
    on the receiver's disk (§49).
    """
    if _writable(preferred):
        return preferred
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    fallback = Path(base) / "Fungi" / name
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


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
    # The pickable models, most recently used first, `model` always among them
    # (spec §66). The settings page and the WebUI header render this as a
    # dropdown, so moving between the two or three models somebody actually uses
    # never means retyping a name. Grows by adding — see remember_model.
    model_list: list[str] = field(default_factory=list)
    # Per-model endpoint+key memory (spec §68): model name -> {"endpoint":…, "api_key":…}.
    # The pickable list may span vendors, and one config file has one endpoint and one
    # key — so switching to a name has to bring the pair that name answers on, or the
    # three MiMo names and the DeepSeek name can never be clickable from one list.
    # Learned where we get proof (a probe came back ok) or where the user said so
    # (they typed the pair and saved): see remember_provider / adopt_provider.
    model_providers: dict[str, dict[str, str]] = field(default_factory=dict)
    # Per-layer model override: layer number (1/2/3) -> model name.
    # Layers without an entry fall back to `model` (see model_for).
    layer_models: dict[int, str] = field(default_factory=dict)
    system_prompt: str | None = None
    # MCP servers (stdio): name -> {command, args?, env?}
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    # File transfer: the local landing directory (no size cap — see spec §48).
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
    # GhostWorld character control (spec §43): when on, the local agent gets the
    # `ghostworld` tool and a watcher that wakes it when the in-game player
    # speaks. Off = neither exists (no tool, no child process, no wakeups).
    # `ghostworld_dir` is the game's own folder — a checkout, or the unpacked
    # release (spec §44); empty means use what PATH gives (console scripts).
    ghostworld: bool = False
    ghostworld_dir: str = ""
    # The decision service this machine points the screen tool at (spec §63.1):
    # `url` (a resident service), `serve` (how to start it), plus `ask`/`weights`/
    # `k`/`timeout`/`wait`/`autostart`. The whole block round-trips as one dict, so
    # the keys the settings page does not show — anything written by hand —
    # survive a save untouched. The page edits only `url` and `serve`.
    decider: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != DEFAULT_API_KEY

    def model_for(self, layer: int) -> str:
        """Model for one TriLayer layer: per-layer override if set, else `model`."""
        return self.layer_models.get(layer) or self.model


def remember_model(cfg: Config, model: str) -> bool:
    """Put `model` at the front of `cfg.model_list` and make it the model in use.

    spec §66 (user, 2026-09-25: "原先的输入框从覆盖变成添加, 如果与之前模型不一样就新添"):
    the box adds, it never overwrites the pickable list. The model being switched
    away from stays one click away, and typing the same name again does not add a
    second row. Returns True when the name was new.
    """
    name = model.strip()
    if not name:
        return False
    fresh = name not in cfg.model_list
    cfg.model_list = [name, *[m for m in cfg.model_list if m != name]]
    cfg.model = name
    return fresh


def provider_for(cfg: Config, model: str) -> tuple[str, str] | None:
    """The `(endpoint, api_key)` this model last answered on - None = never learned.

    spec §68: switching a model has to bring that name's own endpoint and key along
    (the user's words, and why one config file was not enough, live in docs/spec.md).
    """
    known = cfg.model_providers.get(model.strip())
    if not known:
        return None
    return str(known.get("endpoint") or ""), str(known.get("api_key") or "")


def adopt_provider(cfg: Config, model: str) -> bool:
    """Make `cfg.endpoint`/`cfg.api_key` the pair `model` is known to answer on.

    True = the config moved. A model we never learned anything about keeps whatever
    is in the config right now: the user may be about to type the pair by hand, and
    silently pointing the file somewhere else would lose what they typed.
    """
    known = provider_for(cfg, model)
    if known is None:
        return False
    endpoint, api_key = known
    if not endpoint or not api_key or (endpoint == cfg.endpoint and api_key == cfg.api_key):
        return False
    cfg.endpoint, cfg.api_key = endpoint, api_key
    return True


def remember_provider(cfg: Config, model: str, endpoint: str, api_key: str) -> bool:
    """Record the pair `model` answers on. True = this changed what we knew.

    Called at the two moments we have a reason to believe a pairing is real: a probe
    (or a switch, which probes) came back ok, or the user typed the pair and saved.
    """
    name, endpoint, api_key = model.strip(), endpoint.strip(), api_key.strip()
    if not name or not endpoint or not api_key:
        return False
    if cfg.model_providers.get(name) == {"endpoint": endpoint, "api_key": api_key}:
        return False
    cfg.model_providers[name] = {"endpoint": endpoint, "api_key": api_key}
    return True


def switch_model(cfg: Config, model: str) -> bool:
    """Make `model` the one in use, bringing its endpoint+key along. True = new name.

    The one entry point the settings page and the WebUI's header both use, so
    "selecting a model" means the same three writes in both shells (spec §66/§68).
    """
    fresh = remember_model(cfg, model)
    adopt_provider(cfg, cfg.model)
    return fresh


def forget_model(cfg: Config, model: str) -> bool:
    """Drop a name from the pickable list, and forget its endpoint+key. False = not there.

    spec §69 (user, 2026-09-25: the launcher's list needs a delete). Only an explicit
    delete forgets the pairing: `save_config` otherwise keeps names that left the list
    on purpose (§68 - typing an old name back then works on the first try). Deleting is
    the user saying "this one is not coming back", so the key goes with it.

    The model in use is *not* touched here - the caller switches first. A config whose
    `model` is not in `model_list` would open the dropdown with nothing selected.
    """
    name = model.strip()
    if not name or name not in cfg.model_list:
        return False
    cfg.model_list = [m for m in cfg.model_list if m != name]
    cfg.model_providers.pop(name, None)
    return True


# Explorer's "Copy as path" hands over `C:\Users\me\GhostWorld`, and a lone
# backslash is not a legal JSON escape — so a config edited that way failed to
# parse whole, taking the api key and every switch down with it.
_NOT_AN_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Say it once per process: `load_config` runs on every tool call.

    Both ways out are needed: the console keeps its warning, and the log keeps it
    for the windowed exe — which has no stderr at all to print to.
    """
    if key not in _warned:
        _warned.add(key)
        from . import runlog  # noqa: PLC0415 (deferred: runlog reads this module)

        runlog.problem("%s", message)
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def _parse_config(text: str, source: Path) -> dict:
    """Parse the file, repairing the one mistake Windows users keep making.

    Only backslashes JSON itself rejects are doubled, so `\\`, `\\n`, `\\"` and
    `\\u00e9` keep their meaning. The repair is in memory: the user's file is read
    and reported on, never rewritten behind their back.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        repaired = _NOT_AN_ESCAPE.sub(r"\\\\", text)
        if repaired != text:
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                pass
            else:
                _warn_once(
                    f"repaired:{source}",
                    f"[config] {source} 里的 Windows 路径没转义（\\U 这类）：已按字面读进来，文件没改",
                )
                return data
        # Falling back to defaults is silent for the user and total in effect:
        # the api key, the model and every switch they just set are discarded.
        _warn_once(
            f"broken:{source}",
            f"[config] {source} 不是合法 JSON（{exc}）→ 本次按默认值运行，你的设置没有生效",
        )
        return {}


def normalize_dir(value: str) -> tuple[str, bool]:
    """A folder as a person hands it over, in the one shape this program stores.

    Explorer's "Copy as path" gives `"C:\\Users\\me\\GhostWorld"` — quotes included —
    and a hand-typed path keeps its backslashes. Stored as-is inside config.json,
    those backslashes are an invalid JSON escape that made the whole file
    unreadable (api key and every switch with it). Forward slashes, no quotes, is
    what goes in; the second half of the pair is "did we have to change it".
    """
    cleaned = value.strip().strip('"').strip("'").strip()
    cleaned = cleaned.replace("\\", "/")
    return cleaned, cleaned != value


def repair_config_file(path: Path | None = None) -> str | None:
    """Rewrite a config.json that cannot be read, in the correct format.

    Returns a sentence for the user (the settings page shows it top-right) or None
    when there was nothing to do: a file that already parses, a missing file, or
    damage that is not the mistake we can fix without guessing.

    Nothing but the backslashes JSON rejects is touched, and the file is rewritten
    from the *parsed* document — so keys this version knows nothing about survive.
    """
    source = path if path is not None else CONFIG_PATH
    try:
        original = source.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        json.loads(original)
    except json.JSONDecodeError:
        pass
    else:
        return None
    repaired = _NOT_AN_ESCAPE.sub(r"\\\\", original)
    if repaired == original:
        return None
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("ghostworld_dir"), str):
        data["ghostworld_dir"], _ = normalize_dir(data["ghostworld_dir"])
    try:
        source.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return None
    return (
        f"{source.name} 里的 Windows 路径没转义，已改写成合法 JSON（路径换成正斜杠，其它内容没动）"
    )


def load_config(path: Path | None = None) -> Config:
    """Load config from the JSON file — and only from there (spec §65)."""
    cfg = Config()
    source = path if path is not None else CONFIG_PATH
    if source.is_file():
        try:
            data = _parse_config(source.read_text(encoding="utf-8-sig"), source)
        except OSError:
            data = {}
        if data.get("api_key"):
            cfg.api_key = data["api_key"]
        if data.get("endpoint"):
            cfg.endpoint = data["endpoint"]
        if data.get("model"):
            cfg.model = data["model"]
        listed = data.get("model_list")
        if isinstance(listed, list):
            # dict.fromkeys: a hand-edited file with the same name twice must not
            # paint two identical rows in the dropdown.
            cfg.model_list = list(dict.fromkeys(m for m in (str(x).strip() for x in listed) if m))
        # The model in use is always one of the pickable ones: the dropdown would
        # otherwise open with nothing selected (spec §66).
        if cfg.model not in cfg.model_list:
            cfg.model_list.insert(0, cfg.model)
        # Which endpoint+key each name answers on (spec §68). A half-written entry
        # (no endpoint or no key) is not a pairing anybody can use: drop it here
        # rather than carry a row that can only fail at the next switch.
        known = data.get("model_providers")
        if isinstance(known, dict):
            cfg.model_providers = {
                str(name).strip(): {
                    "endpoint": str(v.get("endpoint") or ""),
                    "api_key": str(v.get("api_key") or ""),
                }
                for name, v in known.items()
                if isinstance(v, dict)
                and str(name).strip()
                and v.get("endpoint")
                and v.get("api_key")
            }
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
            # Retired 2026-09-18 (spec §48): transfers are not capped any more.
            # A config that still carries the key must say so once — silently
            # ignoring it would leave someone who set it small believing their
            # disk is protected.
            _warn_once(
                "max_file_mb",
                "[config] max_file_mb 已废止（文件传输不再设上限，见 spec §48）；"
                "这条设置被忽略，config.json 里可以删掉",
            )
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
        cfg.ghostworld = bool(data.get("ghostworld"))
        cfg.ghostworld_dir = str(data.get("ghostworld_dir") or "")
        if isinstance(data.get("decider"), dict):
            cfg.decider = {str(k): v for k, v in data["decider"].items()}
    # 2026-09-25 用户裁决 ("为啥不能模型配置只读, 非要去读环境变量?"): 三个通用名字的覆盖删掉。
    # They are ambient names - on this box they belong to Goose / strands (Xiaomi MiMo), so
    # Fungi handed somebody else's key to its own configured endpoint: 401 "your api key:
    # ****sovy is invalid", a key that is in no config.json and was invisible in the log.
    # Fungi's own env hooks all carry the FUNGI_ prefix (FUNGI_DECIDER / FUNGI_GUI_SCALE /
    # FUNGI_SELFTEST); a future hook goes there, never under a generic name. spec §65.
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
    # Only when there is something to switch to: a one-entry list is just `model`
    # spelled twice, and writing it would grow every user's config.json (§66).
    others = list(dict.fromkeys(m for m in cfg.model_list if m != cfg.model))
    if others:
        data["model_list"] = [cfg.model, *others]
    if cfg.model_providers:
        # Names are kept even after they leave the list: typing an old name back into
        # the add box then works on the first try, which is the whole point of the
        # memory (§68). One endpoint + one key per name is a few dozen bytes.
        data["model_providers"] = {m: dict(p) for m, p in cfg.model_providers.items()}
    if cfg.layer_models:
        data["models"] = {str(k): cfg.layer_models[k] for k in sorted(cfg.layer_models)}
    if cfg.system_prompt:
        data["system_prompt"] = cfg.system_prompt
    if cfg.mcp_servers:
        data["mcp_servers"] = cfg.mcp_servers
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
    if cfg.ghostworld:
        data["ghostworld"] = True
    if cfg.ghostworld_dir:
        data["ghostworld_dir"] = cfg.ghostworld_dir
    if cfg.display:
        data["display"] = cfg.display
    if cfg.decider:
        data["decider"] = cfg.decider
    target.write_text(json.dumps(data, indent=4), encoding="utf-8")
