"""Shared utilities for Venice AI scripts."""

import base64
import datetime as dt
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))

USER_AGENT = "faffmonkey/0.1.0"
API_BASE = "https://api.venice.ai/api/v1"


def require_api_key() -> str:
    """Get the VENICE_API_KEY environment variable, or exit with error."""
    api_key = os.environ.get("VENICE_API_KEY", "").strip()
    if not api_key:
        print("Error: VENICE_API_KEY not found in environment", file=sys.stderr)
        print("Set VENICE_API_KEY in state/.env.", file=sys.stderr)
        print("Get your API key at: https://venice.ai/settings/api", file=sys.stderr)
        sys.exit(2)
    return api_key


def make_request(
    endpoint: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 120,
) -> bytes:
    """Make HTTP request to Venice API with proper headers."""
    url = f"{API_BASE}/{endpoint.lstrip('/')}"

    req_headers = {
        "User-Agent": USER_AGENT,
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, method=method, headers=req_headers, data=data)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Venice API error ({e.code}): {error_body}") from e
    except (urllib.error.URLError, OSError) as e:
        # Every caller catches RuntimeError and prints a clean line. DNS
        # failures, refused connections and timeouts are not HTTPError, so
        # they need wrapping too.
        raise RuntimeError(f"Venice API unreachable: {e}") from e


def api_json(
    endpoint: str,
    method: str = "GET",
    payload: dict | None = None,
    api_key: str | None = None,
    timeout: int = 120,
) -> dict:
    """Make JSON request to Venice API."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    if payload:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None

    resp = make_request(endpoint, method, data, headers, timeout)
    return json.loads(resp.decode("utf-8"))


def list_models(api_key: str, model_type: str = "image") -> list[dict]:
    """Fetch available models from Venice API."""
    data = api_json(f"/models?type={model_type}", api_key=api_key, timeout=30)
    return data.get("data", [])


def print_models(models: list[dict]) -> None:
    """Print models in a formatted table with detailed info."""
    if not models:
        print("No models found.")
        return

    print(f"\n{'Model ID':<35} {'Status':<12} {'Info'}")
    print("-" * 100)
    for m in models:
        model_id = m.get("id", "unknown")
        model_type = m.get("type", "")
        spec = m.get("model_spec", {})

        # Determine status from model_spec fields
        if spec.get("offline", False):
            status = "offline"
        elif spec.get("beta", False):
            status = "beta"
        else:
            status = "available"

        # Check for deprecation warning
        deprecation = spec.get("deprecation", {})
        if deprecation.get("date"):
            status = "deprecated"

        # Build info string based on model type
        info_parts = []

        # Description
        desc = spec.get("description", "")
        if desc:
            info_parts.append(desc[:40])

        # Model-specific details
        constraints = spec.get("constraints", {})

        if model_type == "image":
            resolutions = constraints.get("resolutions", [])
            if resolutions:
                info_parts.append(f"res: {'/'.join(resolutions)}")

        elif model_type == "video":
            durations = constraints.get("durations", [])
            video_type = constraints.get("model_type", "")
            if video_type:
                info_parts.append(video_type)
            if durations:
                info_parts.append(f"dur: {'/'.join(durations)}")

        # Deprecation notice
        if deprecation.get("date"):
            info_parts.insert(0, f"[EOL {deprecation['date'][:10]}]")

        info = " | ".join(info_parts) if info_parts else ""
        print(f"{model_id:<35} {status:<12} {info[:55]}")

    print()
    print(f"Total: {len(models)} models")


def validate_model(api_key: str, model: str, model_type: str = "image") -> tuple[bool, list[str]]:
    """Check if model exists and is available. Returns (exists, list_of_available_models)."""
    try:
        models = list_models(api_key, model_type)
        available = [
            m.get("id") for m in models
            if not m.get("model_spec", {}).get("offline", False)
        ]
        return model in available, available
    except RuntimeError:
        return True, []


def print_media_line(filepath: Path) -> None:
    """Print MEDIA: line for agent auto-attach."""
    print(f"\nMEDIA: {filepath.as_posix()}")


def get_mime_type(filename: str) -> str:
    """Get MIME type from filename."""
    suffix = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }.get(suffix, "application/octet-stream")


def detect_image_ext(data: bytes) -> str:
    """Detect image format from magic bytes. Returns extension including dot."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] in (b"GIF8",):
        return ".gif"
    if data[:2] in (b"BM",):
        return ".bmp"
    return ".png"  # safe fallback


def default_out_dir(prefix: str = "venice") -> Path:
    """Stable per-command folder under workspace/shared/media/.

    A flat folder with timestamped filenames matches openrouter-image-simple
    and stays browsable; a folder per run scatters images and gallery files.
    """
    base = Path(WORKSPACE) / "shared" / "media"
    base.mkdir(parents=True, exist_ok=True)
    return base / prefix


def append_prompt_log(out_path: Path, prompt: str, model: str) -> None:
    """Append {time, file, model, prompt} to prompts.jsonl beside the image,
    so a flat folder keeps its prompts on record without per-run galleries."""
    entry = {
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "file": out_path.name,
        "model": model,
        "prompt": prompt,
    }
    try:
        with open(out_path.parent / "prompts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"warning: could not record prompt: {e}", file=sys.stderr)


def file_to_data_url(filepath: Path) -> str:
    """Convert local file to data URL."""
    suffix = filepath.suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }
    mime = mime_types.get(suffix, "application/octet-stream")
    data = filepath.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


SKILL_NAME = "venice-ai-media"


def _config_file() -> str:
    """Always this skill's own data dir, resolved from WORKSPACE.

    SKILL_DATA must not be consulted: when a script here runs as another
    skill's subprocess (selfie's IMAGE_EDIT_CMD), it inherits the CALLER's
    SKILL_DATA and would read that skill's config instead of this one's.
    Read from the environment on every call: this module is shared, so
    baking the path in at import time would pin it to whichever script
    imported first.
    """
    workspace = os.environ.get("WORKSPACE", "")
    if not workspace:
        workspace = os.path.dirname(os.path.dirname(SKILL_DIR))
    return os.path.join(workspace, "skills-data", SKILL_NAME, "config.json")


def load_config() -> dict:
    """skills-data/venice-ai-media/config.json, copied from the skill's
    seed/config.json on first run and the only config read after that.

    The seed is data, not code, and is never consulted again: editing the
    installed skill directory changes nothing except the skill's install
    hash. No model id lives in code.
    """
    path = _config_file()
    if not os.path.isfile(path):
        seed = Path(SKILL_DIR) / "seed" / "config.json"
        if seed.is_file():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(seed, path)
            print(f"seeded {path}", file=sys.stderr)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        # Silence here once hid a real config behind a hardcoded
        # fallback model; say what happened.
        print(f"warning: {path} unreadable ({e}); no model configured", file=sys.stderr)
        return {}
