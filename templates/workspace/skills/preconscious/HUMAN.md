# preconscious: setup and notes

A small scored buffer of things the agent keeps top-of-mind across
sessions: not facts, not reminders, just ambient awareness that fades
naturally. No setup required.

## How it works

- The buffer lives at `workspace/skills-data/preconscious/buffer.json`,
  capped at 5 items.
- Each item has a Currency score (C, decays by 1 daily) and an
  Importance score (I, fixed at creation). Items rank by C + I.
- Items are dropped when expired and unimportant (C at or below 0 AND I
  at or below 2), or displaced by a higher-scoring item when the buffer
  is full. High-I items survive until displaced or manually dropped;
  repeated decay can push C negative, which is treated the same as 0.
- At session start, bootstrap reads the buffer into the system prompt
  (as untrusted content), so the agent sees its own top-of-mind notes
  without invoking the skill.

## Setup

Daily decay runs via cron. Add to `workspace/config/jobs.json`:

```json
{
  "id": "preconscious-decay",
  "schedule": "0 4 * * *",
  "skill": "preconscious",
  "session": "none",
  "deliver": {"mode": "none"}
}
```

`session: "none"` runs the skill's `run` script (the decay pass) with no
LLM call: zero tokens.

## Maintenance

The buffer is disposable. Delete buffer.json to clear it, or ask the
agent to run `drop_lowest` to free a slot. If an old high-importance
item is stuck, ask the agent to drop it.
