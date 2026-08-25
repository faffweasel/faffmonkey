# reminders: setup and configuration

"Remind me to X at Y" with stdlib natural-language time parsing. Reminders
live in `workspace/skills-data/reminders/reminders.json` (legacy
`config/reminders.json` is still read); delivery rides the cron system.

## Setup

1. Install the skill.
2. Add the delivery job to `workspace/config/jobs.json` (or ask the agent to
   do it via cron-manager), with `channel` set to your channel name
   (telegram, discord):

   ```json
   {
     "id": "reminder-check",
     "schedule": "*/5 * * * *",
     "skill": "reminders",
     "session": "none",
     "deliver": { "mode": "announce", "channel": "telegram" },
     "enabled": true
   }
   ```

   Every 5 minutes the check runs with zero LLM cost; due reminders are
   sent straight to your channel, and quiet runs deliver nothing (the
   script prints `NO_REPLY`, which cron suppresses). Reminder precision is
   the cron interval: with `*/5`, a 9:00 reminder arrives by 9:05.

3. Timezone comes from `workspace/config/location.json`
   (`current.timezone`, IANA name), falling back to the agent's TZ.

## Notes

- `scripts/run.py` (what `session: "none"` cron jobs invoke) delegates to
  `remind.py check`.
- Fired one-shot reminders are removed; recurring reminders advance.
  Delete or edit `skills-data/reminders/reminders.json` by hand if things get tangled.
