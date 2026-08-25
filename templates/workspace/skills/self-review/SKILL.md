---
name: self-review
description: Review and consolidate LEARNINGS.md when it's getting long or noisy. Use periodically (every few weeks, or when entries exceed ~30), after a burst of corrections, or before a major task where stale learnings could mislead. Also use to log structured entries when a command fails, the user corrects you, or you discover outdated knowledge.
actions: add, review, promote
---

## When to use

Log with `add` immediately when:

- The user corrects you ("no, that's wrong", "actually...", "that's outdated")
- A command or operation fails unexpectedly, or an external API or tool breaks
- You discover your knowledge is outdated or incorrect
- A better approach is found for a recurring task
- The user says "log this", "learn this", "remember this mistake"

Run `review` when LEARNINGS.md exceeds ~30 entries, after a burst of corrections, or before a major task where stale learnings could mislead. Run `promote` when review identifies a recurring pattern (3+ similar entries) or a learning has proven broadly applicable.

## Actions

**add**, append a structured entry to `LEARNINGS.md` with a generated sequential ID (TAG-YYYYMMDD-NNN). Always use this instead of editing the file by hand; it enforces the entry format (see `references/TEMPLATES.md`):

- `--tag TAG` (required): `LRN` (learning), `ERR` (error), `FEAT` (feature gap)
- `--summary "text"` (required): one line
- `--details "text"`: full context
- `--priority LEVEL`: `low`, `medium`, `high`, `critical` (default `medium`)
- `--area AREA`: scope tag, e.g. `backend`, `infra`, `tests`, `docs`

```
add --tag LRN --summary "stdlib-only means no requests library, use urllib.request" --priority high --area backend
add --tag ERR --summary "Docker build fails on ARM64" --details "FROM python:3.11-slim needs --platform=linux/amd64 on Apple Silicon" --area infra
```

**review**, analyse LEARNINGS.md against AGENTS.md and MEMORY.md. Read-only; outputs recommendations:

- Entries already reflected in AGENTS.md or MEMORY.md (safe to remove)
- Near-duplicate entries (consider merging)
- Groups of 3+ similar entries (promotion candidates)
- Stale pending entries older than 30 days (verify or remove)

```
review
```

**promote**, extract an entry's summary into AGENTS.md or MEMORY.md as a standing rule or fact, and mark the original as promoted:

- `--id ID` (required): entry ID, e.g. `LRN-20260412-003`
- `--target TARGET` (required): `agents` or `memory`

```
promote --id LRN-20260412-003 --target agents
```

## Interpreting review output

- Act on recommendations yourself: review changes nothing. Verify a summary reads as a good standalone rule before promoting it, and edit the promoted line in the target file afterwards if it needs refinement.
- Duplicate detection is keyword overlap, not semantic: entries phrased differently but meaning the same thing will not be grouped. Stale flagging is a fixed 30-day threshold; an old entry may still be valid.

self-review maintains your own knowledge base. memory-search finds things in memory files. skill-writer extracts proven learnings into reusable skills.
