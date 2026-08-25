---
name: cron-manager
description: Schedule, list, and manage recurring tasks and one-shot reminders. Use when the user says 'every morning', 'remind me on Tuesday', 'check X daily', or wants to automate any periodic task. Also use to list, change, disable, re-enable, or remove existing jobs, and to check whether a job has been running.
actions: list, add, update, remove, disable, enable, history
---

## When to use

Use when the user expresses scheduling intent: "every morning", "remind me on Tuesday", "check X daily", "run this weekly". Also use to list, change, disable, re-enable, or remove existing jobs, and `history` when the user asks whether a job fired, why nothing arrived, or what went wrong.

Do not use for running jobs manually; that is `faff cron run <jobId>` on the host.

**Cron vs heartbeat:** cron is precise, heartbeat drifts. A task at a specific time ("7am briefing", "Friday wrap-up") is cron. An ambient check with no fixed time ("glance at this every so often") is heartbeat territory.

## Actions

**list**, show all jobs with id, schedule, session mode, enabled status, next fire time, and prompt/skill summary:

```
list
```

**add**, validate a job JSON object and append it to the schedule. Rejects duplicate ids, bad cron expressions, and invalid field combinations:

```
add '{"id": "...", "schedule": "...", "prompt": "...", ...}'
```

**update**, change fields on an existing job in place. Pass the id and a JSON object with only the fields to change; a `null` value removes a field. The result is validated like `add`. The id cannot be changed. Use this to change a schedule, prompt, model or delivery channel; never disable-and-add:

```
update heartbeat '{"schedule": "*/30 * * * *"}'
update heartbeat '{"deliver": {"mode": "announce", "channel": "telegram"}}'
```

**remove**, delete a job by id:

```
remove old-reminder
```

**disable** / **enable**, toggle a job by id without deleting it:

```
disable daily-summary
enable daily-summary
```

**history**, the last runs of a job, newest first, with status, duration and the reason for any skip or error. Optional limit, default 10, max 50. The scheduler runs inside the same process as this conversation, so no rows means the job has never come due since the agent started; `skipped` rows name the cause (`outside-active-hours`, `heartbeat-disabled`, `empty-heartbeat-file`); `success` with nothing delivered means the job produced no output or answered NO_REPLY:

```
history heartbeat
history heartbeat 25
```

## Job fields

| Field | Required | Default | Description |
|---|---|---|---|
| `id` | Yes | | Unique. Lowercase, hyphens, no spaces. |
| `schedule` | One of `schedule`/`at` | | Cron expression (5 fields: min hour dom month dow), user timezone. |
| `at` | One of `schedule`/`at` | | One-shot datetime (`YYYY-MM-DD HH:MM`), user timezone. Auto-deletes on success. |
| `prompt` | One of `prompt`/`skill` | | Instruction for the agent. Required for `isolated`, `main`, and `agent` sessions. |
| `skill` | One of `prompt`/`skill` | | Skill to run directly. Required for `none` sessions. The skill must have a `run` script; that is what a `none` session executes. |
| `session` | No | `agent` | See session modes below. Set it explicitly: the default is tool-capable and costs more than `isolated`. The `main` session is the one live conversation shared by every channel. |
| `context` | No | (none) | Set to `"heartbeat"` for the two-layer heartbeat pattern: reads the watchdog triggers file, runs a cheap gate over HEARTBEAT.md, and only escalates on a substantive finding. See the heartbeat skill. |
| `model` | No | `routing.cron_default` | Any model slot configured in `state/config.json`, commonly `main` or `cheap`. Slots are user-defined, so this is not a fixed list. Ignored for `none`. |
| `deliver` | No | `{"mode": "announce"}` | See delivery modes below. |
| `enabled` | No | `true` | Set `false` to disable without deleting. |
| `rotate_session` | No | `false` | `main` sessions only: start a fresh main session after the job completes. |

**Session modes:**

| Mode | Behaviour | When to use |
|---|---|---|
| `isolated` | Fresh context, cron bootstrap, single completion. No tools. | Independent runs: summaries, checks, reminders. |
| `main` | Runs in the active main session with full conversation context. No tools. Output appears in the conversation. | Jobs that must reference the ongoing conversation. |
| `agent` | Ephemeral tool-capable session: can invoke skills, read and write files, run shell. Fresh context, nothing persisted, permission prompts auto-denied, standard loop budgets apply. `model` routes the whole turn. | Default. Any job that needs tools or skill invocation: composing from skill output, writing files, multi-step work. |
| `none` | No agent, no LLM call. Runs the skill's `run` script directly via subprocess. Zero tokens. Fails if the skill has no `run` script. | Watchdogs and data collection with no reasoning. |

Only `agent` can use tools. If the prompt asks the agent to invoke a skill or touch files, the session must be `agent`; in `isolated` or `main` it will have no tools and can only answer from context.

**Delivery modes:**

| Mode | Behaviour |
|---|---|
| `announce` | Send output to a channel. Requires `deliver.channel`: a channel name (`"telegram"`) or `"last"`, the channel the user most recently spoke on. Prefer `"last"` unless the user names a channel. |
| `none` | Internal only; output is logged, nothing sent. |

If the user is meant to read what the job produces (a reminder, a daily word, a briefing, a report), it must be `announce`. `none` is for jobs whose product is a file or a data update: watchdogs, indexers, the evening memory wrap. A job that ran "successfully" with `none` and a user asking why nothing arrived is the sign you picked wrong; `update` the deliver block.

If the agent's output is exactly `NO_REPLY`, delivery is suppressed even in announce mode. Use for conditional delivery: "Only respond if something needs attention, otherwise reply with NO_REPLY."

## Examples

Daily summary (isolated, cheap model, announced):

```
add '{"id": "daily-summary", "schedule": "0 7 * * *", "prompt": "Check AQI for Hanoi and summarise top posts on r/LocalLLaMA.", "session": "isolated", "model": "cheap", "deliver": {"mode": "announce", "channel": "telegram"}}'
```

One-shot reminder (auto-deletes after success):

```
add '{"id": "visa-reminder", "at": "2026-05-15 09:00", "prompt": "Remind me about the visa appointment tomorrow.", "session": "isolated", "deliver": {"mode": "announce", "channel": "telegram"}}'
```

Tool-capable job (agent session, invokes skills):

```
add '{"id": "morning-weather", "schedule": "0 7 * * *", "prompt": "Run weather advice and fold anything notable into a short greeting.", "session": "agent", "model": "cheap", "deliver": {"mode": "announce", "channel": "telegram"}}'
```

Watchdog (no LLM, runs the skill's `run` script):

```
add '{"id": "heartbeat-watchdog", "schedule": "*/30 * * * *", "skill": "heartbeat", "session": "none", "deliver": {"mode": "none"}}'
```

Evening wrap (main session, internal, rotates session):

```
add '{"id": "evening-wrap", "schedule": "0 22 * * *", "prompt": "Review today'\''s conversations and update MEMORY.md with anything worth remembering.", "session": "main", "model": "cheap", "deliver": {"mode": "none"}, "rotate_session": true}'
```

## Limitations

- **Cron OR-semantics gotcha:** when both day-of-month and day-of-week are non-wildcard, standard cron matches either, not both. `0 9 15 * 1` fires on the 15th AND every Monday. Keep one of the two fields as `*`.
- **Top-of-hour stagger:** jobs at minute 0 get a random 0-5 minute offset to avoid thundering-herd. For exact timing, schedule at minute 1 (`1 7 * * *`).
- **One-shot retry:** a failed `at` job stays in the schedule and retries with backoff; it only auto-deletes on success.
- All times are interpreted in the user's timezone with DST handled correctly.
