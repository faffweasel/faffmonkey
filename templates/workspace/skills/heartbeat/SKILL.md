---
name: heartbeat
description: The heartbeat's watchdog and trigger tray. Every few minutes a zero-token script runs health checks, collects triggers dropped by sensor skills and the latest readings, and wakes you (an agent turn with tools) only when there is a trigger. A sensor skill makes the heartbeat notice something by dropping a trigger file; poke wakes you on the next tick by hand.
actions: run, poke
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
---

## When to use

Normally triggered by cron, not invoked directly. Invoke it yourself to:

- see the current watchdog state: `run`, then read the result
- wake yourself on the next tick with a note: `poke <text>`. A fixed-time "look around" is an `agent` cron job whose prompt invokes `poke` (a `none` job can only run `run`); HUMAN.md shows the job.

When the user asks you to watch for something:

- **Needs a reading from the world** (air quality, weather, a price, an inbox, a feed): a sensor skill on a `session: "none"` cron job, via cron-manager. The sensor writes readings and drops a trigger when its rule fires; the heartbeat wakes you with it. If the skill has no `run` action, say so; do not put the watch in HEARTBEAT.md, the heartbeat cannot check anything itself.
- **How to weigh things, or when to stay quiet**: a line in `HEARTBEAT.md`.
- **At a fixed time**: cron-manager.

When you are running AS the heartbeat (a cron job with `context: heartbeat`, woken by a trigger), you have the triggers, the latest readings, `HEARTBEAT.md` and what the heartbeat sent recently. Use tools if a reading is stale or you need more. Reply with one plain message if the user should hear something now; otherwise respond with exactly `NO_REPLY`. Anything that is not NO_REPLY is delivered.

## Actions

**run**, the watchdog: health checks, then the trigger tray and the readings, no LLM. Writes `skills-data/heartbeat/triggers.json` and fixes simple issues itself (creates a missing daily memory file):

```
run
```

**poke**, drop a trigger so the next tick wakes you. Text optional:

```
poke
poke Check whether the afternoon still looks good for the walk.
```

## Trigger files, for sensor skills

A sensor drops `skills-data/heartbeat/triggers.d/<source>-<key>.json`:

```json
{"at": "2026-08-29T15:00:00+07:00", "source": "aqi", "kind": "alert",
 "text": "AQI 192, above your 180 threshold"}
```

`kind` is `alert` (a line was crossed), `occasion` (something is due) or `new` (something appeared). Only `text` is required. Write the same key again rather than adding files. The scheduler deletes a trigger once you have seen it, whether or not you spoke; the sensor's own state (last alerted, last condition, seen ids) decides when it drops the next one.

## Readings, for sensor skills

A sensor appends one line per run to `workspace/readings/<source>.jsonl`:

```json
{"at": "2026-08-29T15:00:00+07:00", "summary": "AQI 192 at Hoan Kiem, PM2.5", "data": {"aqi": 192}}
```

The last line is the latest; keep about seven days. The watchdog puts the last line of every file into the wake prompt, so you see the state of the world without a tool call; `file_read` the file for history.

## Interpreting triggers.json

```json
{
  "checked_at": "2026-08-29T15:05:00+07:00",
  "status": "attention",
  "triggers": [
    "morning_missed: no morning routine stamp after 08:00",
    "aqi (alert): AQI 192, above your 180 threshold"
  ],
  "files": ["aqi-high.json"],
  "readings": ["aqi (5m ago): AQI 192 at Hoan Kiem, PM2.5"],
  "fixed": []
}
```

- `status` is `clean` or `attention`; `attention` means at least one trigger.
- Watchdog triggers: `morning_missed` and `learnings_full` (LEARNINGS.md over its entry threshold, run self-review). Each is raised once per day, recorded in `reported.json`.
- `files` are the tray entries included in this run; `fixed` is what the watchdog repaired itself.

## Limitations

- The watchdog only tests what needs no reasoning: file existence, timestamps, counts, and whether the tray is empty.
- A trigger is consumed by the wake it caused, even one answered NO_REPLY.
- Cross-skill: heartbeat wakes, morning-routine greets, carry-over queues messages, preconscious tracks awareness, reminders deliver on time. Complementary, not overlapping.
