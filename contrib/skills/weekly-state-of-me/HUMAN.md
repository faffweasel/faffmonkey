# weekly-state-of-me: setup and configuration

A weekly written self-reflection: what the agent thinks is happening to it,
the relationship, and whether its SOUL.md still fits. Personality evolution
happens only through proposals you review; never by direct edits.

## Setup

Add the cron job to `workspace/config/jobs.json`:

```json
{
  "id": "weekly-state-of-me",
  "schedule": "0 8 * * 0",
  "session": "agent",
  "prompt": "Run the weekly-state-of-me skill: invoke its generate action, then follow skills/weekly-state-of-me/SKILL.md to fill the reflection.",
  "deliver": { "mode": "announce", "channel": "telegram" },
  "enabled": true
}
```

`session: "agent"` is required (the reflection reads and writes workspace
files). Delivery is effectively silent: the agent replies `NO_REPLY` unless
a soul proposal was written, and cron suppresses `NO_REPLY` messages.

Use a capable model slot; this needs reasoning, not just coherence. Add
`"model": "<slot>"` to route it.

## Soul evolution proposals

Proposals land in `memory/soul-proposals/YYYY-MM-DD.md` with exact
current/proposed/rollback text. To apply one, edit SOUL.md yourself (or tell
the agent to apply it in chat, where you can confirm). Velocity limits: at
most 3 proposals per rolling 30 days before the skill stops proposing.

## Optional integrations

All soft; the skill skips whatever is absent:

- **dreams**: if you have a dreaming system, dream files should go in `memory/dreams/`
- **preconscious** built-in: buffer is read as an input
- **IMAGE_GEN_CMD** in `state/commands.json`: enables the visual journal
  (venice-ai-media or openrouter-image-simple provide it)

## Output

Reflections accumulate in `memory/state-of-me/`, one per week, with images
in `memory/state-of-me/images/`. They are the agent's files; read them when
you're curious, but they're written for the agent's own continuity.
