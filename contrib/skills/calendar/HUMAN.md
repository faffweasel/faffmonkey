# calendar: setup and configuration

Read-only ICS calendar viewer. No OAuth, no CalDAV, no dependencies.

## Config

`workspace/skills-data/calendar/calendars.json` (the agent can write it;
legacy `config/calendars.json` still honoured):

```json
{
  "calendars": [
    {
      "name": "Personal",
      "type": "file",
      "path": "shared/inbox/personal.ics"
    },
    {
      "name": "Work",
      "type": "url",
      "url": "https://calendar.google.com/calendar/ical/.../basic.ics",
      "refresh_minutes": 30
    }
  ]
}
```

- `type: "file"` reads a workspace-relative path fresh on every query.
- `type: "url"` fetches and caches in `skills-data/calendar/cache/` with the
  given TTL; a failed refresh falls back to the cached copy.

## Proton Calendar (manual export)

Proton has no live ICS feed; export by hand:

1. Log in to calendar.proton.me
2. Settings → All settings → Import/export
3. Select the calendar, click Download ICS
4. Save to `workspace/shared/inbox/personal.ics`
5. Re-export periodically or after significant changes

## Google Calendar (subscription URL)

Settings → Settings for my calendar → Integrate calendar → "Secret address
in iCal format". Use that URL with `type: "url"`. Outlook and Fastmail have
equivalent published-ICS URLs.

## Limits

- Recurring events: DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT,
  UNTIL, and weekly BYDAY are supported; anything fancier (BYSETPOS,
  exceptions via EXDATE) is skipped with a stderr warning.
- Timezones: IANA TZIDs convert properly; proprietary timezone names fall
  back to your local timezone with a warning.
