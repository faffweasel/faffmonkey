"""Shared utilities for openrouter-image-simple scripts."""

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
SKILL_NAME = os.path.basename(SKILL_DIR)

DEFAULT_API_URL = "https://openrouter.ai/api/v1/chat/completions"


def config_file() -> str:
    """Locate config.json. Read from the environment on every call: this
    module is shared, so baking the path in at import time would pin it to
    whichever script imported first."""
    workspace = os.environ.get("WORKSPACE", "")
    if not workspace:
        workspace = os.path.dirname(os.path.dirname(SKILL_DIR))
    # Never SKILL_DATA: as another skill's subprocess (IMAGE_GEN_CMD) this
    # inherits the caller's SKILL_DATA and would read the wrong skill's
    # config.
    return os.path.join(workspace, "skills-data", SKILL_NAME, "config.json")


def _seed_config() -> dict:
    """Shipped defaults as data: the skill directory's config.json."""
    seed_path = os.path.join(SKILL_DIR, "config.json")
    try:
        with open(seed_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_config() -> dict:
    merged = _seed_config()
    path = config_file()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                for key, value in user.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key] = {**merged[key], **value}
                    else:
                        merged[key] = value
            return merged
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"warning: {path} unreadable ({e}); using seed defaults",
                file=sys.stderr,
            )
    return merged


def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("Error: OPENROUTER_API_KEY not found in environment", file=sys.stderr)
        print("Set it in state/.env.", file=sys.stderr)
        sys.exit(1)
    return key


def detect_mime_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def load_image_as_base64(path: str) -> str:
    mime_type = detect_mime_type(path)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime_type};base64,{b64}"


def post_chat(
    api_url: str,
    payload: dict,
    api_key: str,
    model: str,
    check_hint: str,
    timeout: int = 180,
) -> dict:
    """POST to the OpenRouter chat endpoint, exiting with guidance on error.

    check_hint is the caller's wording for how to run its own --check.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(api_url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"HTTP Error {e.code}: {error_body}", file=sys.stderr)
        if e.code == 404:
            print(f"\nModel '{model}' not found on OpenRouter.", file=sys.stderr)
            print(check_hint, file=sys.stderr)
            print("Note: OpenRouter returns 404 (not 401) when OPENROUTER_API_KEY is missing or invalid.", file=sys.stderr)
        elif e.code == 402:
            print("\nInsufficient credits: https://openrouter.ai/credits", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URL Error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def append_prompt_log(out_path: str, prompt: str, model: str) -> None:
    """Append {time, file, model, prompt} to prompts.jsonl beside the image,
    matching venice-ai-media, so a flat folder keeps its prompts on record."""
    path = os.path.abspath(out_path)
    entry = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "file": os.path.basename(path),
        "model": model,
        "prompt": prompt,
    }
    try:
        with open(os.path.join(os.path.dirname(path), "prompts.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"warning: could not record prompt: {e}", file=sys.stderr)
