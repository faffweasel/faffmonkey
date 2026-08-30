"""Sensor entry point: what a session "none" cron job runs.

Appends the current conditions and the short-range rain outlook to
workspace/readings/weather.jsonl, and drops a heartbeat trigger when rain
becomes likely within the lookahead. The trigger fires on the dry-to-wet
transition and re-arms once the outlook is dry again, so a showery day
warns once per shower, not once per run.

Config in skills-data/weather/config.json (all optional):

    {"rain_lookahead_hours": 3, "rain_probability": 0.5}
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import weather

READINGS_KEEP_DAYS = 7
FORECAST_STEP_SECONDS = 3 * 3600


def _workspace() -> Path:
    workspace = os.environ.get("WORKSPACE", "")
    if not workspace:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        workspace = os.path.normpath(os.path.join(script_dir, "..", "..", ".."))
    return Path(workspace)


def _skill_data() -> Path:
    return Path(os.environ.get("SKILL_DATA") or _workspace() / "skills-data" / "weather")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def append_reading(workspace: Path, source: str, reading: dict, now: datetime) -> None:
    """One line per run; lines older than READINGS_KEEP_DAYS are dropped."""
    path = workspace / "readings" / f"{source}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = (now - timedelta(days=READINGS_KEEP_DAYS)).isoformat()
    kept: list[str] = []
    try:
        for line in path.read_text().splitlines():
            try:
                if str(json.loads(line).get("at", "")) >= cutoff:
                    kept.append(line)
            except (json.JSONDecodeError, AttributeError):
                continue
    except OSError:
        pass
    kept.append(json.dumps(reading))
    path.write_text("\n".join(kept) + "\n")


def write_trigger(workspace: Path, name: str, trigger: dict) -> None:
    tray = workspace / "skills-data" / "heartbeat" / "triggers.d"
    tray.mkdir(parents=True, exist_ok=True)
    (tray / f"{name}.json").write_text(json.dumps(trigger, indent=2) + "\n")


def rain_outlook(forecast: dict, now_ts: float, lookahead_hours: int, threshold: float) -> tuple[bool, str]:
    """Whether rain is likely within the lookahead, and a phrase saying so.

    Forecast slots are three-hourly, so the slot that started before now
    still counts: it is the one describing the next hour or two.
    """
    offset = forecast.get("city", {}).get("timezone", 0)
    horizon = now_ts + lookahead_hours * 3600
    best: tuple[float, int, str] | None = None
    for entry in forecast.get("list", []):
        dt = int(entry.get("dt", 0))
        if dt < now_ts - FORECAST_STEP_SECONDS or dt > horizon:
            continue
        pop = float(entry.get("pop") or 0)
        if best is None or pop > best[0]:
            best = (pop, dt, (entry.get("weather") or [{}])[0].get("description", ""))
    if best is None:
        return False, "no forecast slots within the lookahead"
    pop, dt, description = best
    when = weather._local(dt, offset).strftime("%H:%M")
    phrase = f"{int(pop * 100)}% chance of rain around {when}"
    if description:
        phrase += f" ({description})"
    return pop >= threshold, phrase


def main() -> int:
    now = datetime.now(ZoneInfo(os.environ.get("TZ", "UTC")))
    workspace = _workspace()
    skill_data = _skill_data()
    config = _load_json(skill_data / "config.json")
    lookahead = int(config.get("rain_lookahead_hours", 3))
    probability = float(config.get("rain_probability", 0.5))

    lat, lon, label = weather.resolve_target(None)
    current = weather.get_current(lat, lon)
    forecast = weather.get_forecast(lat, lon)

    main_block = current.get("main", {})
    conditions = (current.get("weather") or [{}])[0].get("description", "unknown")
    wind = current.get("wind", {})
    wet, outlook = rain_outlook(forecast, now.timestamp(), lookahead, probability)
    place, observed = weather.observation(current)
    summary = (
        f"{conditions}, {main_block.get('temp', '?')}C feels {main_block.get('feels_like', '?')}C, "
        f"humidity {main_block.get('humidity', '?')}%, wind {wind.get('speed', '?')} m/s at {label} "
        f"(observed {observed} at {place}); next {lookahead}h: {outlook}"
    )
    append_reading(workspace, "weather", {
        "at": now.isoformat(timespec="seconds"),
        "summary": summary,
        "data": {
            "temp": main_block.get("temp"),
            "feels_like": main_block.get("feels_like"),
            "humidity": main_block.get("humidity"),
            "wind": wind.get("speed"),
            "conditions": conditions,
            "place": place,
            "observed": observed,
            "rain_likely": wet,
            "rain_outlook": outlook,
        },
    }, now)

    state_path = skill_data / "watch.json"
    state = _load_json(state_path)
    last_state = state.get("last_state", "dry")
    new_state = "wet" if wet else "dry"
    if new_state != last_state:
        # State first, then the trigger.
        skill_data.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({**state, "last_state": new_state}, indent=2) + "\n")
    if wet and last_state == "dry":
        write_trigger(workspace, "weather-rain", {
            "at": now.isoformat(timespec="seconds"),
            "source": "weather",
            "kind": "alert",
            "text": f"Rain likely within {lookahead}h: {outlook}",
        })
        print(f"reading: {summary}; trigger written")
        return 0
    print(f"reading: {summary}; {new_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
