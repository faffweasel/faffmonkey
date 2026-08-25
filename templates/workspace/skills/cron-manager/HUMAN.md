# cron-manager: setup and notes

Lets the agent schedule and manage its own recurring jobs and one-shot
reminders. No setup required; jobs live in `workspace/config/jobs.json`
and the scheduler picks up changes automatically.

## Managing jobs from the host

- `faff cron list`: all jobs with next fire times
- `faff cron run <jobId>`: run a job immediately
- `faff cron history <jobId>`: recent runs with outcomes

You can also edit `workspace/config/jobs.json` directly; `faff doctor`
validates it.

## Session modes and cost

- `none` runs a skill script with no LLM call: zero tokens. Use it for
  data collection and watchdogs.
- `isolated` and `main` are single completions: one LLM call, no tools.
- `agent` is a full tool-capable turn: it can invoke skills and write
  files, so it costs the most. It is ephemeral by design: no session is
  stored, and anything that would normally ask for permission is denied.
  The job's `model` field routes the entire turn, so a dedicated model
  slot can serve a specific job.

## Scheduling behaviour

- All times (cron expressions and `at` datetimes) are interpreted in your
  configured timezone with correct DST handling. A `0 2 * * *` job fires
  at 2am local year-round; if a spring-forward skips the target minute,
  the job fires at the next valid minute after the transition.
- Jobs scheduled at minute 0 of any hour get a random 0-5 minute offset
  to avoid hammering the provider at the top of the hour. Schedule at
  minute 1 to bypass the stagger.
- Failed jobs retry with exponential backoff. One-shot (`at`) jobs are
  removed only after a successful run; check `faff cron history` if one
  appears stuck.
