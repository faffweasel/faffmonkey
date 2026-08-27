#!/usr/bin/env python3
"""Generate images via Venice AI Image API."""

import argparse
import base64
import datetime as dt
import random
import re
import sys
from pathlib import Path

# Import shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from venice_common import (
    load_config,
    append_prompt_log,
    api_json,
    require_api_key,
    list_models,
    print_models,
    validate_model,
    print_media_line,
    default_out_dir,
)

DEFAULT_MODEL = load_config().get("image", {}).get("model", "")


def slugify(text: str, max_len: int = 40) -> str:
    """Convert text to URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text or "image")[:max_len]


def pick_prompts(count: int) -> list[str]:
    """Generate random creative prompts."""
    subjects = [
        "a Venetian canal at golden hour",
        "a cyberpunk market street",
        "a minimalist sculpture",
        "an ancient library interior",
        "a bioluminescent forest",
        "a steampunk airship",
        "a serene Japanese garden",
    ]
    styles = [
        "cinematic photography",
        "oil painting style",
        "architectural visualization",
        "editorial photo",
        "concept art",
        "hyperrealistic render",
        "impressionist painting",
    ]
    moods = [
        "dramatic lighting",
        "soft morning light",
        "neon glow",
        "foggy atmosphere",
        "warm sunset tones",
        "cool blue hour",
    ]
    return [
        f"{random.choice(styles)} of {random.choice(subjects)}, {random.choice(moods)}"
        for _ in range(count)
    ]


def list_styles(api_key: str) -> list[str]:
    """Fetch available image styles from Venice API."""
    data = api_json("/image/styles", api_key=api_key, timeout=30)
    return data.get("data", [])


def generate_image(
    api_key: str,
    prompt: str,
    model: str,
    width: int | None,
    height: int | None,
    fmt: str,
    cfg_scale: float | None,
    seed: int | None,
    negative_prompt: str | None,
    style_preset: str | None,
    resolution: str | None,
    aspect_ratio: str | None,
    safe_mode: bool,
    hide_watermark: bool,
    variants: int = 1,
    embed_exif: bool = False,
    lora_strength: int | None = None,
    enable_web_search: bool = False,
    steps: int | None = None,
) -> dict:
    """Call Venice Image Generate API.
    
    Args:
        variants: Number of images to generate (1-4). More efficient than multiple calls.
        embed_exif: Embed prompt generation info in EXIF metadata.
        lora_strength: LoRA strength 0-100 for applicable models.
        enable_web_search: Enable web search for image generation.
        steps: Number of inference steps (model-dependent).
    """
    # Sizing is model-specific: SD-era models take width/height, newer
    # ones (qwen-image-3, nano-banana-2) take aspect_ratio or resolution and
    # reject width/height. Send only what the caller asked for; with no
    # sizing flags at all the model's own default applies.
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "format": fmt,
        "safe_mode": safe_mode,
        "hide_watermark": hide_watermark,
    }
    if width is not None:
        payload["width"] = width
    if height is not None:
        payload["height"] = height

    # Use variants for efficient batch generation (API supports 1-4)
    if variants > 1:
        payload["variants"] = min(variants, 4)

    if cfg_scale is not None:
        payload["cfg_scale"] = cfg_scale
    if seed is not None:
        payload["seed"] = seed
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if style_preset:
        payload["style_preset"] = style_preset
    if resolution:
        payload["resolution"] = resolution
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if embed_exif:
        payload["embed_exif_metadata"] = True
    if lora_strength is not None:
        payload["lora_strength"] = lora_strength
    if enable_web_search:
        payload["enable_web_search"] = True
    if steps is not None:
        payload["steps"] = steps

    return api_json("/image/generate", "POST", payload, api_key, timeout=120)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate images via Venice AI API.")
    ap.add_argument("--prompt", help="Image description. If omitted, generates random prompts.")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model ID (default: {DEFAULT_MODEL or 'none configured'})")
    ap.add_argument("--count", type=int, default=1, help="Number of images to generate (default: 1)")
    ap.add_argument("--width", type=int, help="Image width in px (width/height models only; omit to use the model default)")
    ap.add_argument("--height", type=int, help="Image height in px (width/height models only; omit to use the model default)")
    ap.add_argument("--format", dest="fmt", default="webp", choices=["jpeg", "png", "webp"], help="Output format (default: webp)")
    ap.add_argument("--cfg-scale", type=float, help="Prompt adherence 0-20 (default: 7.5)")
    ap.add_argument("--seed", type=int, help="Random seed for reproducibility")
    ap.add_argument("--negative-prompt", help="What to exclude from the image")
    ap.add_argument("--style-preset", help="Visual style preset")
    ap.add_argument("--resolution", help="Resolution preset (1K, 2K, 4K)")
    ap.add_argument("--aspect-ratio", help="Aspect ratio (1:1, 16:9, etc.)")
    ap.add_argument("--safe-mode", action="store_true", default=False, help="Blur adult content (default: false)")
    ap.add_argument("--no-safe-mode", action="store_false", dest="safe_mode", help="Disable safe mode")
    ap.add_argument("--hide-watermark", action="store_true", help="Remove Venice watermark")
    ap.add_argument("--embed-exif", action="store_true", help="Embed prompt info in image EXIF metadata")
    ap.add_argument("--lora-strength", type=int, help="LoRA strength 0-100 for applicable models")
    ap.add_argument("--enable-web-search", action="store_true", help="Enable web search for image generation")
    ap.add_argument("--steps", type=int, help="Inference steps (model-dependent)")
    ap.add_argument("--out-dir", help="Output directory (default: auto-generated)")
    ap.add_argument("--output", "-o", help="Output file path (single image mode — overrides --out-dir, --count, --format)")
    ap.add_argument("--list-models", action="store_true", help="List available image models and exit")
    ap.add_argument("--list-styles", action="store_true", help="List available style presets and exit")
    ap.add_argument("--no-validate", action="store_true", help="Skip model validation")
    args = ap.parse_args()

    if not args.model:
        print(
            "Error: no model configured. Set it in "
            "skills-data/venice-ai-media/config.json, or pass --model.",
            file=sys.stderr,
        )
        return 2
    api_key = require_api_key()

    # Handle --list-models
    if args.list_models:
        try:
            models = list_models(api_key, "image")
            print_models(models)
            return 0
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Handle --list-styles
    if args.list_styles:
        try:
            styles = list_styles(api_key)
            print("\nAvailable Image Styles:")
            print("-" * 40)
            for style in styles:
                print(f"  {style}")
            print(f"\nTotal: {len(styles)} styles")
            print("\nUsage: --style-preset \"Style Name\"")
            return 0
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Validate model if not skipped
    if not args.no_validate:
        exists, available = validate_model(api_key, args.model, "image")
        if not exists and available:
            print(f"Error: Model '{args.model}' not found or unavailable.", file=sys.stderr)
            print(f"Available image models: {', '.join(available)}", file=sys.stderr)
            return 2

    # --- Single-file output mode (IMAGE_GEN_CMD compatible) ---
    if args.output:
        if not args.prompt:
            print("Error: --prompt is required with --output", file=sys.stderr)
            return 2

        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Infer format from extension
        ext = out_path.suffix.lower().lstrip(".")
        fmt = ext if ext in ("jpeg", "jpg", "png", "webp") else "webp"
        if fmt == "jpg":
            fmt = "jpeg"

        print(f"Generating: {args.prompt[:60]}{'...' if len(args.prompt) > 60 else ''}")

        try:
            res = generate_image(
                api_key=api_key,
                prompt=args.prompt,
                model=args.model,
                width=args.width,
                height=args.height,
                fmt=fmt,
                cfg_scale=args.cfg_scale,
                seed=args.seed,
                negative_prompt=args.negative_prompt,
                style_preset=args.style_preset,
                resolution=args.resolution,
                aspect_ratio=args.aspect_ratio,
                safe_mode=args.safe_mode,
                hide_watermark=args.hide_watermark,
                embed_exif=args.embed_exif,
                lora_strength=args.lora_strength,
                enable_web_search=args.enable_web_search,
                steps=args.steps,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        images = res.get("images", [])
        if not images:
            print("Error: No images returned", file=sys.stderr)
            return 1

        out_path.write_bytes(base64.b64decode(images[0]))
        print(f"Saved: {out_path.as_posix()}")
        append_prompt_log(out_path, args.prompt, args.model)
        print_media_line(out_path)
        return 0

    # --- Batch mode (original behaviour) ---
    auto_out_dir = args.out_dir is None
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else default_out_dir("venice-image")
    out_dir.mkdir(parents=True, exist_ok=True)
    run_ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    items: list[dict] = []
    image_counter = 0
    
    # If using same prompt for multiple images, use variants for efficiency (up to 4 per request)
    if args.prompt:
        remaining = args.count
        batch_num = 0
        
        while remaining > 0:
            batch_num += 1
            variants = min(remaining, 4)  # API max is 4 variants per request
            
            print(f"[Batch {batch_num}] Generating {variants} image(s): {args.prompt[:60]}{'...' if len(args.prompt) > 60 else ''}")
            
            try:
                res = generate_image(
                    api_key=api_key,
                    prompt=args.prompt,
                    model=args.model,
                    width=args.width,
                    height=args.height,
                    fmt=args.fmt,
                    cfg_scale=args.cfg_scale,
                    seed=args.seed,
                    negative_prompt=args.negative_prompt,
                    style_preset=args.style_preset,
                    resolution=args.resolution,
                    aspect_ratio=args.aspect_ratio,
                    safe_mode=args.safe_mode,
                    hide_watermark=args.hide_watermark,
                    variants=variants,
                    embed_exif=args.embed_exif,
                    lora_strength=args.lora_strength,
                    enable_web_search=args.enable_web_search,
                    steps=args.steps,
                )
            except RuntimeError as e:
                print(f"  Error: {e}", file=sys.stderr)
                remaining -= variants
                continue

            images = res.get("images", [])
            if not images:
                print(f"  Warning: No images returned", file=sys.stderr)
                remaining -= variants
                continue

            for img_idx, image_b64 in enumerate(images):
                image_counter += 1
                filename = f"{run_ts}-{image_counter:03d}-{slugify(args.prompt)}.{args.fmt}"
                filepath = out_dir / filename

                try:
                    filepath.write_bytes(base64.b64decode(image_b64))
                    print(f"  Saved: {filename}")
                    items.append({"prompt": args.prompt, "file": filename})
                    append_prompt_log(filepath, args.prompt, args.model)
                except Exception as e:
                    print(f"  Error saving image: {e}", file=sys.stderr)
            
            remaining -= variants
    else:
        # Different prompts for each image - can't use variants
        prompts = pick_prompts(args.count)
        
        for idx, prompt in enumerate(prompts, start=1):
            print(f"[{idx}/{len(prompts)}] {prompt[:80]}{'...' if len(prompt) > 80 else ''}")

            try:
                res = generate_image(
                    api_key=api_key,
                    prompt=prompt,
                    model=args.model,
                    width=args.width,
                    height=args.height,
                    fmt=args.fmt,
                    cfg_scale=args.cfg_scale,
                    seed=args.seed,
                    negative_prompt=args.negative_prompt,
                    style_preset=args.style_preset,
                    resolution=args.resolution,
                    aspect_ratio=args.aspect_ratio,
                    safe_mode=args.safe_mode,
                    hide_watermark=args.hide_watermark,
                    embed_exif=args.embed_exif,
                    lora_strength=args.lora_strength,
                    enable_web_search=args.enable_web_search,
                    steps=args.steps,
                )
            except RuntimeError as e:
                print(f"  Error: {e}", file=sys.stderr)
                continue

            images = res.get("images", [])
            if not images:
                print(f"  Warning: No images returned", file=sys.stderr)
                continue

            for img_idx, image_b64 in enumerate(images):
                image_counter += 1
                suffix = f"-{img_idx + 1}" if len(images) > 1 else ""
                filename = f"{run_ts}-{image_counter:03d}{suffix}-{slugify(prompt)}.{args.fmt}"
                filepath = out_dir / filename

                try:
                    filepath.write_bytes(base64.b64decode(image_b64))
                    print(f"  Saved: {filename}")
                    items.append({"prompt": prompt, "file": filename})
                    append_prompt_log(filepath, prompt, args.model)
                except Exception as e:
                    print(f"  Error saving image: {e}", file=sys.stderr)

    if items:
        # MEDIA line attaches the first image to the agent's reply
        first_file = out_dir / items[0]["file"]
        print_media_line(first_file)

    if not items and auto_out_dir:
        # Remove the folder a failed run would otherwise leave empty. rmdir
        # refuses a non-empty directory, so this only ever removes debris.
        try:
            out_dir.rmdir()
        except OSError:
            pass
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
