---
name: weather
description: Current weather and 5-day forecast via OpenWeatherMap. Reads location from config/location.json. Caches responses for 15 minutes. Use for weather, forecast, will-it-rain, temperature, and what-to-wear questions.
metadata: '{"faffmonkey":{"requires":{"env":["OPENWEATHERMAP_API_KEY"]}}}'
actions: weather
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

- feels-like vs actual temperature (wind chill: "12C but feels 8C, wear layers")
- rain probability for today/tomorrow ("60% rain, bring an umbrella")
- wind speed (above ~8 m/s is genuinely windy)
- sunrise/sunset when timing matters (runs, photos, commutes)

Keep the advice to a sentence or two grounded in the numbers, and keep the OpenWeatherMap attribution when quoting data verbatim. Responses are cached for 15 minutes, so repeat calls in a conversation are free; do not hesitate to re-check.
