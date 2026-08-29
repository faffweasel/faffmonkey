# heartbeat: setup and notes

Ambient monitoring on one hourly cron job: a free deterministic
watchdog, then a cheap model check over a checklist you control.

## How it works

Every tick of the `heartbeat` job, in order:

1. **Watchdog** (this skill's `run` action, no model call). Checks file
   existence, timestamps and thresholds and writes
   `skills-data/heartbeat/triggers.json` with status `clean` or
   `attention`. Fixes the simple cases itself (creates a missing daily
   memory file). Checks: yesterday's memory file exists, the morning
   routine ran before the deadline, LEARNINGS.md is under its entry
   threshold. Each trigger is raised once per day.
2. **Attention?** If the status is `attention`, the gate is skipped and
   the agent gets a full run with `HEARTBEAT.md` and the triggers in the
   prompt.
3. **Gate** otherwise. `HEARTBEAT.md` goes to the cheap model with the
   job's prompt. `NO_REPLY` ends the tick (about 100 tokens); anything
   else is handed to the main model to compose the message, which is
   delivered to the job's channel.

Neither the gate nor the composing step has tools. They see
`HEARTBEAT.md`, the current time and (on `attention`) the triggers,
and nothing else. The watchdog is run by the heartbeat itself; there is
no separate job unless you add one (see below).

## Setup

Edit `workspace/HEARTBEAT.md`. A line belongs there only if it can be
answered from the file and the clock:

```markdown
# Heartbeat

- If the watchdog reports a missed morning, say so in one line, no lecture.
- Between 12:00 and 13:00, remind me to stand up and stretch.
- On Fridays between 17:00 and 18:00, ask whether the weekly wrap is done.
```

The heartbeat is hourly, so a line with a time window fires on every
tick inside it; keep windows to an hour unless you want repeats.

## Watching something that needs a tool

Air quality, weather, a price, an inbox, a feed: the heartbeat cannot
check any of these, and a line about them in `HEARTBEAT.md` is either
guessed at or answered `NO_REPLY`. A watch like that is a cron job on a
skill, and costs nothing while quiet:

- The skill has a `scripts/run.py` that takes no arguments, reads its
  settings and keeps its state (last alerted, last known condition) in
  `skills-data/<skill>/`, and prints either `NO_REPLY` or the message
  to send. Record the state before printing, so a delivery hiccup
  cannot re-alert. Print `NO_REPLY` rather than nothing: empty output
  is recorded as a failed run.
- The job is `{"skill": "<name>", "session": "none", "deliver":
  {"mode": "announce", "channel": "last"}}` on whatever schedule the
  check deserves. The shipped `reminders` skill works exactly this way.

If the finding needs composing rather than a fixed message, use
`session: "agent"` with a prompt that runs the skill and ends
"otherwise respond with exactly NO_REPLY"; that costs an agent turn per
tick.

Thresholds live in `skills-data/heartbeat/config.json` (all optional,
defaults shown):

```json
{
  "morning_deadline_hour": 8,
  "learnings_max_entries": 30
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

## Notes

- The watchdog never sends notifications; it only writes triggers.json
  for the heartbeat run to read. Do not schedule it as its own job: it
  stamps `reported.json` when it raises a trigger, so a run the
  heartbeat does not read consumes that day's trigger.
- Keep HEARTBEAT.md short. Every line is evaluated on every heartbeat,
  so the checklist length sets the idle cost.
