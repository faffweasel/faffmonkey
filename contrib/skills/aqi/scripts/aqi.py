#!/usr/bin/env python3
"""
AQI data from aqicn.org API — improved with station awareness.

Features:
  - Search stations in a city/area
  - Fetch by station ID, or pin one so `current` always uses it
  - Fetch by lat/lng (nearest station)
  - Fetch by city name (default API behaviour)
  - Multi-station aggregation for a bounding box
  - Per-pollutant breakdown (PM2.5, PM10, O3, NO2, CO, SO2)
  - Forecast (3-5 days PM2.5)

Free tier: 1000 calls/day.
"""
import json
import os
import sys
import urllib.error
import urllib.request

TOKEN = os.environ.get("AQICN_API_KEY", "")
BASE = "https://api.waqi.info"
USER_AGENT = "faffmonkey"


def _fetch(url):
    """Fetch JSON from WAQI API. Returns parsed dict or error dict."""
    if not TOKEN:
        return {"error": "AQICN_API_KEY not set"}
    # Append token if not already in URL
    sep = "&" if "?" in url else "?"
    if "token=" not in url:
        url = f"{url}{sep}token={TOKEN}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "ok":
            return {"error": f"API error: {data.get('data', 'unknown')}"}
        return data
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def _parse_feed(data):
    """Extract structured info from a /feed/ response."""
    d = data.get("data", {})
    if isinstance(d, str):
        return {"error": d}

    # Per-pollutant breakdown from iaqi
    iaqi = d.get("iaqi", {})
    pollutants = {}
    for key in ("pm25", "pm10", "o3", "no2", "co", "so2"):
        if key in iaqi:
            pollutants[key] = iaqi[key].get("v")

    city_info = d.get("city", {})

    return {
        "aqi": d.get("aqi"),
        "dominant": d.get("dominentpol", "unknown"),
        "city": city_info.get("name", "unknown"),
        "geo": city_info.get("geo", []),
        "url": city_info.get("url", ""),
        "time": d.get("time", {}).get("s", "unknown"),
        "pollutants": pollutants,
        "attributions": [a.get("name", "") for a in d.get("attributions", [])],
    }


def search_stations(keyword):
    """
    Search for AQI monitoring stations by keyword.
    Returns list of {uid, name, aqi, geo, url}.
    """
    url = f"{BASE}/search/?keyword={urllib.request.quote(keyword)}&token={TOKEN}"
    data = _fetch(url)
    if "error" in data:
        return data
    results = []
    for item in data.get("data", []):
        station = item.get("station", {})
        results.append({
            "uid": item.get("uid"),
            "aqi": item.get("aqi"),
            "name": station.get("name", "unknown"),
            "geo": station.get("geo", []),
            "url": station.get("url", ""),
            "time": item.get("time", {}).get("stime", ""),
        })
    return results


def get_aqi(city_or_id):
    """Get current AQI for a city name. Returns structured dict."""
    data = _fetch(f"{BASE}/feed/{urllib.request.quote(city_or_id)}/")
    if "error" in data:
        return data
    return _parse_feed(data)


def get_aqi_by_id(station_uid):
    """Get current AQI for a specific station by its numeric UID."""
    data = _fetch(f"{BASE}/feed/@{station_uid}/")
    if "error" in data:
        return data
    return _parse_feed(data)


def get_aqi_by_geo(lat, lng):
    """Get current AQI from the nearest station to lat/lng."""
    data = _fetch(f"{BASE}/feed/geo:{lat};{lng}/")
    if "error" in data:
        return data
    return _parse_feed(data)


def get_stations_in_bounds(lat1, lng1, lat2, lng2):
    """
    Get all stations within a bounding box.
    lat1,lng1 = south-west corner; lat2,lng2 = north-east corner.
    Returns list of {uid, aqi, name, lat, lng}.
    """
    latlng = f"{lat1},{lng1},{lat2},{lng2}"
    url = f"{BASE}/map/bounds/?latlng={latlng}&token={TOKEN}"
    data = _fetch(url)
    if "error" in data:
        return data
    results = []
    for item in data.get("data", []):
        results.append({
            "uid": item.get("uid"),
            "aqi": item.get("aqi"),
            "name": item.get("station", {}).get("name", "unknown"),
            "lat": item.get("lat"),
            "lng": item.get("lon"),
        })
    return results


def _coordinate_problem(lat: float, lng: float) -> str:
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
    """
    Load location from workspace/config/location.json.
    Workspace resolution: WORKSPACE env var if set, else derived from the
    script location (skills/<name>/scripts/ is three levels below workspace).
    Returns (lat, lng, city) or None.
    """
    candidates = []
    workspace = os.environ.get("WORKSPACE", "")
    if not workspace:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        workspace = os.path.normpath(os.path.join(script_dir, "..", "..", ".."))
    candidates.append(os.path.join(workspace, "config", "location.json"))
    candidates.append(os.path.join(workspace, "location.json"))

    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
            loc = data.get("current") or data.get("home") or {}
            lat = loc.get("lat")
            lng = loc.get("lng")
            city = loc.get("city", "unknown")
            if lat is not None and lng is not None:
                return (float(lat), float(lng), city)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            continue
    return None


def _pin_path():
    """Where the pinned station lives: the skill's data directory."""
    data_dir = os.environ.get("SKILL_DATA", "")
    if not data_dir:
        workspace = os.environ.get("WORKSPACE", "")
        if not workspace:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            workspace = os.path.normpath(os.path.join(script_dir, "..", "..", ".."))
        data_dir = os.path.join(workspace, "skills-data", "aqi")
    return os.path.join(data_dir, "config.json")


def load_pin():
    """The pinned station as {"uid": ..., "name": ...}, or None."""
    try:
        with open(_pin_path()) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    pin = data.get("pinned_station") if isinstance(data, dict) else None
    if not isinstance(pin, dict) or "uid" not in pin:
        return None
    return pin


def save_pin(pin):
    """Write or clear (None) the pinned station, keeping other keys."""
    path = _pin_path()
    data = {}
    try:
        with open(path) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        pass
    if pin is None:
        data.pop("pinned_station", None)
    else:
        data["pinned_station"] = pin
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def get_current():
    """The reading to give when nobody named a station.

    The pinned station if there is one; otherwise the multi-station
    aggregate around the configured location. A box average is the wrong
    answer once the user has seen one station read 4 and the next read
    101, which is why pinning exists.
    """
    pin = load_pin()
    if pin is not None:
        result = get_aqi_by_id(pin["uid"])
        if "error" not in result:
            result["pinned"] = True
        return result
    result = get_local_multi()
    if isinstance(result, dict) and "error" in result:
        result["error"] += " (or pin a station: aqi pin <uid>)"
    return result


def get_local_multi():
    """
    Get AQI from multiple stations near the configured location.
    Reads lat/lng from location.json and builds a bounding box
    0.05 degrees (about 5km) around it.
    Returns individual readings + summary stats.
    """
    radius = 0.05
    loc = _load_location()
    if not loc:
        return {"error": "No location configured — check location.json"}
    lat, lng, city = loc
    problem = _coordinate_problem(lat, lng)
    if problem:
        return {"error": f"location.json for {city}: {problem}; set the real coordinates"}

    stations = get_stations_in_bounds(
        lat - radius, lng - radius, lat + radius, lng + radius
    )
    if isinstance(stations, dict) and "error" in stations:
        return stations
    if not stations:
        return {"error": f"No stations found near {city} ({lat}, {lng})"}

    # Filter to numeric AQI values
    valid = []
    for s in stations:
        try:
            s["aqi_num"] = int(s["aqi"]) if s["aqi"] != "-" else None
        except (ValueError, TypeError):
            s["aqi_num"] = None
        if s["aqi_num"] is not None:
            valid.append(s)

    if not valid:
        return {"stations": stations, "count": 0, "city": city,
                "summary": "No numeric AQI readings available"}

    aqis = [s["aqi_num"] for s in valid]
    return {
        "stations": valid,
        "city": city,
        "count": len(valid),
        "min_aqi": min(aqis),
        "max_aqi": max(aqis),
        "avg_aqi": round(sum(aqis) / len(aqis)),
        "worst": max(valid, key=lambda s: s["aqi_num"])["name"],
        "best": min(valid, key=lambda s: s["aqi_num"])["name"],
    }


def get_forecast(city_or_id):
    """Get AQI forecast (3-5 days). Returns list of daily forecasts per pollutant."""
    data = _fetch(f"{BASE}/feed/{urllib.request.quote(city_or_id)}/")
    if "error" in data:
        return data
    forecast = data.get("data", {}).get("forecast", {}).get("daily", {})
    result = {}
    for pollutant in ("pm25", "pm10", "o3"):
        entries = forecast.get(pollutant, [])
        if entries:
            result[pollutant] = [
                {"date": d["day"], "avg": d["avg"], "min": d["min"], "max": d["max"]}
                for d in entries
            ]
    return result if result else {"error": "No forecast data available"}


def aqi_description(value):
    """Human-readable AQI level."""
    if value is None:
        return "unknown"
    try:
        v = int(value)
    except (ValueError, TypeError):
        return "unknown"
    if v <= 50:
        return "Good"
    if v <= 100:
        return "Moderate"
    if v <= 150:
        return "Unhealthy for sensitive groups"
    if v <= 200:
        return "Unhealthy"
    if v <= 300:
        return "Very unhealthy"
    return "Hazardous"


def _print_reading(result):
    level = aqi_description(result['aqi'])
    poll_str = _format_pollutants(result.get('pollutants', {}))
    tag = " [pinned station]" if result.get("pinned") else ""
    print(f"{result['city']}: AQI {result['aqi']} ({level}){tag}")
    print(f"  Dominant: {result['dominant']}, Measured: {result['time']}")
    if poll_str:
        print(f"  Pollutants: {poll_str}")
    if result.get('attributions'):
        print(f"  Source: {result['attributions'][0]}")


def _print_multi(result):
    city = result.get("city", "Local")
    if isinstance(result, dict) and "error" in result:
        print(f"Error: {result['error']}")
    elif result.get("count", 0) == 0:
        print(f"{city} AQI — no stations with valid readings")
        if result.get("stations"):
            print(f"  ({len(result['stations'])} station(s) reporting '-' or offline)")
    else:
        print(f"{city} AQI — {result['count']} stations reporting:")
        print(f"  Range: {result['min_aqi']} – {result['max_aqi']} (avg {result['avg_aqi']})")
        print(f"  Best:  {result['best']} ({result['min_aqi']})")
        print(f"  Worst: {result['worst']} ({result['max_aqi']})")
        print(f"  Overall: {aqi_description(result['avg_aqi'])}")
        print()
        for st in sorted(result['stations'], key=lambda x: x.get('aqi_num', 0), reverse=True):
            level = aqi_description(st['aqi_num'])
            print(f"  {st['aqi_num']:>4} ({level:>30}) — {st['name']}")


def _format_pollutants(pollutants):
    """Format pollutant dict for display."""
    if not pollutants:
        return ""
    labels = {"pm25": "PM2.5", "pm10": "PM10", "o3": "O₃", "no2": "NO₂", "co": "CO", "so2": "SO₂"}
    parts = []
    for key, val in pollutants.items():
        label = labels.get(key, key)
        parts.append(f"{label}={val}")
    return ", ".join(parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  aqi.py <city>                  — AQI for city (API's default station)")
        print("  aqi.py <city> forecast          — 3-5 day forecast")
        print("  aqi.py search <keyword>         — find stations in area")
        print("  aqi.py station <uid>            — AQI from specific station ID")
        print("  aqi.py geo <lat> <lng>          — nearest station to coordinates")
        print("  aqi.py multi                    — all stations near configured location")
        print("  aqi.py current                  — the pinned station, else multi")
        print("  aqi.py pin <uid>                — make a station the default for current")
        print("  aqi.py pin                      — show the pinned station")
        print("  aqi.py unpin                    — clear it")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "current":
        result = get_current()
        if isinstance(result, dict) and "error" in result:
            print(f"Error: {result['error']}")
        elif result.get("pinned"):
            _print_reading(result)
        else:
            _print_multi(result)
        sys.exit(0)

    if cmd == "pin":
        if len(sys.argv) < 3:
            pin = load_pin()
            print(f"Pinned station: UID {pin['uid']} ({pin.get('name', '?')})" if pin else "No station pinned.")
            sys.exit(0)
        uid = sys.argv[2]
        if not uid.isdigit():
            print("Error: station UID must be a number (find one with: aqi search <area>)")
            sys.exit(1)
        result = get_aqi_by_id(uid)
        if "error" in result:
            print(f"Error: station {uid} could not be read, not pinned: {result['error']}")
            sys.exit(1)
        save_pin({"uid": int(uid), "name": result["city"]})
        print(f"Pinned station UID {uid}: {result['city']} (AQI {result['aqi']} now). `aqi current` uses it from now on.")
        sys.exit(0)

    if cmd == "unpin":
        save_pin(None)
        print("Unpinned. `aqi current` falls back to all stations near the configured location.")
        sys.exit(0)

    if cmd == "search":
        keyword = sys.argv[2] if len(sys.argv) > 2 else "hanoi"
        results = search_stations(keyword)
        if isinstance(results, dict) and "error" in results:
            print(f"Error: {results['error']}")
        else:
            print(f"Found {len(results)} station(s) for '{keyword}':")
            for s in results:
                level = aqi_description(s['aqi'])
                geo_str = f"({s['geo'][0]:.4f}, {s['geo'][1]:.4f})" if len(s.get('geo', [])) >= 2 else ""
                print(f"  UID {s['uid']:>6}: AQI {str(s['aqi']):>4} ({level:>30}) — {s['name']} {geo_str}")

    elif cmd == "station":
        if len(sys.argv) < 3:
            print("Usage: aqi.py station <uid>")
            sys.exit(1)
        uid = sys.argv[2]
        result = get_aqi_by_id(uid)
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            _print_reading(result)

    elif cmd == "geo":
        if len(sys.argv) < 4:
            print("Usage: aqi.py geo <lat> <lng>")
            sys.exit(1)
        lat, lng = sys.argv[2], sys.argv[3]
        result = get_aqi_by_geo(lat, lng)
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            _print_reading(result)

    elif cmd in ("hanoi-multi", "multi", "local-multi"):
        _print_multi(get_local_multi())

    elif len(sys.argv) > 2 and sys.argv[2] == "forecast":
        city = sys.argv[1]
        result = get_forecast(city)
        if isinstance(result, dict) and "error" in result:
            print(f"Error: {result['error']}")
        else:
            for pollutant, days in result.items():
                labels = {"pm25": "PM2.5", "pm10": "PM10", "o3": "O₃"}
                print(f"\n{labels.get(pollutant, pollutant)} forecast:")
                for day in days:
                    print(f"  {day['date']}: avg {day['avg']} (range {day['min']}-{day['max']}) — {aqi_description(day['avg'])}")

    else:
        # Default: city lookup
        city = sys.argv[1]
        result = get_aqi(city)
        if "error" in result:
            print(f"Error: {result['error']}")
        else:
            _print_reading(result)
