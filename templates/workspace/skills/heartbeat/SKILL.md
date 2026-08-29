---
name: heartbeat
description: Ambient awareness on the hourly heartbeat. A zero-token watchdog checks files and timestamps, then a cheap model reads HEARTBEAT.md, which holds only checks answerable from that file and the clock. Anything to watch that needs a tool, a skill or the web is a cron job (cron-manager), never a HEARTBEAT.md line.
actions: run
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
---

## When to use

This skill is normally triggered by cron, not invoked directly. Invoke it yourself only to see the current watchdog state on demand: run the `run` action and read the result.

When the user asks you to watch for something, where it goes depends on what answering it takes:

- **Answerable from HEARTBEAT.md and the current time alone** (a standing instruction, a reminder inside a time window, how to word a watchdog trigger): add a line to `HEARTBEAT.md`.
- **Needs a tool, a skill or the web** (air quality, weather, a price, an inbox, a feed, a repo): a cron job via cron-manager. The heartbeat has no tools; a line about any of these in HEARTBEAT.md can only be guessed at or answered NO_REPLY. The zero-token form is `session: "none"` on a skill whose `run` script prints `NO_REPLY` or the message (the reminders skill is the shipped example). If the finding needs composing, `session: "agent"` with a prompt that ends "otherwise respond with exactly NO_REPLY".
- **At a fixed time**: cron-manager.

When you are running AS the heartbeat (a cron job with `context: heartbeat`), go through the HEARTBEAT.md you were given. A line that tells you to report something is acted on every time; the rest are things to watch for. If any line gives you something to say now, say it; otherwise respond with exactly `NO_REPLY`. Anything that is not NO_REPLY is delivered to the user, so only respond with substance.

## Actions

**run**, the watchdog: deterministic file and timestamp checks, no LLM. Writes `skills-data/heartbeat/triggers.json` and auto-fixes simple issues (e.g. creates missing memory files):

```
run
```

## Interpreting triggers.json

```json
{
  "checked_at": "2026-05-14T10:30:00+07:00",
  "status": "attention",
  "triggers": [
    "morning_missed: no morning routine stamp after 08:00",
    "learnings_full: 35 entries (threshold: 30)"
  ],
  "fixed": [
    "created missing memory file: memory/daily/2026-05-13.md"
  ]
}
```

- `status` is `clean` or `attention`. `triggers` lists the reasons; `fixed` lists what the watchdog already repaired itself.
- Trigger kinds: `morning_missed` (no morning routine stamp past the deadline) and `learnings_full` (LEARNINGS.md over its entry threshold, consider running self-review). Each is raised once per day, recorded in `reported.json`.

## Limitations

- The watchdog only tests what needs no reasoning: file existence, timestamps, counts.
- The model that reads HEARTBEAT.md has no tools. It sees the file and the time, nothing else.
- Keep HEARTBEAT.md concise; every line costs tokens on every heartbeat evaluation.
- Cross-skill: heartbeat monitors, morning-routine greets, carry-over queues messages, preconscious tracks awareness. Complementary, not overlapping.
