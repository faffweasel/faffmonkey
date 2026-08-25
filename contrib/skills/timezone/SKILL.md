---
name: timezone
description: Multi-timezone conversion with DST handling, 12h/24h input, date-change indicators, and a diff command with scheduling advice. 70+ city and abbreviation aliases. Use for any what-time-is-it-there or call-scheduling question.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: tz
---

## When to use

- "What time is it in [city]?" → `tz now <city>`
- "When is 3pm ICT in London?" → `tz 3pm hanoi london`
- "What time is that for me?" → convert into the user's zone; with no target zones the script defaults to their configured timezone
- "Time difference between Hanoi and London?" → `tz diff hanoi london`
- "Can I schedule a call at 9am UK time?" → `tz 9am london <user's zone>`, then use the diff advice

## Commands

```
tz now <tz1> <tz2> ...        current time in zones (no args: user's zone)
tz <time> <from> <to> ...     convert a time (accepts 15:00, 3pm, 3:30pm, 15)
tz diff <tz1> <tz2>           offset difference + scheduling advice
```

City names, country names, and abbreviations resolve via aliases (hanoi, london, nyc, la, tokyo, kl, hk, utc, est, pst, cet, ...). Full IANA names always work. If a zone comes back "unknown timezone", retry with the IANA name (Region/City).

## Interpreting output

- `[+1 day]` / `[-1 day]` markers mean the conversion crosses midnight; always mention this when scheduling.
- Abbreviations like GMT/EST are treated as their DST-aware region (Europe/London, America/New_York), so summer/winter shifts are already handled; do not manually adjust for DST.
- diff includes a practical note (good overlap / schedule carefully / async preferred); pass it on when the question is about meetings.
