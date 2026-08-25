#!/usr/bin/env python3
"""
Timezone conversion with DST handling.

Uses IANA timezone names (Europe/London, Asia/Tokyo) which
automatically handle DST transitions. Common abbreviations
are mapped to IANA names via aliases config.

Key DST gotcha: "GMT" is a fixed offset (UTC+0, always).
"Europe/London" switches between GMT (winter) and BST (summer).
When someone says "UK time" they mean Europe/London, not GMT.
This script defaults ambiguous abbreviations to the location-
based name that handles DST correctly.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _workspace() -> Path:
    ws = os.environ.get("WORKSPACE", "")
    if ws:
        return Path(ws)
    return Path(__file__).resolve().parent.parent.parent.parent

# Default aliases — overridden by skills-data/timezone/aliases.json if
# present (legacy config/aliases.json still honoured)
DEFAULT_ALIASES = {
    # Common abbreviations → IANA names (DST-aware)
    "uk": "Europe/London",
    "london": "Europe/London",
    "cambridge": "Europe/London",
    "wales": "Europe/London",
    "gmt": "Europe/London",       # Treat GMT as UK time (handles BST)
    "bst": "Europe/London",       # BST is just London in summer
    "hanoi": "Asia/Bangkok",      # Vietnam uses ICT = Asia/Bangkok
    "vietnam": "Asia/Bangkok",
    "ict": "Asia/Bangkok",
    "saigon": "Asia/Bangkok",
    "hcmc": "Asia/Bangkok",
    "bangkok": "Asia/Bangkok",
    "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "jst": "Asia/Tokyo",
    "hk": "Asia/Hong_Kong",
    "hong kong": "Asia/Hong_Kong",
    "hongkong": "Asia/Hong_Kong",
    "hkt": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    "taiwan": "Asia/Taipei",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "cst-china": "Asia/Shanghai",
    "kl": "Asia/Kuala_Lumpur",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "malaysia": "Asia/Kuala_Lumpur",
    "phnom penh": "Asia/Phnom_Penh",
    "cambodia": "Asia/Phnom_Penh",
    "vientiane": "Asia/Vientiane",
    "laos": "Asia/Vientiane",
    "yangon": "Asia/Yangon",
    "myanmar": "Asia/Yangon",
    "seoul": "Asia/Seoul",
    "korea": "Asia/Seoul",
    "kst": "Asia/Seoul",
    "nyc": "America/New_York",
    "new york": "America/New_York",
    "est": "America/New_York",    # Handles EST/EDT
    "edt": "America/New_York",
    "la": "America/Los_Angeles",
    "pst": "America/Los_Angeles", # Handles PST/PDT
    "pdt": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "cst": "America/Chicago",     # US Central
    "sydney": "Australia/Sydney",
    "aest": "Australia/Sydney",
    "melbourne": "Australia/Sydney",
    "singapore": "Asia/Singapore",
    "sgt": "Asia/Singapore",
    "berlin": "Europe/Berlin",
    "cet": "Europe/Berlin",
    "paris": "Europe/Paris",
    "amsterdam": "Europe/Amsterdam",
    "dublin": "Europe/Dublin",
    "utc": "UTC",
}


def load_aliases():
    """Load aliases from skills-data/timezone/aliases.json (legacy
    config/aliases.json still honoured), falling back to defaults."""
    config_path = _workspace() / "skills-data" / "timezone" / "aliases.json"
    if not config_path.is_file():
        legacy = _workspace() / "config" / "aliases.json"
        if legacy.is_file():
            config_path = legacy
    aliases = DEFAULT_ALIASES.copy()
    if config_path.exists():
        try:
            with open(config_path) as f:
                custom = json.load(f)
        except (json.JSONDecodeError, OSError):
            return aliases
        # Filter out non-alias keys (like _comment)
        for key, val in custom.items():
            if not key.startswith("_") and isinstance(val, str):
                aliases[key.lower()] = val
    return aliases


def local_tz_name():
    """The user's timezone: config/location.json current.timezone, else the
    TZ env var the runtime sets, else UTC."""
    location_path = _workspace() / "config" / "location.json"
    if location_path.exists():
        try:
            with open(location_path) as f:
                data = json.load(f)
            tz = (data.get("current") or {}).get("timezone", "")
            if tz:
                return tz
        except (json.JSONDecodeError, OSError):
            pass
    return os.environ.get("TZ", "") or "UTC"


def resolve_tz(name, aliases):
    """Resolve a timezone name/alias to a ZoneInfo object."""
    lookup = name.lower().strip()

    # Check aliases first
    if lookup in aliases:
        return ZoneInfo(aliases[lookup])

    # Try as IANA name directly
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        pass

    # Try common patterns
    for prefix in ["Asia/", "Europe/", "America/", "Australia/", "Pacific/", "Africa/"]:
        try:
            return ZoneInfo(f"{prefix}{name.title()}")
        except (ZoneInfoNotFoundError, KeyError):
            pass

    return None


def parse_time(time_str):
    """
    Parse time string. Accepts:
      15:00, 15:30, 3pm, 3:30pm, 3:30PM, 3:30 pm, 15, 3p
    Returns (hour, minute) or None.
    """
    s = time_str.strip().lower()

    # Try HH:MM with optional am/pm
    m = re.match(r'^(\d{1,2}):(\d{2})\s*(am|pm|a|p)?$', s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        ampm = m.group(3)
        if ampm:
            if hour < 1 or hour > 12:
                return None
            if ampm.startswith('p') and hour != 12:
                hour += 12
            elif ampm.startswith('a') and hour == 12:
                hour = 0
        # Only the bare-HH branch checked its range, so "25:00" and "12:99"
        # returned out-of-range values and the caller handed the user a
        # ValueError traceback from datetime.replace().
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    # Try H am/pm or Hpm (no colon)
    m = re.match(r'^(\d{1,2})\s*(am|pm|a|p)$', s)
    if m:
        hour = int(m.group(1))
        ampm = m.group(2)
        if hour < 1 or hour > 12:
            return None
        if ampm.startswith('p') and hour != 12:
            hour += 12
        elif ampm.startswith('a') and hour == 12:
            hour = 0
        return hour, 0

    # Try bare HH (24h)
    m = re.match(r'^(\d{1,2})$', s)
    if m:
        hour = int(m.group(1))
        if 0 <= hour <= 23:
            return hour, 0

    return None


def format_time(dt, reference_date=None):
    """Format datetime with timezone abbreviation, UTC offset, and date-change indicator."""
    offset = dt.strftime("%z")
    offset_formatted = f"UTC{offset[:3]}:{offset[3:]}"
    abbr = dt.strftime("%Z")

    base = f"{dt.strftime('%H:%M %A %d %B')} ({abbr}, {offset_formatted})"

    # Show date-change indicator if different from reference
    if reference_date and dt.date() != reference_date:
        diff_days = (dt.date() - reference_date).days
        if diff_days == 1:
            base += " [+1 day]"
        elif diff_days == -1:
            base += " [-1 day]"
        elif diff_days != 0:
            base += f" [{diff_days:+d} days]"

    return base


def convert_time(time_str, from_tz_name, to_tz_names, aliases):
    """Convert a specific time from one timezone to others."""
    from_tz = resolve_tz(from_tz_name, aliases)
    if not from_tz:
        return f"Unknown timezone: {from_tz_name}"

    parsed = parse_time(time_str)
    if parsed is None:
        return f"Can't parse time: {time_str}. Use HH:MM, 3pm, or 15:30 format."
    hour, minute = parsed

    now = datetime.now(from_tz)
    source_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    reference_date = source_dt.date()

    results = [f"**{from_tz_name}:** {format_time(source_dt)}"]
    for tz_name in to_tz_names:
        tz = resolve_tz(tz_name, aliases)
        if tz:
            converted = source_dt.astimezone(tz)
            results.append(f"**{tz_name}:** {format_time(converted, reference_date)}")
        else:
            results.append(f"**{tz_name}:** unknown timezone")

    return "\n".join(results)


def now_in(tz_names, aliases):
    """Show current time in multiple timezones."""
    results = []
    for tz_name in tz_names:
        tz = resolve_tz(tz_name, aliases)
        if tz:
            dt = datetime.now(tz)
            results.append(f"**{tz_name}:** {format_time(dt)}")
        else:
            results.append(f"**{tz_name}:** unknown timezone")
    return "\n".join(results)


def time_diff(tz1_name, tz2_name, aliases):
    """Show the current time difference between two timezones."""
    tz1 = resolve_tz(tz1_name, aliases)
    tz2 = resolve_tz(tz2_name, aliases)
    if not tz1:
        return f"Unknown timezone: {tz1_name}"
    if not tz2:
        return f"Unknown timezone: {tz2_name}"

    now1 = datetime.now(tz1)
    now2 = datetime.now(tz2)

    # Offset difference in hours
    off1 = now1.utcoffset().total_seconds() / 3600
    off2 = now2.utcoffset().total_seconds() / 3600
    diff = off2 - off1

    sign = "+" if diff >= 0 else ""
    # Format as integer if whole hours, else show .5
    if diff == int(diff):
        diff_str = f"{sign}{int(diff)}h"
    else:
        diff_str = f"{sign}{diff:.1f}h"

    abbr1 = now1.strftime("%Z")
    abbr2 = now2.strftime("%Z")

    lines = [
        f"**{tz1_name}:** {format_time(now1)} ",
        f"**{tz2_name}:** {format_time(now2)}",
        f"",
        f"Difference: {tz2_name} is {diff_str} from {tz1_name}",
    ]

    # Practical note about overlap
    if abs(diff) <= 3:
        lines.append(f"Good overlap for calls/meetings.")
    elif abs(diff) <= 8:
        lines.append(f"Some overlap — schedule carefully.")
    else:
        lines.append(f"Minimal overlap — async communication preferred.")

    return "\n".join(lines)


if __name__ == "__main__":
    aliases = load_aliases()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  tz.py now <tz1> <tz2> ...          — current time in zones")
        print("  tz.py 15:00 <from_tz> <to_tz> ..   — convert specific time")
        print("  tz.py 3pm <from_tz> <to_tz> ..     — 12h format works too")
        print("  tz.py diff <tz1> <tz2>              — show time difference")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "now":
        zones = sys.argv[2:] if len(sys.argv) > 2 else [local_tz_name()]
        print(now_in(zones, aliases))

    elif cmd == "diff":
        if len(sys.argv) < 4:
            print("Usage: tz.py diff <tz1> <tz2>")
            sys.exit(1)
        print(time_diff(sys.argv[2], sys.argv[3], aliases))

    else:
        time_str = sys.argv[1]
        from_tz = sys.argv[2] if len(sys.argv) > 2 else local_tz_name()
        to_tzs = sys.argv[3:] if len(sys.argv) > 3 else [local_tz_name()]
        print(convert_time(time_str, from_tz, to_tzs, aliases))
