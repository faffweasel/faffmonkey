"""faff doctor — system health check and onboarding guide."""

import json
from pathlib import Path
from zoneinfo import ZoneInfo

from faffmonkey.cli.setup_provider import (
    DEFAULT_CONTEXT_WINDOW,
    _safe_url,
    detect_context_window,
)
from faffmonkey.config import ConfigError, load_config
from faffmonkey.runtime.scheduler import load_jobs
from faffmonkey.runtime.tokens import count_tokens


GREEN = "ok"
YELLOW = "--"
RED = "!!"


def _print_check(label: str, status: str, detail: str, hint: str = "") -> None:
    icon = status if status in (GREEN, YELLOW, RED) else "??"
    print(f"  {label:24s} {icon:3s} {detail}")
    if hint:
        print(f"  {'':24s} ->  {hint}")


def _check_dirs(base: Path) -> str:
    workspace = base / "workspace"
    state = base / "state"
    if workspace.is_dir() and state.is_dir():
        _print_check("Directory structure", GREEN, "workspace/ and state/ exist")
        return GREEN
    missing = []
    if not workspace.is_dir():
        missing.append("workspace/")
    if not state.is_dir():
        missing.append("state/")
    _print_check(
        "Directory structure", RED,
        f"Missing: {', '.join(missing)}",
        'Run: faff init',
    )
    return RED


def _config_needs_provider(state_dir: Path) -> bool:
    try:
        raw = json.loads((state_dir / "config.json").read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(raw, dict) and not raw.get("models")


def _check_config(state_dir: Path) -> tuple[str, object]:
    config_path = state_dir / "config.json"
    if not config_path.exists():
        _print_check("Config file", RED, "state/config.json missing", 'Run: faff init')
        return RED, None
    try:
        config = load_config(config_path)
        _print_check("Config file", GREEN, "state/config.json valid")
        return GREEN, config
    except (ConfigError, json.JSONDecodeError) as e:
        suggestion = 'Run: faff setup provider' if _config_needs_provider(state_dir) else ""
        _print_check("Config file", RED, f"parse error: {e}", suggestion)
        return RED, None


def _check_provider(config: object) -> str:
    from faffmonkey.config import Config
    if not isinstance(config, Config):
        return RED

    if not config.models or "main" not in config.models:
        _print_check("LLM provider", RED, "No provider configured", 'Run: faff setup provider')
        return RED

    main = config.models["main"]

    from faffmonkey.config import validate_base_url
    url_err = validate_base_url(main.base_url, allow_insecure=main.allow_insecure)
    if url_err is not None:
        _print_check(
            "LLM provider", RED,
            f"{main.provider} ({main.model}) -- bad base_url",
            url_err,
        )
        return RED

    from faffmonkey.runtime.scheduler import provider_preflight, clear_preflight_cache
    clear_preflight_cache()
    if provider_preflight(main.base_url):
        _print_check(
            "LLM provider", GREEN,
            # Not "responding": provider_preflight returns True for every
            # non-local host without contacting it, by design.
            f"{main.provider} ({main.model}) -- configured",
        )
        return GREEN
    else:
        _print_check(
            "LLM provider", RED,
            f"{main.provider} ({main.model}) -- not reachable",
            f"Check base_url: {_safe_url(main.base_url)}",
        )
        return RED


def _check_context_window(config: object, state_dir: Path) -> str:
    """The conversation slot's window against what the provider says.
    A slot without context_window runs on the 128000 default whatever
    the model can take, and status prints that default as if it were a
    fact, so this is the one place the gap is visible."""
    from faffmonkey.config import Config
    if not isinstance(config, Config):
        return RED
    slot = config.routing.get("conversation", "main")
    model = config.models.get(slot)
    if model is None:
        return RED
    try:
        raw = json.loads((state_dir / "config.json").read_text())
        explicit = "context_window" in raw["models"][slot]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        explicit = False
    reported = detect_context_window(model.base_url, model.api_key, model.model)
    hint = f"Run: faff setup provider, or set models.{slot}.context_window"
    if not explicit:
        if reported is None:
            detail = f"{slot}: not set, running on the {DEFAULT_CONTEXT_WINDOW} default"
        else:
            detail = (
                f"{slot}: not set, running on the {DEFAULT_CONTEXT_WINDOW} default;"
                f" {model.model} reports {reported}"
            )
        _print_check("Context window", YELLOW, detail, hint)
        return YELLOW
    if reported is None:
        _print_check(
            "Context window", GREEN,
            f"{slot}: {model.context_window} tokens ({model.model})",
        )
        return GREEN
    if reported != model.context_window:
        _print_check(
            "Context window", YELLOW,
            f"{slot}: {model.context_window} configured, {model.model} reports {reported}",
            hint,
        )
        return YELLOW
    _print_check(
        "Context window", GREEN,
        f"{slot}: {model.context_window} tokens ({model.model}), matches the provider",
    )
    return GREEN


def _check_channels(config: object, base: Path | None = None) -> str:
    from faffmonkey.config import Config
    if not isinstance(config, Config):
        return RED

    if not config.channels:
        _print_check("Channels", YELLOW, "Not configured", "Run: faff setup telegram (optional)")
        return YELLOW

    worst = GREEN
    for name, ch_config in config.channels.items():
        if not ch_config.enabled:
            _print_check(f"  {name}", YELLOW, "disabled")
            continue

        module = ch_config.module
        if not module:
            from faffmonkey.cli.__main__ import BUILTIN_CHANNELS
            module = BUILTIN_CHANNELS.get(name, "")

        if not module:
            _print_check(f"  {name}", RED, "no module configured")
            worst = RED
            continue

        from faffmonkey.wiring import _import_class, WiringError
        try:
            cls = _import_class(module, workspace=base)
            _print_check(f"  {name}", GREEN, f"{module} -- loaded")
        except WiringError as e:
            err_str = str(e)
            if "file not found" in err_str:
                _print_check(f"  {name}", RED, f"{module} -- file not found")
            elif "dependency not installed" in err_str:
                _print_check(
                    f"  {name}", RED, f"{module} -- ImportError",
                    "Add dependency to requirements.extra.txt and rebuild",
                )
            else:
                _print_check(f"  {name}", RED, f"{module} -- {e}")
            worst = RED

    return worst


def _check_commands(state_dir: Path, base: Path) -> str:
    import shlex
    from faffmonkey.runtime.skills import _COMMAND_KEY_RE, _RESERVED_COMMAND_KEYS

    path = state_dir / "commands.json"
    if not path.is_file():
        _print_check("Commands", GREEN, "No commands.json (optional)")
        return GREEN

    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _print_check("Commands", RED, f"commands.json is invalid: {e}")
        return RED

    if not isinstance(raw, dict):
        _print_check("Commands", RED, "commands.json must be a JSON object")
        return RED

    if not raw:
        _print_check("Commands", GREEN, "No commands configured")
        return GREEN

    worst = GREEN
    for key, value in raw.items():
        if not isinstance(key, str) or not _COMMAND_KEY_RE.match(key) or key in _RESERVED_COMMAND_KEYS:
            _print_check(f"  {key}", YELLOW, "invalid or reserved key, will be skipped")
            worst = YELLOW
            continue
        if not isinstance(value, str) or not value.strip():
            _print_check(f"  {key}", YELLOW, "value must be a non-empty string, will be skipped")
            worst = YELLOW
            continue
        try:
            tokens = shlex.split(value)
        except ValueError:
            _print_check(f"  {key}", YELLOW, "value is not shell-splittable")
            worst = YELLOW
            continue
        # Commands run with cwd=workspace (runtime/skills.py), so relative
        # script paths resolve against workspace/, not the data root.
        missing = [
            t for t in tokens
            if t.endswith(".py")
            and not (base / "workspace" / t).is_file()
            and not Path(t).is_file()
        ]
        if missing:
            _print_check(f"  {key}", YELLOW, f"script not found: {missing[0]}")
            worst = YELLOW
        else:
            _print_check(f"  {key}", GREEN, "ok")
    return worst


def _check_extensions(base: Path) -> str:
    extensions_dir = base / "extensions"
    origin_path = extensions_dir / ".origin.json"

    if not extensions_dir.is_dir():
        _print_check("Extensions", GREEN, "No extensions directory")
        return GREEN

    if not origin_path.exists():
        ext_files = [f for f in extensions_dir.iterdir() if f.suffix == ".py"]
        if ext_files:
            _print_check("Extensions", YELLOW, f"{len(ext_files)} extension(s), no .origin.json")
        else:
            _print_check("Extensions", GREEN, "No extensions configured")
        return GREEN

    try:
        origin = json.loads(origin_path.read_text())
    except (json.JSONDecodeError, OSError):
        _print_check("Extensions", RED, ".origin.json is invalid")
        return RED

    if not origin:
        _print_check("Extensions", GREEN, "No extensions configured")
        return GREEN

    from faffmonkey.cli.update import classify_origin

    classified = classify_origin(base, origin)

    if classified["bad_source"]:
        for filename, reason in classified["bad_source"]:
            if reason.startswith("source file does not exist"):
                _print_check(
                    f"  {filename}", RED, "source file does not exist",
                    f"Expected: {reason.split(': ', 1)[1]}",
                )
            else:
                _print_check(
                    f"  {filename}", RED, "invalid source path in .origin.json",
                    "Source path escapes contrib/ directory",
                )
        return RED

    if classified["tampered"]:
        _print_check("Extension integrity", RED, f"{len(classified['tampered'])} extension(s) modified")
        for f in classified["tampered"]:
            _print_check(f"  {f}", RED, "deployed file modified since install")
        return RED

    if classified["unverifiable"]:
        _print_check("Extension integrity", YELLOW, f"{len(classified['unverifiable'])} extension(s) unverifiable")
        for f in classified["unverifiable"]:
            _print_check(f"  {f}", YELLOW, "reinstall to enable verification")
        return YELLOW

    if classified["stale"]:
        _print_check("Contrib updates", YELLOW, f"{len(classified['stale'])} extension(s) have updates")
        for f, short in classified["stale"]:
            _print_check(
                f"  {f}", YELLOW, "contrib version updated",
                f"Run on the host: ./bin/faff update-extension {short}",
            )
        return YELLOW

    _print_check("Contrib updates", GREEN, "All extensions up to date")
    return GREEN


def _check_heartbeat(config: object, workspace_dir: Path) -> str:
    from faffmonkey.config import Config
    if not isinstance(config, Config):
        return YELLOW

    if not config.heartbeat.enabled:
        _print_check("Heartbeat", YELLOW, "Disabled in config")
        return YELLOW

    # The real schedule is the job, not the config. Reporting a config
    # interval that nothing read told operators the heartbeat was running
    # when no job existed to run it.
    start, end = config.heartbeat.active_hours
    jobs = [j for j in load_jobs(workspace_dir) if j.context == "heartbeat"]
    if not jobs:
        _print_check(
            "Heartbeat", YELLOW,
            f"Enabled ({start:02d}:00-{end:02d}:00) but no heartbeat job",
            'Add a job with "context": "heartbeat" to workspace/config/jobs.json',
        )
        return YELLOW
    active = [j for j in jobs if j.enabled]
    if not active:
        _print_check(
            "Heartbeat", YELLOW,
            f"Enabled ({start:02d}:00-{end:02d}:00) but every heartbeat job is disabled",
        )
        return YELLOW
    stale = [j for j in active if j.session != "agent"]
    if stale:
        _print_check(
            "Heartbeat", YELLOW,
            f"Job {stale[0].id!r} runs as {stale[0].session!r}; a heartbeat wake is an agent turn",
            'Run "faff update" to rewrite the job, or set "session": "agent" on it',
        )
        return YELLOW
    schedules = ", ".join(j.schedule or f"at {j.at}" for j in active)
    _print_check(
        "Heartbeat", GREEN,
        f"Enabled {start:02d}:00-{end:02d}:00, schedule: {schedules}",
    )
    return GREEN


def _check_bootstrap_files(workspace: Path) -> str:
    files = ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md"]
    worst = GREEN
    for name in files:
        path = workspace / name
        if not path.exists():
            _print_check(name, YELLOW, "Missing", "Consider creating this file")
            worst = YELLOW
            continue

        content = path.read_text().strip()
        if not content:
            _print_check(name, YELLOW, "Empty")
            worst = YELLOW
            continue

        tokens = count_tokens(content)
        is_template = "{{" in content or content.startswith("#") and len(content) < 200
        detail = f"Present ({tokens} tokens)"
        if is_template:
            detail += " (template -- consider editing)"
        _print_check(name, GREEN, detail)

    return worst


def _check_database(state_dir: Path) -> str:
    from faffmonkey.runtime.session import SCHEMA_VERSION
    db_path = state_dir / "sessions.db"
    if not db_path.exists():
        _print_check("Database", YELLOW, "No database yet (created on first run)")
        return YELLOW

    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            # The table exists with no row: a first run killed between
            # executescript's CREATEs and the INSERT. The database is
            # otherwise healthy, so repair it rather than telling the
            # operator to intervene by hand forever.
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,),
            )
            conn.commit()
            conn.close()
            _print_check(
                "Database", YELLOW,
                f"schema version row was missing, restored to v{SCHEMA_VERSION}",
            )
            return YELLOW
        conn.close()
        version = row[0]
        if version == SCHEMA_VERSION:
            _print_check("Database", GREEN, f"state/sessions.db -- schema v{version}")
            return GREEN
        else:
            _print_check(
                "Database", YELLOW,
                f"schema v{version}, expected v{SCHEMA_VERSION}",
                "Run: faff update",
            )
            return YELLOW
    except Exception as e:
        _print_check("Database", RED, f"error reading database: {e}")
        return RED


def _check_timezone(config: object) -> str:
    from faffmonkey.config import Config
    if not isinstance(config, Config):
        return RED

    try:
        ZoneInfo(str(config.timezone))
        _print_check("Timezone", GREEN, str(config.timezone))
        return GREEN
    except (KeyError, ValueError):
        _print_check("Timezone", RED, f"invalid: {config.timezone}")
        return RED


def _check_skills(workspace: Path) -> str:
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        _print_check("Skills", YELLOW, "No skills directory")
        return YELLOW

    skills = list(skills_dir.iterdir())
    skill_dirs = [s for s in skills if s.is_dir()]
    if not skill_dirs:
        _print_check("Skills", YELLOW, "No skills installed")
        return YELLOW

    valid = 0
    for sd in skill_dirs:
        if (sd / "SKILL.md").exists():
            valid += 1

    skills_data = workspace / "skills-data"
    writable = True
    if skills_data.exists() and not skills_data.is_dir():
        writable = False
    if skills_data.is_dir():
        import tempfile
        try:
            tf = tempfile.NamedTemporaryFile(dir=str(skills_data), delete=True)
            tf.close()
        except OSError:
            writable = False

    if not writable:
        _print_check("Skills", RED, f"{valid} skill(s) found, skills-data not writable")
        return RED

    _print_check("Skills", GREEN, f"{valid} skill(s) found")
    return GREEN


def _check_jobs(workspace_dir: Path) -> str:
    """Report cron jobs the scheduler refuses to load."""
    jobs_path = workspace_dir / "config" / "jobs.json"
    if not jobs_path.exists():
        _print_check("Cron jobs", GREEN, "No jobs.json (no scheduled jobs)")
        return GREEN

    try:
        raw = json.loads(jobs_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _print_check(
            "Cron jobs", RED, f"jobs.json is unreadable: {e}",
            "Every scheduled job is stopped until this parses",
        )
        return RED
    if not isinstance(raw, list):
        _print_check(
            "Cron jobs", RED, "jobs.json must be a list of job objects",
            "Every scheduled job is stopped until this is fixed",
        )
        return RED

    loaded = load_jobs(workspace_dir)
    rejected = len(raw) - len(loaded)
    if rejected > 0:
        _print_check(
            "Cron jobs", RED,
            f"{rejected} of {len(raw)} job(s) rejected, {len(loaded)} will run",
            "Run faff run and read the scheduler log lines for the reason",
        )
        return RED
    _print_check("Cron jobs", GREEN, f"{len(loaded)} job(s) valid")
    return GREEN


def run_doctor(base: Path) -> int:
    state_dir = base / "state"
    workspace_dir = base / "workspace"

    print()
    has_red = False

    status = _check_dirs(base)
    if status == RED:
        has_red = True

    config_status, config = _check_config(state_dir)
    if config_status == RED:
        has_red = True

    if config is not None:
        if _check_provider(config) == RED:
            has_red = True
        else:
            _check_context_window(config, state_dir)
        if _check_channels(config, base=base) == RED:
            has_red = True
    else:
        _print_check("LLM provider", YELLOW, "Skipped (no valid config)")
        _print_check("Channels", YELLOW, "Skipped (no valid config)")

    if _check_extensions(base) == RED:
        has_red = True
    if _check_commands(state_dir, base) == RED:
        has_red = True
    if config is not None:
        _check_heartbeat(config, workspace_dir)

    if workspace_dir.is_dir():
        _check_bootstrap_files(workspace_dir)
        # These three can return RED. Discarding the result printed the
        # failure and still reported "Ready to run" with exit 0.
        if _check_skills(workspace_dir) == RED:
            has_red = True

    if _check_database(state_dir) == RED:
        has_red = True

    if workspace_dir.is_dir():
        if _check_jobs(workspace_dir) == RED:
            has_red = True

    if config is not None:
        if _check_timezone(config) == RED:
            has_red = True

    # next step guidance
    print()
    if not (state_dir / "config.json").exists():
        print('  Run: faff init')
    elif config is None:
        if _config_needs_provider(state_dir):
            print('  No LLM provider configured. Run: faff setup provider')
        else:
            print(
                '  Config is invalid. Fix state/config.json by hand, or run '
                '"faff init", which reports the parse error, keeps the file '
                'as config.json.corrupt and recreates it from defaults.'
            )
    elif not config.models or "main" not in config.models:
        print('  Run: faff setup provider')
    elif not has_red:
        print('  Ready to run: faff chat (CLI) or faff run (all channels)')
    print()

    return 1 if has_red else 0
