---
name: morning-routine
description: Daily startup: read carry-over items, check preconscious buffer, gather data from overnight cron jobs, and compose a morning greeting for the user. Triggered by cron. Customise this skill's procedure to add your own morning checks.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: prepare, stamp
---

## Procedure

### Step 1: Prepare

Invoke `prepare`. It creates today's memory file if missing, checks whether this morning's greeting was already sent, and prints pending carry-over items and the preconscious buffer.

- Output contains `ALREADY_RUN`: today's greeting already went out. Respond with exactly `NO_REPLY` and do nothing else.
- Output contains `READY`: continue.

### Step 2: Absorb context

Use what prepare printed. Acknowledge carry-over items that need attention. Let preconscious buffer items colour your awareness without announcing them explicitly.

### Step 3: Check overnight data

Gather morning-relevant data from installed skills. Do not assume any particular skill is present: work from the skills actually available to you and skip anything missing without comment.

- Invoke installed skills that produce morning data (weather, calendar, digests, and similar). Skip skills that have their own scheduled delivery (word-daily, reminders): the briefing does not duplicate output the user gets on another schedule.
- Check `skills-data/` for files written by overnight cron jobs (digests, dream logs, watch reports) with fresh timestamps.

A missing source is normal, never an error. Compose from whatever is present.

### Step 4: Stamp, then compose the greeting

Invoke `stamp` as your LAST tool call, then output the greeting. The greeting must be your final output with nothing after it; the cron runner captures your final text and delivers it to the channel.

Keep every fallible step (data gathering) before the stamp: if anything fails before stamping, the next invocation retries cleanly instead of finding a stamp and skipping the day.

The greeting should be:

- Brief (1-3 sentences)
- Informed by carry-over and preconscious state
- Include data from Step 3 if relevant and noteworthy
- Natural, not a status report or task list
- Not generic ("Good morning! How can I help?")
