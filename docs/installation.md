# Installation

Getting from a clone to a running agent. Everything after that
(running, upgrading, backups, cron, heartbeat) is in
[operating.md](operating.md).

## Docker (recommended)

### Prerequisites

- Docker with BuildKit
- Docker Compose
- Python 3.14+ on the host, only for the extension wizards
  (`./bin/faff setup telegram` and friends run outside the container)

### Build and initialise

Clone the repository anywhere (`git clone https://github.com/faffweasel/faffmonkey.git`). The checkout is only code: your data
lives in the data root, `$FAFF_HOME` (default `~/.faffmonkey`), which
`faff init` populates. Create its directories first, so Docker never
has to create the mounts as root.

```bash
mkdir -p ~/.faffmonkey/workspace ~/.faffmonkey/state ~/.faffmonkey/extensions
docker compose build
docker compose run --rm faffmonkey faff init
docker compose run --rm faffmonkey faff setup provider
```

To put the data root somewhere else, write `FAFF_HOME=/absolute/path`
into a `.env` file next to `docker-compose.yml` before any of the
above, and create the three directories under that path instead of
`~/.faffmonkey`. Both compose and `./bin/faff` read the file. A
relative path resolves against the checkout, which is exactly where
data must not live.

### Several agents on one machine

One checkout and one data root per agent. Clone into directories with
different names (compose names the container, image and network after
the directory), give each checkout's `.env` its own absolute
`FAFF_HOME`, and run every command for that agent from its own
checkout. Nothing is shared: each container sees only its own four
mounts, and each `./bin/faff` writes only to its own data root.

`faff init` asks a short setup: timezone (detected, confirm or
correct), the heartbeat's active hours (the window in which it may
message you), your name, the agent's name, its role, your preferred
communication style, and one thing the agent should remember about
you. Every question can be skipped with Enter; skipped answers leave
template placeholders to edit later. Re-running init offers the
configured values as defaults and never touches identity files that
already exist.

`faff setup provider` walks through LLM provider configuration and
writes the `main`, `cheap` and `vision` model slots. See
[configuration.md](configuration.md) for what it writes and how to
change it.

### File ownership

The container runs as uid 1000, not root, so everything it writes to
the `workspace/` and `state/` bind mounts is owned by uid 1000 on the
host. If your user is not 1000:1000, set `FAFF_UID` and `FAFF_GID` for
every compose command, either in the shell or in a `.env` file next to
`docker-compose.yml` (that file holds `FAFF_UID`, `FAFF_GID` and
`FAFF_HOME`, is read by compose and by `./bin/faff`, and is separate
from `state/.env`):

```bash
echo "FAFF_UID=$(id -u)" >> .env
echo "FAFF_GID=$(id -g)" >> .env
```

Create the data-root directories before the first compose command (the
`mkdir -p` in the quickstart), so compose never has to create them as
root. If an older run already left root-owned files behind, repair them
once with `sudo chown -R $(id -u):$(id -g) ~/.faffmonkey`.

### Channels

`faff run` needs at least one channel. The wizards run on the host, not
in the container: they copy the extension module into
`$FAFF_HOME/extensions/` (which the container mounts read-only) and add
its pip dependency to `$FAFF_HOME/requirements.extra.txt`, mirroring it
into the checkout for `docker compose build` to pick up. Run inside the container they detect the read-only
mount and refuse with this guidance.

```bash
./bin/faff setup telegram   # Telegram
./bin/faff setup discord    # Discord
docker compose build        # rebuild so the image installs the dependency
```

Each wizard walks through creating the bot, validates the token, asks
for your user id (only listed users get replies), and, the first time,
creates the four jobs that make the agent's day: an hourly heartbeat,
a 07:05 morning greeting, a 22:00 evening memory wrap and a silent
06:01 preconscious-decay pass (no LLM, keeps the top-of-mind buffer
decaying as designed). They deliver
to whichever channel you last spoke on, so a second channel changes
nothing. Discord replies in guild channels are visible to everyone
in the channel. The wizards are described step by step in
[extensions.md](extensions.md).

### Web search (optional)

```bash
./bin/faff setup search
```

Enables the `web_search` tool via Brave Search; needs a Brave API key
(brave.com/search/api) but no extra pip packages, so no rebuild, just
a restart once the agent is running.

### Voice (optional)

```bash
./bin/faff setup voice
```

Voice notes on Telegram and Discord are transcribed before the agent
sees them, and replies to voice notes are spoken back. Uses an
OpenAI-compatible API (Whisper-style transcription, TTS-style
synthesis); needs an API key but no extra pip packages.

### Start and verify

```bash
docker compose up -d
docker compose run --rm faffmonkey faff doctor
```

`faff doctor` checks: directory structure, config validity, provider
configuration (local endpoints are contacted, remote hosts are only
validated, not called), channel module loading, extension integrity,
`state/commands.json` validity, heartbeat config and job, bootstrap
files (SOUL.md, IDENTITY.md, USER.md, AGENTS.md), skills, database
schema version, cron jobs the scheduler would reject, and timezone. It
prints a next-step hint based on what is missing and exits non-zero if
any check is red.

If the container keeps restarting, `faff run` is exiting because no
channel is configured or a channel failed to start; `docker compose
logs --tail=30 faffmonkey` shows the one-line reason, and `faff doctor`
works while it loops.

Send the bot a message. From here, [operating.md](operating.md).

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest
.venv/bin/pytest tests/ -v --tb=short
```

To run the CLI without Docker, do it outside the repo directory so
`workspace/`, `state/`, `extensions/` and `backups/` are not created in
the source tree:

```bash
mkdir ~/faff-dev && cd ~/faff-dev
python3 /path/to/faffmonkey/bin/faff init
python3 /path/to/faffmonkey/bin/faff setup provider
python3 /path/to/faffmonkey/bin/faff chat
```

`faff chat` is an interactive terminal session with the same agent
loop and workspace as the channels. Type `/help` for commands, Ctrl+C
to exit.

## What `faff init` creates

```
workspace/              The agent's world
workspace/memory/       Memory files
workspace/memory/daily/ Daily logs (YYYY-MM-DD.md)
workspace/skills/       Skills (SKILL.md per skill)
workspace/skills-data/  Persistent skill state
workspace/shared/       File exchange with you
workspace/shared/inbox/ Inbound media from channels
workspace/config/       Agent-facing config (jobs.json)
workspace/documents/    Files the agent writes for you, and files you give it; searchable
workspace/tmp/          Scratch space
state/                  Runtime config and secrets (0700)
state/backups/          Snapshots (0700)
extensions/             Extension modules (0700)
```

| File | Source | Content |
|---|---|---|
| `workspace/SOUL.md` | template | Values, personality, boundaries |
| `workspace/IDENTITY.md` | template | Name, role, how it introduces itself |
| `workspace/USER.md` | template | Who you are |
| `workspace/AGENTS.md` | template | Working rules: output style, tool use, memory, its own files |
| `workspace/HEARTBEAT.md` | template | What the heartbeat watches for |
| `workspace/skills/*` | template | Built-in skills |
| `workspace/MEMORY.md` | template | Index skeleton (key facts, projects, people, lessons); the memory question's answer goes under key facts |
| `workspace/config/jobs.json` | generated | `[]`; the first channel wizard adds the heartbeat, morning and evening jobs |
| `state/config.json` | generated | Defaults with your timezone and active hours |
| `state/.env` | generated | API key template (0600) |
| `state/commands.json` | generated | `{}`, the command seam between skills |
| `extensions/.origin.json` | generated | `{}` |
| `requirements.extra.txt` | generated | Empty, for extension pip dependencies |

Template files are only copied if the destination does not exist.
Existing files are never overwritten. All paths in the table are
relative to the data root (`$FAFF_HOME`, default `~/.faffmonkey`),
except the `requirements.extra.txt` build mirror the checkout keeps.
