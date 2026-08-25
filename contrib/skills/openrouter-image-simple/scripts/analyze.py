#!/usr/bin/env python3
"""
Image Analysis (Vision) via OpenRouter - Pure Python stdlib.
Analyze/understand images using vision-capable models.

Usage:
    python3 analyze.py image.png "Describe what's in this image"
    python3 analyze.py image.png "Who is this person?" --model google/gemini-3.6-flash

Requires OPENROUTER_API_KEY environment variable.
Vision model configured in skills-data/openrouter-image-simple/config.json.
"""

import os
import sys
import json
from pathlib import Path

# Import shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from openrouter_common import (
    DEFAULT_API_URL,
    get_api_key,
    load_config,
    load_image_as_base64,
    post_chat,
)

_config = load_config()
DEFAULT_VISION_MODEL = _config.get("vision", {}).get("model", "")
API_URL = _config.get("apiUrl", DEFAULT_API_URL)


def analyze_image(image_path: str, prompt: str, model: str | None = None) -> str:
    api_key = get_api_key()
    model = model or DEFAULT_VISION_MODEL

    if not os.path.exists(image_path):
        print(f"Error: Image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    data_url = load_image_as_base64(image_path)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }

    result = post_chat(
        API_URL, payload, api_key, model,
        "Run generate.py --check to verify account access.",
    )

    try:
        choices = result.get("choices", [])
        if not choices:
            print("Error: No choices in response", file=sys.stderr)
            print(json.dumps(result, indent=2), file=sys.stderr)
            sys.exit(1)

        content = choices[0].get("message", {}).get("content", "")
        print(content)
        return content

    except (KeyError, IndexError) as e:
        print(f"Error parsing response: {e}", file=sys.stderr)
        print(json.dumps(result, indent=2), file=sys.stderr)
        sys.exit(1)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze images via OpenRouter vision models (pure stdlib, no dependencies)"
    )
    parser.add_argument("image", help="Path to image file to analyze")
    parser.add_argument("prompt", help="What to ask about the image")
    parser.add_argument(
        "--model", "-m",
        default=DEFAULT_VISION_MODEL,
        help=f"OpenRouter vision model ID (default: {DEFAULT_VISION_MODEL})",
    )

    args = parser.parse_args()
    analyze_image(args.image, args.prompt, args.model)


if __name__ == "__main__":
    main()
