---
name: openrouter-image-simple
description: Generate, edit, and analyse images via OpenRouter. Stdlib only. Use when the user asks for an image to be created, an existing image modified, or a picture described/analysed.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"],"env":["OPENROUTER_API_KEY"]}}}'
actions: generate, analyze
timeout: 600
---

## When to use

- "Draw/generate/make me a picture of X" → `generate --prompt "X"`
- "Edit this image / make it sunset" → `generate --prompt "instructions" --output <path> --input <source image>`
- "What's in this image?" → `analyze <image path> "question"`
- Another skill needs an image → it calls this via IMAGE_GEN_CMD; nothing for you to do

## Commands

```
generate --prompt "text"                                 text-to-image, saved to shared/media/openrouter/<date>-<time>.png
generate --prompt "text" --output path.png               text-to-image to a path the user named
generate --prompt "text" --input in.png                  image editing
generate ... --model gemini-pro                         alias or full model ID
generate --check                                        account and model diagnostics
analyze image.png "Describe what you see"               vision analysis
```

Leave `--output` off unless the user named a file: the skill dates the file and keeps it with the other generated media. The `MEDIA:` line attaches the file to your reply automatically. Describe the image in a sentence when sending; do not narrate the generation process.

## Failure handling

- 404 usually means the API key is missing or invalid (OpenRouter's quirk), not that the model is gone; suggest `generate --check`.
- 402 means insufficient credits; tell the user, link openrouter.ai/credits.
- If a named model fails, retry once with the default model before reporting failure.
