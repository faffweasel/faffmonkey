"""Sensor entry point: what a session "none" cron job runs.

Appends the current reading to workspace/readings/aqi.jsonl and, when the
AQI is above the watch threshold, drops a heartbeat trigger once a day.

Config in skills-data/aqi/config.json (the same file as the station pin):

    {"watch_threshold": 180}

No threshold means readings only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aqi

READINGS_KEEP_DAYS = 7


def _workspace() -> Path:
    workspace = os.environ.get("WORKSPACE", "")
    if not workspace:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        workspace = os.path.normpath(os.path.join(script_dir, "..", "..", ".."))
    return Path(workspace)


def _skill_data() -> Path:
    return Path(os.environ.get("SKILL_DATA") or _workspace() / "skills-data" / "aqi")


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


def main() -> int:
    now = datetime.now(ZoneInfo(os.environ.get("TZ", "UTC")))
    workspace = _workspace()
    skill_data = _skill_data()

    result = aqi.get_current()
    if not isinstance(result, dict) or "error" in result:
        detail = result.get("error") if isinstance(result, dict) else result
        print(f"error: {detail}", file=sys.stderr)
        return 1
    city = result.get("city", "configured location")
    if isinstance(result.get("aqi"), (int, float)):
        # One station: the pinned one, or the API's pick.
        value = result["aqi"]
        level = aqi.aqi_description(value)
        summary = f"AQI {value} ({level}) at {city}, dominant {result.get('dominant', '?')}"
        data = {"aqi": value, "level": level, "city": city, "dominant": result.get("dominant")}
    elif isinstance(result.get("avg_aqi"), (int, float)):
        # Every station near the configured location, averaged.
        value = result["avg_aqi"]
        level = aqi.aqi_description(value)
        summary = (
            f"AQI {value} ({level}) average of {result.get('count', '?')} stations near {city}, "
            f"range {result.get('min_aqi', '?')}-{result.get('max_aqi', '?')}, worst {result.get('worst', '?')}"
        )
        data = {
            "aqi": value, "level": level, "city": city,
            "min": result.get("min_aqi"), "max": result.get("max_aqi"), "worst": result.get("worst"),
        }
    else:
        print(f"error: no AQI value in reading: {result}", file=sys.stderr)
        return 1
    append_reading(workspace, "aqi", {
        "at": now.isoformat(timespec="seconds"),
        "summary": summary,
        "data": data,
    }, now)

    threshold = _load_json(skill_data / "config.json").get("watch_threshold")
    if not isinstance(threshold, (int, float)):
        print(f"reading: {summary} (no watch threshold)")
        return 0
    if value <= threshold:
        print(f"reading: {summary}; at or below {threshold}")
        return 0
    state_path = skill_data / "watch.json"
    state = _load_json(state_path)
    today = now.date().isoformat()
    if state.get("last_alerted") == today:
        print(f"reading: {summary}; above {threshold}, already alerted today")
        return 0
    # State first, then the trigger: a wake that fails must not re-alert
    # every tick for the rest of the day.
    skill_data.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({**state, "last_alerted": today}, indent=2) + "\n")
    write_trigger(workspace, "aqi-high", {
        "at": now.isoformat(timespec="seconds"),
        "source": "aqi",
        "kind": "alert",
        "text": f"{summary}, above your {threshold} threshold",
    })
    print(f"reading: {summary}; trigger written (above {threshold})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
