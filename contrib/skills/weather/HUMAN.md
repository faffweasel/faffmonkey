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
   default.

## Notes

- Responses cache in `skills-data/weather/cache.json` for 15 minutes; the
  free tier's rate limits are generous but this keeps chat snappy.
- Data is ODbL-licensed; the attribution line in output is required.
- Works well with a morning cron job (use `"session": "agent"` so the skill
  can be invoked): "run weather advice and fold anything notable into the
  greeting".
