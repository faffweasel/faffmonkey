---
name: reminders
description: Natural language reminders with timezone awareness, one-shot and recurring, delivered via cron. Use whenever the user says remind me, set a reminder, don't let me forget, or gives a task with a time attached.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: remind, run
---

## When to use

- "Remind me to call mum tomorrow at 9" → `remind add "call mum" "tomorrow 9am"`
- "Don't let me forget the flight check-in in 2 hours" → `remind add "check in for flight" "in 2 hours"`
- "Remind me every Monday at 10 about standup" → `remind add "standup" "every monday 10am"`
- "What reminders do I have?" → `remind list`
- "Cancel the mum reminder" → `remind list`, find the id, `remind remove rem_003`

For complex scheduled agent tasks (jobs with prompts, models, delivery modes), use cron-manager instead; this skill is for simple tell-me-at-a-time reminders.

## Commands

```
remind add "text" "when"    create (prints the id and resolved fire time)
remind list                 pending reminders
remind remove <id>          cancel
remind check                fire due reminders (cron calls this, not you)
```

## Time formats

- Relative: `in 30 minutes`, `in 2 hours`, `in 3 days`
- Day words: `tomorrow 9am`, `today 5pm`, `tonight` (20:00 default), `tomorrow` (09:00 default)
- Weekdays: `friday 3pm`, `next friday 3pm` (next occurrence, skips today if the time has passed)
- Absolute: `2026-05-20 14:00`, `2026-05-20` (09:00 default)
- Bare times: `9am`, `14:30` (today, or tomorrow if already passed)
- Recurring: `every day 9am`, `every monday 10am`

All times resolve in the user's timezone. Past one-shot times are rejected; relay the error and ask for a corrected time.

## Behaviour

- After adding, confirm back the resolved time the script printed, so mistakes surface immediately ("Set: call mum, Thursday 9:00").
- When the reminder-check cron output contains `REMINDER:` lines, deliver each to the user verbatim as its own message, with nothing added except natural phrasing.
- One-shot reminders disappear after firing; recurring ones advance to the next occurrence automatically.
