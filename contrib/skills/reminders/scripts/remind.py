#!/usr/bin/env python3
"""
Natural language reminders over faffmonkey's cron system. Stdlib only.

Usage:
  remind.py add "call mum" "tomorrow 9am"
  remind.py add "take medicine" "in 2 hours"
  remind.py add "check flight" "2026-05-20 14:00"
  remind.py add "weekly standup" "every monday 10am"
  remind.py list
  remind.py remove <id>
  remind.py check          — fire due reminders (called by cron)

Reminders are stored in workspace/skills-data/reminders/reminders.json
(legacy config/reminders.json is still read; writes go to the new path). Timezone comes from
config/location.json (current.timezone), else the TZ env var, else UTC.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = os.environ.get("WORKSPACE", "")
if not WORKSPACE:
    WORKSPACE = os.path.dirname(os.path.dirname(SKILL_DIR))

SKILL_DATA = Path(os.environ.get(
    "SKILL_DATA", str(Path(WORKSPACE) / "skills-data" / "reminders"),
))
# Reads fall back to the legacy location; writes always go to the new one,
# so the store migrates itself on the first change.
REMINDERS_FILE = SKILL_DATA / "reminders.json"
LEGACY_REMINDERS_FILE = Path(WORKSPACE) / "config" / "reminders.json"

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_UNIT_SECONDS = {
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
}


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


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Parse 9am, 9:30pm, 14:00, 14 into (hour, minute)."""
    s = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_when(text: str, now: datetime) -> tuple[datetime, str | None]:
    """Parse a natural-language time. Returns (fire_at, recurring_cron).

    recurring_cron is a cron expression for "every ..." phrases, else None.
    Raises ValueError on unparseable or past times.
    """
    s = " ".join(text.strip().lower().split())

    # in N units
    m = re.fullmatch(r"in\s+(\d+)\s*([a-z]+)", s)
    if m:
        unit = m.group(2)
        if unit not in _UNIT_SECONDS:
            raise ValueError(f"unknown time unit: {unit!r}")
        return now + timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[unit]), None

    # every day / every <weekday> [time]
    m = re.fullmatch(r"every\s+(\w+)(?:\s+(.*))?", s)
    if m:
        target, clock_text = m.group(1), m.group(2) or "9am"
        clock = _parse_clock(clock_text)
        if clock is None:
            raise ValueError(f"can't parse time: {clock_text!r}")
        hour, minute = clock
        if target in ("day", "morning"):
            cron = f"{minute} {hour} * * *"
            fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if fire <= now:
                fire += timedelta(days=1)
            return fire, cron
        if target in _WEEKDAYS:
            weekday = _WEEKDAYS[target]
            cron = f"{minute} {hour} * * {(weekday + 1) % 7}"
            fire = _next_weekday(now, weekday, hour, minute)
            return fire, cron
        raise ValueError(f"can't parse recurrence: {target!r}")

    # tomorrow / today / tonight [time]
    m = re.fullmatch(r"(tomorrow|today|tonight)(?:\s+(.*))?", s)
    if m:
        word, clock_text = m.group(1), m.group(2)
        if clock_text:
            clock = _parse_clock(clock_text)
            if clock is None:
                raise ValueError(f"can't parse time: {clock_text!r}")
        else:
            clock = (20, 0) if word == "tonight" else (9, 0)
        hour, minute = clock
        fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if word == "tomorrow":
            fire += timedelta(days=1)
        if fire <= now:
            raise ValueError(f"that time has already passed ({fire:%H:%M})")
        return fire, None

    # [next] <weekday> [time]
    m = re.fullmatch(r"(?:next\s+)?(\w+)(?:\s+(.*))?", s)
    if m and m.group(1) in _WEEKDAYS:
        clock = _parse_clock(m.group(2) or "9am")
        if clock is None:
            raise ValueError(f"can't parse time: {m.group(2)!r}")
        hour, minute = clock
        return _next_weekday(now, _WEEKDAYS[m.group(1)], hour, minute), None

    # absolute: YYYY-MM-DD [HH:MM]
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?", s)
    if m:
        date_part = m.group(1)
        time_part = m.group(2) or "09:00"
        try:
            fire = datetime.strptime(
                f"{date_part} {time_part}", "%Y-%m-%d %H:%M",
            ).replace(tzinfo=now.tzinfo)
        except ValueError as e:
            raise ValueError(f"invalid date: {e}")
        if fire <= now:
            raise ValueError("that time is in the past")
        return fire, None

    # bare clock time -> today, or tomorrow if passed
    clock = _parse_clock(s)
    if clock is not None:
        hour, minute = clock
        fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if fire <= now:
            fire += timedelta(days=1)
        return fire, None

    raise ValueError(f"can't parse when: {text!r}")


def _next_weekday(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - now.weekday()) % 7
    fire += timedelta(days=days_ahead)
    if fire <= now:
        fire += timedelta(days=7)
    return fire


def next_occurrence(cron: str, after: datetime) -> datetime:
    """Next fire time for the limited cron shapes this skill generates:
    'M H * * *' (daily) and 'M H * * D' (weekly, D: 0=Sunday..6)."""
    parts = cron.split()
    minute, hour, dow = int(parts[0]), int(parts[1]), parts[4]
    fire = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dow == "*":
        while fire <= after:
            fire += timedelta(days=1)
        return fire
    weekday = (int(dow) - 1) % 7  # cron 0=Sunday -> python 6
    days_ahead = (weekday - fire.weekday()) % 7
    fire += timedelta(days=days_ahead)
    while fire <= after:
        fire += timedelta(days=7)
    return fire


def load_reminders() -> dict:
    """The default shape, or the file's contents when they match it.

    Four callers index ["reminders"] and iterate it expecting dicts. The
    file is hand-editable, so anything else here reached them as a TypeError
    or an AttributeError with no mention of the file.
    """
    reminders_file = REMINDERS_FILE
    if not reminders_file.is_file() and LEGACY_REMINDERS_FILE.is_file():
        reminders_file = LEGACY_REMINDERS_FILE
    if not reminders_file.is_file():
        return {"reminders": []}
    try:
        data = json.loads(reminders_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"Cannot read {reminders_file}: {e}", file=sys.stderr)
        return {"reminders": []}
    if not isinstance(data, dict) or not isinstance(data.get("reminders"), list):
        print(f"Ignoring malformed {reminders_file}", file=sys.stderr)
        return {"reminders": []}
    entries = [r for r in data["reminders"] if isinstance(r, dict)]
    if len(entries) != len(data["reminders"]):
        print(f"Ignoring non-object entries in {reminders_file}", file=sys.stderr)
    data["reminders"] = entries
    return data


def save_reminders(data: dict) -> None:
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMINDERS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _new_id(data: dict) -> str:
    numbers = [
        int(m.group(1))
        for r in data["reminders"]
        if (m := re.fullmatch(r"rem_(\d+)", r.get("id", "")))
    ]
    return f"rem_{(max(numbers) + 1) if numbers else 1:03d}"


def cmd_add(text: str, when: str) -> int:
    now = datetime.now(local_tz())
    try:
        fire_at, recurring = parse_when(when, now)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    data = load_reminders()
    reminder = {
        "id": _new_id(data),
        "text": text,
        "fire_at": fire_at.isoformat(timespec="seconds"),
        "recurring": recurring,
        "created": now.isoformat(timespec="seconds"),
    }
    data["reminders"].append(reminder)
    save_reminders(data)

    kind = f"recurring ({recurring})" if recurring else "one-shot"
    print(f"Added {reminder['id']} ({kind}): \"{text}\"")
    print(f"  Next: {fire_at:%A %d %B %Y, %H:%M} ({fire_at.tzname() or 'local'})")
    return 0


def cmd_list() -> int:
    data = load_reminders()
    pending = data["reminders"]
    if not pending:
        print("No pending reminders.")
        return 0
    for r in sorted(pending, key=lambda x: x.get("fire_at", "")):
        marker = " (recurring)" if r.get("recurring") else ""
        try:
            fire = datetime.fromisoformat(r["fire_at"])
            when = f"{fire:%a %d %b %H:%M}"
        except (KeyError, ValueError):
            when = "?"
        print(f"  {r['id']}: \"{r['text']}\" — {when}{marker}")
    return 0


def cmd_remove(reminder_id: str) -> int:
    data = load_reminders()
    before = len(data["reminders"])
    data["reminders"] = [r for r in data["reminders"] if r.get("id") != reminder_id]
    if len(data["reminders"]) == before:
        print(f"No reminder with id {reminder_id}")
        return 1
    save_reminders(data)
    print(f"Removed {reminder_id}")
    return 0


def cmd_check() -> int:
    now = datetime.now(local_tz())
    data = load_reminders()
    fired = 0
    keep: list[dict] = []
    for r in data["reminders"]:
        try:
            fire_at = datetime.fromisoformat(r["fire_at"])
        except (KeyError, ValueError):
            keep.append(r)
            continue
        if fire_at > now:
            keep.append(r)
            continue

        print(f"REMINDER: {r['text']}")
        fired += 1
        if r.get("recurring"):
            r["fire_at"] = next_occurrence(r["recurring"], now).isoformat(
                timespec="seconds",
            )
            keep.append(r)
        # one-shot fired reminders are dropped

    data["reminders"] = keep
    save_reminders(data)
    if fired == 0:
        # NO_REPLY suppresses cron channel delivery when nothing is due
        print("NO_REPLY")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: remind.py [add \"text\" \"when\" | list | remove <id> | check]")
        return 1
    cmd = args[0]
    if cmd == "add":
        if len(args) < 3:
            print("Usage: remind.py add \"text\" \"when\"")
            return 1
        return cmd_add(args[1], " ".join(args[2:]))
    if cmd == "list":
        return cmd_list()
    if cmd == "remove":
        if len(args) < 2:
            print("Usage: remind.py remove <id>")
            return 1
        return cmd_remove(args[1])
    if cmd == "check":
        return cmd_check()
    print(f"Unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
