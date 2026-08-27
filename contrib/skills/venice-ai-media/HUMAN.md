# venice-ai-media: setup and configuration

Image generation, editing, upscaling, and image-to-video via Venice AI.
Stdlib only, no pip packages.

## Setup

1. Get an API key at venice.ai/settings/api and add to `state/.env`:

   ```
   VENICE_API_KEY=...
   ```

2. Verify by asking the agent to generate a test image.

## As the media provider for other skills

To make Venice the image backend for command-seam consumers (dreaming,
weekly-state-of-me, selfie, ...), add to `state/commands.json`:

```json
{
  "IMAGE_GEN_CMD": "python3 skills/venice-ai-media/scripts/venice-image.py",
  "IMAGE_EDIT_CMD": "python3 skills/venice-ai-media/scripts/venice-edit.py"
}
```

Both accept the seam contract (`--prompt/--output`, edit adds `--input`) and
print `MEDIA:` lines.

## Configuration (optional)

The models live in `skills-data/venice-ai-media/config.json` (same
pattern as openrouter-image-simple):

```json
{
  "image": { "model": "qwen-image-3" },
  "edit": { "model": "firered-image-edit-1.1" },
  "video": { "model": "wan-3-0-image-to-video" }
}
```

That file is created on first run by copying the skill's
`seed/config.json`, and is the only config the scripts read after that.
Edit it, not the seed: the seed is never consulted again, and changing
anything in the installed skill directory only makes `faff update`
report the skill as modified. No model id lives in code. `--model` on
any script overrides per call. Venice's catalogue moves quickly and
varies by account tier, so run `--list-models` on venice-image,
venice-edit, or venice-video and pin what your tier actually offers.

## Notes

- Default output directory is a flat `workspace/shared/media/<command>/`
  (filenames are timestamped). Every saved image appends a
  `{time, file, model, prompt}` line to `prompts.jsonl` beside it, so
  the folder stays browsable with its prompts on record.
- Video generation costs more and takes minutes; check Venice pricing.
