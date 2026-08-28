# Configuration

Everything the runtime reads from `state/`: `config.json` key by key,
`.env`, and the provider setup that writes most of it for you.

Validation is strict. Unknown top-level keys warn; `tool_permissions`
and `permissions` (old names for `tools`) are hard errors; wrong types,
bad `api_key_env` names and insecure base URLs are errors. A config
error reaches you as one line and exit 1; `faff --debug` shows the
traceback. `faff doctor` validates the whole file.

## state/config.json

```json
{
  "timezone": "Asia/Ho_Chi_Minh",
  "models": { "main": {...}, "cheap": {...}, "vision": {...} },
  "routing": { "conversation": "main", "compaction": "cheap", "heartbeat": "cheap",
               "cron_default": "main", "image_understanding": "vision" },
  "fallback_models": [],
  "heartbeat": { "enabled": true, "active_hours": [9, 22], "ack_max_chars": 300 },
  "compaction": { "threshold": 0.5, "target_ratio": 0.2, "protect_last_n": 20, "hard_message_limit": 400 },
  "daily_note": { "every_turns": 10, "every_minutes": 60 },
  "channels": { "telegram": { "enabled": true, "allowed_users": ["123456"] } },
  "tools": { "file_read": "always", "file_list": "always", "file_write": "always", "file_edit": "always",
             "file_search": "always", "file_copy": "always", "file_move": "always", "file_delete": "always",
             "web_search": "always", "web_fetch": "always", "shell_exec": "ask",
             "skill_invoke": "always", "shell_preapproved": [] },
  "search": { "provider": "brave", "api_key_env": "BRAVE_API_KEY" },
  "voice": { ... }
}
```

### timezone

IANA name. Every cron schedule, `at` time, active-hours window and
displayed timestamp uses it. `faff init` detects it and asks.

### models

A map of named slots. `main` is required; `faff setup provider`
creates `main`, `cheap` and `vision`. Add any other slot you like and
name it from a cron job's `model` field.

| Field | Default | Description |
|---|---|---|
| `provider` | required | Provider name (`openrouter`, `ollama-local`, ...) |
| `model` | required | Model identifier |
| `base_url` | required | API endpoint |
| `api_key_env` | `""` | Env var holding the key; empty for keyless local endpoints |
| `module` | `""` | Dotted path to a custom `Provider` class in `extensions/` |
| `timeout` | `120` | Request timeout, seconds |
| `allow_insecure` | `false` | Permit `http://` to a non-local host |
| `context_window` | `128000` | Tokens. Sets the bootstrap budget (60% of it) and the compaction threshold. `faff setup provider` reads it from the provider where it can and asks otherwise; `faff doctor` warns when the conversation slot is running on the default or disagrees with what the provider reports |

### routing

Task to slot. Missing entries take the defaults shown above. Several
tasks can share a slot.

| Task | Used for |
|---|---|
| `conversation` | Normal turns |
| `compaction` | Summarising the context; the request is chunked to this model's window, so a small model here still works |
| `heartbeat` | The heartbeat gate (runs every hour, keep it cheap) |
| `cron_default` | Cron jobs without a `model`, and heartbeat escalation |
| `image_understanding` | Any turn that carries an image, including a cron job's `agent` turn with its own `model` override |

### fallback_models

Array of slot-shaped objects tried in order when the primary fails.
Applies to every request, whichever route chose the primary.

Per request: the primary gets up to 3 attempts, waiting 1s then 2s
between them (a `Retry-After` header overrides, capped at 30s). Rate
limits (429), server errors (500, 502, 503), timeouts and connection
errors retry. Auth errors (401/403) and a refused connection skip
straight to the fallbacks without retrying. Any other HTTP error (a 404
for an unknown model, a 400 the provider rejects) fails the request at
once, with no fallback: it would fail the same way anywhere. Each
fallback gets the same 3 attempts. When everything is exhausted the
request fails.

### heartbeat

| Field | Default | Description |
|---|---|---|
| `enabled` | `true` | `false` skips every heartbeat tick |
| `active_hours` | `[9, 22]` | `[start, end)` hours in your timezone when it may message you; `[22, 7]` wraps past midnight |
| `ack_max_chars` | `300` | A cron response shorter than this that looks like "on it" is re-prompted once |

The schedule itself is the heartbeat job's cron expression in
`workspace/config/jobs.json`; there is no interval here, and a leftover
`interval_minutes` is a config error.

### compaction

| Field | Default | Description |
|---|---|---|
| `threshold` | `0.5` | Fraction of `context_window` the whole request (system prompt plus history) may reach before compaction. An existing config that sets it keeps its value |
| `target_ratio` | `0.2` | Target size after compaction, as a fraction |
| `protect_last_n` | `20` | Most recent messages kept verbatim (minimum 1) |
| `hard_message_limit` | `400` | Message count that triggers compaction regardless of tokens |

### daily_note

| Field | Default | Description |
|---|---|---|
| `every_turns` | `10` | User turns since the last note before the loop asks the compaction-routed model for a daily-log entry |
| `every_minutes` | `60` | Minutes since the last note before it asks anyway; whichever comes first |

Both are positive integers. Nothing fires while nobody is talking; see
the Daily note section of [architecture.md](architecture.md).

### channels

Map of channel name to:

| Field | Default | Description |
|---|---|---|
| `enabled` | `false` | Must be a JSON boolean; the string `"false"` is an error, not a disabled channel |
| `allowed_users` | `[]` | Sender ids that get replies. Empty means nobody |
| `group_policy` | `"mention"` | `mention`, `open` or `dm_only`; honoured by Discord |
| `module` | `""` | Only for custom channels; `telegram` and `discord` resolve by name |

### tools

Permission per tool: `always`, `ask` or `never`. Tools you leave out
keep the defaults below; a tool the runtime does not know is ignored.
`ask` prompts in `faff chat` and is denied under `faff run` and in
cron, where nobody is there to answer; the agent is told the tool is
not available on that channel and pointed at the file tools, so it
does not retry variants. A tool set to `never` is not offered to the
model at all.

`shell_preapproved` inside the same block is a list of glob patterns
(`fnmatch` against the whole command) that `shell_exec` runs without
asking. The hardline blocklist still applies first and cannot be
overridden.

| Tool | Default |
|---|---|
| `file_read`, `file_list`, `file_write`, `file_edit` | `always` |
| `file_search`, `file_copy`, `file_move`, `file_delete` | `always` |
| `web_search`, `web_fetch` | `always` |
| `skill_invoke` | `always` |
| `shell_exec` | `ask` |

### search

`provider` (`brave`), `api_key_env`, optional `module`. Written by
`faff setup search`. Absent means the `web_search` tool errors with
"not configured".

### voice

| Field | Default |
|---|---|
| `transcriber`, `transcriber_module`, `transcriber_model` | `""`, `""`, `"whisper-1"` |
| `synthesiser`, `synthesiser_module`, `synthesiser_model`, `synthesiser_voice` | `""`, `""`, `"tts-1"`, `"alloy"` |
| `api_key_env` | `""` |
| `base_url` | `"https://api.openai.com/v1"` |

Written by `faff setup voice`. Either side can be configured alone.

## Data root

All runtime data lives under `$FAFF_HOME` (default `~/.faffmonkey`):
`workspace/`, `state/`, `extensions/`, `backups/` and
`requirements.extra.txt`. Set `FAFF_HOME` in the shell or in the `.env`
file next to `docker-compose.yml` to relocate it. Both compose and the
host-side `./bin/faff` read that file, so every setup wizard writes to
the same data root the container mounts; the shell wins if both are set.
Use an absolute path: a relative one resolves against the checkout,
which puts the data where a deploy can delete it. Inside the container
the image pins `FAFF_HOME=/app`, where compose mounts the host's data
root.

Running several agents on one machine means one checkout and one data
root per agent, each checkout's `.env` naming its own `FAFF_HOME`.

## state/.env

Secrets only. The runtime reads keys from its environment, never from
the file; it is compose that turns the file into the container's
environment (see below). Env var names must match
`[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)`: `OPENROUTER_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `BRAVE_API_KEY`. A configured `api_key_env` that
is not set at startup is a config error, not a runtime surprise. A
`faff` command run on the host outside compose does not see the file
either; the setup wizards export what they write for their own run,
and `faff skill install` reads it to check a skill's required vars.

`state/.env` is separate from the `.env` next to `docker-compose.yml`,
which only holds `FAFF_UID`, `FAFF_GID` and `FAFF_HOME`.

The runtime does not read the file itself: compose injects it as the
container's environment at creation time. After adding or changing a
key, `docker compose up -d` (or `--force-recreate` if it says nothing
changed); `docker compose restart` keeps the environment the container
started with and the new key stays invisible.

## Provider setup

```bash
docker compose run --rm faffmonkey faff setup provider
```

1. Lists the presets in `contrib/providers/openai-compatible/` plus
   "Custom OpenAI-compatible".
2. For a preset, fills in the base URL. For custom, asks for the base
   URL and the API key env var name (pasting the key itself here is
   caught and asked again).
3. Asks for the API key (hidden input) unless the env var is already
   set. Skipped for keyless presets such as local Ollama. The key is
   written to `state/.env` only after the connection test in step 5
   passes.
4. Asks for the model. For local Ollama it lists what
   `localhost:11434` has.
5. Sends a one-word test request to `{base_url}/chat/completions`.
6. Asks whether `cheap` and `vision` should use the same model; if
   not, asks for each.
7. Reads each model's context window from the provider: the `/models`
   list for OpenRouter, Venice and most OpenAI-compatible servers,
   then Ollama's native `/api/ps` and `/api/show`. Where nothing
   reports one it asks, showing `128000` as the default so the number
   is a choice rather than a silent fallback. A local Ollama reports
   the window the model was loaded with, which is the server's
   `num_ctx` rather than the trained length; the connection test in
   step 5 is what loads it.
8. Writes the `models` block.

### Bundled presets

| Key | Base URL | Default model | Key env |
|---|---|---|---|
| `ollama-local` | `http://localhost:11434/v1` | `llama3` | none |
| `ollama-cloud` | `https://ollama.com/v1` | `kimi-k3:cloud` | `OLLAMA_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `google/gemini-3.6-flash` | `OPENROUTER_API_KEY` |
| `venice` | `https://api.venice.ai/api/v1` | `qwen-3-8-27b` | `VENICE_API_KEY` |

A preset may also carry `cheap_model`: setup offers it as the default for
the cheap slot instead of asking whether to reuse the main model
(`ollama-cloud` suggests `kimi-k2.6:cloud` alongside `kimi-k3:cloud`).

### Adding a preset

Any endpoint that speaks the OpenAI chat-completions API is a JSON
file in `contrib/providers/openai-compatible/`:

```json
{
  "name": "My Provider",
  "provider_key": "my-provider",
  "base_url": "https://api.example.com/v1",
  "api_key_env": "MY_PROVIDER_API_KEY",
  "default_model": "some-model",
  "notes": "Sign up at example.com"
}
```

It appears in the wizard on the next run; `provider_key` becomes the
slot's `provider`.

### A provider that is not OpenAI-compatible

Write a class satisfying `Provider` (`src/faffmonkey/seams/provider.py`),
put it in `extensions/`, and set the slot's `module`:

```json
{
  "models": {
    "main": {
      "provider": "my-custom",
      "model": "the-model",
      "base_url": "https://api.example.com",
      "api_key_env": "MY_CUSTOM_API_KEY",
      "module": "extensions.my_provider.MyProvider"
    }
  }
}
```

The class is constructed with `base_url`, `api_key` and `timeout`, plus
`allow_insecure` if its constructor declares it. See
[extensions.md](extensions.md).

### Base URL rules

`https://` or `http://`, no embedded credentials. `http://` is only
accepted for `localhost`, `127.0.0.1`, `::1` and
`host.docker.internal` unless the slot sets `allow_insecure: true`.

## Switching models at runtime

`/model` in any session shows the slots and routing. `/model <slot>
<model>` changes the model name within that slot's existing provider
and base URL; `/model <slot> <provider> <model>` moves the slot to
another provider, taking connection details from a slot already on it
or from the contrib preset (the preset's API key env var must already
be in the environment). Either form asks the provider for the new
model's context window the same way setup does and writes the whole
change to `config.json`, so it survives a restart; when the provider
does not report a window the old value is kept and the reply says so.
`/status` shows the window in play and the compaction threshold. New
slots are edits to the file.
