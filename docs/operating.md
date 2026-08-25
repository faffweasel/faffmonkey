# Operating faffmonkey

Day-to-day use once the install in [installation.md](installation.md)
is done: running it, talking to it, cron and heartbeat, upgrading,
backups, and what is yours versus the repo's.

Commands shown as `docker compose run --rm faffmonkey faff ...` run in
the container. The ones shown as `./bin/faff ...` must run on the host,
because they write to `extensions/`, which the container mounts
read-only.

## Running

```bash
docker compose up -d                      # start
docker compose logs -f faffmonkey         # watch
docker compose restart                    # after config.json or extension changes
docker compose up -d                      # after a state/.env change (restart keeps the old environment)
docker compose down                       # stop
```

`faff run` (what the container runs) starts every enabled channel and
the cron scheduler in one process. `faff chat` is a separate,
terminal-only session: it shares the workspace and memory, but cron and
heartbeat deliveries only ever reach the channels `faff run` started.
If you talk to the agent in `faff chat`, nothing scheduled will show up
there.

`faff run` exits when no channel is configured or a channel fails to
start, and compose restarts it forever. `docker compose logs --tail=30
faffmonkey` shows the one-line reason.

## Command reference

| Command | What it does |
|---|---|
| `faff init` | Create `workspace/`, `state/`, templates, config. Safe to re-run; never overwrites identity files |
| `faff setup provider` | Configure the LLM provider and the `main`, `cheap`, `vision` slots |
| `./bin/faff setup telegram\|discord\|search\|voice` | Install a contrib extension and write its config (host only) |
| `faff chat` | Interactive terminal session |
| `faff run` | Channels plus scheduler; what the container runs |
| `faff status` | Model slots, active goal, last heartbeat, last ten cron runs |
| `faff doctor` | Check everything; exits non-zero on a red |
| `faff cron list` | Jobs with next fire time |
| `faff cron run <id>` | Run a job now and print its output. Never delivers to a channel |
| `faff cron history <id>` | Last runs of a job with status and duration |
| `faff trust status` | Which workspace files are trusted, untrusted, or always trusted |
| `faff trust <path>` / `faff untrust <path>` | Trust or untrust a workspace file or directory |
| `faff skill install <name>` / `faff skill list` | Optional skills from `contrib/skills/` (weather, reminders, calendar, ...; list in [skills.md](skills.md)) |
| `faff export [--session ID] [--format json\|openai] [--output FILE]` | Conversation history as JSON |
| `faff backup` | Tarball of the whole data root into `$FAFF_HOME/backups/` |
| `faff restore <snapshot> [--force]` | Replace the data root from a tarball |
| `faff update` | Snapshot, schema check, template and skill sync, stale-extension report |
| `./bin/faff update-extension <name>` | Refresh one extension from `contrib/` (host only) |

`faff --debug <command>` shows a traceback instead of the one-line
error.

## Talking to the agent

### Channels

Only senders in a channel's `allowed_users` get a reply; everyone else
is dropped silently with a log line. Direct messages always work for
allowed users. In Discord guild channels the `group_policy` applies:
`mention` (default) answers only when the bot is @mentioned, `open`
answers everything, `dm_only` never answers in a guild. Replies in a
guild channel are visible to everyone there.

Voice notes are transcribed before the agent sees them and the reply
comes back as audio as well as text (when voice is set up). Photos are
saved to `workspace/shared/inbox/` and passed to the `vision` slot.
Long replies are split to fit the channel's message limit.

Telegram and Discord are two doors into one conversation: ask on one,
continue on the other. One turn runs at a time across both. Messages
in a Telegram group or a Discord guild channel are the exception: each
room gets its own conversation, because everyone in the room reads the
reply. `faff chat` is separate from all of it.

### Slash commands

Handled by the runtime without a model call, on every channel and in
`faff chat`. Telegram registers them as the bot's command menu at
startup.

| Command | Effect |
|---|---|
| `/help` | List these |
| `/status` | Routing, session id and message count, token usage this process, cron health (runs in the last 24h, failures) |
| `/new` | Save memory, then start a new session |
| `/clear` | New session without the memory flush |
| `/model` | Show slots and routing; `/model <slot> <model>` switches the model, `/model <slot> <provider> <model>` also switches provider (live, persisted to `state/config.json`; the provider's API key must already be in the environment, otherwise it says so and changes nothing) |
| `/compact` | Compact the context now |
| `/skill <name> [action] [args]` | Read a SKILL.md, or run an action directly |
| `/cron` | List cron jobs with status and next fire time; `/cron history <job-id>` shows recent runs. No model call, so it works even when the model is what you are debugging |
| `/goal <text>` | Start an autonomous goal; `/goal status` checks, `/goal stop` ends it |

### Sending files to the agent

`workspace/documents/` is the exchange directory: the agent puts
anything it writes for you there (AGENTS.md tells it to), anything you
drop there is readable by its file tools, and the document-search
skill indexes it. Channel uploads land in `workspace/shared/inbox/`.

## Cron

Jobs live in `workspace/config/jobs.json`, a JSON array. The scheduler
inside `faff run` re-reads it when it changes, so edits take effect
without a restart.

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Lowercase, hyphens |
| `schedule` or `at` | one of | 5-field cron in your timezone, or a one-shot `YYYY-MM-DD HH:MM` that deletes itself after a successful run |
| `prompt` or `skill` | one of | `skill` only with `session: "none"` |
| `session` | no | `agent` (default, tools, most expensive), `isolated` (fresh context, no tools), `main` (in the live conversation, no tools), `none` (runs the skill's `run.py`, no model) |
| `model` | no | A slot name; default `routing.cron_default` |
| `deliver` | no | `{"mode": "announce", "channel": "telegram"}`, or `"channel": "last"` for wherever you last spoke, or `{"mode": "none"}` |
| `enabled` | no | `true` by default |
| `rotate_session` | no | `main` only; start a fresh session after the run |
| `context` | no | `"heartbeat"` for the heartbeat job |

The first channel wizard creates three jobs, the day's skeleton:

| Job | When | What |
|---|---|---|
| `heartbeat` | hourly | Checks `HEARTBEAT.md`, speaks only if there is something to say (below) |
| `morning` | 07:05 | Runs the morning-routine skill: carry-over, preconscious buffer, overnight skill output, a greeting |
| `evening` | 22:00 | Reviews the day inside the live conversation, then the memory flush writes `MEMORY.md`, today's daily log and person/project files, and the session starts fresh |

All three deliver to `last`. Edit or remove them like any other job;
the wizard never recreates one that exists, and never touches one you
wrote with the same id.

Three ways to manage jobs: ask the agent (it uses the cron-manager
skill: `list`, `add`, `update`, `remove`, `enable`, `disable`,
`history`), run the skill yourself with `/skill cron-manager ...`, or
edit the file. `faff doctor` reports jobs the scheduler would refuse.

Behaviour worth knowing:

- Output that is exactly `NO_REPLY` is never delivered.
- Top-of-hour jobs get a 0-5 minute stagger; schedule at minute 1 to
  avoid it.
- After a failure a job backs off (30s doubling to 60m) and a one-shot
  stays in the file until it succeeds.
- A restart replays at most the last eleven minutes of missed fires.
- `faff cron run` runs the job and prints the result; it says which
  channel the scheduler would have delivered to and sends nothing.
- A job can only deliver to a channel that `faff run` started. An
  `announce` job pointing at a channel that is not running logs an
  error and its output goes nowhere.

## Heartbeat

The heartbeat is one cron job (`id: heartbeat`, hourly, `context:
"heartbeat"`) that the first channel wizard creates alongside `morning`
and `evening`. It is a check, not a ping: every hour it reads
`workspace/HEARTBEAT.md` and only messages you when there is something
to say.

What happens on a tick, in order:

1. Skipped if `heartbeat.enabled` is false or the hour is outside
   `heartbeat.active_hours` (both in `state/config.json`). Recorded as
   `skipped` with the reason.
2. The watchdog script runs (no model call). If it flags something
   (stale carry-over, missing daily file), the agent gets a full run
   with that context.
3. Otherwise, if `HEARTBEAT.md` is missing or empty, the tick ends
   with no model call and nothing sent.
4. Otherwise the `heartbeat` route (default slot `cheap`) is asked
   whether anything in `HEARTBEAT.md` needs attention. `NO_REPLY` ends
   the tick. Anything else is handed to the `cron_default` route
   (default `main`) to compose the message, which is delivered to the
   job's `deliver.channel`.

So "the heartbeat never messages me" is usually one of:

- **Empty HEARTBEAT.md.** Put the things you want watched in it.
- **Wrong channel.** A wizard-created job delivers to `last`, the
  channel you most recently sent a direct message on; a job that names
  a channel delivers there. Check with `faff cron list`; change it by
  asking the agent to `update heartbeat '{"deliver": {"mode":
  "announce", "channel": "last"}}'` or editing `jobs.json`.
- **Outside active hours.** `faff cron history heartbeat` shows
  `skipped outside-active-hours`.
- **The gate said NO_REPLY.** `faff cron run heartbeat` shows exactly
  what the gate answered. The gate is told that a line asking it to
  report something is acted on every time, but a job created before
  23 August 2026 carries the older prompt ("Check HEARTBEAT.md items.
  If nothing needs attention, respond with NO_REPLY"), under which a
  standing instruction reads as nothing needing attention. Replace it:

  ```
  /skill cron-manager update heartbeat {"prompt": "Go through HEARTBEAT.md. If any line asks you to report something now, or anything on it needs attention, write what the user should hear. Only if there is nothing to say, respond with exactly NO_REPLY."}
  ```

- **`faff run` is not up.** No rows in `faff cron history heartbeat`
  since the last start means the scheduler never reached a tick.

The agent can edit `HEARTBEAT.md` itself, so "keep an eye on X" in
conversation is enough to add a line.

## Identity files and memory

`SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md` and `HEARTBEAT.md` in
`workspace/` are the agent's own. It can read and write all five;
`AGENTS.md` tells it to update `USER.md`, `AGENTS.md` and
`HEARTBEAT.md` as it learns, and to ask before rewriting `SOUL.md` or
`IDENTITY.md`. They are loaded into the system prompt without any
filtering, which is why the same rule tells it never to copy web or
document content into them. If you would rather the two core files
were yours alone, `chmod 444 workspace/SOUL.md workspace/IDENTITY.md`;
the container runs as the files' owner, so this is a speed bump, not a
wall.

Memory is `workspace/MEMORY.md` (a short index), `memory/daily/` (one
file per day; today and yesterday load into every session), and
`memory/person/`, `memory/project/` (found via the memory-search
skill). Everything you say in conversation beats what the files say,
and the agent corrects the files rather than arguing with them.

How a day gets recorded: the agent appends to today's daily log as
things happen (`AGENTS.md` tells it to); the `evening` job reviews the
day and the memory flush that follows writes anything missed to the
daily log, `MEMORY.md` and person or project files, then starts a fresh
session; the `morning` job creates the next day's log and reads
carry-over and memory for the greeting. A compaction mid-day runs the
same flush. If yesterday has no daily log, one of those did not run:
`faff cron history evening` and `morning` say which.

Memory files are loaded as untrusted data unless you vouch for them:
`faff trust MEMORY.md` (paths are workspace-relative; a trailing `/`
trusts a directory) records a hash, `faff trust status`
shows the state, and a trusted file that changes goes back to
untrusted. The identity files are always trusted and never tracked.

## Upgrading

The code runs from the image, not the bind mounts, so nothing changes
until the image is rebuilt:

```bash
git pull
./bin/faff update
docker compose build
./bin/faff update-extension telegram      # for each extension update reports as stale
docker compose up -d
```

`faff update` also works via `docker compose run` for routine updates;
the one-time data-root migration and the build-mirror refresh need the
host, so `./bin/faff update` is the canonical form.

`faff update`, in order: move a pre-release in-checkout install to the
data root (one time, with confirmation, container stopped); snapshot
the data root to `$FAFF_HOME/backups/` (keeps the last five); check the
database schema version; copy any new workspace templates and built-in
skills, and replace built-in skills you have not modified when their
template changed (modified ones are reported, never touched); compare
deployed extensions and installed contrib skills against `contrib/` and
list what is stale; refresh the checkout's `requirements.extra.txt`
build mirror.

`./bin/faff update-extension <name>` copies the current `contrib/`
module over the deployed one, keeping the old copy as `.bak`
(`.bak2`, `.bak3`, ...). A restart makes the agent import it; a
rebuild is only needed if the extension's pip dependency changed.

Customise compose with a `docker-compose.override.yml` (merged
automatically, ignored by git) rather than editing
`docker-compose.yml`, so a pull never conflicts.

## Backup and restore

```bash
./bin/faff backup
./bin/faff restore <name>.tar.gz
```

`faff backup` writes `$FAFF_HOME/backups/<timestamp>.tar.gz` (0600)
covering the whole data root: a safe copy of `sessions.db`, the rest of
`state/`, `workspace/`, `extensions/` and `requirements.extra.txt`. It
does not rotate; only `faff update` prunes to the last five. `backups/`
sits outside everything it protects, and on-disk is still one disk:
copy snapshots off the machine.

`faff restore` replaces the data root's contents (except `backups/`)
with the tarball's. The current data is snapshotted first unless you
pass `--force`. Old state-only tarballs from before the data root
existed restore into `state/`. A name that does not exist lists the available snapshots. Run
`faff doctor` afterwards.

## What is yours and what is the repo's

Tracked by git: `src/`, `templates/`, `contrib/`, `bin/`, `tests/`,
`docs/`, `Dockerfile`, `docker-compose.yml`.

Yours, outside the checkout in `$FAFF_HOME` (default `~/.faffmonkey`),
untouchable by anything that replaces the checkout:

```
workspace/              The agent's world: identity files, memory, skills, documents
state/                  config.json, .env, sessions.db, trust store, cron state, logs
extensions/             Deployed extension modules
requirements.extra.txt  Extension pip dependencies (source of truth)
backups/                faff backup and faff update snapshots
```

Still in the checkout, gitignored: the `requirements.extra.txt` build
mirror, `.env` (compose variables: FAFF_UID, FAFF_GID, FAFF_HOME) and
`docker-compose.override.yml`.

What the agent can reach: its file tools are confined to `workspace/`
and refuse `state/.env`, `state/config.json` and
`workspace/config/jobs.json` (the cron-manager skill is the route to
the last). `shell_exec` is confined only by the container, which is the
real boundary.

| Host path | Container path | Mode |
|---|---|---|
| `$FAFF_HOME/workspace` | `/app/workspace` | read-write |
| `$FAFF_HOME/state` | `/app/state` | read-write |
| `$FAFF_HOME/extensions` | `/app/extensions` | read-only |
