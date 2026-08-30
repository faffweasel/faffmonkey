---
name: weather
description: Current weather and 5-day forecast via OpenWeatherMap. Reads location from config/location.json. Use for weather, forecast, will-it-rain, temperature, and what-to-wear questions. Its run action is the rain sensor for the heartbeat.
metadata: '{"faffmonkey":{"requires":{"env":["OPENWEATHERMAP_API_KEY"]}}}'
actions: weather, run
---

## When to use

- "What's the weather?" / "temperature outside?" → `weather now`
- "Weather in [city]?" → `weather now <city>`
- "Will it rain this week?" / "forecast?" → `weather forecast`
- "What's tomorrow like?" → `weather tomorrow`
- "Should I bring a jacket / umbrella?" / "good day for a run?" → `weather advice`, then advise from the data

For air quality questions, use the aqi skill, not this one.

## Commands

```
weather now [city]         current conditions (default city: config/location.json)
weather forecast [city]    5 days, grouped by day with high/low, conditions, rain %
weather tomorrow [city]    tomorrow only
weather advice [city]      current + next two days together
```

## Giving advice

`advice` outputs data, not advice; the interpretation is yours. Ground it in:

- the `Observed:` line first: it names the place and time OpenWeatherMap actually used. If that is not the configured city, or the numbers contradict the season or what the user says they are feeling, say the reading is wrong and stop there; do not explain it away. Wrong weather advice before a long run is a health risk.
- feels-like vs actual temperature (wind chill: "12C but feels 8C, wear layers")
- rain probability for today/tomorrow ("60% rain, bring an umbrella")
- wind speed (above ~8 m/s is genuinely windy)
- sunrise/sunset when timing matters (runs, photos, commutes)

Keep the advice to a sentence or two grounded in the numbers, and keep the OpenWeatherMap attribution when quoting data verbatim.

## Watching for rain

"Warn me before it rains" is the sensor, not a HEARTBEAT.md line. Make sure a sensor job exists via cron-manager:

```
add '{"id": "weather-sensor", "schedule": "*/15 * * * *", "skill": "weather", "session": "none", "deliver": {"mode": "none"}}'
```

The `run` action appends conditions and the short-range rain outlook to `readings/weather.jsonl` and drops a heartbeat trigger when rain becomes likely within the next three hours, once per dry-to-wet transition; the heartbeat wakes you with it. Lookahead and probability live in `skills-data/weather/config.json` (`rain_lookahead_hours`, `rain_probability`). Do not invoke `run` in conversation; `weather now` and `weather advice` are the answers to questions.
