import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def data_root() -> Path:
    """The directory holding workspace/, state/, extensions/ and backups/.

    $FAFF_HOME when set, else ~/.faffmonkey. Inside the container the image
    pins FAFF_HOME=/app, where compose mounts the host's data root. Data
    lives outside the checkout so a deploy that replaces the checkout
    cannot delete it.
    """
    return Path(os.environ.get("FAFF_HOME") or "~/.faffmonkey").expanduser().resolve()


def apply_compose_env(checkout: Path) -> None:
    """Honour FAFF_HOME from the .env beside docker-compose.yml.

    Compose reads that file for its mounts, so the host CLI must resolve
    the same data root or a setup wizard writes into a different install.
    The shell environment wins; a relative value resolves against the
    checkout, exactly as compose resolves it.
    """
    if os.environ.get("FAFF_HOME"):
        return
    try:
        lines = (checkout / ".env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() != "FAFF_HOME":
            continue
        value = value.strip().strip("'\"")
        if value:
            os.environ["FAFF_HOME"] = str((checkout / Path(value).expanduser()).resolve())
        return


class ConfigError(Exception):
    pass


_API_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)$")
# The single list. config.py and provider_openai_compat.py each kept their
# own and drifted: config allowed host.docker.internal and rejected ::1,
# the provider did the reverse. A config that loaded cleanly could then
# fail on every turn, or vice versa.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
_TZ_RE = re.compile(r"^[A-Za-z0-9_+/\-]+$")


def read_json_object(path: Path, label: str) -> dict:
    """Read a JSON object from disk, raising ConfigError instead of a traceback,
    so the setup wizards can stop with a message before writing anything.
    """
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise ConfigError(f"{label}: cannot read {path} ({e.strerror})") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{label}: {path} is not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{label}: {path} must contain a JSON object")
    return data


def write_json_object(path: Path, data: dict) -> None:
    """Write a JSON object to disk atomically.

    config.json was written with Path.write_text at four call sites, which
    truncates before it writes. A kill in that window leaves an empty or
    half-written file, and every later faff command fails to parse it. The
    wizard is the worst case: it persists the API key atomically and then
    destroys the config on the next line. _append_env_var has used this
    pattern for .env since that was found there.
    """
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def validate_base_url(base_url: str, allow_insecure: bool = False) -> str | None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("https", "http"):
        return "base_url must use https:// or http://"
    if parsed.username or parsed.password:
        return "base_url must not contain embedded credentials"
    if parsed.scheme == "http" and not allow_insecure:
        host = parsed.hostname or ""
        if host not in LOCAL_HOSTS:
            return (
                "http:// is only allowed for localhost, 127.0.0.1, "
                "or host.docker.internal"
            )
    return None


DEFAULT_ROUTING: dict[str, str] = {
    "conversation": "main",
    "compaction": "cheap",
    "heartbeat": "cheap",
    "cron_default": "main",
    "image_understanding": "vision",
}

DEFAULT_TOOL_PERMISSIONS: dict[str, str] = {
    "web_search": "always",
    "web_fetch": "always",
    "file_read": "always",
    "file_list": "always",
    "file_write": "always",
    "file_edit": "always",
    "file_search": "always",
    "file_copy": "always",
    "file_move": "always",
    "file_delete": "always",
    "shell_exec": "ask",
    "skill_invoke": "always",
}


@dataclass
class ModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    module: str = ""
    timeout: int = 120
    allow_insecure: bool = False
    # The bootstrap budget and the compaction threshold are fractions of
    # the model's context window, so each model carries its own.
    context_window: int = 128000


@dataclass
class HeartbeatConfig:
    # No interval here. The cron expression on the heartbeat job is the
    # schedule; a second source of truth that nothing read was worse than
    # none.
    active_hours: tuple[int, int] = (9, 22)
    ack_max_chars: int = 300
    enabled: bool = True


@dataclass
class CompactionConfig:
    threshold: float = 0.8
    target_ratio: float = 0.2
    protect_last_n: int = 20
    hard_message_limit: int = 400


@dataclass
class DailyNoteConfig:
    """How often the loop asks the cheap model for a daily-log note.

    Whichever comes first: this many user turns since the last note, or
    this many minutes since it. Nothing fires while nobody is talking.
    """
    every_turns: int = 10
    every_minutes: int = 60


@dataclass
class ChannelConfig:
    enabled: bool
    module: str = ""
    allowed_users: list[str] = field(default_factory=list)
    # Discord only, but parsed for every channel so the setup wizard's
    # documented value is not silently dropped on the way through.
    group_policy: str = "mention"


@dataclass
class SearchConfig:
    provider: str = ""
    module: str = ""
    api_key_env: str = ""


@dataclass
class VoiceConfig:
    transcriber: str = ""
    transcriber_module: str = ""
    transcriber_model: str = "whisper-1"
    synthesiser: str = ""
    synthesiser_module: str = ""
    synthesiser_model: str = "tts-1"
    synthesiser_voice: str = "alloy"
    api_key_env: str = ""
    base_url: str = "https://api.openai.com/v1"


@dataclass
class Config:
    models: dict[str, ModelConfig]
    routing: dict[str, str]
    fallback_models: list[ModelConfig]
    timezone: ZoneInfo
    heartbeat: HeartbeatConfig
    compaction: CompactionConfig
    channels: dict[str, ChannelConfig]
    tool_permissions: dict[str, str]
    shell_preapproved: list[str] = field(default_factory=list)
    daily_note: DailyNoteConfig = field(default_factory=DailyNoteConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    def resolve_model(self, task: str, override: str | None = None) -> ModelConfig:
        slot = override or self.routing.get(task)
        if slot is None:
            raise ConfigError(f"no routing for task {task!r}")
        model = self.models.get(slot)
        if model is None:
            raise ConfigError(f"no model configured for slot {slot!r}")
        return model


def _parse_model(raw: dict, label: str) -> ModelConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"model {label!r}: must be a JSON object")
    provider = raw.get("provider")
    if not provider:
        raise ConfigError(f"model {label!r}: missing 'provider'")
    model = raw.get("model")
    if not model:
        raise ConfigError(f"model {label!r}: missing 'model'")

    base_url = raw.get("base_url", "")
    if not base_url:
        raise ConfigError(f"model {label!r}: missing 'base_url'")

    allow_insecure_raw = raw.get("allow_insecure", False)
    if isinstance(allow_insecure_raw, str):
        raise ConfigError(
            f"model {label!r}: allow_insecure must be true or false, not a string"
        )
    allow_insecure = bool(allow_insecure_raw)
    url_err = validate_base_url(base_url, allow_insecure=allow_insecure)
    if url_err is not None:
        raise ConfigError(f"model {label!r}: {url_err}")

    api_key_env = raw.get("api_key_env", "")
    if api_key_env and not _API_KEY_ENV_RE.match(api_key_env):
        raise ConfigError(
            f"model {label!r}: api_key_env {api_key_env!r} must match "
            f"[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)"
        )
    api_key = ""
    if api_key_env:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise ConfigError(
                f"model {label!r}: env var {api_key_env!r} not set"
            )

    timeout = raw.get("timeout", 120)
    # bool subclasses int, so "timeout": true was accepted as a 1 second
    # timeout rather than rejected as the wrong type.
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ConfigError(f"model {label!r}: timeout must be a positive integer")

    context_window = raw.get("context_window", 128000)
    if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        raise ConfigError(
            f"model {label!r}: context_window must be a positive integer"
        )

    return ModelConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        module=raw.get("module", ""),
        timeout=timeout,
        allow_insecure=allow_insecure,
        context_window=context_window,
    )


def _parse_heartbeat(raw: dict | None) -> HeartbeatConfig:
    if raw is None:
        return HeartbeatConfig()
    hours = raw.get("active_hours", [9, 22])
    if (
        not isinstance(hours, list)
        or len(hours) != 2
        or not all(isinstance(h, int) for h in hours)
    ):
        raise ConfigError(
            "heartbeat active_hours must be a list of two integers"
        )
    if "interval_minutes" in raw:
        raise ConfigError(
            "heartbeat interval_minutes has been removed; the heartbeat job's"
            " cron expression in workspace/config/jobs.json is the schedule"
        )
    ack_max_chars = raw.get("ack_max_chars", 300)
    if not isinstance(ack_max_chars, int) or isinstance(ack_max_chars, bool):
        raise ConfigError("heartbeat ack_max_chars must be an integer")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("heartbeat enabled must be a boolean")
    return HeartbeatConfig(
        active_hours=(hours[0], hours[1]),
        ack_max_chars=ack_max_chars,
        enabled=enabled,
    )


def _parse_daily_note(raw: dict | None) -> DailyNoteConfig:
    if raw is None:
        return DailyNoteConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'daily_note' must be a JSON object")
    result = DailyNoteConfig()
    for key in ("every_turns", "every_minutes"):
        value = raw.get(key, getattr(result, key))
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"daily_note {key} must be a positive integer")
        setattr(result, key, value)
    return result


def _parse_compaction(raw: dict | None) -> CompactionConfig:
    if raw is None:
        return CompactionConfig()
    threshold = raw.get("threshold", 0.8)
    if not isinstance(threshold, (int, float)) or not (0 < threshold < 1):
        raise ConfigError("compaction threshold must be between 0 and 1 (exclusive)")
    target_ratio = raw.get("target_ratio", 0.2)
    if not isinstance(target_ratio, (int, float)) or not (0 < target_ratio < 1):
        raise ConfigError("compaction target_ratio must be between 0 and 1 (exclusive)")
    protect_last_n = raw.get("protect_last_n", 20)
    # 0 loaded cleanly and then indexed one past the end of the message
    # list on every compaction, which propagated all the way out of
    # handle_message and left the agent unable to answer at all.
    if not isinstance(protect_last_n, int) or isinstance(protect_last_n, bool) or protect_last_n < 1:
        raise ConfigError("compaction protect_last_n must be a positive integer")
    hard_message_limit = raw.get("hard_message_limit", 400)
    if not isinstance(hard_message_limit, int) or hard_message_limit <= 0:
        raise ConfigError("compaction hard_message_limit must be a positive integer")
    return CompactionConfig(
        threshold=threshold,
        target_ratio=target_ratio,
        protect_last_n=protect_last_n,
        hard_message_limit=hard_message_limit,
    )


def _parse_voice(raw: dict | None) -> VoiceConfig:
    if raw is None:
        return VoiceConfig()
    defaults = VoiceConfig()
    for key in (
        "transcriber", "transcriber_module", "transcriber_model",
        "synthesiser", "synthesiser_module", "synthesiser_model",
        "synthesiser_voice", "api_key_env", "base_url",
    ):
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(f"voice: {key} must be a string")
    api_key_env = raw.get("api_key_env", "")
    if api_key_env and not _API_KEY_ENV_RE.match(api_key_env):
        raise ConfigError(
            f"voice: api_key_env {api_key_env!r} must match "
            f"[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)"
        )
    base_url = raw.get("base_url", defaults.base_url)
    url_err = validate_base_url(base_url)
    if url_err is not None:
        raise ConfigError(f"voice: {url_err}")
    return VoiceConfig(
        transcriber=raw.get("transcriber", ""),
        transcriber_module=raw.get("transcriber_module", ""),
        transcriber_model=raw.get("transcriber_model", defaults.transcriber_model),
        synthesiser=raw.get("synthesiser", ""),
        synthesiser_module=raw.get("synthesiser_module", ""),
        synthesiser_model=raw.get("synthesiser_model", defaults.synthesiser_model),
        synthesiser_voice=raw.get("synthesiser_voice", defaults.synthesiser_voice),
        api_key_env=api_key_env,
        base_url=base_url,
    )


_VALID_GROUP_POLICIES = frozenset({"mention", "open", "dm_only"})


def _parse_channels(raw: dict | None) -> dict[str, ChannelConfig]:
    if raw is None:
        return {}
    result: dict[str, ChannelConfig] = {}
    for name, ch in raw.items():
        if not isinstance(ch, dict):
            raise ConfigError(f"channel {name!r} must be a JSON object")
        allowed_users = ch.get("allowed_users", [])
        if not isinstance(allowed_users, list):
            raise ConfigError(
                f"channel {name!r}: allowed_users must be a list"
            )
        if not all(isinstance(u, str) for u in allowed_users):
            raise ConfigError(
                f"channel {name!r}: allowed_users elements must be strings"
            )
        group_policy = ch.get("group_policy", "mention")
        if group_policy not in _VALID_GROUP_POLICIES:
            raise ConfigError(
                f"channel {name!r}: group_policy must be one of "
                f"{', '.join(sorted(_VALID_GROUP_POLICIES))}"
            )
        enabled = ch.get("enabled", False)
        # Never type-checked, so "enabled": "false" started the channel:
        # any non-empty string is truthy. LLM-written JSON produces exactly
        # that, and every other field here is validated.
        if not isinstance(enabled, bool):
            raise ConfigError(
                f"channel {name!r}: enabled must be true or false, got {enabled!r}"
            )
        result[name] = ChannelConfig(
            enabled=enabled,
            module=ch.get("module", ""),
            allowed_users=allowed_users,
            group_policy=group_policy,
        )
    return result


_KNOWN_CONFIG_KEYS = frozenset({
    "models", "routing", "fallback_models", "timezone",
    "heartbeat", "compaction", "channels", "tools", "search", "voice",
    "daily_note",
})

_HARD_ERROR_UNKNOWN_KEYS = frozenset({
    "tool_permissions", "permissions",
})


def validate_config_schema(raw: dict) -> list[str]:
    warnings: list[str] = []
    for key in raw:
        if key not in _KNOWN_CONFIG_KEYS:
            warnings.append(f"unknown config key: {key!r}")
    if "models" in raw and not isinstance(raw["models"], dict):
        warnings.append("'models' must be a JSON object")
    if "routing" in raw and not isinstance(raw["routing"], dict):
        warnings.append("'routing' must be a JSON object")
    if "fallback_models" in raw and not isinstance(raw["fallback_models"], list):
        warnings.append("'fallback_models' must be a JSON array")
    if "timezone" in raw and not isinstance(raw["timezone"], str):
        warnings.append("'timezone' must be a string")
    if "heartbeat" in raw and not isinstance(raw["heartbeat"], dict):
        warnings.append("'heartbeat' must be a JSON object")
    if "compaction" in raw and not isinstance(raw["compaction"], dict):
        warnings.append("'compaction' must be a JSON object")
    if "channels" in raw and not isinstance(raw["channels"], dict):
        warnings.append("'channels' must be a JSON object")
    if "tools" in raw and not isinstance(raw["tools"], dict):
        warnings.append("'tools' must be a JSON object")
    if "search" in raw and not isinstance(raw["search"], dict):
        warnings.append("'search' must be a JSON object")
    if "voice" in raw and not isinstance(raw["voice"], dict):
        warnings.append("'voice' must be a JSON object")
    return warnings


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    with open(path) as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(str(e)) from e

    schema_warnings = validate_config_schema(raw)
    for w in schema_warnings:
        logger.warning(w)
    for key in raw:
        if key not in _KNOWN_CONFIG_KEYS and key in _HARD_ERROR_UNKNOWN_KEYS:
            raise ConfigError(
                f"unknown config key {key!r} resembles a security-relevant "
                f"setting; check for typos (did you mean 'tools'?)"
            )

    raw_models = raw.get("models")
    if not raw_models:
        raise ConfigError("missing 'models' in config")
    if not isinstance(raw_models, dict):
        raise ConfigError("'models' must be a JSON object")
    if "main" not in raw_models:
        raise ConfigError("missing 'main' model slot in config")

    models: dict[str, ModelConfig] = {}
    for slot, model_raw in raw_models.items():
        models[slot] = _parse_model(model_raw, slot)

    raw_routing = raw.get("routing", {})
    if not isinstance(raw_routing, dict):
        raise ConfigError("'routing' must be a JSON object")
    routing = {**DEFAULT_ROUTING, **raw_routing}

    raw_fallbacks = raw.get("fallback_models", [])
    if not isinstance(raw_fallbacks, list):
        raise ConfigError("'fallback_models' must be a JSON array")
    fallback_models: list[ModelConfig] = []
    for i, fb in enumerate(raw_fallbacks):
        fallback_models.append(_parse_model(fb, f"fallback[{i}]"))

    tz_str = raw.get("timezone", "UTC")
    if not isinstance(tz_str, str):
        logger.error("timezone must be a string, got %s; using UTC", type(tz_str).__name__)
        tz_str = "UTC"
    if not _TZ_RE.match(tz_str):
        raise ConfigError(f"invalid timezone string: {tz_str!r}")
    try:
        timezone = ZoneInfo(tz_str)
    except (KeyError, ValueError):
        raise ConfigError(f"invalid timezone: {tz_str!r}")

    raw_search = raw.get("search", {})
    if not isinstance(raw_search, dict):
        raise ConfigError("'search' must be a dict")
    search_api_key_env = raw_search.get("api_key_env", "")
    if search_api_key_env and not _API_KEY_ENV_RE.match(search_api_key_env):
        raise ConfigError(
            f"search: api_key_env {search_api_key_env!r} must match "
            f"[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)"
        )
    search = SearchConfig(
        provider=raw_search.get("provider", ""),
        module=raw_search.get("module", ""),
        api_key_env=search_api_key_env,
    )

    raw_voice = raw.get("voice")
    if raw_voice is not None and not isinstance(raw_voice, dict):
        raise ConfigError("'voice' must be a JSON object")
    voice = _parse_voice(raw_voice)

    raw_tools = raw.get("tools", {})
    if not isinstance(raw_tools, dict):
        raise ConfigError("'tools' must be a JSON object")
    _VALID_TOOL_PERMISSIONS = {"always", "ask", "never"}
    for k, v in raw_tools.items():
        if k == "shell_preapproved":
            continue
        if v not in _VALID_TOOL_PERMISSIONS:
            raise ConfigError(
                f"tools.{k}: permission must be one of "
                f"{sorted(_VALID_TOOL_PERMISSIONS)}, got {v!r}"
            )

    shell_preapproved = raw_tools.get("shell_preapproved", [])
    if not isinstance(shell_preapproved, list) or not all(
        isinstance(s, str) for s in shell_preapproved
    ):
        raise ConfigError("tools.shell_preapproved must be a list of strings")

    return Config(
        models=models,
        routing=routing,
        fallback_models=fallback_models,
        timezone=timezone,
        heartbeat=_parse_heartbeat(raw.get("heartbeat")),
        compaction=_parse_compaction(raw.get("compaction")),
        channels=_parse_channels(raw.get("channels")),
        tool_permissions={**DEFAULT_TOOL_PERMISSIONS, **{
            k: v for k, v in raw_tools.items()
            if k != "shell_preapproved"
        }},
        shell_preapproved=shell_preapproved,
        search=search,
        voice=voice,
        daily_note=_parse_daily_note(raw.get("daily_note")),
    )
