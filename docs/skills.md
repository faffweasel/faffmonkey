# Skills

## What a skill is

A skill is a directory in `workspace/skills/` containing a `SKILL.md` file with YAML frontmatter and an optional `scripts/` directory. The agent sees skill names and descriptions in every system prompt and decides when to invoke them. Scripts run as subprocesses, not in the agent's process.

Directory structure:

```
workspace/skills/<name>/
  SKILL.md              Required. Frontmatter + instructions.
  scripts/              Optional. Python scripts, one per action.
  references/           Optional. Docs loaded on demand via file_read.
  assets/               Optional. Templates and data files.

workspace/skills-data/<name>/
                        Persistent data (indexes, queues, caches).
                        Created automatically on first invocation.
```

There are three ways a skill gets run, and they all reach the same script:

- **The agent decides to.** `skill_invoke` is the tool the agent calls; you never type it. Saying "remind me on Friday" or "what's the weather" is enough.
- **You run it directly** with the `/skill` slash command, in Telegram, Discord or `faff chat`: `/skill <name>` shows the SKILL.md, `/skill <name> <action> [args]` runs the script with no model involved. Handy for checking what a skill actually returns.
- **Cron** runs a skill's `run.py` on a schedule with `session: "none"`. `run.py` is the skill's unattended entry point: it takes no arguments, keeps its settings and state in `SKILL_DATA`, and prints either `NO_REPLY` or a message. With `deliver: announce` the message goes straight to the channel and `NO_REPLY` sends nothing, so a skill can watch something (a threshold, a change, a due reminder) at zero token cost. Empty output is recorded as a failed run, so print `NO_REPLY` rather than nothing. The contrib `reminders` skill is the worked example.

With just a name and no action, the full SKILL.md comes back. With an action, the matching script in `scripts/` runs and its output comes back.

## Built-in skills

These ship in `templates/workspace/skills/` and are copied to `workspace/skills/` by `faff update` or at first boot (during bootstrap).

| Skill | Actions | Description |
|-------|---------|-------------|
| **carry-over** | add, list, get, done, clear | Queue something to tell the user in a future session. Items have priority levels (urgent, normal, curious, simmering) and are surfaced at next session start. |
| **document-search** | index, search | Full-text search (FTS5) over `workspace/documents/` (md, txt, csv, xlsx, pdf). |
| **cron-manager** | list, add, update, remove, disable, enable, history | Schedule, list, and manage recurring tasks and one-shot reminders. CRUD operations on `workspace/config/jobs.json`. |
| **heartbeat** | run, poke | The heartbeat's watchdog: health checks, then every trigger sensors dropped in `skills-data/heartbeat/triggers.d/` and the latest line of every `workspace/readings/*.jsonl`, written to `triggers.json`. The scheduler wakes the agent (an agent turn with tools) only when there is a trigger. `poke` drops a trigger by hand. |
| **memory-search** | index, search | Search across all memory files. FTS5 keyword search, optional vector semantic search, or hybrid (RRF merge). Index is incremental (SHA-256 content hashing), refreshed before every search and by the runtime after each memory flush and daily note. |
| **morning-routine** | prepare, stamp | Daily startup: read carry-over, check preconscious buffer, gather overnight cron data, compose a greeting. Triggered by cron, not invoked directly. |
| **preconscious** | add, read, run, drop_lowest | Track what should be top-of-mind for the next few days. Scored buffer of max 5 items with Currency (freshness) and Importance scores. Items decay daily and fade naturally. |
| **self-review** | add, review, promote | Review and consolidate LEARNINGS.md. Log structured entries (LRN/ERR/FEAT tags), find duplicates and promotion candidates, promote recurring patterns to AGENTS.md or MEMORY.md. |
| **skill-writer** | init_skill, quick_validate, package_skill | Create new skills from templates. Scaffolds directory structure, validates frontmatter, and packages skills for distribution. |

## Installation

Built-in skills are installed by `faff init` and kept current by `faff update`. Bootstrap also copies any missing skill directories at first boot as a fallback.

`faff init` and `faff update` record provenance for each installed built-in in `workspace/skills/.origin.json` (source path plus a content hash of what was installed), the same mechanism `faff skill install` uses for contrib skills. On each run, `faff update` reconciles built-ins against the templates:

- **Missing**: installed, with provenance recorded.
- **Unmodified since install, template changed**: replaced with the new template version. Your copy is byte-identical to what shipped, so this is always safe.
- **Modified locally**: never touched. If the template has also changed, update prints a warning; delete the skill's directory from `workspace/skills/` and re-run `faff update` to re-sync, losing your changes.
- **No provenance** (installed before provenance existed): adopted silently if identical to the current template, otherwise flagged as unverifiable and left alone.

```bash
faff update
```

To reset any built-in skill to its template version, delete its directory from `workspace/skills/` and run `faff update`.

## Contrib skills

Optional skills live in `contrib/skills/`. None are installed by default; install the ones you want, one at a time, from the host's terminal:

```bash
docker compose run --rm faffmonkey faff skill list             # installed and available
docker compose run --rm faffmonkey faff skill install weather
```

| Skill | What it does | Needs |
|---|---|---|
| **weather** | Current weather and 5-day forecast (OpenWeatherMap); reads your location from `config/location.json` | `OPENWEATHERMAP_API_KEY` |
| **aqi** | Air quality by station, with per-pollutant breakdown | `AQICN_API_KEY` |
| **calendar** | Read-only view of ICS files and subscription URLs (Google, Outlook, Proton export) | |
| **reminders** | Natural-language one-shot and recurring reminders, delivered via cron | |
| **currency** | Currency conversion on ECB daily rates | |
| **timezone** | Multi-timezone conversion with DST handling | |
| **unit-converter** | Offline unit conversion, including cooking measures | |
| **digest-engine** | Multi-topic RSS/Atom digests with duplicate tracking across runs | |
| **github-deps** | Watch GitHub repos for new releases via their Atom feeds | |
| **word-daily** | One vocabulary word a day with spaced repetition, any language pair | |
| **weekly-state-of-me** | Weekly self-reflection over memory and learnings | |
| **openrouter-image-simple** | Generate, edit and analyse images via OpenRouter | `OPENROUTER_API_KEY` |
| **venice-ai-media** | Generate, edit and upscale images, image-to-video, via Venice AI | `VENICE_API_KEY` |

A key goes in `state/.env`, then `docker compose up -d` (not `restart`: compose injects `.env` when it creates the container, and a restart keeps the old environment).

Install copies the directory to `workspace/skills/<name>/` and records provenance in `workspace/skills/.origin.json` with a directory content hash. Skills are live on creation and `workspace/` is a volume, so no rebuild or restart is needed. Contrib skills are stdlib-only; nothing is added to `requirements.extra.txt`.

Rules: install refuses to overwrite a same-named skill that did not come from contrib; re-running on an unmodified install updates it; a locally modified install needs `--force`. `faff update` reports stale or modified contrib installs alongside stale extensions.

Each contrib skill ships a `SKILL.md` (runtime instructions for the agent) and a `HUMAN.md` (setup and configuration notes for you). Read the HUMAN.md after installing; a skill that declares a `requires` env var you have not set is silently absent from the catalog (see "Load-time gating" below) and the agent will say it cannot do that.

## Skill data directories

Each skill gets a persistent data directory at `workspace/skills-data/<name>/`. This directory is created automatically on first invocation by `skills.invoke()`. Single-consumer configuration (a skill's own repos, digests, calendars, aliases) lives here too; `workspace/config/` is reserved for cross-skill files the agent also treats as knowledge (`location.json`) and the restricted `jobs.json`.

The data directory is passed to scripts via the `SKILL_DATA` environment variable. Skills use it for indexes, queues, caches, config, and any state that should persist across invocations.

Observations of the world do not go there. A skill that measures something (a sensor: the contrib `aqi` and `weather` skills) appends one JSON line per run to `workspace/readings/<source>.jsonl` (`at`, `summary`, `data`; latest last; about seven days kept) and, when its rule fires, drops a trigger file in `skills-data/heartbeat/triggers.d/` for the heartbeat to wake the agent with. The skill's own state (threshold, last alerted, last condition) stays in its data directory. The heartbeat skill's SKILL.md gives both formats.

Examples:

| Skill | Data directory contents |
|-------|------------------------|
| carry-over | `queue.json` |
| heartbeat | `config.json`, `triggers.json`, `reported.json`, `triggers.d/` (the trigger tray sensors write into) |
| memory-search | `index.sqlite`, `config.json` |
| preconscious | `buffer.json` |

## Environment variables passed to scripts

Scripts run as a subprocess with the full parent environment, any `state/commands.json` entries (the command seam), and three additional variables:

| Variable | Value | Description |
|----------|-------|-------------|
| `WORKSPACE` | Absolute path to `workspace/` | The agent's working directory |
| `SKILL_DATA` | Absolute path to `workspace/skills-data/<name>/` | Persistent data for this skill |
| `TZ` | Timezone string (e.g. `Europe/London`) | User's configured timezone |

The working directory is set to `workspace/`.

Scripts are always invoked as `python3 <script> [args...]`. The runtime looks for a file matching the action name in `scripts/`, first without extension, then with `.py`.

### Timeout

Default: 600 seconds, which is also the maximum and matches the agent loop's inactivity timeout. A skill can declare a lower budget via the `timeout` frontmatter field; values above 600 are clamped.

## Load-time gating

The `metadata` frontmatter field supports a `requires` block for declaring dependencies:

```yaml
metadata: '{"faffmonkey":{"requires":{"bins":["python3"],"env":["OPENROUTER_API_KEY"]}}}'
```

Fields:

- `bins`: executables that must be available on `PATH`
- `env`: environment variables that must be set

This metadata **is** enforced. `scan_skills()` calls `unmet_requirements()` on each skill's frontmatter and, when a declared binary or environment variable is missing, leaves the skill out of the catalog entirely: the agent is never told it exists and cannot invoke it. Check the log for `skill <name> not offered: missing ...` if a skill you installed does not appear.

This matters more than a filtered listing sounds. A skill whose `env` declaration names an unset API key is invisible rather than failing loudly at invocation, so the usual symptom is the agent claiming it has no such capability.

## SKILL.md frontmatter

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Lowercase kebab-case, max 64 characters. Must match directory name. |
| `description` | Yes | 1-1024 characters. Appears in every system prompt. Must be specific enough for the agent to decide when to invoke. |
| `actions` | No | Comma-separated list of available script actions. |
| `timeout` | No | Script timeout in seconds (default and max 600). |
| `metadata` | No | Single-line JSON. Used for `requires` declarations. |

The body should contain five sections: When to use, What it does, Arguments and flags, Examples, Limitations.

## Creating a skill

### Using skill-writer

Ask the agent to write one, or run the scaffolder yourself from any chat:

```
/skill skill-writer init_skill my-skill
/skill skill-writer init_skill my-skill --resources scripts,references
```

This creates the directory structure and a SKILL.md template. Edit the template to fill in the five body sections.

After editing, validate:

```
/skill skill-writer quick_validate my-skill
```

Checks: name format (kebab-case, no consecutive hyphens, max 64 chars), description length (under 1024 chars), no unexpected frontmatter keys, and warns if the description lacks trigger language.

### Manually

Create the directory and SKILL.md:

```
workspace/skills/my-skill/
  SKILL.md
  scripts/
    my-action.py
```

Frontmatter:

```yaml
---
name: my-skill
description: One line describing when the agent should use this skill.
actions: my-action
---
```

Scripts receive action arguments as `sys.argv[1:]`. Read `WORKSPACE` and `SKILL_DATA` from environment variables. Write persistent state to `SKILL_DATA`. Print output to stdout. Exit 0 on success, non-zero on error.

To attach files to the response, print `MEDIA:<path>` lines to stdout. The runtime parses these and includes the files as attachments.

### Packaging for distribution

```
/skill skill-writer package_skill my-skill
```

Creates a `.skill` zip file. Validates the skill first, then bundles all files while skipping symlinks, `.git`, `__pycache__`, and `node_modules`. Rejects files that resolve outside the skill root.

## Removing a skill

Delete the skill directory from `workspace/skills/`:

```bash
rm -rf workspace/skills/my-skill
```

Optionally remove its data:

```bash
rm -rf workspace/skills-data/my-skill
```

For built-in skills, deleting the directory means `faff update` will re-copy the template version on next run. There is no clean way to opt out of a built-in: an empty directory in its place stops the re-copy, but `faff update` then reports it as "no provenance and differs from the template" on every run.

## How skills appear to the agent

At bootstrap, `scan_skills()` iterates `workspace/skills/`, reads the `name` and `description` from each SKILL.md frontmatter, and includes them in the system prompt as a list of available skills.

When the agent invokes a skill:

1. With no action: the full SKILL.md content is returned (so the agent can read instructions).
2. With an action: the matching script runs and its stdout/stderr is returned.

The agent decides when to invoke skills based on the description in the system prompt. Descriptions should front-load capability keywords and include trigger phrases ("Use when the user says...").
