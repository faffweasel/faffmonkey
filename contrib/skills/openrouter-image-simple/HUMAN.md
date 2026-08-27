# openrouter-image-simple: setup and configuration

Image generation, editing, and vision analysis through OpenRouter. Stdlib
only, no pip packages.

## Setup

1. Get an API key at openrouter.ai/settings/keys and add to `state/.env`:

   ```
   OPENROUTER_API_KEY=sk-or-v1-...
   ```

2. Verify: ask the agent to run `generate --check`, which lists available
   image models and validates the configured default.

## As the image provider for other skills

To make this the image backend for skills that use the command seam
(dreaming, weekly-state-of-me, ...), add to `state/commands.json`:

```json
{
  "IMAGE_GEN_CMD": "python3 skills/openrouter-image-simple/scripts/generate.py",
  "IMAGE_EDIT_CMD": "python3 skills/openrouter-image-simple/scripts/generate.py"
}
```

Paths are relative to `workspace/`, which is the working directory both
skill scripts and `shell_exec` run in.

`generate.py` accepts `--prompt/--output/--input`, so the same script serves
both generation and editing.

## Configuration (optional)

The models live in `skills-data/openrouter-image-simple/config.json`:

```json
{
  "generation": {
    "model": "google/gemini-3.1-flash-image",
    "aliases": { "nano-banana-2": "google/gemini-3.1-flash-image" }
  },
  "vision": { "model": "google/gemini-3.6-flash" }
}
```

That file is created on first run by copying the skill's
`seed/config.json` (currently Gemini 3.1 Flash Image for generation,
Gemini 3.6 Flash for vision), and is the only config the scripts read
after that. Edit it, not the seed: the seed is never consulted again,
and changing anything in the installed skill directory only makes
`faff update` report the skill as modified. No model id lives in code.
Aliases let you say `--model gemini-pro` instead of full IDs.

## Output

Every saved image appends a `{time, file, model, prompt}` line to
`prompts.jsonl` beside it, matching venice-ai-media, so a flat image
folder keeps its prompts on record.
