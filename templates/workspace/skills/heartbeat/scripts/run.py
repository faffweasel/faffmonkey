"""Deterministic heartbeat checks. Zero LLM cost.
Runs standalone (session: "none") via cron.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CONFIG = {
    "morning_deadline_hour": 8,
    "learnings_max_entries": 30,
    "carryover_stale_days": 7,
}


def load_config(skill_data: Path) -> dict:
    config_path = skill_data / "config.json"
    if not config_path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(config_path.read_text())
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def check_yesterday_memory(workspace: Path, tz: ZoneInfo) -> tuple[list[str], list[str]]:
    triggers: list[str] = []
    fixed: list[str] = []
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    yesterday_file = daily_dir / f"{yesterday.isoformat()}.md"
    if not yesterday_file.exists():
        yesterday_file.write_text(f"# {yesterday.isoformat()}\n")
        fixed.append(f"created missing memory file: memory/daily/{yesterday.isoformat()}.md")
    return triggers, fixed


def check_morning_stamp(workspace: Path, tz: ZoneInfo, deadline_hour: int) -> list[str]:
    now = datetime.now(tz)
    if now.hour < deadline_hour:
        return []
    today = now.date()
    today_file = workspace / "memory" / "daily" / f"{today.isoformat()}.md"
    if not today_file.exists():
        return [f"morning_missed: no morning routine stamp after {deadline_hour:02d}:00"]
    content = today_file.read_text()
    if "morning" in content.lower():
        return []
    return [f"morning_missed: no morning routine stamp after {deadline_hour:02d}:00"]


def check_carryover_stale(workspace: Path, tz: ZoneInfo, stale_days: int) -> list[str]:
    queue_path = workspace / "skills-data" / "carry-over" / "queue.json"
    if not queue_path.exists():
        return []
    try:
        queue = json.loads(queue_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    now = datetime.now(tz)
    stale_count = 0
    for item in queue:
        if item.get("status") != "pending":
            continue
        try:
            ts = datetime.fromisoformat(item["timestamp"])
            if (now - ts).days >= stale_days:
                stale_count += 1
        except (ValueError, TypeError, KeyError):
            pass
    if stale_count > 0:
        return [f"carryover_stale: {stale_count} item(s) pending > {stale_days} days"]
    return []


def check_learnings_full(workspace: Path, max_entries: int) -> list[str]:
    learnings_path = workspace / "LEARNINGS.md"
    if not learnings_path.exists():
        return []
    try:
        content = learnings_path.read_text()
    except OSError:
        return []
    # self-review writes "## [TAG-YYYYMMDD-NNN] label" headings; hand-written
    # files use bullets. Counting only bullets meant a file full of
    # self-review entries counted as zero and this never fired.
    entries = sum(
        1 for line in content.splitlines()
        if line.startswith("## [") or line.strip().startswith("- ")
    )
    if entries > max_entries:
        return [f"learnings_full: {entries} entries (threshold: {max_entries})"]
    return []


def _once_a_day(triggers: list[str], skill_data: Path, key: str, today: str) -> list[str]:
    """Raise a trigger the first time it is seen on a given day, then not again.

    A missed morning stayed true on every hourly tick until midnight, so the
    heartbeat escalated and messaged the user about it every hour of the
    day. Once is the news; the rest is nagging.
    """
    if not triggers:
        return triggers
    path = skill_data / "reported.json"
    try:
        reported = json.loads(path.read_text())
        if not isinstance(reported, dict):
            reported = {}
    except (OSError, json.JSONDecodeError):
        reported = {}
    if reported.get(key) == today:
        return []
    reported[key] = today
    skill_data.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reported, indent=2) + "\n")
    return triggers


def run_watchdog(workspace: Path, skill_data: Path, tz: ZoneInfo) -> dict:
    config = load_config(skill_data)
    all_triggers: list[str] = []
    all_fixed: list[str] = []

    triggers, fixed = check_yesterday_memory(workspace, tz)
    all_triggers.extend(triggers)
    all_fixed.extend(fixed)

    all_triggers.extend(_once_a_day(
        check_morning_stamp(workspace, tz, config["morning_deadline_hour"]),
        skill_data, "morning_missed", datetime.now(tz).date().isoformat(),
    ))
    all_triggers.extend(
        check_carryover_stale(workspace, tz, config["carryover_stale_days"])
    )
    all_triggers.extend(
        check_learnings_full(workspace, config["learnings_max_entries"])
    )

    status = "attention" if all_triggers else "clean"
    result = {
        "checked_at": datetime.now(tz).isoformat(),
        "status": status,
        "triggers": all_triggers,
        "fixed": all_fixed,
    }

    skill_data.mkdir(parents=True, exist_ok=True)
    (skill_data / "triggers.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    tz_name = os.environ.get("TZ", "UTC")
    tz = ZoneInfo(tz_name)

    result = run_watchdog(workspace, skill_data, tz)
    parts = []
    if result["fixed"]:
        parts.append("fixed: " + ", ".join(result["fixed"]))
    if result["triggers"]:
        parts.append("triggers: " + ", ".join(result["triggers"]))
    summary = "; ".join(parts) if parts else "all clear"
    print(f"watchdog: {result['status']} ({summary})")


if __name__ == "__main__":
    main()
