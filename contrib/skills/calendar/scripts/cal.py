#!/usr/bin/env python3
"""
Read-only calendar viewer over ICS files and URLs. Stdlib only.

(Named cal.py, not calendar.py: the script directory is sys.path[0] in the
skill subprocess, and calendar.py would shadow the stdlib calendar module
that datetime.strptime depends on.)

Usage:
  cal.py today [--json]
  cal.py tomorrow [--json]
  cal.py week [--json]
  cal.py on 2026-05-20 [--json]
  cal.py next [--json]
  cal.py refresh          — re-fetch URL calendars
  cal.py list             — configured calendars

Config: workspace/skills-data/calendar/calendars.json (legacy config/calendars.json still honoured). File calendars are read fresh from
workspace-relative paths; URL calendars cache in skills-data/calendar/cache/
with a per-calendar refresh_minutes TTL.

RRULE support covers the common cases (DAILY/WEEKLY/MONTHLY/YEARLY with
INTERVAL, COUNT, UNTIL, and BYDAY for weekly); events with unsupported rules
are skipped with a warning on stderr.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))
SKILL_DATA = os.environ.get(
    "SKILL_DATA", os.path.join(WORKSPACE, "skills-data", "calendar"),
)
CACHE_DIR = Path(SKILL_DATA) / "cache"
SKILL_DATA = Path(os.environ.get(
    "SKILL_DATA", str(Path(WORKSPACE) / "skills-data" / "calendar"),
))
CONFIG_FILE = SKILL_DATA / "calendars.json"
LEGACY_CONFIG_FILE = Path(WORKSPACE) / "config" / "calendars.json"

USER_AGENT = "faffmonkey/0.1.0"
_MAX_ICS_BYTES = 10 * 1024 * 1024
_MAX_RRULE_ITERATIONS = 1000

_ICS_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def local_tz() -> ZoneInfo:
    location_path = Path(WORKSPACE) / "config" / "location.json"
    tz_name = ""
    if location_path.is_file():
        try:
            data = json.loads(location_path.read_text(encoding="utf-8"))
            tz_name = (data.get("current") or {}).get("timezone", "")
        except (json.JSONDecodeError, OSError):
            pass
    if not tz_name:
        tz_name = os.environ.get("TZ", "") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


# --- ICS parsing ---

def unfold_lines(text: str) -> list[str]:
    """RFC 5545 line unfolding: continuation lines start with space or tab."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """Split 'NAME;PARAM=V;PARAM2=V2:value' into (name, params, value)."""
    m = re.match(r"^([A-Za-z0-9-]+)((?:;[^:]*)?):(.*)$", line)
    if not m:
        return None
    name = m.group(1).upper()
    params: dict[str, str] = {}
    for part in (m.group(2) or "").lstrip(";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, m.group(3)


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def parse_dt(value: str, params: dict[str, str], default_tz: ZoneInfo):
    """Parse an ICS date or date-time. Returns (datetime|date, all_day)."""
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date(), True
    utc = value.endswith("Z")
    core = value.rstrip("Z")
    dt = datetime.strptime(core, "%Y%m%dT%H%M%S")
    if utc:
        return dt.replace(tzinfo=timezone.utc), False
    tzid = params.get("TZID", "")
    if tzid:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid)), False
        except (KeyError, ValueError):
            print(f"warning: unknown TZID {tzid!r}, using local tz", file=sys.stderr)
    return dt.replace(tzinfo=default_tz), False


def parse_rrule(value: str) -> dict[str, str]:
    rule: dict[str, str] = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.upper()] = v
    return rule


def parse_ics(text: str, default_tz: ZoneInfo) -> list[dict]:
    """Parse VEVENT blocks. Returns raw event dicts (pre-expansion)."""
    events: list[dict] = []
    current: dict | None = None
    for line in unfold_lines(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current is not None and "DTSTART" in current:
                events.append(current)
            current = None
            continue
        if current is None:
            continue
        prop = _parse_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name in ("DTSTART", "DTEND"):
            try:
                current[name] = parse_dt(value, params, default_tz)
            except ValueError:
                print(f"warning: bad {name}: {value!r}", file=sys.stderr)
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
            current[name] = _unescape(value)
        elif name == "RRULE":
            current["RRULE"] = parse_rrule(value)
    return events


# --- Recurrence expansion ---

def _advance(start, rule_freq: str, interval: int):
    if rule_freq == "DAILY":
        return start + timedelta(days=interval)
    if rule_freq == "WEEKLY":
        return start + timedelta(weeks=interval)
    if rule_freq == "MONTHLY":
        month = start.month - 1 + interval
        year = start.year + month // 12
        month = month % 12 + 1
        try:
            return start.replace(year=year, month=month)
        except ValueError:
            return None  # e.g. 31st in a shorter month; skip occurrence
    if rule_freq == "YEARLY":
        try:
            return start.replace(year=start.year + interval)
        except ValueError:
            return None
    return None


def expand_event(event: dict, window_start, window_end, default_tz: ZoneInfo) -> list[dict]:
    """Expand an event (with or without RRULE) into occurrences inside the
    window. window_start/window_end are timezone-aware datetimes."""
    start, all_day = event["DTSTART"]
    dtend = event.get("DTEND")
    if dtend:
        duration = _as_dt(dtend[0], default_tz, all_day) - _as_dt(start, default_tz, all_day)
    else:
        duration = timedelta(days=1) if all_day else timedelta(hours=1)

    rule = event.get("RRULE")
    occurrences: list = []

    if not rule:
        occurrences.append(start)
    else:
        freq = rule.get("FREQ", "")
        if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
            print(
                f"warning: unsupported RRULE FREQ={freq!r} for "
                f"{event.get('SUMMARY', '?')!r}, skipping",
                file=sys.stderr,
            )
            return []
        try:
            interval = max(1, int(rule.get("INTERVAL", "1")))
        except ValueError:
            interval = 1
        count = None
        if "COUNT" in rule:
            try:
                count = int(rule["COUNT"])
            except ValueError:
                count = None
        until = None
        if "UNTIL" in rule:
            try:
                until, _ = parse_dt(rule["UNTIL"], {}, default_tz)
            except ValueError:
                until = None

        bydays = []
        if freq == "WEEKLY" and rule.get("BYDAY"):
            bydays = [
                _ICS_WEEKDAYS[d] for d in rule["BYDAY"].split(",")
                if d in _ICS_WEEKDAYS
            ]

        current = start
        emitted = 0
        for _ in range(_MAX_RRULE_ITERATIONS):
            if count is not None and emitted >= count:
                break
            candidates = [current]
            if bydays:
                week_base = current - timedelta(days=current.weekday())
                candidates = [week_base + timedelta(days=d) for d in sorted(bydays)]
            stop = False
            for occ in candidates:
                if occ < start:
                    continue
                if count is not None and emitted >= count:
                    break
                if until is not None and _cmp_after(occ, until, default_tz, all_day):
                    stop = True
                    break
                emitted += 1
                occurrences.append(occ)
            if stop:
                break
            nxt = _advance(current, freq, interval)
            if nxt is None:
                current = _advance(current, freq, interval * 2)
                if current is None:
                    break
                continue
            current = nxt
            if _as_dt(current, default_tz, all_day) > window_end + duration:
                break

    results = []
    for occ in occurrences:
        occ_start = _as_dt(occ, default_tz, all_day)
        occ_end = occ_start + duration
        if occ_end <= window_start or occ_start >= window_end:
            continue
        results.append({
            "summary": event.get("SUMMARY", "(no title)"),
            "location": event.get("LOCATION", ""),
            "description": event.get("DESCRIPTION", ""),
            "start": occ_start,
            "end": occ_end,
            "all_day": all_day,
        })
    return results


def _as_dt(value, tz: ZoneInfo, all_day: bool) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz)
    return datetime(value.year, value.month, value.day, tzinfo=tz)


def _cmp_after(occ, until, tz: ZoneInfo, all_day: bool) -> bool:
    if isinstance(until, date) and not isinstance(until, datetime):
        until = datetime(until.year, until.month, until.day, 23, 59, tzinfo=tz)
    return _as_dt(occ, tz, all_day) > _as_dt(until, tz, False)


# --- Calendar sources ---

def load_config() -> list[dict]:
    config_file = CONFIG_FILE
    if not config_file.is_file() and LEGACY_CONFIG_FILE.is_file():
        config_file = LEGACY_CONFIG_FILE
        print(
            f"note: reading legacy {LEGACY_CONFIG_FILE}; move it to {CONFIG_FILE}",
            file=sys.stderr,
        )
    if not config_file.is_file():
        return []
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: calendars.json unreadable: {e}", file=sys.stderr)
        return []
    return data.get("calendars", [])


def _cache_path(name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
    return CACHE_DIR / f"{safe}.ics"


def fetch_calendar(cal: dict, force: bool = False) -> str | None:
    """Return ICS text for a configured calendar, or None on failure."""
    if cal.get("type") == "file":
        path = Path(WORKSPACE) / cal.get("path", "")
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"warning: {cal.get('name', '?')}: {e}", file=sys.stderr)
            return None

    if cal.get("type") == "url":
        cache = _cache_path(cal.get("name", "unnamed"))
        ttl = int(cal.get("refresh_minutes", 30)) * 60
        if not force and cache.is_file() and time.time() - cache.stat().st_mtime < ttl:
            return cache.read_text(encoding="utf-8", errors="replace")
        url = cal.get("url", "")
        if not url.startswith(("http://", "https://")):
            print(f"warning: {cal.get('name', '?')}: invalid url", file=sys.stderr)
            return None
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(_MAX_ICS_BYTES)
        except (urllib.error.URLError, OSError) as e:
            print(f"warning: {cal.get('name', '?')}: fetch failed: {e}", file=sys.stderr)
            if cache.is_file():
                return cache.read_text(encoding="utf-8", errors="replace")
            return None
        text = raw.decode("utf-8", errors="replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return text

    print(f"warning: {cal.get('name', '?')}: unknown type", file=sys.stderr)
    return None


def events_in_window(window_start: datetime, window_end: datetime) -> list[dict]:
    tz = local_tz()
    results: list[dict] = []
    for cal in load_config():
        text = fetch_calendar(cal)
        if text is None:
            continue
        for raw_event in parse_ics(text, tz):
            for occ in expand_event(raw_event, window_start, window_end, tz):
                occ["calendar"] = cal.get("name", "unnamed")
                results.append(occ)
    results.sort(key=lambda e: e["start"])
    return results


# --- Output ---

def format_events(events: list[dict], as_json: bool) -> str:
    if as_json:
        return json.dumps([
            {**e, "start": e["start"].isoformat(), "end": e["end"].isoformat()}
            for e in events
        ], indent=2)
    if not events:
        return "No events."
    lines: list[str] = []
    current_day = None
    for e in events:
        day = e["start"].strftime("%A %d %B")
        if day != current_day:
            lines.append(f"{day}:")
            current_day = day
        if e["all_day"]:
            when = "[all day]"
        else:
            when = f"[{e['start']:%H:%M}-{e['end']:%H:%M}]"
        lines.append(f"  {when} {e['summary']} ({e['calendar']})")
        if e["location"]:
            lines.append(f"    Location: {e['location']}")
    return "\n".join(lines)


def _day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]

    if not args:
        print("Usage: cal.py [today|tomorrow|week|on DATE|next|refresh|list] [--json]")
        return 1

    cmd = args[0]
    tz = local_tz()
    now = datetime.now(tz)

    if cmd == "refresh":
        for cal in load_config():
            if cal.get("type") == "url":
                ok = fetch_calendar(cal, force=True) is not None
                print(f"  {cal.get('name', '?')}: {'refreshed' if ok else 'failed'}")
        return 0

    if cmd == "list":
        cals = load_config()
        if not cals:
            print("No calendars configured (workspace/skills-data/calendar/calendars.json).")
            return 0
        for cal in cals:
            text = fetch_calendar(cal)
            count = "?"
            if text is not None:
                raw = parse_ics(text, tz)
                count = str(len(raw))
            source = cal.get("path") or cal.get("url", "")
            print(f"  {cal.get('name', '?')} ({cal.get('type', '?')}): {count} event(s) — {source}")
        return 0

    if cmd == "today":
        start, end = _day_window(now.date(), tz)
    elif cmd == "tomorrow":
        start, end = _day_window(now.date() + timedelta(days=1), tz)
    elif cmd == "week":
        start, _ = _day_window(now.date(), tz)
        end = start + timedelta(days=7)
    elif cmd == "on":
        if len(args) < 2:
            print("Usage: cal.py on YYYY-MM-DD")
            return 1
        try:
            target = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date: {args[1]}")
            return 1
        start, end = _day_window(target, tz)
    elif cmd == "next":
        start, end = now, now + timedelta(days=90)
        events = [e for e in events_in_window(start, end) if e["start"] >= now]
        print(format_events(events[:1], as_json) if events else "No upcoming events.")
        return 0
    else:
        print(f"Unknown command: {cmd}")
        return 1

    print(format_events(events_in_window(start, end), as_json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
