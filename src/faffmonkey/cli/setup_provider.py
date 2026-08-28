import getpass
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from faffmonkey.cli.init import _find_project_root
from faffmonkey.config import (
    _API_KEY_ENV_RE,
    read_json_object,
    validate_base_url,
    write_json_object,
)
from faffmonkey.runtime.scheduler import HEARTBEAT_GATE_PROMPT, LAST_CHANNEL
from faffmonkey.runtime.redaction import redact

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _sanitise_display(text: str) -> str:
    """Strip escapes and non-printables from anything a remote API echoes back."""
    text = _ANSI_RE.sub("", text)
    return "".join(ch for ch in text if ch.isprintable())


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


CUSTOM_PROVIDER = {
    "name": "Custom OpenAI-compatible",
    "provider_key": "custom",
    "base_url": "",
    "api_key_env": "",
    "default_model": "",
    "notes": "Any endpoint that speaks the OpenAI chat completions API.",
}


def _load_providers(provider_dir: Path) -> list[dict]:
    providers: list[dict] = []
    if provider_dir.is_dir():
        for path in sorted(provider_dir.glob("*.json")):
            with open(path) as f:
                providers.append(json.load(f))
    providers.append(CUSTOM_PROVIDER)
    return providers


def _read_input(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return value or default


def _read_api_key_env_name(
    default: str = "",
    read_input: Callable[[str, str], str] = _read_input,
) -> str:
    """The name of the environment variable that will hold a key, never the
    key itself.

    The prompt read "API key env var" and people pasted the key. The wizard
    then echoed the whole key back inside an "Invalid name" error and
    exited, so the secret landed in the terminal scrollback for nothing.
    """
    hint = f"Enter for {default}" if default else "blank if none"
    while True:
        name = read_input(f"Environment variable name for the key ({hint})", default)
        if not name or _API_KEY_ENV_RE.match(name):
            return name
        print(
            "  That is not a variable name. Names look like OPENAI_API_KEY"
            " (upper case, ending in _API_KEY, _TOKEN or _SECRET)."
        )
        print(
            "  If you pasted the key itself: this prompt wants the name;"
            " the key goes in at the next prompt."
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: object, code: int,
        msg: str, headers: object, newurl: str,
    ) -> None:
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)


def _test_connection(base_url: str, api_key: str, model: str) -> bool:
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "max_tokens": 10,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _no_redirect_opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read())
            choices = data.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                # redact() strips key patterns; it does not strip escapes.
                # This line prints whatever an unverified endpoint returns,
                # so it needs the same treatment the bot-name lines get.
                text = _sanitise_display(redact(text.strip()[:1024]))
                print(f"  Response: {text}")
                return True
            print("  Warning: empty response from provider.")
            return True
    except urllib.error.HTTPError as e:
        print(f"  Connection failed: HTTP {e.code}")
        return False
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"  Connection failed: {type(e).__name__}")
        return False


def _list_ollama_models() -> list[str] | None:
    req = urllib.request.Request(
        "http://localhost:11434/api/tags", method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = data.get("models", [])
            return [m["name"] for m in models if "name" in m]
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError):
        return None


DEFAULT_CONTEXT_WINDOW = 128000

# Keys an OpenAI-style /models entry may carry the window under:
# OpenRouter and Venice use context_length, vLLM max_model_len,
# LM Studio max_context_length. Venice also nests it in model_spec.
_CONTEXT_KEYS = ("context_length", "max_model_len", "max_context_length")


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _get_json(url: str, api_key: str, body: dict | None = None) -> object:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode() if body is not None else None
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _no_redirect_opener.open(req, timeout=10) as resp:
        return json.loads(resp.read())


def _window_from_model_list(base_url: str, api_key: str, model: str) -> int | None:
    data = _get_json(f"{base_url.rstrip('/')}/models", api_key)
    if not isinstance(data, dict):
        return None
    for entry in data.get("data", []):
        if not isinstance(entry, dict) or entry.get("id") != model:
            continue
        candidates = [entry.get(key) for key in _CONTEXT_KEYS]
        spec = entry.get("model_spec")
        if isinstance(spec, dict):
            candidates.append(spec.get("availableContextTokens"))
        for value in candidates:
            found = _positive_int(value)
            if found is not None:
                return found
    return None


def _window_from_ollama(base_url: str, api_key: str, model: str) -> int | None:
    """Ollama's native API sits one level above the /v1 prefix.

    /api/ps reports the window a loaded model was actually started with,
    which the server's default caps below the trained length; the
    connection test has just loaded the main model, so that is the
    number to prefer. /api/show reports the trained length under
    model_info "<family>.context_length", capped by a num_ctx in the
    Modelfile parameters when there is one.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    # Ollama Cloud has no /api/ps; a failure here must not cost the show.
    try:
        running = _get_json(f"{root}/api/ps", api_key)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        running = None
    if isinstance(running, dict):
        for entry in running.get("models", []):
            if not isinstance(entry, dict):
                continue
            if model in (entry.get("name"), entry.get("model")):
                found = _positive_int(entry.get("context_length"))
                if found is not None:
                    return found
    shown = _get_json(f"{root}/api/show", api_key, {"model": model})
    if not isinstance(shown, dict):
        return None
    info = shown.get("model_info")
    if not isinstance(info, dict):
        return None
    trained = None
    for key, value in info.items():
        if key.endswith(".context_length"):
            trained = _positive_int(value)
            break
    if trained is None:
        return None
    params = shown.get("parameters", "")
    if isinstance(params, str):
        match = re.search(r"^num_ctx\s+(\d+)", params, re.MULTILINE)
        if match:
            return min(trained, int(match.group(1)))
    return trained


def detect_context_window(base_url: str, api_key: str, model: str) -> int | None:
    """How many tokens the provider says the model takes; None when it
    will not say. The OpenAI-style model list is tried first, then
    Ollama's native endpoints, so no provider-specific branching is
    needed: a server without the endpoint answers 404 and is skipped."""
    for probe in (_window_from_model_list, _window_from_ollama):
        try:
            found = probe(base_url, api_key, model)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            continue
        if found is not None:
            return found
    return None


def _resolve_context_windows(
    base_url: str, api_key: str, models: list[str],
) -> dict[str, int]:
    """One window per distinct model name: read from the provider where
    it reports one, asked for where it does not. Every slot gets an
    explicit value; the 128000 default is only ever a shown choice,
    never a silent fallback, because it fits neither a 1M model nor a
    4k one and nothing downstream can tell which is in play."""
    windows: dict[str, int] = {}
    fallback = DEFAULT_CONTEXT_WINDOW
    for name in dict.fromkeys(models):
        found = detect_context_window(base_url, api_key, name)
        if found is not None:
            print(f"  Context window for {name}: {found} tokens (reported by the provider)")
            windows[name] = found
            continue
        print(f"  The provider does not report a context window for {name}.")
        while True:
            answer = _read_input(f"Context window for {name}, in tokens", str(fallback))
            if answer.isdigit() and int(answer) > 0:
                break
            print("  Enter a positive whole number of tokens.")
        windows[name] = int(answer)
        fallback = windows[name]
    return windows


def _validate_env_value(key: str, value: str) -> str:
    value = value.strip()
    if "=" in value:
        raise ValueError(f"invalid characters in value for {key}")
    for ch in value:
        if ch == "\t":
            continue
        if ord(ch) < 32:
            raise ValueError(
                f"env value contains non-printable character: {ch!r}"
            )
    return value


def _append_env_var(env_path: Path, key: str, value: str) -> None:
    value = _validate_env_value(key, value)
    if env_path.is_symlink():
        raise ValueError(f"refusing to write through symlink: {env_path}")
    content = env_path.read_text() if env_path.exists() else ""
    lines = content.splitlines()
    lines = [line for line in lines if not line.lstrip("# ").startswith(f"{key}=")]
    lines.append(f"{key}={value}")

    import tempfile
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=".env.", suffix=".tmp",
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp_name, env_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


HEARTBEAT_JOB = {
    "id": "heartbeat",
    "schedule": "0 * * * *",
    "prompt": HEARTBEAT_GATE_PROMPT,
    "context": "heartbeat",
    "session": "isolated",
    "model": "cheap",
    "deliver": {"mode": "announce", "channel": LAST_CHANNEL},
}

MORNING_JOB = {
    "id": "morning",
    "schedule": "5 7 * * *",
    "prompt": "Invoke the morning-routine skill and follow its procedure.",
    "session": "agent",
    "model": "cheap",
    "deliver": {"mode": "announce", "channel": LAST_CHANNEL},
}

# A main-session job has no tools; the memory flush that rotate_session
# triggers is what writes the files. This turn's job is to leave the flush
# a clear note of what mattered, inside the history it is about to read.
EVENING_JOB = {
    "id": "evening",
    "schedule": "0 22 * * *",
    "prompt": (
        "End of day. List briefly what from today's conversation is worth "
        "remembering: decisions, facts about the user, tasks done or "
        "promised, preferences. This note is for the memory flush that "
        "follows, not for the user."
    ),
    "session": "main",
    "model": "cheap",
    "deliver": {"mode": "none"},
    "rotate_session": True,
}

# The preconscious skill's buffer decays by a daily run of its decay
# script; the skill assumed the job existed and no wizard created it, so
# items never decayed. No LLM, no delivery: its product is the updated
# buffer. 06:01 keeps the buffer fresh for the 07:05 morning routine.
PRECONSCIOUS_DECAY_JOB = {
    "id": "preconscious-decay",
    "schedule": "1 6 * * *",
    "skill": "preconscious",
    "session": "none",
    "deliver": {"mode": "none"},
}

DEFAULT_JOBS = (HEARTBEAT_JOB, MORNING_JOB, EVENING_JOB, PRECONSCIOUS_DECAY_JOB)


def _has_job(jobs: list, default: dict) -> dict | None:
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("id") == default["id"]:
            return job
        if default["id"] == "heartbeat" and job.get("context") == "heartbeat":
            return job
    return None


def ensure_default_jobs(workspace: Path) -> None:
    """Add the daily skeleton (heartbeat, morning, evening) where missing.

    init cannot create these: they deliver to a channel and the first
    channel wizard is the first time one exists. They deliver to "last",
    the channel the user most recently spoke on, so a second wizard run or
    a second channel changes nothing and never adds a duplicate. A job the
    operator has written with the same id, or any heartbeat job, is left
    exactly as it is.
    """
    jobs_path = workspace / "config" / "jobs.json"
    jobs: list = []
    if jobs_path.exists():
        try:
            jobs = json.loads(jobs_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Not adding default jobs: {jobs_path} is unreadable ({e})")
            return
        if not isinstance(jobs, list):
            print(f"  Not adding default jobs: {jobs_path} is not a JSON array")
            return
    added: list[str] = []
    for default in DEFAULT_JOBS:
        existing = _has_job(jobs, default)
        if existing is not None:
            print(f"  Job {existing.get('id')!r} already present, left as is")
            continue
        jobs.append(dict(default))
        added.append(default["id"])
    if not added:
        return
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(json.dumps(jobs, indent=2) + "\n")
    print(
        f"  Added to {jobs_path}: {', '.join(added)} (heartbeat hourly, morning "
        f"07:05, evening 22:00, preconscious decay 06:01; announcements go "
        f"to the channel you last used)"
    )


def merge_config(
    config_path: Path,
    key: str,
    value: object,
    subkey: str | None = None,
) -> None:
    """Merge one key into config.json, refusing to write over a config that
    already fails schema validation. With subkey, merges into that nested
    object instead of replacing it."""
    config: dict = {}
    if config_path.exists():
        config = read_json_object(config_path, "config.json")
        from faffmonkey.config import validate_config_schema
        warnings = validate_config_schema(config)
        for w in warnings:
            print(f"  Warning: {w}")
        if warnings:
            raise SystemExit(1)

    if subkey is None:
        config[key] = value
    else:
        section = config.setdefault(key, {})
        if not isinstance(section, dict):
            print(f"  Invalid config: {key!r} must be a JSON object")
            raise SystemExit(1)
        section[subkey] = value

    write_json_object(config_path, config)


def _mirror_requirements(requirements_path: Path, project_root: Path) -> None:
    """Refresh the checkout's build mirror of requirements.extra.txt.

    The data root holds the source of truth; docker compose build can only
    COPY from the checkout, so the checkout carries a disposable, gitignored
    mirror. Best effort: faff update re-syncs it.
    """
    mirror = project_root / "requirements.extra.txt"
    if mirror.resolve() == requirements_path.resolve():
        return
    try:
        shutil.copy2(requirements_path, mirror)
    except OSError as e:
        print(f"  Warning: could not refresh {mirror}: {e}")
        print("  Run ./bin/faff update on the host before rebuilding.")


def install_extension(
    base_dir: Path,
    contrib_file: str,
    dep_line: str | None = None,
    confirm_prompt: str | None = None,
    read_input: Callable[[str, str], str] | None = None,
) -> None:
    """Copy contrib/<contrib_file> into extensions/ and record provenance.

    dep_line, when given, is appended to requirements.extra.txt. read_input
    comes from the calling wizard so its test patches apply to the confirm
    prompt.
    """
    if read_input is None:
        read_input = _read_input

    project_root = _find_project_root()
    contrib_src = project_root / "contrib" / contrib_file
    extensions_dir = base_dir / "extensions"
    dst = extensions_dir / contrib_file
    origin_path = extensions_dir / ".origin.json"

    if not contrib_src.exists():
        print(f"contrib/{contrib_file} not found in project.")
        raise SystemExit(1)

    origin: dict = {}
    if origin_path.exists():
        origin = read_json_object(origin_path, ".origin.json")

    # An existing copy we did not put there is the user's own file.
    if dst.exists() and contrib_file not in origin:
        print(
            f"  {dst} already exists and did not come from contrib. "
            f"Rename it first to avoid collision."
        )
        raise SystemExit(1)

    if confirm_prompt is not None and not dst.exists():
        if read_input(confirm_prompt, "y").lower() not in ("y", "yes"):
            print("Aborted.")
            raise SystemExit(0)

    from faffmonkey.cli.init import ensure_extensions_writable
    ensure_extensions_writable(extensions_dir)
    extensions_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(extensions_dir, 0o700)

    if dst.is_symlink():
        print(f"  Warning: refusing to write through symlink: {dst}")
        raise SystemExit(1)
    shutil.copy2(contrib_src, dst)
    print(f"  Copied contrib/{contrib_file} -> extensions/{contrib_file}")

    if dep_line is not None:
        requirements_path = base_dir / "requirements.extra.txt"
        existing = (
            requirements_path.read_text() if requirements_path.exists() else ""
        )
        if dep_line not in existing:
            with open(requirements_path, "a") as f:
                f.write(dep_line + "\n")
            print(f"  Added {dep_line} to requirements.extra.txt")
        _mirror_requirements(requirements_path, project_root)

    origin[contrib_file] = {
        # Project-relative: doctor resolves this against the project root,
        # and an absolute path would discard that root.
        "source": f"contrib/{contrib_file}",
        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contrib_source_hash": hashlib.sha256(
            contrib_src.read_bytes()
        ).hexdigest()[:12],
    }
    origin_path.write_text(json.dumps(origin, indent=2) + "\n")

    if dep_line is not None:
        print("  If running in Docker, rebuild the image: docker compose build")


def _update_config_models(
    config_path: Path,
    provider_key: str,
    base_url: str,
    model: str,
    cheap_model: str,
    vision_model: str,
    api_key_env: str,
    context_windows: dict[str, int],
) -> None:
    def entry(name: str) -> dict:
        slot: dict = {"provider": provider_key, "model": name}
        if base_url:
            slot["base_url"] = base_url
        if api_key_env:
            slot["api_key_env"] = api_key_env
        if name in context_windows:
            slot["context_window"] = context_windows[name]
        return slot

    merge_config(config_path, "models", {
        "main": entry(model),
        "cheap": entry(cheap_model),
        "vision": entry(vision_model),
    })


def _pick_ollama_model(default_model: str) -> str:
    models = _list_ollama_models()
    if models:
        print("\n  Available models:")
        for i, name in enumerate(models, 1):
            print(f"    {i}) {name}")
        print()
        choice = _read_input("Pick a model number or enter a name", default_model)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                return models[idx]
        except ValueError:
            pass
        return choice
    print("  Ollama is not running or has no models. Enter a model name manually.")
    return _read_input("Model name", default_model)


def run_setup_provider(state_dir: Path, provider_dir: Path | None = None) -> None:
    if provider_dir is None:
        provider_dir = _find_project_root() / "contrib" / "providers" / "openai-compatible"
    providers = _load_providers(provider_dir)

    print("LLM Provider Setup")
    print("=" * 40)
    print()
    print("Choose a provider:")
    for i, p in enumerate(providers, 1):
        print(f"  {i}) {p['name']}")
    print()

    choice = _read_input("Provider number", "1")
    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(providers):
            raise ValueError
    except ValueError:
        print(f"Invalid choice: {choice}")
        raise SystemExit(1)

    provider = providers[idx]
    print(f"\nSelected: {provider['name']}")
    print(f"  {provider['notes']}")

    if provider["provider_key"] == "custom":
        base_url = _read_input("Base URL (e.g. http://localhost:8080/v1)")
        if not base_url:
            print("Base URL is required for custom providers.")
            raise SystemExit(1)
        # load_config rejects names that do not match _API_KEY_ENV_RE, so
        # accepting one here wrote a config the runtime refuses to load.
        api_key_env = _read_api_key_env_name()
    else:
        base_url = provider["base_url"]
        api_key_env = provider["api_key_env"]
        print(f"  Base URL: {_safe_url(base_url)}")

    api_key = ""
    env_path = state_dir / ".env"
    key_needs_persist = False
    if api_key_env:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            try:
                api_key = getpass.getpass(f"API key ({api_key_env}): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                raise SystemExit(1)
            if not api_key:
                print("API key is required.")
                raise SystemExit(1)
            key_needs_persist = True

    _SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    provider_key = provider["provider_key"]
    if provider_key == "custom":
        provider_key = _read_input("Provider name for config", "custom")
        if not _SLUG_RE.match(provider_key):
            print(f"Invalid provider name: must match [a-zA-Z0-9][a-zA-Z0-9_-]*")
            raise SystemExit(1)

    if provider_key == "ollama-local":
        model = _pick_ollama_model(provider["default_model"])
    else:
        model = _read_input("Model name", provider["default_model"])
    if not model:
        print("Model name is required.")
        raise SystemExit(1)

    url_err = validate_base_url(base_url)
    if url_err is not None:
        print(f"\nInvalid base URL: {url_err}")
        raise SystemExit(1)

    print(f"\nTesting connection to {_safe_url(base_url)}...")
    if not _test_connection(base_url, api_key, model):
        print("\nConnection test failed. Check your settings and try again.")
        raise SystemExit(1)
    print("  Connection successful.")

    if key_needs_persist:
        _append_env_var(env_path, api_key_env, api_key)
        os.environ[api_key_env] = api_key
        print(f"  Saved to {env_path}")

    print()
    preset_cheap = provider.get("cheap_model", "")
    if preset_cheap:
        # The preset pairs a distinct cheap-slot model with the main one
        # (Ollama Cloud suggests kimi-k3 for main, kimi-k2.6 for cheap).
        cheap_model = _read_input("Cheap model name", preset_cheap)
        vision_model = _read_input("Vision model name", model)
    else:
        reuse = _read_input("Use the same model for cheap and vision slots? [Y/n]", "y")
        if reuse.lower() in ("n", "no"):
            cheap_model = _read_input("Cheap model name", model)
            vision_model = _read_input("Vision model name", model)
        else:
            cheap_model = model
            vision_model = model

    print()
    context_windows = _resolve_context_windows(
        base_url, api_key, [model, cheap_model, vision_model],
    )

    config_path = state_dir / "config.json"
    _update_config_models(
        config_path,
        provider_key=provider_key,
        base_url=base_url,
        model=model,
        cheap_model=cheap_model,
        vision_model=vision_model,
        api_key_env=api_key_env,
        context_windows=context_windows,
    )
    print(f"\n  Config written to {config_path}")
    print(
        '\nProvider configured. Run "faff chat" to test, '
        'or "faff setup telegram" to add Telegram.'
    )
