# timezone: setup and notes

Timezone conversion with DST handling. Stdlib zoneinfo; no API, no key.

## Setup

None required. Two optional config files in `workspace/config/`:

- `location.json`: if `current.timezone` is set (IANA name), it becomes the
  default zone for "what time is that for me" style conversions. Without it,
  the agent's configured timezone (TZ) is used.

  ```json
  { "current": { "city": "Hanoi", "timezone": "Asia/Bangkok" } }
  ```

- `skills-data/timezone/aliases.json` (legacy `config/aliases.json` still
  read): custom aliases merged over the 70+ built-ins. Keys starting
  with `_` are ignored (use `_comment` for notes).

  ```json
  { "_comment": "personal shortcuts", "mum": "Europe/London", "office": "Asia/Singapore" }
  ```

## Notes

- Abbreviations are deliberately mapped to DST-aware regions: "gmt" means
  Europe/London (so BST is handled), "est" means America/New_York. If you
  genuinely need fixed UTC, use "utc".
- Purely on-demand; no cron jobs.
