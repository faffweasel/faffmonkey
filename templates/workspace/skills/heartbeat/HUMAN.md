# heartbeat: setup and notes

Ambient monitoring on one hourly cron job: a free deterministic
watchdog, then a cheap model check over a checklist you control.

## How it works

Every tick of the `heartbeat` job, in order:

1. **Watchdog** (this skill's `run` action, no model call). Checks file
   existence, timestamps and thresholds and writes
   `skills-data/heartbeat/triggers.json` with status `clean` or
   `attention`. Fixes the simple cases itself (creates a missing daily
   memory file). Default checks: yesterday's memory file exists, the
   morning routine ran before the deadline, carry-over items are not
   stale, LEARNINGS.md is under its entry threshold.
2. **Attention?** If the status is `attention`, the gate is skipped and
   the agent gets a full run with `HEARTBEAT.md` and the triggers in the
   prompt.
3. **Gate** otherwise. `HEARTBEAT.md` goes to the cheap model with the
   job's prompt. `NO_REPLY` ends the tick (about 100 tokens); anything
   else is handed to the main model to compose the message, which is
   delivered to the job's channel.

The watchdog is run by the heartbeat itself; there is no separate job
unless you add one (see below).

## Setup

Edit `workspace/HEARTBEAT.md` to define what the agent watches for:

```markdown
# Heartbeat

Check these periodically:
- Any carry-over items simmering for 3+ days? Surface them.
- No user contact in 48 hours? A single gentle check-in.
```

Thresholds live in `skills-data/heartbeat/config.json` (all optional,
defaults shown):

```json
{
  "morning_deadline_hour": 8,
  "learnings_max_entries": 30,
  "carryover_stale_days": 7
}
```

Cron jobs in `workspace/config/jobs.json`. `faff setup telegram` and
`faff setup discord` add the heartbeat job for you (with the morning
and evening jobs); this is what they write, for reference or for
editing the schedule:

```json
[
  {
    "id": "heartbeat",
    "schedule": "0 * * * *",
    "prompt": "Go through HEARTBEAT.md. If any line asks you to report something now, or anything on it needs attention, write what the user should hear. Only if there is nothing to say, respond with exactly NO_REPLY.",
    "context": "heartbeat",
    "session": "isolated",
    "model": "cheap",
    "deliver": {"mode": "announce", "channel": "last"}
  }
]
```

Optional: a separate watchdog job, only if you want the checks on a
different schedule from the heartbeat. It costs nothing (`session:
none`) and writes triggers.json for the next heartbeat tick to read:

```json
{
  "id": "watchdog",
  "schedule": "*/30 * * * *",
  "skill": "heartbeat",
  "session": "none",
  "deliver": {"mode": "none"}
}
```

## Notes

- The watchdog never sends notifications; it only writes triggers.json
  for the heartbeat run to read.
- Keep HEARTBEAT.md short. Every line is evaluated on every heartbeat,
  so the checklist length sets the idle cost.
