# aqi: setup and configuration

Air quality data from the World Air Quality Index project (aqicn.org).
Stdlib-only. The free token's default quota is 1,000 requests per second,
so polling is never the constraint.

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

## Sensor: readings and a threshold alert

`scripts/run.py` is the sensor entry point. On a `session: "none"` cron
job it appends the current reading (the pinned station, else the
average of the stations near your location) to
`workspace/readings/aqi.jsonl`, kept for seven days, and drops a
heartbeat trigger the first time each day the AQI is above your
threshold. The heartbeat then wakes the agent with the reading and
your standing instructions, and it decides what to say.

1. Set the threshold in `skills-data/aqi/config.json` (the same file as
   the station pin), or tell the agent "warn me if the AQI goes above
   180":

   ```json
   {"watch_threshold": 180}
   ```

   Without it the sensor only records readings, which still lets the
   agent answer "how has the air been this week" from the file.

2. Add the job to `workspace/config/jobs.json`, or ask the agent:

   ```json
   {"id": "aqi-sensor", "schedule": "0 * * * *", "skill": "aqi",
    "session": "none", "deliver": {"mode": "none"}}
   ```

Once-a-day is recorded in `skills-data/aqi/watch.json`; delete it to
re-arm. The morning briefing still runs `aqi current` itself.
