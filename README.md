# faffmonkey

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

A minimal, self-hosted personal AI agent. One agent, stdlib-only 
Python core, Docker-required deployment. Talks to you on Telegram 
or Discord (or the terminal), remembers across sessions, runs cron 
jobs and an hourly heartbeat, and is extended through typed seams: 
channels, LLM providers (any OpenAI-compatibleAPI), web search, 
and voice. Runs with zero optional config out of the box.

## Status

A personal project, maintained for my own use. It is published so it can
be cloned, read, and forked, not as a supported product: there is no
roadmap, and issues and pull requests may go unanswered.

## Quickstart

```bash
git clone https://github.com/faffweasel/faffmonkey.git
cd faffmonkey
mkdir -p ~/.faffmonkey/{workspace,state,extensions,backups}
# Relocating the data root? Put FAFF_HOME=/absolute/path in .env here and
# create these four directories under it instead.
docker compose build
docker compose run --rm faffmonkey faff init
docker compose run --rm faffmonkey faff setup provider
./bin/faff setup telegram   # or discord; runs on the host
docker compose build        # the image installs the channel's dependency
docker compose up -d
docker compose run --rm faffmonkey faff doctor
```

The checkout is only code. `faff init` creates `workspace/` (the
agent's world) and `state/` (config, secrets, sessions) under the data
root: `$FAFF_HOME`, default `~/.faffmonkey`. Delete or re-clone the
checkout freely; the agent's data never lives inside it.

## Docs

| | |
|---|---|
| [docs/installation.md](docs/installation.md) | From clone to first message |
| [docs/operating.md](docs/operating.md) | Running it, talking to it, cron and heartbeat, upgrading, backups |
| [docs/configuration.md](docs/configuration.md) | `state/config.json` key by key, providers and model routing |
| [docs/extensions.md](docs/extensions.md) | The setup wizards, updating extensions, writing your own |
| [docs/skills.md](docs/skills.md) | Built-in and contrib skills, writing a skill |
| [docs/architecture.md](docs/architecture.md) | How it works and why, the security model |

`CLAUDE.md` is the guide for working on the code: stack, code rules, file layout.

## Troubleshooting

- **"No config found"**: run `faff init` first
- **"No LLM provider configured"**: run `faff setup provider`
- **"env var not set"**, or a skill saying its key is not configured: the key is missing from `state/.env`, or you added it and ran `docker compose restart`; it takes `docker compose up -d` to reload the environment
- **Bootstrap exceeds context budget**: trim workspace files, raise the slot's `context_window`, or use `--allow-overflow`
- **Tool call denied**: check the `tools` block in `state/config.json`; `shell_exec` is `ask` by default and nobody can answer under `faff run`
- **Cron jobs not firing**: `faff cron list`, `faff cron history <id>`, then `faff doctor`
- **Heartbeat never messages you**: see "Heartbeat" in [docs/operating.md](docs/operating.md)
- **"preflight failed"**: the LLM provider endpoint is unreachable
- **Container keeps restarting**: `faff run` exits when no channel is configured or a channel fails to start, and compose restarts it. `docker compose logs --tail=30 faffmonkey` shows the one-line reason; `docker compose run --rm faffmonkey faff doctor` works while it loops

## License

[AGPL-3.0-only](LICENSE)
