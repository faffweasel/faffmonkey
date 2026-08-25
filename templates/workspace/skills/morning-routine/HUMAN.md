# morning-routine: setup and notes

Composes a short morning greeting from carry-over items, the
preconscious buffer, and whatever morning-relevant skills are installed.
Idempotent: running it twice in a day sends one greeting.

## How it works

- `prepare` creates today's `memory/daily/YYYY-MM-DD.md` if missing and checks
  it for the stamp line `Morning message sent`. If present, it reports
  ALREADY_RUN and the agent stays silent (NO_REPLY). Otherwise it prints
  pending carry-over items and the preconscious buffer, and the agent
  composes the greeting.
- `stamp` appends `Morning message sent HH:MM` to today's memory file.
  The same line satisfies the heartbeat watchdog's `morning_missed`
  check, so a delivered greeting also clears that trigger.
- The briefing content is discovery-based: the agent uses whichever
  installed skills produce morning data and skips absent ones, so
  installing or removing a contrib skill changes the briefing without
  any edits here.

## Cron configuration

The job needs `session: "agent"` so the agent can invoke skills and
read data files. Example `workspace/config/jobs.json` entry:

```json
{
  "id": "morning",
  "schedule": "5 7 * * *",
  "prompt": "Invoke the morning-routine skill and follow its procedure.",
  "session": "agent",
  "model": "cheap",
  "deliver": {"mode": "announce", "channel": "telegram"}
}
```

Schedule data-collection jobs a few minutes earlier so their output is
ready when the routine runs, e.g. a `session: "none"` job on a skill
with a `run` script at `0 7 * * *`.

## If the greeting never arrived

`faff cron history morning` (or `/cron history morning` in chat) showing
an error after the run started usually means the compose step failed
after `stamp` had already written today's stamp, so every retry sees
ALREADY_RUN and stays silent for the rest of the day. To retry: delete
the `Morning message sent` line from today's
`memory/daily/YYYY-MM-DD.md`, then run the job again.

## Customisation

Edit Step 3 of SKILL.md to add your own morning checks: project status,
specific files to glance at, anything the discovery pass would not find.
Contrib skills that produce morning data describe their cron setup in
their own HUMAN.md; install the skill and schedule its collection job,
and the briefing picks it up.
