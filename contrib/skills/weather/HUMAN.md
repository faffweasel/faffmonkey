# weather: setup and configuration

Current conditions and 5-day forecast from OpenWeatherMap's free tier.
Stdlib only.

## Setup

1. Get a free API key at https://openweathermap.org/appid and add to
   `state/.env`:

   ```
   OPENWEATHERMAP_API_KEY=...
   ```

2. Set your default location in `workspace/config/location.json` (shared
   with the aqi and timezone skills):

   ```json
   {
     "current": { "city": "Hanoi", "lat": 21.028, "lng": 105.854 }
   }
   ```

   Named cities in queries are geocoded, so the location file is only the
   default. `lat`/`lng` must be real: `0, 0` and out-of-range values are
   refused with an error rather than queried. The `Observed:` line in
   `now` output names the point and time OpenWeatherMap actually used.

## Sensor: readings and a rain alert

`scripts/run.py` is the sensor entry point. On a `session: "none"` cron
job it appends current conditions plus the rain outlook for the next
few hours to `workspace/readings/weather.jsonl` (seven days kept), and
drops a heartbeat trigger when rain becomes likely within the
lookahead. The trigger fires on the dry-to-wet transition and re-arms
when the outlook is dry again, so a shower in the morning and another
in the evening are two warnings, and a showery afternoon is one.

```json
{"id": "weather-sensor", "schedule": "*/15 * * * *", "skill": "weather",
 "session": "none", "deliver": {"mode": "none"}}
```

Tuning, all optional, in `skills-data/weather/config.json`:

```json
{"rain_lookahead_hours": 3, "rain_probability": 0.5}
```

The free tier allows 1,000 calls a day; every 15 minutes is 192 (a
current and a forecast call each run). The transition state is in
`skills-data/weather/watch.json`.

## Notes

- Data is ODbL-licensed; the attribution line in output is required.
- Works well with a morning cron job (use `"session": "agent"` so the skill
  can be invoked): "run weather advice and fold anything notable into the
  greeting".
