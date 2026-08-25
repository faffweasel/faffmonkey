---
name: aqi
description: Air quality lookups with station selection, multi-station aggregation, and per-pollutant breakdown. Use for any question about air quality, AQI, pollution, or whether outdoor activity is advisable.
metadata: '{"faffmonkey":{"requires":{"env":["AQICN_API_KEY"]}}}'
actions: aqi
---

## When to use

- "AQI", "air quality", "how's the air" → `aqi multi` for the multi-station overview of the configured location
- "AQI in [city]" → `aqi <city>`
- "Which station is worst?" → `aqi multi`
- "AQI near [coordinates/place]" → `aqi geo <lat> <lng>`
- "Should I run outside?" → `aqi multi`, then advise from the average AQI
- "AQI forecast" → `aqi <city> forecast` using the configured city name

## Commands

```
aqi <city>              AQI for a city (API picks the station)
aqi <city> forecast     3-5 day forecast (PM2.5, PM10, O3)
aqi search <keyword>    find stations in an area (returns UIDs)
aqi station <uid>       AQI from a specific pinned station
aqi geo <lat> <lng>     nearest station to coordinates
aqi multi               all stations near the configured location, with min/max/avg
aqi current             the pinned station if one is set, otherwise multi
aqi pin <uid>           make a station the default that current uses; aqi pin shows it
aqi unpin               clear the pin
```

**Use `aqi current` whenever the user asks about air quality without naming a place or station**, including in the morning briefing. Nearby stations can disagree wildly (one reading 4 while the next reads 101), so once the user has chosen one, that is the answer; `multi` averages the outliers in. When the user says which station they trust, `aqi pin <uid>` records it in this skill's data directory and it survives restarts.

`multi` reads the location from `config/location.json`; if it errors with "No location configured", tell the user to set that file up (see HUMAN.md). `hanoi-multi` is an alias for `multi`.

## Morning briefing

Run `aqi current`. Report it only if it matters: silent up to 100, a brief note above 100, a strong recommendation above 150.

## Interpreting output

Levels: 0-50 Good, 51-100 Moderate, 101-150 Unhealthy for sensitive groups, 151-200 Unhealthy, 201-300 Very unhealthy, 300+ Hazardous.

When advising:

- avg ≤ 100: fine for most people; mention sensitive groups above 50
- avg > 100: advise limiting outdoor exertion, mention the range
- avg > 150: advise against outdoor exercise, name the worst station
- If stations vary widely (max - min > 50), say so; one station is not the whole city

Quote the dominant pollutant and measurement time when precision matters. For weather questions, use web search instead; this skill is air quality only.
