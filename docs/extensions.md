# Extensions

An extension is a single Python module at the root of `extensions/`
that implements one of the seam protocols in `src/faffmonkey/seams/`:
`Channel`, `Provider`, `SearchProvider`, `Transcriber` or
`Synthesiser`. The container mounts `extensions/` read-only. The layout
is flat by design: wiring rejects nested packages under `extensions/`
and refuses to load if `extensions/__init__.py` exists.

The ones that ship with faffmonkey live in `contrib/` and are copied
into `extensions/` by the setup wizards, with provenance (source path,
SHA-256 of the copy, timestamp) recorded in `extensions/.origin.json`.
Nothing in `contrib/` is loaded directly.

```
contrib/
  channel_telegram.py              Telegram channel (python-telegram-bot)
  channel_discord.py               Discord channel (discord.py)
  search_provider_brave.py         Brave Search (stdlib only)
  transcriber_openai.py            OpenAI-compatible speech-to-text (stdlib only)
  synthesiser_openai.py            OpenAI-compatible text-to-speech (stdlib only)
  skills/                          Optional skills, see skills.md
  providers/openai-compatible/     Provider presets, see below
```

The two subdirectories are not extensions. `contrib/skills/` holds
optional skills installed into `workspace/skills/` with `faff skill
install` ([skills.md](skills.md)). `contrib/providers/` holds JSON
presets that `faff setup provider` reads to build its menu; they are
never copied anywhere, and the provider implementation itself is the
built-in `OpenAICompatProvider`. Adding a preset is dropping a file
there ([configuration.md](configuration.md)).

## The wizards

All four run on the host as `./bin/faff setup <name>`, not via `docker
compose run`: they write to `$FAFF_HOME/extensions/`, which the
container mounts read-only, and to `$FAFF_HOME/requirements.extra.txt`,
mirrored into the checkout for `docker compose build`. Run inside the
container they detect the read-only mount and refuse with that
guidance.

Each wizard copies the module, records it in `.origin.json`, asks for
what it needs, writes secrets to `state/.env` (0600) and config to
`state/config.json`. Re-running a wizard updates the config in place.

### Telegram

```bash
./bin/faff setup telegram
docker compose build
```

1. Copies `channel_telegram.py`; adds `python-telegram-bot>=21,<22` to
   `requirements.extra.txt`.
2. Walks through creating a bot with @BotFather and validates the
   token against the Telegram API.
3. Writes `TELEGRAM_BOT_TOKEN` to `state/.env`.
4. Asks for your Telegram user id (from @userinfobot) and writes the
   channel config with you as the only allowed user.
5. Adds the `heartbeat`, `morning` and `evening` jobs to
   `workspace/config/jobs.json`, whichever are missing, delivering to
   `last` (the channel you most recently spoke on).

At startup the channel registers the slash commands as the bot's
command menu, replacing whatever a reused bot token had before. The
Start button's `/start` is answered as `/help`.

### Discord

```bash
./bin/faff setup discord
docker compose build
```

1. Copies `channel_discord.py`; adds `discord.py>=2,<3`.
2. Walks through creating an application in the Discord Developer
   Portal, including enabling the Message Content privileged intent,
   and validates the token.
3. Writes `DISCORD_BOT_TOKEN` to `state/.env`.
4. Asks for your Discord user id and writes the channel config with
   `group_policy: "mention"`.
5. Adds the `heartbeat`, `morning` and `evening` jobs, whichever are
   missing, delivering to `last`.
6. Prints the OAuth2 URL to invite the bot to a server.

`group_policy` controls guild channels: `mention` answers only when
@mentioned, `open` answers every message, `dm_only` never answers in a
guild. Direct messages always work for allowed users. Replies in a
guild channel are visible to everyone there, not just `allowed_users`,
which is why each guild channel gets its own conversation rather than
sharing the main one (the channel sets `group_id` on the message; the
runtime does the rest). Cron announcements only ever go to your DM,
whichever room you last spoke in. Telegram groups work the same way.

Known gap: the separate conversation is the history only. A group turn
still gets the full system prompt, which includes MEMORY.md, USER.md,
the daily logs and the carry-over and preconscious items. AGENTS.md
tells the agent not to repeat any of that in a room; nothing in the
code withholds it. If a room has people in it you would not show your
memory files to, set `group_policy: "dm_only"` (Discord) or keep the
bot out of the group (Telegram) until that is fixed.

### Search

```bash
./bin/faff setup search
docker compose restart
```

1. Lists the available search providers (currently Brave Search).
2. Copies `search_provider_brave.py`.
3. Asks for the Brave API key (brave.com/search/api) and writes it to
   `state/.env`.
4. Writes the `search` block to `state/config.json`.

After setup the `web_search` tool is live. No pip dependency, so a
restart is enough. If `search.provider` is `brave` with no `module`,
wiring tries `extensions.search_provider_brave` and then
`contrib.search_provider_brave`, so a hand-written config works
without the copy.

### Voice

```bash
./bin/faff setup voice
docker compose restart
```

1. Asks which sides to enable: transcription, synthesis, or both.
2. Asks for the API key env var name (default `OPENAI_API_KEY`; if you
   paste the key itself here it asks again), then the key if that
   variable is not already set.
3. Asks for the API base URL (default `https://api.openai.com/v1`; any
   OpenAI-compatible audio endpoint works).
4. Copies `transcriber_openai.py` and/or `synthesiser_openai.py`.
5. Asks for the model names (defaults `whisper-1`, `tts-1`, voice
   `alloy`) and writes the `voice` block.

No pip dependency. Voice notes are transcribed before the agent sees
them, marked as transcripts, and replies to voice notes are synthesised
and sent as audio alongside the text.

## Updating an extension

`faff update` (in the container) compares every deployed extension
against its `contrib/` source and reports the stale ones, plus any
whose deployed hash no longer matches what `.origin.json` recorded,
meaning the file was edited after deployment. `faff doctor` runs the
same check.

To refresh one, on the host:

```bash
./bin/faff update-extension telegram
./bin/faff update-extension brave
docker compose restart
```

The short name is the filename minus its `channel_`,
`search_provider_`, `transcriber_` or `synthesiser_` prefix and `.py`.
The command looks the file up in `.origin.json`, checks the source is
inside `contrib/`, keeps the current copy as `.bak` (`.bak2`, `.bak3`,
... on later updates), copies the new version and updates the hash.
Already up to date is reported and nothing changes. A file that did not
come from contrib cannot be updated this way.

The restart makes the running agent import the new copy. A rebuild is
only needed if the extension's pip dependency changed.

## Writing your own

1. Read the protocol in `src/faffmonkey/seams/`. Every public method
   on the protocol is required, including the ones that have a default
   body: wiring compares method names, and a missing one fails at
   startup with `missing methods: [...]`.
2. Write a module with a class that satisfies it, using the contrib
   file for the same seam as the example.
3. Put the module at the root of `extensions/`.
4. Add any pip dependency to `requirements.extra.txt` and rebuild the
   image.
5. Reference the class in `state/config.json` with a `module` field.
6. `faff doctor` to confirm it loads.

| Protocol | File | Methods |
|---|---|---|
| `Channel` | `channel.py` | `start`, `stop`, `receive`, `poll`, `send`, `is_allowed`, `is_closed` |
| `Provider` | `provider.py` | `complete` |
| `SearchProvider` | `search_provider.py` | `search` |
| `Transcriber` | `transcriber.py` | `transcribe` |
| `Synthesiser` | `synthesiser.py` | `synthesise` |

Constructor keyword arguments the runtime passes:

| Seam | Always | Only if the constructor declares it |
|---|---|---|
| Channel | `allowed_users` (list of str), `workspace` (Path) | `group_policy` (str) |
| Provider | `base_url`, `api_key`, `timeout` | `allow_insecure` (bool) |
| SearchProvider | `api_key` | |
| Transcriber | `api_key`, `base_url`, `model` | |
| Synthesiser | `api_key`, `base_url`, `model`, `voice` | |

Config for a custom channel:

```json
{
  "channels": {
    "my-channel": {
      "enabled": true,
      "allowed_users": ["123456"],
      "module": "extensions.my_channel.MyChannel"
    }
  }
}
```

`telegram` and `discord` need no `module`: the runtime maps those
names to `extensions.channel_telegram.TelegramChannel` and
`extensions.channel_discord.DiscordChannel`. A custom provider goes in
the model slot's `module` field, a custom search provider in
`search.module`, and custom voice implementations in
`voice.transcriber_module` and `voice.synthesiser_module`.

Module paths may only start with `extensions.`, `contrib.` or
`faffmonkey.seams.`; symlinks in the import path are rejected. The
runtime validates the instance against the protocol at wiring time and
crashes at startup with a clear message if anything is missing,
distinguishing a missing file from a missing dependency.
