#!/usr/bin/env python3
"""
Weather via OpenWeatherMap free tier. Stdlib only.

Usage:
  weather.py now [city]        — current conditions
  weather.py forecast [city]   — 5-day forecast grouped by day
  weather.py tomorrow [city]   — tomorrow only
  weather.py advice [city]     — current + today, structured for agent advice

Default city comes from workspace/config/location.json. run.py is the
sensor entry point for cron; it appends readings to workspace/readings/.

Requires OPENWEATHERMAP_API_KEY. Data: OpenWeatherMap (ODbL).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))
SKILL_DATA = os.environ.get(
    "SKILL_DATA", os.path.join(WORKSPACE, "skills-data", "weather"),
)

API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "")
BASE = "https://api.openweathermap.org"
USER_AGENT = "faffmonkey"

ATTRIBUTION = "Data: OpenWeatherMap"

_COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def wind_direction(degrees) -> str:
    try:
        return _COMPASS[int((float(degrees) + 11.25) / 22.5) % 16]
    except (ValueError, TypeError):
        return "?"


def coordinate_problem(lat: float, lng: float) -> str:
    """Why these coordinates must not be queried, or "" if they may be.

    0,0 is a placeholder, not a place: it is a real point in the Gulf of
    Guinea and would be reported under the configured city's name.
    """
    if lat == 0 and lng == 0:
        return "lat and lng are both 0, a placeholder, not a place"
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return f"lat {lat}, lng {lng} is out of range (lat -90..90, lng -180..180)"
    return ""


def _load_location():
    """Return (lat, lon, city) from workspace/config/location.json or None."""
    path = Path(WORKSPACE) / "config" / "location.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    loc = data.get("current") or data.get("home") or {}
    lat, lng = loc.get("lat"), loc.get("lng")
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng), loc.get("city", "configured location")
    except (TypeError, ValueError):
        return None


def _fetch(path: str, params: dict) -> dict:
    if not API_KEY:
        print("Error: OPENWEATHERMAP_API_KEY not set (see HUMAN.md)", file=sys.stderr)
        sys.exit(2)
    params = {**params, "appid": API_KEY}
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Error: API key rejected (HTTP 401)", file=sys.stderr)
        else:
            print(f"Error: HTTP {e.code} from OpenWeatherMap", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    return data


def geocode(city: str):
    """Resolve a city name to (lat, lon, label)."""
    results = _fetch("/geo/1.0/direct", {"q": city, "limit": 1})
    if not results:
        print(f"Error: no location found for {city!r}", file=sys.stderr)
        sys.exit(1)
    top = results[0]
    label = top.get("name", city)
    country = top.get("country", "")
    if country:
        label = f"{label}, {country}"
    return top["lat"], top["lon"], label


def resolve_target(city_arg: str | None):
    if city_arg:
        return geocode(city_arg)
    loc = _load_location()
    if loc is None:
        print(
            "Error: no city given and no config/location.json configured",
            file=sys.stderr,
        )
        sys.exit(1)
    lat, lng, city = loc
    problem = coordinate_problem(lat, lng)
    if problem:
        print(
            f"Error: config/location.json for {city}: {problem}; "
            "set the real coordinates (see HUMAN.md)",
            file=sys.stderr,
        )
        sys.exit(1)
    return loc


def get_current(lat, lon) -> dict:
    return _fetch("/data/2.5/weather", {"lat": lat, "lon": lon, "units": "metric"})


def get_forecast(lat, lon) -> dict:
    return _fetch("/data/2.5/forecast", {"lat": lat, "lon": lon, "units": "metric"})


def _local(ts: int, offset_seconds: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(seconds=offset_seconds)))


def observation(data: dict) -> tuple[str, str]:
    """(place, local time) OpenWeatherMap says a current reading is for.

    The header names the configured city; this names the point the API
    actually used, so the two can be compared.
    """
    coord = data.get("coord") or {}
    place = data.get("name") or "unnamed point"
    if coord.get("lat") is not None and coord.get("lon") is not None:
        place += f" ({coord['lat']}, {coord['lon']})"
    ts = data.get("dt")
    when = _local(int(ts), data.get("timezone", 0)).strftime("%H:%M") if ts else "?"
    return place, when


def format_current(data: dict, label: str) -> str:
    main = data.get("main", {})
    weather = (data.get("weather") or [{}])[0]
    wind = data.get("wind", {})
    sys_block = data.get("sys", {})
    offset = data.get("timezone", 0)
    place, when = observation(data)

    lines = [f"Current weather — {label}"]
    lines.append(f"  Observed: {when} local at {place}")
    lines.append(
        f"  Conditions: {weather.get('description', 'unknown')}"
    )
    lines.append(
        f"  Temperature: {main.get('temp', '?')}C (feels like {main.get('feels_like', '?')}C)"
    )
    lines.append(f"  Humidity: {main.get('humidity', '?')}%")
    speed = wind.get("speed")
    lines.append(
        f"  Wind: {speed if speed is not None else '?'} m/s {wind_direction(wind.get('deg', 0))}"
    )
    visibility = data.get("visibility")
    if visibility is not None:
        lines.append(f"  Visibility: {visibility / 1000:.1f} km")
    lines.append(f"  Pressure: {main.get('pressure', '?')} hPa")
    sunrise, sunset = sys_block.get("sunrise"), sys_block.get("sunset")
    if sunrise and sunset:
        lines.append(
            f"  Sunrise: {_local(sunrise, offset).strftime('%H:%M')}, "
            f"Sunset: {_local(sunset, offset).strftime('%H:%M')} (local)"
        )
    lines.append(f"  {ATTRIBUTION}")
    return "\n".join(lines)


def group_forecast(data: dict) -> list[tuple[str, dict]]:
    """Group 3-hour forecast entries into per-day summaries.

    Returns [(date_str, {high, low, conditions, rain_prob})] in date order.
    """
    offset = data.get("city", {}).get("timezone", 0)
    days: dict[str, dict] = {}
    for entry in data.get("list", []):
        dt_local = _local(entry.get("dt", 0), offset)
        key = dt_local.strftime("%Y-%m-%d (%A)")
        main = entry.get("main", {})
        weather = (entry.get("weather") or [{}])[0]
        day = days.setdefault(key, {
            "high": float("-inf"), "low": float("inf"),
            "conditions": {}, "rain_prob": 0.0,
        })
        temp = main.get("temp")
        if temp is not None:
            day["high"] = max(day["high"], temp)
            day["low"] = min(day["low"], temp)
        desc = weather.get("description", "")
        if desc:
            day["conditions"][desc] = day["conditions"].get(desc, 0) + 1
        pop = entry.get("pop")
        if pop is not None:
            day["rain_prob"] = max(day["rain_prob"], float(pop))
    return sorted(days.items())


def format_forecast(data: dict, label: str, limit_days: int | None = None,
                    skip_today: bool = False) -> str:
    grouped = group_forecast(data)
    offset = data.get("city", {}).get("timezone", 0)
    today = _local(int(time.time()), offset).strftime("%Y-%m-%d")
    if skip_today:
        grouped = [g for g in grouped if not g[0].startswith(today)]
    if limit_days:
        grouped = grouped[:limit_days]

    lines = [f"Forecast — {label}"]
    for date_str, day in grouped:
        conditions = max(day["conditions"], key=day["conditions"].get) \
            if day["conditions"] else "unknown"
        rain = f", rain {int(day['rain_prob'] * 100)}%" if day["rain_prob"] > 0 else ""
        lines.append(
            f"  {date_str}: {day['low']:.0f}-{day['high']:.0f}C, {conditions}{rain}"
        )
    lines.append(f"  {ATTRIBUTION}")
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("now", "forecast", "tomorrow", "advice"):
        print("Usage: weather.py [now|forecast|tomorrow|advice] [city]")
        return 1

    cmd = args[0]
    city_arg = " ".join(args[1:]) if len(args) > 1 else None
    lat, lon, label = resolve_target(city_arg)

    if cmd == "now":
        print(format_current(get_current(lat, lon), label))
    elif cmd == "forecast":
        print(format_forecast(get_forecast(lat, lon), label))
    elif cmd == "tomorrow":
        print(format_forecast(
            get_forecast(lat, lon), label, limit_days=1, skip_today=True,
        ))
    elif cmd == "advice":
        print(format_current(get_current(lat, lon), label))
        print()
        print(format_forecast(get_forecast(lat, lon), label, limit_days=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
