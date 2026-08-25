---
name: calendar
description: Read-only calendar viewer parsing ICS files and subscription URLs (Proton via manual export, Google, Outlook, any ICS provider). Use for what's-on-today, meetings-tomorrow, this-week, and when-is-my-next-event questions.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: cal
---

## When to use

- "What's on today?" → `cal today`
- "Any meetings tomorrow?" → `cal tomorrow`
- "My calendar this week" → `cal week`
- "What's happening on the 20th?" → `cal on 2026-05-20`
- "When's my next meeting?" → `cal next`
- Morning briefing composition → `cal today --json`

This skill is read-only. If the user asks to add, move, or cancel an event, say so and point them at their calendar provider.

## Commands

```
cal today|tomorrow|week [--json]
cal on YYYY-MM-DD [--json]
cal next [--json]
cal refresh          force re-fetch of URL calendars
cal list             configured calendars with event counts
```

Times display in the user's timezone; all-day events show `[all day]`. Events with exotic recurrence rules are skipped with a warning on stderr; if the user insists an event is missing, mention that limitation and suggest checking the provider.

## Answering

- Summarise, don't dump: lead with the count and the next relevant event, then the list.
- Mention gaps when useful ("nothing until 14:00, your morning is clear").
- If a file-type calendar (Proton) looks stale, remind the user it only updates when they re-export the ICS (see HUMAN.md).
