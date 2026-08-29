# heartbeat: setup and notes

One cron job that costs nothing while nothing is happening. Scripts
decide whether to wake the agent; the agent decides what to say.

## How it works

Every tick of the `heartbeat` job (every five minutes by default):

1. **Watchdog** (this skill's `run` action, no model call). Runs its
   health checks (yesterday's memory file exists, the morning routine
   ran before its deadline, LEARNINGS.md is under its entry threshold),
   then reads every trigger in `skills-data/heartbeat/triggers.d/` and
   the last line of every `workspace/readings/*.jsonl`. Writes
   `triggers.json` with status `clean` or `attention`.
2. **Clean**: the tick ends. No model, no log row.
3. **Attention**: one agent turn with tools, on the `heartbeat` model
   route (default `cheap`). Its prompt is the job's prompt, the
   triggers, the latest readings, `HEARTBEAT.md`, and what the heartbeat
   has sent in the last two days. It replies with one message or
   `NO_REPLY`. The triggers it saw are then deleted.

Health-check triggers are raised once per day (`reported.json`).
Sensor triggers are raised whenever a sensor drops one; the sensor's
own state decides how often that is.

## Setup

`workspace/HEARTBEAT.md` is standing instructions: how to weigh what
the heartbeat finds and when to stay quiet. It is read only on a wake,
so it costs nothing while quiet. The template is a reasonable start;
add your own preferences ("I don't run above 32C", "never message me
about the AQI twice in a day") in plain English.

Thresholds for the health checks live in
`skills-data/heartbeat/config.json` (all optional, defaults shown):

```json
{
  "morning_deadline_hour": 8,
  "learnings_max_entries": 30
}
```

The job, in `workspace/config/jobs.json`. `faff setup telegram` and
`faff setup discord` write it for you alongside the morning, evening
and preconscious-decay jobs; this is what they write:

```json
{
  "id": "heartbeat",
  "schedule": "*/5 * * * *",
  "prompt": "The heartbeat woke you because something needs a decision. Below are the triggers that woke you, the latest sensor readings, your standing instructions from HEARTBEAT.md, and what you have already sent recently. Decide whether the user should hear anything now. If so, write it plainly as one message. If not, respond with exactly NO_REPLY.",
  "context": "heartbeat",
  "session": "agent",
  "model": "cheap",
  "deliver": {"mode": "announce", "channel": "last"}
}
```

`heartbeat.active_hours` and `heartbeat.enabled` in `state/config.json`
stop wakes outside the hours you want to hear from it.

## Writing a sensor

A sensor is a skill with a `scripts/run.py`, scheduled as a
`session: "none"` job with `deliver: {"mode": "none"}`. Each run:

1. Fetches one thing.
2. Appends a reading to `workspace/readings/<source>.jsonl`:
   `{"at": ISO time, "summary": one line, "data": {a few fields}}`.
   Trim the file to about seven days.
3. Applies its rules against its own state in `skills-data/<skill>/`
   (a threshold from `config.json`, ids already seen, the last known
   condition) and, if one fires, writes
   `skills-data/heartbeat/triggers.d/<source>-<key>.json`:
   `{"at", "source", "kind": "alert" | "occasion" | "new", "text"}`.
   Record the state before writing the trigger, so a failed wake cannot
   cause a re-alert loop.
4. Prints a status line.

The rules are deliberately dumb: crossed a number, not seen before,
now due. Whether it matters is the agent's call on the wake. The
contrib `aqi` and `weather` skills are the shipped examples; the
`reminders` skill drops an `occasion` trigger for each reminder it
delivers, so the wake can add advice if the readings argue with it.

A job per sensor, on the cadence the thing deserves:

```json
{"id": "aqi-sensor", "schedule": "0 * * * *", "skill": "aqi",
 "session": "none", "deliver": {"mode": "none"}}
```

## A scheduled look-around

To have the agent look at all the readings at fixed times with no
trigger from any sensor, schedule `poke` as an agent job:

```json
{"id": "look-around", "schedule": "0 7,13,19 * * *",
 "prompt": "Invoke the heartbeat skill's poke action with no arguments and reply NO_REPLY.",
 "session": "agent", "model": "cheap", "deliver": {"mode": "none"}}
```

The next heartbeat tick then wakes with every reading in front of it.
That is the one place a model runs on a schedule rather than because a
script found something; three a day is the default suggestion.

## Notes

- The watchdog never sends anything; it only writes `triggers.json`.
  Do not schedule it as its own job: the tick runs it, and a run the
  tick does not read would consume that day's health-check triggers.
- Messages the heartbeat delivers are remembered in
  `state/cron-state.json` (last ten, two days) and shown to the next
  wake, so "already said" is evidence rather than memory.
- A wake that fails (provider error) keeps its triggers for the retry.
  A wake that succeeds but cannot deliver loses that message; the
  sensor will drop a fresh trigger when its rule next fires.
