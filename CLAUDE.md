# CLAUDE.md: faffmonkey

## What this is

A minimal, self-hosted personal AI agent. Docker-required, stdlib-only Python core, seams pattern for extensibility. The living architecture doc is `docs/architecture.md`. Read it before writing code.

CLI command is `faff`. Project name is `faffmonkey`. Repo: `github.com/faffweasel/faffmonkey`.

## Stack (overrides global)

Python 3.14+, stdlib-only core, sqlite3, json, urllib.request, subprocess, zoneinfo. No TypeScript, no React, no Supabase, no Tailwind, no Biome.

pip dependencies allowed ONLY for seam implementations (python-telegram-bot, discord.py). Never for core runtime. Prefer stdlib even in seam implementations (the Brave search and OpenAI voice extensions use urllib).

Dev dependencies: pytest.

## Local Development Environment

Python venv at `.venv/`. All Python commands must use the venv:
- `.venv/bin/python` (not `python` or `python3`)
- `.venv/bin/pytest` (not `python -m pytest`)

Do not install packages globally. Do not run `pip install` outside the venv.

## Code Principles (overrides global)

- **stdlib or nothing.** Before importing any third-party package, check if stdlib can do it. urllib.request not requests. json not pydantic. sqlite3 not sqlalchemy. subprocess not sh.
- Prefer plain functions. A class earns its place only by owning mutable state with a lifecycle (`AgentLoop`, `SessionStore`, `ToolRegistry`, `Scheduler`), by being a `typing.Protocol` seam interface or its concrete implementation, by being a dataclass, or by being an exception. No inheritance hierarchies beyond `Exception`.
- No `typing.Any`. Type hints on all public functions. Dataclasses for structured data, not dicts.
- One file per concern. Don't split prematurely, don't combine unrelated logic.
- No dead code, no commented-out blocks, no TODO comments that aren't tracked in an issue.

## Architecture: Read Before Writing

The seams pattern is the only extensibility mechanism. Read the "Seams and wiring" section of `docs/architecture.md` before touching any seam.

Key constraints:
- All seam interfaces are in `src/faffmonkey/seams/`. They are `typing.Protocol` classes.
- All config-driven seam wiring happens in `src/faffmonkey/wiring.py`. It is the only place a seam implementation is chosen from config; the CLI resolves channels through the same helpers.
- Noop defaults exist for every optional seam. The agent runs with zero optional config.
- `workspace/` is the agent's world. `state/` is runtime config. The agent's tools cannot reach state/ via file_read/file_write (but shell_exec can, the container is the real boundary).

## File Structure

Top level: `bin/faff` (host-side CLI wrapper, used by the compose
quickstart), `src/`, `contrib/`, `templates/`, `tests/`, `docs/`,
`conftest.py`.

The trees below are exhaustive for `src/faffmonkey/` and `contrib/`, and
that is the scope of the "do not create files outside this structure"
rule.

```
src/faffmonkey/
├── __init__.py
├── types.py           ← InboundMessage, OutboundMessage, CompletionRequest/Response, ToolCall/Result
├── config.py          ← load config.json, model routing, resolve_model()
├── wiring.py          ← single point: reads config, exports live seam instances
├── runtime/
│   ├── loop.py        ← main agent loop, tool dispatch, goal check
│   ├── session.py     ← SQLite session store (WAL mode)
│   ├── bootstrap.py   ← system prompt assembly from workspace files
│   ├── tokens.py      ← token counting heuristic and budget checking
│   ├── tools.py       ← tool registry, permissions, approval flow
│   ├── skills.py      ← skill scanning, loading, invocation
│   ├── scheduler.py   ← in-process cron (TZ-aware)
│   ├── compaction.py  ← context summarisation pipeline
│   ├── goal.py        ← goal loop (GOAL_DONE token, turn budget)
│   ├── blocklist.py   ← hardline command blocklist
│   ├── redaction.py   ← outbound secret stripping
│   ├── ingest.py      ← nonce-wrapped untrusted content, injection scanning
│   ├── trust.py       ← file trust store (sha256 verification)
│   ├── lint.py        ← post-write lint (Python, JSON, TOML, YAML)
│   └── retry.py       ← retry with exponential backoff and fallback chain
├── seams/
│   ├── channel.py     ← Channel Protocol
│   ├── channel_cli.py
│   ├── channel_noop.py
│   ├── provider.py    ← Provider Protocol
│   ├── provider_openai_compat.py
│   ├── transcriber.py ← Transcriber Protocol + Noop
│   ├── synthesiser.py ← Synthesiser Protocol + Noop
│   └── search_provider.py ← SearchProvider Protocol + Noop
└── cli/
    ├── __main__.py    ← argparse entry point (faff init/chat/run/trust/...)
    ├── init.py        ← project initialisation (directories, templates, config)
    ├── setup_provider.py ← interactive LLM provider setup
    ├── setup_search.py   ← interactive search provider setup
    ├── setup_telegram.py ← interactive Telegram channel setup
    ├── setup_discord.py  ← interactive Discord channel setup
    ├── setup_voice.py    ← interactive voice (STT/TTS) setup
    ├── status.py      ← runtime status and recent activity
    ├── doctor.py      ← diagnose misconfigurations
    ├── cron.py        ← cron job listing, manual run, history
    ├── trust.py       ← manage file trust status
    ├── skill.py       ← contrib skill install/list with provenance
    ├── export.py      ← conversation history export (json/openai)
    ├── backup.py      ← safe SQLite backup + tar
    └── update.py      ← pre-upgrade migrations

contrib/
├── channel_telegram.py          ← Telegram channel implementation
├── channel_discord.py           ← Discord channel implementation
├── search_provider_brave.py     ← Brave search provider implementation
├── transcriber_openai.py        ← OpenAI-compatible STT (stdlib-only)
├── synthesiser_openai.py        ← OpenAI-compatible TTS (stdlib-only)
├── skills/                      ← optional skills, installed via faff skill install
└── providers/
    └── openai-compatible/
        ├── ollama-local.json
        ├── ollama-cloud.json
        ├── openrouter.json
        └── venice.json
```

Do not create files outside these two trees without asking. Do not add subdirectories within `runtime/` or `seams/`.

## Python

- Python 3.14+. The suite is only ever run on 3.14, so that is what is
  supported; claiming 3.11 while never testing it hid a whole test file that
  could not import. `tomllib` and `zoneinfo` are the stdlib floor.
- `dataclasses` for all data types. No pydantic, no attrs.
- `typing.Protocol` for seam interfaces. Not `abc.ABC`.
- `sqlite3` with WAL mode for all persistence.
- `urllib.request` for HTTP. Not requests, not httpx, not aiohttp.
- `subprocess.run()` for shell execution. Not os.system, not sh.
- `zoneinfo.ZoneInfo` for timezone handling. Not pytz.
- `json` for config. Not YAML.
- `pathlib.Path` throughout. Not os.path.
- f-strings for formatting. Not .format(), not %.
- `logging` module, not print(), for anything the operator reads in a log. Output is the stdlib default format; there is no JSON formatter and adding one is not planned.
- No async. The agent loop is synchronous. urllib.request is synchronous. sqlite3 is synchronous. Adding asyncio doubles complexity for zero benefit in a single-user agent.

## Testing

- pytest only. No unittest.TestCase subclasses.
- Tests in `tests/` mirroring `src/faffmonkey/` structure.
- `tests/` also holds the machinery the suite is built on, which pytest
  does not collect because it is not named `test_*`: `fakes.py` and
  `faux_provider.py` (the seam fakes), `e2e/scripted_provider.py`, and
  `mutations.py` (the mutation harness, run directly rather than under
  pytest). Test machinery belongs in `tests/`, not in a scratch directory.
- A test must derive from something other than the code it tests: a defect
  that actually happened, a Protocol or wire format, an invariant that
  survives a rewrite, or adversarial input. A test written by reading the
  implementation can only confirm it.
- A failing test is a question about which side is wrong. Do not edit
  whichever of the two is cheaper to change until that question is answered.
- Mock external I/O (provider calls, filesystem, network) with `unittest.mock.patch`.
- tmp_path fixture for filesystem tests. Never write to real workspace/state.
- conftest.py in the project root adds src/ to sys.path so that all test files can import faffmonkey without requiring pip install, do not add sys.path manipulation to individual test files or source files.

## Verification requirements (overrides global)

After any code change:
```bash
.venv/bin/pytest tests/ -v --tb=short
```

Do not report tests pass without showing the output.

## Security

- Secrets in `state/.env`, loaded as environment variables. Never in workspace, never in code.
- File tools restricted to workspace/ paths. Reject any path with `..` traversal.
- `runtime/blocklist.py` patterns checked BEFORE tool permission checking. No override.
- `runtime/redaction.py` strips API key patterns from all outbound messages.
- The container is the trust boundary, not workspace path restrictions. Document this honestly.

## Git (extends global)

- Same commit conventions as global.
- Tag releases: `v0.1.0`, `v0.1.1`.
- Semver: patch for new seam implementations and bug fixes. Minor for interface changes.
- Never commit `state/`, `backups/`, `workspace/` contents (except templates/).

## Docker

- Multi-stage Dockerfile. Builder installs deps, slim final stage.
- Base image pinned to digest.
- `tzdata` in final image.
- BuildKit required.
- Log driver: `json-file`, `max-size: 10m`, `max-file: 3`.
- Compose service name: `faffmonkey`.

## What Not To Do (extends global)

- Don't add async. This is a synchronous agent.
- Don't add a web UI. CLI, Telegram, and Discord are the interfaces.
- Don't add vector embeddings to core. That's a skill concern.
- Don't add MCP. Skills call APIs directly.
- Don't add a plugin loader. Seams are compile-time wiring.
- Don't add pydantic, attrs, or any validation framework. Dataclasses + manual checks.
- Don't add requests, httpx, or aiohttp. Use urllib.request.
- Don't add sqlalchemy. Use sqlite3.
- Don't add click or typer. Use argparse.
- Don't add any dependency without confirming it's required and that stdlib can't do it.

## Testing Safety

Never point any faff command at the repo directory (`--path .`, `--base-dir .`, or `FAFF_HOME` set to the repo). Data defaults to `$FAFF_HOME` (`~/.faffmonkey`); for manual testing use a throwaway data root, e.g. `FAFF_HOME=/tmp/test-faff faff init`. The automated tests pass tmp_path fixtures and never touch a real data root.

## Context

docs/architecture.md is the source of truth for how the system works and must be updated in the same change as any behaviour it describes. This file is the source of truth for how to write the code. When in doubt, read the architecture doc and the code. When they disagree, the code is right and the doc gets fixed.
