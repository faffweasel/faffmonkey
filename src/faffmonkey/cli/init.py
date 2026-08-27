import json
import logging
import os
import shutil
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

from faffmonkey.config import (
    DEFAULT_ROUTING,
    DEFAULT_TOOL_PERMISSIONS,
    CompactionConfig,
    HeartbeatConfig,
    write_json_object,
)

log = logging.getLogger(__name__)


_PROJECT_ROOT: Path | None = None


def _find_project_root() -> Path:
    global _PROJECT_ROOT
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            _PROJECT_ROOT = current
            return current
        current = current.parent
    raise RuntimeError("cannot locate project root (no pyproject.toml found)")

DIRS = [
    "workspace",
    "workspace/memory",
    "workspace/memory/daily",
    "workspace/skills",
    "workspace/skills-data",
    "workspace/shared",
    "workspace/shared/inbox",
    "workspace/config",
    "workspace/documents",
    "workspace/tmp",
    "state",
    "state/backups",
    "extensions",
    "backups",
]

# Built from config.py's own defaults so the file faff init writes cannot
# drift from what load_config fills in.
DEFAULT_CONFIG = {
    "timezone": "UTC",
    "models": {},
    "routing": dict(DEFAULT_ROUTING),
    "fallback_models": [],
    "channels": {},
    "heartbeat": asdict(HeartbeatConfig()),
    "compaction": asdict(CompactionConfig()),
    "tools": dict(DEFAULT_TOOL_PERMISSIONS),
}

ENV_TEMPLATE = """\
# faffmonkey — API keys and secrets
# Uncomment and fill in the keys for your provider.

# OPENROUTER_API_KEY=sk-or-...
# VENICE_API_KEY=...
# OLLAMA_API_KEY=...
# TELEGRAM_BOT_TOKEN=...
"""

_AGENT_NAME_PLACEHOLDER = "<!-- Your agent's name -->"
_AGENT_ROLE_PLACEHOLDER = (
    '<!-- e.g. "personal assistant", "research aide", "ops monitor" -->'
)
_AGENT_PRESENTATION_PLACEHOLDER = (
    "<!-- How the agent introduces itself. One sentence. -->"
)
_USER_NAME_PLACEHOLDER = "<!-- Your name -->"
_USER_LOCATION_PLACEHOLDER = "<!-- City, timezone -->"
_USER_PREFERENCES_PLACEHOLDER = (
    "<!-- Communication style, topics of interest, things to avoid -->"
)
_MEMORY_SEED_PLACEHOLDER = (
    "<!-- Persistent facts: timezone, critical preferences, recurring context -->"
)


def _prompt_line(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        print()
        return default
    except KeyboardInterrupt:
        _abort()
    return value or default


def _abort() -> NoReturn:
    # Only EOF skips a question (non-interactive init); Ctrl-C exits, as
    # in the other setup wizards.
    print("\nAborted.")
    raise SystemExit(1)


def _render_template(src: Path, values: dict[str, str]) -> str:
    text = src.read_text()
    for placeholder, value in values.items():
        if value:
            text = text.replace(placeholder, value)
    return text


def _detect_timezone() -> str:
    try:
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        if local_tz is not None:
            name = str(local_tz)
            ZoneInfo(name)
            return name
    except (KeyError, ValueError):
        pass

    try:
        link = Path("/etc/localtime")
        if link.is_symlink():
            target = str(link.resolve())
            idx = target.find("zoneinfo/")
            if idx != -1:
                name = target[idx + len("zoneinfo/"):]
                ZoneInfo(name)
                return name
    except (KeyError, ValueError, OSError):
        pass

    return "UTC"


def _existing_timezone(config_path: Path) -> str | None:
    """The timezone already configured, if it is usable."""
    try:
        existing = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(existing, dict):
        return None
    tz = existing.get("timezone")
    if not isinstance(tz, str) or not tz:
        return None
    try:
        ZoneInfo(tz)
    except (KeyError, ValueError):
        return None
    return tz


def _prompt_timezone(current: str | None = None) -> str:
    # The configured timezone is the default on a re-run, not the machine
    # one: inside the container detection always yields UTC, and accepting
    # it would shift every cron schedule and dated memory file by the offset.
    detected = current or _detect_timezone()
    label = "Configured" if current else "Detected"
    print(f"\n  {label} timezone: {detected}")
    while True:
        try:
            confirm = input(f"  Use {detected}? [Y/n] ").strip().lower()
        except EOFError:
            print()
            return detected
        except KeyboardInterrupt:
            _abort()

        if confirm in ("", "y", "yes"):
            try:
                ZoneInfo(detected)
                return detected
            except (KeyError, ValueError):
                print(f"  Detected timezone {detected!r} is invalid.")
                break
        elif confirm in ("n", "no"):
            break
        else:
            print("  Please answer y or n.")

    while True:
        try:
            tz = input("  Enter timezone (e.g. America/New_York): ").strip()
        except EOFError:
            print()
            return "UTC"
        except KeyboardInterrupt:
            _abort()
        try:
            ZoneInfo(tz)
            return tz
        except (KeyError, ValueError):
            print(f"  Invalid timezone {tz!r}, please try again.")


def _existing_active_hours(config_path: Path) -> tuple[int, int] | None:
    """The heartbeat active hours already configured, if usable."""
    try:
        existing = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(existing, dict):
        return None
    heartbeat = existing.get("heartbeat")
    if not isinstance(heartbeat, dict):
        return None
    hours = heartbeat.get("active_hours")
    if _parse_active_hours(f"{hours[0]}-{hours[1]}" if isinstance(hours, list) and len(hours) == 2 else "") is None:
        return None
    return (hours[0], hours[1])


def _parse_active_hours(text: str) -> tuple[int, int] | None:
    """Accepts '9-22', '09:00-22:00' and overnight ranges like '22-7'.

    Whole hours only: the scheduler compares the current hour, so minutes
    would be silently ignored rather than honoured.
    """
    parts = text.replace(" ", "").split("-")
    if len(parts) != 2:
        return None
    hours: list[int] = []
    for part in parts:
        head, _sep, minutes = part.partition(":")
        if not head.isdigit() or minutes not in ("", "00"):
            return None
        hour = int(head)
        if hour > 23:
            return None
        hours.append(hour)
    if hours[0] == hours[1]:
        return None
    return (hours[0], hours[1])


def _prompt_active_hours(current: tuple[int, int] | None) -> tuple[int, int]:
    start, end = current or DEFAULT_CONFIG["heartbeat"]["active_hours"]
    default = f"{start:02d}:00-{end:02d}:00"
    print("\n  The heartbeat may message you between these hours (local time).")
    while True:
        try:
            raw = input(f"  Active hours [{default}]: ").strip()
        except EOFError:
            print()
            return (start, end)
        except KeyboardInterrupt:
            _abort()
        if not raw:
            return (start, end)
        hours = _parse_active_hours(raw)
        if hours is not None:
            return hours
        print("  Enter a range like 9-22 or 22:00-07:00 (whole hours, 0-23).")


def _check_no_symlink_components(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError(f"{path} is not under {root}")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"refusing to proceed: {current} is a symlink")


def _write_if_missing(path: Path, content: str, *, root: Path | None = None) -> bool:
    if root is not None:
        _check_no_symlink_components(path, root)
    if path.is_symlink():
        log.warning("refusing to write through symlink: %s", path)
        return False
    if path.exists():
        print(f"  exists, skipping: {path}")
        return False
    try:
        path.write_text(content)
    except OSError as e:
        print(f"  note: cannot write {path} ({e.strerror}), skipping")
        return False
    print(f"  created: {path}")
    return True


def _copy_if_missing(src: Path, dst: Path, *, root: Path | None = None) -> bool:
    if root is not None:
        _check_no_symlink_components(dst, root)
    if src.is_symlink():
        log.warning("refusing to copy symlink source: %s", src)
        return False
    if dst.is_symlink():
        log.warning("refusing to write through symlink: %s", dst)
        return False
    if dst.exists():
        print(f"  exists, skipping: {dst}")
        return False
    shutil.copy2(src, dst)
    print(f"  created: {dst}")
    return True


def ensure_extensions_writable(
    extensions_dir: Path,
    command: str = "setup <name>",
    then: str = "rebuild the image: docker compose build",
) -> None:
    if extensions_dir.is_dir() and not os.access(extensions_dir, os.W_OK):
        print(f"Cannot write to {extensions_dir}: it is read-only.")
        print("Docker mounts extensions/ read-only, so this command must run")
        print("on the host, where the directory is writable:")
        print(f"  ./bin/faff {command}")
        print(f"Then {then}")
        raise SystemExit(1)


def run_init(base_path: Path) -> None:
    print("Initialising faffmonkey project...")

    sensitive_dirs = {"state", "state/backups", "extensions", "backups"}
    for d in DIRS:
        p = base_path / d
        if d in sensitive_dirs:
            try:
                st = p.lstat()
                if stat.S_ISLNK(st.st_mode):
                    raise RuntimeError(f"refusing to proceed: {p} is a symlink")
            except FileNotFoundError:
                pass
        if not p.exists():
            try:
                if d in sensitive_dirs:
                    os.mkdir(p, 0o700)
                else:
                    p.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                # A bind mount whose host directory did not exist is created
                # by Docker as root, and the container runs as FAFF_UID.
                raise SystemExit(
                    f"cannot create {p}: permission denied.\n"
                    "The data root is owned by another user, usually because "
                    "Docker created a missing mount directory as root. On the "
                    "host, delete it and create the data root as yourself:\n"
                    "  mkdir -p $FAFF_HOME/workspace $FAFF_HOME/state $FAFF_HOME/extensions"
                )
            print(f"  created: {p}/")
        elif d in sensitive_dirs:
            try:
                os.chmod(p, 0o700)
            except OSError:
                print(f"  note: cannot chmod {p} (read-only mount?), skipping")

    workspace = base_path / "workspace"
    template_workspace = _find_project_root() / "templates" / "workspace"

    tz = _prompt_timezone(_existing_timezone(base_path / "state" / "config.json"))
    active_hours = _prompt_active_hours(
        _existing_active_hours(base_path / "state" / "config.json")
    )

    identity_path = workspace / "IDENTITY.md"
    user_path = workspace / "USER.md"
    memory_path = workspace / "MEMORY.md"

    user_name = agent_name = role = style = seed = ""
    if not (identity_path.exists() and user_path.exists() and memory_path.exists()):
        print("\nAgent setup (press Enter to skip a question):")
    if not user_path.exists():
        user_name = _prompt_line("Your name")
    if not identity_path.exists():
        agent_name = _prompt_line("Agent's name")
        role = _prompt_line("Agent's role", "personal assistant")
    if not user_path.exists():
        style = _prompt_line("Communication style (terse/normal/detailed)", "normal")
    if not memory_path.exists():
        seed = _prompt_line("One thing the agent should remember about you")

    if template_workspace.is_dir():
        print("\nCopying workspace templates:")
        identity_src = template_workspace / "IDENTITY.md"
        if identity_src.is_file() and not identity_path.exists():
            presentation = ""
            if agent_name and role:
                presentation = f"I'm {agent_name}, your {role}."
            _write_if_missing(identity_path, _render_template(identity_src, {
                _AGENT_NAME_PLACEHOLDER: agent_name,
                _AGENT_ROLE_PLACEHOLDER: role,
                _AGENT_PRESENTATION_PLACEHOLDER: presentation,
            }), root=base_path)
        user_src = template_workspace / "USER.md"
        if user_src.is_file() and not user_path.exists():
            prefs = f"Communication style: {style}." if style else ""
            _write_if_missing(user_path, _render_template(user_src, {
                _USER_NAME_PLACEHOLDER: user_name,
                _USER_LOCATION_PLACEHOLDER: f"Timezone: {tz}",
                _USER_PREFERENCES_PLACEHOLDER: prefs,
            }), root=base_path)
        memory_src = template_workspace / "MEMORY.md"
        if memory_src.is_file() and not memory_path.exists():
            _write_if_missing(memory_path, _render_template(memory_src, {
                _MEMORY_SEED_PLACEHOLDER: f"- {seed}" if seed else "",
            }), root=base_path)

        from faffmonkey.cli.update import _sync_templates
        try:
            _sync_templates(workspace)
        except RuntimeError as e:
            print(f"  skipped template sync: {e}")

    print("\nGenerating config files:")

    config_path = base_path / "state" / "config.json"
    existing: dict | None = None
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            # Say what is actually wrong and where. "previous config was
            # unreadable" left the operator with a renamed file and no idea
            # which line broke it, which is the whole reason they were sent
            # here by doctor in the first place.
            print(f"  {config_path} is not valid JSON: {e}")
            loaded = None
        except OSError as e:
            print(f"  cannot read {config_path}: {e.strerror}")
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
        else:
            # init is the repair both _check_provider_configured and doctor
            # recommend for a corrupt config, so it has to survive one.
            damaged = config_path.with_suffix(".json.corrupt")
            config_path.replace(damaged)
            print(f"  previous config was unreadable, kept as: {damaged}")

    hours_label = f"{active_hours[0]:02d}:00-{active_hours[1]:02d}:00"
    if existing is not None:
        existing["timezone"] = tz
        heartbeat = existing.get("heartbeat")
        if not isinstance(heartbeat, dict):
            heartbeat = dict(DEFAULT_CONFIG["heartbeat"])
            existing["heartbeat"] = heartbeat
        heartbeat["active_hours"] = list(active_hours)
        write_json_object(config_path, existing)
        print(f"  timezone in {config_path}: {tz}")
        print(f"  heartbeat active hours: {hours_label}")
    else:
        config = {
            **DEFAULT_CONFIG,
            "timezone": tz,
            "heartbeat": {**DEFAULT_CONFIG["heartbeat"], "active_hours": list(active_hours)},
        }
        write_json_object(config_path, config)
        print(f"  created: {config_path}")
        print(f"  heartbeat active hours: {hours_label}")

    env_path = base_path / "state" / ".env"
    if _write_if_missing(env_path, ENV_TEMPLATE, root=base_path):
        os.chmod(env_path, 0o600)
    _write_if_missing(base_path / "state" / "commands.json", "{}\n", root=base_path)
    _write_if_missing(base_path / "workspace" / "config" / "jobs.json", "[]\n", root=base_path)
    _write_if_missing(base_path / "requirements.extra.txt", "")
    _write_if_missing(base_path / "extensions" / ".origin.json", "{}\n", root=base_path)

    print('\nRun "faff setup provider" to configure your LLM.')
