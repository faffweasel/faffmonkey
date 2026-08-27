# faffmonkey architecture

The living description of how faffmonkey works, as built. The original
design document (SPEC v1.3) is retired; this file and the code are
authoritative.

A deliberately minimal, self-hosted personal AI agent. One agent, one
config directory, does the boring thing reliably.

## Principles

1. **Security first.** The container is the trust boundary. Secrets are
   never visible to the agent. Unconfigured permissions deny. The core
   runtime has zero external dependencies; every pip package is an
   attack vector.
2. **Upgrades just work.** The data directory is the contract. Replace
   the container, data survives. Schema changes follow
   expand-migrate-contract.
3. **Extend via seams, not plugins.** Typed interfaces for every
   swappable concern, each with a noop default, wired from config. No
   runtime discovery, no plugin manifests.

Deliberate non-goals: no multi-agent orchestration, no MCP, no web UI,
no GUI config, no skill marketplace, no vector store in core, no wake
word, no conversation undo or versioning (the user has git; the
compaction checkpoints in `state/backups/` are crash recovery, not
history).

## Directory layout and ownership

```
Checkout (git-tracked, disposable):
src/faffmonkey/     core code, stdlib-only
contrib/            reviewed reference extensions, skills, provider data
templates/          default workspace files and built-in skills

Data root ($FAFF_HOME, default ~/.faffmonkey; never in the checkout):
workspace/          the agent's entire visible world, bind-mounted rw
state/              runtime config and secrets; the agent never sees it
state/backups/      compaction checkpoints
extensions/         active extensions, user-controlled, mounted read-only
backups/            faff backup and faff update snapshots
requirements.extra.txt  extension pip deps (a build mirror stays in the checkout)
```

The split exists because a deploy replaces the checkout wholesale: on
2026-08-24 a deploy rsync deleted an install's workspace, state and the
backups stored inside state/, all of which lived in the checkout.
Inside the container the image pins FAFF_HOME=/app, where compose
mounts the host's data root, so container paths never change. On the
host, bin/faff reads FAFF_HOME from the .env beside docker-compose.yml
(config.apply_compose_env) when the shell has not set it, so the setup
wizards and compose always resolve the same data root; a second agent
is a second checkout with its own .env and data root.

- **workspace/** is the agent's filesystem: SOUL.md, IDENTITY.md,
  USER.md, AGENTS.md, HEARTBEAT.md, MEMORY.md, LEARNINGS.md, memory/,
  skills/, skills-data/, shared/ (file exchange, with inbox/ for
  inbound media), config/ (jobs.json, location.json), documents/.
- **state/** holds config.json, .env (secrets), commands.json,
  sessions.db, trusted.json, cron-state.json, logs/, backups/. File
  tools cannot reach it; shell
  can, because the container, not the path check, is the security
  perimeter.
- **contrib/** code is never loaded directly; it is copied into
  extensions/ (seam implementations, via `faff setup <name>`) or
  workspace/skills/ (skills, via `faff skill install <name>`) with
  provenance recorded in a `.origin.json` beside the copies.

## Runtime

### Message pipeline

```
Inbound:   Channel -> Access Control -> Transcriber -> Agent
Outbound:  Agent -> Secret Redaction -> Synthesiser -> Channel
```

Redaction runs before synthesis so secrets are never spoken. Voice
replies are synthesised only when the inbound message was voice, and
the text reply always accompanies the audio. A transcript is marked
`[voice note, transcribed]` and the system prompt states both facts
when voice is configured, so the agent neither hunts for an audio file
nor denies being able to send voice. Inbound images route
through the `vision` model slot when configured. Access control is a
per-channel `allowed_users` list; unknown senders are dropped silently.

Inbound images land in `workspace/shared/inbox/` and the **path**, not
the bytes, travels with the message and into sessions.db. History stays
small, the file remains addressable by the agent's file tools, and the
bytes are read and base64-encoded only when a request is built. A turn
whose request carries any image resolves the `image_understanding`
route; if none is configured it falls back to `conversation` with a
warning. At most four images are expanded per request and older ones
degrade to a text reference, so a long photo conversation cannot grow
into a megabyte of base64 per turn.

### Agent loop

`runtime/loop.py`. Synchronous. One turn: sync history with the store,
slash-command check (handled without an LLM call), persist the user
message, resolve the model slot,
then the completion loop: call the provider, dispatch any tool calls
through the registry, append results, repeat until a text-only
response. Guards: 50 tool calls per turn, 20 LLM round-trips per turn,
and two turn clocks: a 600s inactivity timeout that genuinely resets on
each provider response and each tool result, plus a 3600s absolute cap
so a turn that keeps making progress cannot run all day. Empty
responses retry up to 3 times with a nudge; before that can trigger,
the provider parser treats a response whose content is empty but whose
reasoning/reasoning_content field carries text (reasoning models over
OpenAI-compatible endpoints, e.g. Kimi via Ollama Cloud) as that
reasoning text, and logs the response shape whenever content comes back
empty so the failure is diagnosable. When a turn is cut short
mid-batch, every remaining tool call still gets an error result, since
an assistant message with unanswered tool calls is rejected by strict
providers on every later turn. On the wire every message carries a
`content` string, empty if need be, unless it is an assistant message
with tool calls: an empty string is valid on every OpenAI-compatible
endpoint, a missing key is rejected by Ollama's compat layer.

A failing turn does not take the channel with it. `run()` contains
errors per message, replies with the failure, and keeps listening, so
a provider outage is visible in the conversation rather than killing
a channel thread that Docker's restart policy cannot see.

An inbound voice message that cannot be transcribed is answered with a
note and runs no turn at all. Substituting a placeholder persisted it
as the user's own words.

### Sessions

- **Main**: the one continuous conversation, persisted in sessions.db
  (WAL), resumed on restart. Under `faff run` every channel loop keys
  it on `MAIN_SESSION_KEY`, so Telegram and Discord are two doors into
  the same history; the spec always described one MEMORY.md, one daily
  log and one greeting, and a session per channel was the one line
  that disagreed. Turns are serialised across loops by the shared
  session lock (an RLock held for the whole turn), every persist sets
  the other loops' "history dirty" event, and each loop re-syncs at the
  start of its next turn and never mid-turn, because a mid-turn reload
  replaces the session the current message was just persisted to. A
  group or guild message (`InboundMessage.group_id`, set by the channel)
  switches the loop to a `<channel>:<group>` session for that turn, so
  a reply the whole room reads never draws on the direct conversation.
  The reply carries the same `group_id` (`OutboundMessage.group_id`)
  and the channel sends it to that room; a message without one, which
  is every cron announcement, goes to the owner's direct chat, the
  only target a channel remembers across restarts. The group session
  isolates history only: the system prompt is the full bootstrap,
  memory files included, and AGENTS.md rather than code keeps it out
  of the room.
  `faff chat` keeps its own `cli` session: it is a separate process with
  no way to hear the signals.
- **Isolated**: fresh context for cron and heartbeat runs, ephemeral,
  completion-only.
- **Agent** (cron only): one ephemeral tool-capable AgentLoop turn.
  Zero persistence (sessions.db is never touched), standard loop
  budgets, `ask` permissions denied with a "not available on this
  channel" result (no human present, so no retry loop), `job.model`
  routes the turn, except that a request carrying an image goes to
  `image_understanding` whatever the override says (see cron modes
  below). The only durable artefacts are what skills write to the
  workspace.

### Model routing

Three model slots by default (`main`, `cheap`, `vision`), and routing
rules mapping tasks to slots: conversation, compaction, heartbeat,
cron_default, image_understanding. `models` is an open map, so extra
slots can be added and named by a job's `model` override. Cron jobs can
override per job. On
rate-limit, timeout, or connection errors the provider layer retries
with exponential backoff (honouring retry_after) then walks
`fallback_models`; auth errors skip retry and fall through
immediately. Local endpoints get a preflight probe (cached 5 minutes) before cron and
heartbeat runs. It is not there to save tokens: a dead local endpoint
costs nothing to call, it simply fails. It is there so an outage is
recorded as an outage. Without it every job against a stopped Ollama
becomes a logged failure with backoff, and a delivering job sends the
error to the channel; with it the run is recorded as skipped, which is
what actually happened. Remote endpoints are not probed: the probe
carries no credentials, so a 401 from a remote provider is not evidence
of an outage. Any HTTP status counts as reachable; only a transport
error with no status means down.

Cron jobs on `session: "agent"` resolve `cron_default` like every other
mode, so `"cron_default": "cheap"` covers every job without a `model`.

### Bootstrap (system prompt assembly)

`runtime/bootstrap.py` assembles, in order: SOUL.md, IDENTITY.md,
USER.md, AGENTS.md, tool summary, skill catalog (names and
descriptions only), instruction-source policy, current time and
location, MEMORY.md, LEARNINGS.md, today and yesterday's daily logs
(memory/daily/YYYY-MM-DD.md),
pending carry-over items, and the preconscious buffer. Cron and
heartbeat modes load reduced subsets.

Bootstrap tokens are counted (len/3.5 heuristic) against a hard 60%
cap of the model's context window; overflow refuses to start with a
per-file breakdown (`--allow-overflow` overrides).

Carry-over is a shared to-do list, not an outbox. Items are read at
bootstrap into every session's prompt and stay pending until the
operator or the agent resolves one with the skill's `done` action. The
runtime never marks an item done on its own: a follow-up nobody has
got to must survive into the next prompt, which is the one case the
feature exists for. The cost is that an unresolved item is paid for on
every turn until somebody says it is finished.

Pending `simmering` items promote to `normal` once they are 3 days old.
This happens in `_load_carry_over`, under the queue lock it already
holds, so the priority sort in the same pass sees the new value. The
carry-over skill's `get` action applies the same rule for the agent's
own reads; bootstrap does not depend on the agent invoking it.

### Compaction

When token usage crosses the threshold (default 0.8 of context), the
message count exceeds the hard limit (400), the user runs `/compact`,
or the provider returns a context-length error. Pipeline: checkpoint
(SQLite backup, abort compaction if it fails), an unconditional
memory-flush LLM turn (write important facts to memory files before
they are summarised away), summarise via the cheap model into
structured sections, rebuild the context with the summary plus a
protected tail (`protect_last_n`, default 20, minimum 1;
tool-call/result pairs never split). Three-tier fallback ends in
deterministic truncation, so compaction always succeeds: the routed
compaction model, then the `cheap` model **slot** if it differs, then
truncation. Tier 2 reads the slot directly rather than routing, because
`cheap` is a slot and no config has a routing task by that name.

A tool call that is still unanswered when its turn is compacted gets a
stub error result, timestamped one microsecond after the assistant
message it belongs to, because history is ordered by timestamp and the
stub must sit beside its call.

### Daily note

Recording the day is the loop's job, not the model's. After each reply,
`daily_note_due` checks the user turns since the last note (the cursor
is `sessions.daily_note_at`, the timestamp of the last message noted,
so it survives restarts and is shared by every loop on the session).
When `every_turns` have accumulated or `every_minutes` have passed
since the last note, whichever first, `daily_note` sends the user and
assistant text since the cursor to the compaction-routed model with a
single tool, `daily_note(content)`. The runtime appends the content,
prefixed with the local time, to `memory/daily/<today>.md` in the
configured timezone; the model never chooses the path and nothing is
ever overwritten. No tool call means nothing worth keeping; the cursor
still advances. Nothing else changes: no compaction, no new session,
history untouched. Idle sessions make no calls. The evening job's full
flush remains the detailed write-up; the note is what survives when it
fails.

### Goal loop

`/goal <text>` starts lightweight autonomous execution: each turn
re-injects the goal, a `GOAL_DONE` token stops the loop, a turn budget
(default 20) bounds it, and user input preempts.

## Seams and wiring

A seam is a typed `Protocol` in `src/faffmonkey/seams/` that abstracts
a swappable dependency. Every optional seam has a noop default, so the
agent runs with zero optional config. A noop that produces a *result*
lies to the model, so the search and transcriber noops raise instead:
an empty result list read as "the web has no answer", and a
transcription placeholder was persisted as the user's own words.

| Seam | Built-in | Contrib implementations |
|---|---|---|
| Channel | CLI, noop | Telegram, Discord |
| Provider | OpenAI-compatible | (provider data JSON files) |
| Transcriber | noop | OpenAI-compatible STT (stdlib) |
| Synthesiser | noop | OpenAI-compatible TTS (stdlib) |
| SearchProvider | noop | Brave |

`channels.<name>.group_policy` (`mention`, `open`, `dm_only`) is parsed
and passed to any channel whose constructor takes it, which today means
Discord. `shell_exec` runs with the inherited environment plus `WORKSPACE`, `TZ`
and `state/commands.json` entries such as `IMAGE_GEN_CMD`; those command
strings are relative to `workspace/`, which is the working directory for
both. It is **not** identical to a skill's environment: `SKILL_DATA` is
derived from a skill's own name and its directory is created by
`invoke()`, and `shell_exec` has no skill name to derive it from. A
script that requires `SKILL_DATA` must be run through `skill_invoke`.

`wiring.py` is the single file that imports concrete **seam**
implementations from config. The CLI resolves channels through the same
`_import_class`/`_validate_protocol` helpers, and the runtime imports
its own built-in classes directly; the rule is about config-driven
implementation choice, not about every import in the tree.
It reads config.json, resolves built-ins from a lookup table and
extensions via importlib from a `module` field, validates the loaded
class against the Protocol (`runtime_checkable` isinstance), and
crashes at startup with a clear message if methods are missing or an
import fails (distinguishing missing file from missing dependency).
Users never edit wiring.py.

Providers that speak the OpenAI chat-completions API are configured
purely by data: a JSON file in `contrib/providers/openai-compatible/`
(name, base_url, api_key_env, default model). Adding one is dropping a
file.

Extension dependencies go in `requirements.extra.txt`; the Docker
image installs them if present. The core image has zero pip packages.

## Skills

A skill is a directory under `workspace/skills/`: `SKILL.md` (required)
plus optional `scripts/`, `references/`, `assets/`, and a `HUMAN.md`.
Runtime data lives separately in `workspace/skills-data/<name>/`, and so does a skill's own configuration; `config/` holds only cross-skill files (`location.json`) and the restricted `jobs.json`.

**Two documents per skill.** SKILL.md is LLM-facing runtime
instruction: when to invoke, actions, how to interpret output. HUMAN.md
is human-facing: setup, configuration, architecture, maintenance. It is
never loaded into context.

**Progressive disclosure.** Tier 1: every skill's name and description
(~50 tokens) in every system prompt. Tier 2: the full SKILL.md body,
loaded on invocation. Tier 3: scripts run via subprocess and references
read on demand; never loaded implicitly.

**Invocation.** Actions map to script filenames (`index` runs
`scripts/index.py`; no name translation, so action names must match
files exactly). Scripts run via `python3` subprocess with the parent
environment plus `WORKSPACE`, `SKILL_DATA`, `TZ`, and any command-seam
entries. stdout is the tool result; `MEDIA: <path>` lines become
outbound attachments. Frontmatter can gate a skill on required env vars
or binaries; unmet requirements exclude it from the catalog. Skills are
live on creation: no enable/disable, delete the directory to remove.

**Naming rules.** A skill run by `session: "none"` cron jobs must name
its entry script `run.py`; that is the only script such jobs execute.
Scripts must never share a name with a Python stdlib module (the script
directory is sys.path[0] in its own subprocess).

**Two skill tiers.**

- **Built-ins** (`templates/workspace/skills/`, installed for everyone
  by `faff init`, reconciled by `faff update`): carry-over (a to-do
  list shared with the operator across sessions), cron-manager (jobs.json CRUD with schema
  validation), document-search (FTS5 over workspace/documents/),
  heartbeat (watchdog checks), memory-search (FTS5 plus optional
  embedding hybrid over memory files, recency-weighted, self-indexing
  on every search), morning-routine (idempotent
  daily briefing composer), preconscious (decaying top-of-mind buffer),
  self-review (LEARNINGS.md lifecycle), skill-writer (scaffold,
  validate, package new skills).
- **Contrib** (`contrib/skills/`, opt-in via `faff skill install`):
  weather, calendar, reminders, aqi, currency, timezone,
  unit-converter, digest-engine, github-deps, word-daily,
  openrouter-image-simple, venice-ai-media, weekly-state-of-me.

**Provenance and update.** Installs record source path and a directory
content hash in `workspace/skills/.origin.json`. `faff update`
auto-updates built-in skills whose installed copy is byte-identical to
what was installed (pristine) when the template has changed, warns and
leaves locally modified copies alone, and adopts pre-provenance
installs that match the current template. Contrib skills are reported
stale or modified with the `faff skill install` command to refresh
(local modifications need `--force`).

**Command seam.** Skills that provide a capability to other skills
(image generation, image editing) are wired through named commands in
`state/commands.json`, injected into skill subprocess environments on
every invocation. Keys are env-var style; runtime-owned names
(WORKSPACE, PATH, LD_*, ...) are reserved. Consumers treat an unset
command as capability-not-available and skip gracefully. Media
commands take `--prompt`/`--output` (edit adds `--input`) and print
`MEDIA: <path>`. Those paths ride the `ToolResult` out to the reply as
channel attachments, so a generated image is sent rather than merely
named. Skills declaring a `requires` block (env vars, binaries) are
filtered out of the catalog when the host does not satisfy it, instead
of being offered and then failing. A required env var counts as
satisfied when either the process environment or `commands.json`
supplies it, since the latter is merged into every skill subprocess.

## Cron

An in-process scheduler reads `workspace/config/jobs.json`. Schedules
are written in the user's timezone; candidate minutes are walked in UTC.
That is what makes the transitions work: on fall-back the repeated hour
produces distinct instants rather than comparing equal and being
suppressed, and on spring-forward the wall-clock minutes that never
happen are detected as a gap, so a job inside the lost hour still runs
at the first real instant after it.

Job fields: `id`; `schedule` (5-field cron, day-of-week accepts 7 for
Sunday) or `at` (one-shot, deleted on success only); `prompt` or
`skill`; `session`; `model` slot override; `deliver` (`announce` to a
channel, or `none`); `enabled`; `rotate_session` (main only: flush
memory and rotate the session after the run); `context: "heartbeat"`
for the two-layer heartbeat pattern.

Jobs are validated as whole shapes, not field by field: `enabled` and
`rotate_session` must be real booleans (the string `"false"` is truthy
and is what LLM-written JSON produces), `rotate_session: true` is
rejected on any session but `main`, only `session: "none"` reads
`skill`, and every other mode needs a `prompt`. A rejected job is
logged and skipped.

Session modes: `agent` (ephemeral tool-capable turn, the default, and
the only mode that can invoke skills or write files), `isolated` (fresh
context, completion-only), `main` (in the live conversation,
completion-only), `none` (no LLM; runs the skill's `run` script
directly, zero tokens).

No mode is both tool-capable and in the live conversation, and that is
deliberate. `main` is completion-only because a tool-calling AgentLoop
driven from the scheduler thread against the shared session is a
second agent in the user's conversation, and nothing in the day cycle
needs one: the morning job is `agent` (tools, no history), the evening
job is `main` (history, no tools) and its memory flush does the file
writes. What a `main` job would otherwise gain over `agent` is the
record of what it sent, and every delivering mode gets that from the
delivery step below.

`agent` resolves its slot itself and passes it to the loop, rather than
routing for `conversation`. Image turns still route through
`image_understanding`, because a slot chosen for text cannot necessarily
read a picture. Like the completion-only modes it re-prompts once on a
stale ack, and a provider that returns nothing is recorded as a failed
run rather than delivering the loop's "empty response" message as the
job's answer.

Delivered output is appended to the main session as an assistant
message tagged with the job id, and every channel loop is signalled to
reload. Without both halves the agent has no record of the message it
just sent you, so replying to a morning briefing lands in a history
with no briefing in it. `session: "main"` has already written the
exchange, so it takes the signal only; it writes the exchange only
once the reply is non-empty, after the same empty-reply retries as
`isolated`, since every row in the shared session is replayed on every
later turn. The signal is the "history dirty" event, distinct from
session rotation because the session id does not change. Both events
reach only loops in the same process, so the store is the authority on
which session is active: each loop checks it at every turn boundary,
which is how a rotation done by `faff cron run` in another process is
followed. `deliver.channel` may be `"last"`: the channel the
user most recently sent a direct message on, remembered by the
scheduler in `cron-state.json`, falling back to the first running
channel. The wizard-created jobs use it, so nothing is tied to
whichever wizard ran first.

Behaviour: exponential backoff on failure (30s to 60m, reset on
success); a deterministic 0-5 minute stagger for top-of-hour jobs
(schedule at minute 1 to bypass), applied as a deadline the tick fires
past rather than a window it has to land inside; JSONL run logs per job
in `state/logs/cron/`, timestamped in UTC and rendered in the config
timezone at display time; output of a bare `NO_REPLY` (quotes,
backticks and trailing punctuation tolerated) suppresses delivery; a stale-ack guard re-prompts once when the response looks
like "on it" rather than a result; `faff cron list|run|history` for
operations. `faff cron run` has no channels, so it prints the job's
output and says which channel the scheduler would deliver to, rather
than recording a failed delivery. Cron OR-semantics gotcha: day-of-month and day-of-week both
set matches either, not both.

A tick will look back for a fire it owes by a six-minute catch-up plus
the five-minute stagger allowance, eleven minutes in total. That covers
the stagger and a brief restart without replaying a morning's worth of
jobs after a long outage. Last-fire and backoff persist to
`state/cron-state.json`, so a restart neither re-runs a job that has
already fired nor clears an hour of backoff against a dead provider.

Failure posture: a failed run never deletes a one-shot, since
cron-manager promises they retry and a reminder destroyed by one
provider hiccup is unrecoverable. `faff cron run` never deletes one
either: testing a reminder is when you least expect to lose it. A
delivery that raises is caught, logged and recorded as a failed run,
not allowed to abort the rest of the tick. A scheduler-driven deletion
does not announce itself as a jobs.json edit. A tick stops at the next
job boundary once shutdown is signalled.

### Heartbeat

One cron job, three cost tiers. The first channel wizard (`faff setup
telegram` or `discord`) creates it, hourly and delivering to `last`,
alongside the `morning` (07:05, agent session, morning-routine skill)
and `evening` (22:00, main session, `rotate_session`) jobs, plus a
silent `preconscious-decay` pass (06:01, `session: "none"`, the skill's
daily decay script), whichever of the four are missing; `faff init`
cannot, because there is no channel to deliver to yet. The evening job's own turn has no tools: it
leaves a note of what mattered in the history, and the memory flush
inside rotation is what writes the files. A no-tools completion
(isolated, main, heartbeat escalation) that emits tool-call syntax
as text is re-prompted once and errors rather than delivering raw
XML to the channel. A `context: "heartbeat"` run first
invokes the heartbeat skill's watchdog (zero tokens: file existence,
timestamps and thresholds, written to
`skills-data/heartbeat/triggers.json`), then reads it. On `attention` it
skips straight to a full run with the trigger context and HEARTBEAT.md
in the prompt (the cron bootstrap does not load the file, and without
it a missed morning produced the missed-morning line and nothing the
checklist asked for); otherwise a cheap gate evaluates HEARTBEAT.md (~100 tokens; `NO_REPLY` ends the run) and
only substantive findings escalate. Escalation frames the gate's answer neutrally (compose what the user should hear, plainly) rather than as an alert, which inflated one-line gate answers into themed monologues. The gate's system prompt frames
the file as things to watch plus standing instructions that are acted
on every time; asked only whether anything "needs attention", it
answered NO_REPLY to "always report the current time". Missing or empty HEARTBEAT.md means
no model call at all.

The watchdog runs inline rather than as its own cron job, so it cannot
drift out of step with the heartbeat that reads it; a separate
`session: "none"` job is still supported for anyone who wants it on a
different schedule. The gate is routed by `heartbeat` and escalation by
`cron_default`, because detecting that something is worth saying and
composing what to say are different jobs, and only the gate runs on
every heartbeat.

## Memory

- **MEMORY.md**: condensed index, target ~50 lines, agent-written
  into the template's skeleton (key facts, active projects, key
  people, lessons). Not the source of truth.
- **Daily logs** (`memory/daily/YYYY-MM-DD.md`): the detailed record; today
  and yesterday auto-load into context. Written by the loop's daily
  note (see Compaction) and the memory flush; the agent writes to them
  directly only when asked.
- **Memory flush** (before compaction, on `/new`, on session rotation):
  the history is sent with a single `file_write` tool, described as
  append-only and as the only tool in the step, and the instruction to
  reply `NOTHING_TO_SAVE` when there is nothing worth keeping. A reply
  that is neither (prose, or a tool the history shows but the step does
  not offer) is corrected once, naming what the model did; a model
  that answers wrongly twice hands over to the `compaction` slot. The
  outcome is saved, nothing or failed; only failed makes compaction
  preserve the head as a blob or `/new` report that memory was not
  saved.
- **Person and project files** (`memory/person/`, `memory/project/`):
  created as needed, found via memory-search, not auto-loaded.
- **sessions.db**: conversation history; disposable without losing
  identity.

The user's direct statement in conversation always beats memory files;
files are corrected, they do not arbitrate.

## Security model

Defence in depth, from the outside in:

1. **Container boundary** is the primary perimeter; all tool execution
   happens inside it.
2. **Zero-dependency core**: no supply chain surface in the runtime.
3. **Workspace restriction**: file tools resolve-then-prefix-check
   paths, reject traversal and symlinks (writes use O_NOFOLLOW).
4. **Shell controls**: default `ask`, glob pre-approval list, approval
   bound to the exact command hash with 300s expiry, TOCTOU hash
   verification of referenced files, and a hardline blocklist checked
   before all other logic with no override. Commands are decomposed
   across pipes, chains, and substitutions before matching.
5. **Protected paths**: writes to `state/.env`, `state/config.json`
   and `config/jobs.json` are refused by the file tools, with a result
   naming the route instead: the cron-manager skill for jobs.json, the
   operator by hand for `state/`. There is no approval flow, and the
   result says so; "confirm with the user" had the agent asking, being
   told yes, and failing again. The identity files (SOUL.md,
   IDENTITY.md, USER.md, AGENTS.md, HEARTBEAT.md) and `skills/` are the
   agent's own and are writable: it creates skills with skill-writer
   and fills in what the scaffold leaves, and customises installed
   ones (a modified built-in is left alone by `faff update`, which
   says so). The rule about asking before rewriting SOUL.md or
   IDENTITY.md lives in AGENTS.md, not in code. Because identity files
   and SKILL.md are loaded unfiltered (item 6), the agent writing them
   is the one path by which injected content could persist; `chmod
   444` is the operator's opt-in if that trade is not wanted. The
   memory flush, which has no human in the loop, is separately barred
   from `skills/`, `config/` and `extensions/`.
6. **Trust system**: workspace files loaded into context are verified
   against SHA-256 hashes in `state/trusted.json` (`faff trust`).
   Identity files are always-trusted and loaded verbatim; everything
   else is untrusted by default and wrapped (item 7) until the
   operator trusts it. Since the agent can write the identity files,
   always-trusted means "the agent and the operator are the only
   authors", which AGENTS.md is what enforces.
7. **Injection pipeline** (`runtime/ingest.py`): untrusted content is
   stripped of invisible characters, homoglyph-normalised, scanned for
   injection patterns (blocked with a reason on match), and wrapped in
   nonce-tagged `<untrusted>` blocks the agent is instructed to treat
   as data. The bootstrap injects an explicit instruction-source
   policy naming which sources may instruct the agent.
8. **SSRF protection** on web_fetch: scheme and port restrictions, DNS
   resolution with private/reserved range blocking, connection pinning
   against rebinding, redirects blocked, cloud metadata hostnames
   blocked.
9. **Outbound redaction** (`runtime/redaction.py`): known API key,
   token, and JWT patterns replaced with `[REDACTED]` on all channel
   output.
10. **Deny by default**: unconfigured tools are `never`; unknown
    senders are dropped.

Skill and shell subprocesses deliberately inherit the full environment
including API keys: skills need the keys to function, and in-process
env partitioning cannot contain a hostile process that can read
`/proc`. The load-bearing controls are that secrets never land in
workspace-readable files and outbound content is redacted. The
container's egress is the real limit on exfiltration.

## Tools

| Tool | Default | Notes |
|---|---|---|
| file_read / file_list / file_write / file_edit | always | workspace-only; file_list is one directory, non-recursive, symlinks skipped; post-write lint (.py/.json/.toml/.yaml, informational) |
| web_search | always | via SearchProvider seam; errors if unconfigured |
| web_fetch | always | 50KB cap, 30s timeout, SSRF-guarded, no redirects |
| shell_exec | ask | 600s timeout, cwd=workspace, blocklist first |
| skill_invoke | always | loads SKILL.md, optionally runs an action script |

## Configuration

- **state/config.json**: timezone, model slots and routing,
  fallback_models, channels (with allowed_users), heartbeat block,
  compaction parameters, daily_note interval, search and voice seam
  config, tool permissions and shell_preapproved. Validation lives in `config.py`
  and is strict: bad api_key_env naming, insecure base_urls, wrong
  types, and security-typo'd keys are ConfigErrors.
- **state/.env**: secrets only, loaded as environment variables.
- **state/commands.json**: the command seam (see Skills).
- **workspace/config/jobs.json**: cron jobs (see Cron).

`faff doctor` validates all of it and doubles as an onboarding guide.
It reports cron jobs the scheduler refuses to load (`faff cron list`
cannot tell a broken file from an empty one) and repairs a
`schema_version` table left without a row by an interrupted first run.

`faff update` snapshots, checks the database schema version (no
migrations exist yet; the first schema change introduces them
following expand-migrate-contract), syncs templates and
built-in skills, and flags stale contrib copies. Damage it finds after
the snapshot (an unreadable database, a symlinked `workspace/`) is a
reported skipped step, not an abort. Extension backups are versioned
(`.bak`, `.bak2`, ...) so repeated updates never refuse. `faff export`
gets conversation history out as portable JSON.

`faff init` is what both `faff doctor` and the provider check recommend
for a broken config, so it survives one: an unparseable or non-object
`config.json` is reported with the parse error and the line it failed
on, kept as `config.json.corrupt`, and recreated from defaults. Note
what that does and does not mean. It does not merge or salvage a
damaged file: a config that fails to parse loses whatever was in it
beyond the timezone, and the `.corrupt` copy is the only record. Fixing
the file by hand is usually the better move, and the printed parse
error exists to make that possible. A re-run offers the **configured** timezone as the default,
not the machine-detected one, which inside the container is always
UTC.

A misconfiguration reaches the operator as one line and exit 1. Pass
`--debug` for the traceback.

## Operational surface

CLI: `faff init`, `faff setup provider|search|telegram|discord|voice`,
`faff skill install|list`, `faff chat`, `faff run`, `faff status`,
`faff doctor`, `faff cron list|run|history`, `faff trust|untrust`,
`faff backup`, `faff restore`, `faff update`, `faff update-extension`,
`faff export`.

In-session slash commands (handled by the runtime, no LLM call):
`/goal`, `/new`, `/clear`, `/model`, `/compact`, `/status`, `/skill`,
`/cron`, `/help`. They work on every channel; the Telegram channel also
registers them as the bot's command menu at startup, replacing
whatever a reused bot had before, and strips the `@botname` suffix
groups append.

`/status` reports routing (task to model and slot), the session id and
its message count, token usage accumulated by the loop this process,
and a one-line cron health summary (runs in the last 24h and any
failures). It reads cron logs through `scheduler.recent_cron_runs`, the
inverse of `_log_run`, which `faff status` also uses for its longer
table. The channel user gets the health line because they have no
terminal; the full run history stays in the CLI.

Docker is required for production: multi-stage build, digest-pinned
base, tzdata, BuildKit, log rotation caps, and the bind mounts
from the data root (workspace rw, state rw, extensions read-only). The process runs as
an unprivileged user (uid 1000, overridable via `FAFF_UID`/`FAFF_GID`
in compose) so files it writes to the mounts belong to the operator,
not root. `faff` is on PATH in the image and there is no entrypoint, so
`docker compose run` and `docker compose exec` both take the same
`faff <command>` the docs show.
