#!/usr/bin/env python3
"""
Image Generation via OpenRouter - Pure Python stdlib, no dependencies.
Routes through OpenRouter for spend control via credit balance.

Usage:
    python3 generate.py "prompt" output.png
    python3 generate.py "edit instructions" output.png --input original.png
    python3 generate.py "prompt" output.png --model gemini-pro
    python3 generate.py --check

Requires OPENROUTER_API_KEY environment variable.
Model and aliases configured in skills-data/openrouter-image-simple/config.json.
"""

import datetime as dt
import os
import sys
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

# Import shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from openrouter_common import (
    DEFAULT_API_URL,
    get_api_key,
    load_config,
    append_prompt_log,
    load_image_as_base64,
    post_chat,
)

_FALLBACK_ALIASES = {
    "gemini": "google/gemini-2.5-flash-image",
    "gemini-3.1": "google/gemini-3.1-flash-image-preview",
    "gemini-pro": "google/gemini-3-pro-image-preview",
    "sourceful": "sourceful/riverflow-v2-fast",
    "sourceful-pro": "sourceful/riverflow-v2-pro",
    "seedream": "bytedance-seed/seedream-4.5",
    "flux": "sourceful/riverflow-v2-fast",
}


_config = load_config()
_gen_cfg = _config.get("generation", {})
DEFAULT_MODEL = _gen_cfg.get("model", "")
ALIASES = _gen_cfg.get("aliases", _FALLBACK_ALIASES)
API_URL = _config.get("apiUrl", DEFAULT_API_URL)
MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=image"


def resolve_model(model: str) -> str:
    return ALIASES.get(model, model)


def check_account() -> None:
    """Query available image models and verify account access."""
    api_key = get_api_key()
    req = urllib.request.Request(
        MODELS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} checking account: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)

    models = [m.get("id") for m in data.get("data", [])]
    print(f"Account OK — {len(models)} image model(s) available\n")

    default_ok = DEFAULT_MODEL in models
    status = "✓" if default_ok else "✗ NOT FOUND"
    print(f"Default model: {DEFAULT_MODEL} — {status}")

    print("\nAll available image models:")
    for m in sorted(models):
        print(f"  {m}")

    print("\nConfigured aliases:")
    for alias, target in sorted(ALIASES.items()):
        found = "✓" if target in models else "✗ not available"
        print(f"  {alias:15} → {target}  [{found}]")

    if not default_ok:
        print(f"\n! Default model '{DEFAULT_MODEL}' not found.", file=sys.stderr)
        print("Update generation.model in skills-data/openrouter-image-simple/config.json", file=sys.stderr)
        sys.exit(1)


def generate_image(prompt: str, output_path: str, input_image_path: str | None = None, model: str | None = None) -> str:
    api_key = get_api_key()
    model = resolve_model(model or DEFAULT_MODEL)

    if input_image_path:
        if not os.path.exists(input_image_path):
            print(f"Error: Input image not found: {input_image_path}", file=sys.stderr)
            sys.exit(1)
        data_url = load_image_as_base64(input_image_path)
        content = [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    # Gemini models need both modalities; image-only models use just "image"
    is_gemini = model.startswith("google/")
    modalities = ["image", "text"] if is_gemini else ["image"]

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "modalities": modalities,
    }

    result = post_chat(
        API_URL, payload, api_key, model,
        "Run --check to see available models and verify account access.",
    )

    try:
        choices = result.get("choices", [])
        if not choices:
            print("Error: No choices in response", file=sys.stderr)
            print(json.dumps(result, indent=2), file=sys.stderr)
            sys.exit(1)

        message = choices[0].get("message", {})
        images = message.get("images", [])

        if not images:
            print("Error: No images in response", file=sys.stderr)
            print(json.dumps(result, indent=2), file=sys.stderr)
            sys.exit(1)

        image_url = images[0].get("image_url", {}).get("url", "")
        if not image_url:
            print("Error: Empty image URL in response", file=sys.stderr)
            sys.exit(1)

        if ";base64," in image_url:
            b64_data = image_url.split(";base64,", 1)[1]
        else:
            b64_data = image_url

        img_data = base64.b64decode(b64_data)

        output_dir = Path(output_path).parent
        if output_dir and not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=True)

        with open(output_path, "wb") as f:
            f.write(img_data)

        print(f"Saved: {output_path}")
        append_prompt_log(output_path, prompt, model)
        print(f"\nMEDIA: {output_path}")
        return output_path

    except (KeyError, IndexError) as e:
        print(f"Error parsing response: {e}", file=sys.stderr)
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)


def default_output() -> str:
    """shared/media/openrouter/<date>-<time>.png under the workspace.

    Venice keeps flat per-command folders under shared/media/; this skill made the
    agent invent a path and it chose a bare shared/images/<name>.png, so
    nothing said when an image was made or kept the two skills together.
    """
    workspace = os.environ.get("WORKSPACE", "") or os.getcwd()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return os.path.join(workspace, "shared", "media", "openrouter", f"{stamp}.png")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate or edit images via OpenRouter (pure stdlib, no dependencies)"
    )
    parser.add_argument("prompt_pos", nargs="?", help="Image prompt (positional)")
    parser.add_argument("output_pos", nargs="?", help="Output file path (positional)")
    parser.add_argument("--prompt", "-p", dest="prompt_flag", help="Image prompt (named flag)")
    parser.add_argument("--output", "-o", dest="output_flag", help="Output file path (default: shared/media/openrouter/<date>-<time>.png)")
    parser.add_argument("--input", "-i", help="Input image for editing (optional)")
    parser.add_argument(
        "--model", "-m",
        default=DEFAULT_MODEL,
        help=f"Model ID or alias (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify account access and list available image models",
    )

    args = parser.parse_args()

    if args.check:
        check_account()
        return

    prompt = args.prompt_flag or args.prompt_pos
    output = args.output_flag or args.output_pos or default_output()

    if not prompt:
        parser.error("prompt is required (positional or --prompt)")

    generate_image(prompt, output, args.input, args.model)


if __name__ == "__main__":
    main()
