---
name: heartbeat
description: Ambient awareness. Periodically checks if anything needs the user's attention. Watchdog script runs deterministic checks (zero tokens). LLM heartbeat evaluates HEARTBEAT.md checklist (cheap model, ~100 tokens when idle). Customise HEARTBEAT.md to define what the agent watches for.
actions: run
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
---

## When to use

This skill is normally triggered by cron, not invoked directly. Invoke it yourself only in these cases:

- The user asks you to watch for something periodically with no fixed time ("keep an eye on X", "tell me if Y changes"): add a line to `HEARTBEAT.md`. For tasks at a specific time, use cron-manager instead.
- You want the current watchdog state on demand: run the `run` action and read the result.

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
- Default trigger kinds: `morning_missed` (no morning routine stamp past the deadline), `carryover_stale` (carry-over items pending too long), `learnings_full` (LEARNINGS.md over its entry threshold, consider running self-review).

## Limitations

- The watchdog only tests what needs no reasoning: file existence, timestamps, counts. Judgment calls belong in the HEARTBEAT.md checklist.
- Keep HEARTBEAT.md concise; every line costs tokens on every heartbeat evaluation.
- Cross-skill: heartbeat monitors, morning-routine greets, carry-over queues messages, preconscious tracks awareness. Complementary, not overlapping.
