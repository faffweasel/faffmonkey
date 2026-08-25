---
name: venice-ai-media
description: Generate, edit, and upscale images and create videos from images via Venice AI. Use when the user asks for image generation/editing/upscaling or image-to-video, or when another skill needs media via the command seam.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"],"env":["VENICE_API_KEY"]}}}'
actions: venice-image, venice-edit, venice-upscale, venice-video
timeout: 600
---

## When to use

- "Generate/draw an image of X" → `venice-image --prompt "X"`
- "Edit this image: ..." → `venice-edit --input <path> --prompt "instructions"`
- "Upscale/sharpen this image" → `venice-upscale <path> --scale 2` (add `--enhance` for detail enhancement)
- "Make a video from this image" → `venice-video --image <path> --prompt "motion description"`
- Other skills call these via IMAGE_GEN_CMD/IMAGE_EDIT_CMD; nothing for you to do

## Commands

```
venice-image --prompt "text" [--output path] [--width N --height N | --resolution 2K | --aspect-ratio 16:9]
             [--model ID] [--count N] [--style-preset X] [--negative-prompt "..."] [--seed N]
venice-edit  --input img.png --prompt "instructions" [--output path] [--aspect-ratio auto]
venice-upscale img.png [--scale 1-4] [--enhance --enhance-prompt "..."]
venice-video --image img.png --prompt "motion" [--duration 5s] [--resolution 720p] [--audio]
```

All commands print a `MEDIA:` line so the file attaches to your reply. Without `--output`, files land under `shared/media/`. `--list-models` on image/edit/video shows what the account can use.

## Behaviour

- Video generation is slow (minutes) and polls; warn the user it may take a while before starting, and only for explicit requests.
- Describe the result in a sentence when sending; do not narrate steps or dump parameters.
- On auth errors, check VENICE_API_KEY is configured (HUMAN.md); on model errors, run the relevant `--list-models` and retry with an available model.
