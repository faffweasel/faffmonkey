# aqi: setup and configuration

Air quality data from the World Air Quality Index project (aqicn.org).
Stdlib-only; free tier allows 1000 calls/day.

## Setup

1. Get a free API key: https://aqicn.org/data-platform/token/
2. Add it to `state/.env`:

   ```
   AQICN_API_KEY=your-token
   ```

3. Configure your location in `workspace/config/location.json`:

   ```json
   {
     "current": { "city": "Lisbon", "lat": 38.722, "lng": -9.139 }
   }
   ```

   The `multi` command aggregates all stations within roughly 5km of these
   coordinates. If you move cities, update this file; every location-aware
   skill adapts automatically.

## Station pinning

The default city lookup returns whichever station the API selects, which can
vary, and `multi` averages every station in the box, so one station reporting
4 next to one reporting 101 gives a meaningless middle. Find your nearby
stations with `aqi search <area>` and pin the one you trust:

```
aqi pin 1451
```

(or tell the agent "pin station 1451"). From then on `aqi current`, which is
what the agent uses for a plain "what's the air like" and for the morning
briefing, reads that station. The pin lives in `skills-data/aqi/config.json`;
`aqi unpin` clears it.

## Cron alerts (optional)

Add a morning cron job with `"session": "agent"` (skill invocation needs a
tool-capable session) and a prompt like "Run the aqi skill's multi command
and report only if it matters". The SKILL.md gives the agent thresholds:
silent up to 100, brief note above 100, strong recommendation above 150.
