"""The heartbeat's watchdog. Zero LLM cost.

Runs at the start of every heartbeat tick: its own health checks, then
every trigger sensors have dropped in triggers.d/ and the latest line of
every reading in workspace/readings/. Writes triggers.json; the scheduler
wakes the agent only when status is "attention".
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
}

TRIGGER_KINDS = frozenset({"alert", "occasion", "new"})


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


def check_yesterday_memory(workspace: Path, tz: ZoneInfo) -> list[str]:
    """Create yesterday's daily file if missing. Returns what was fixed."""
    yesterday = (datetime.now(tz) - timedelta(days=1)).date()
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    yesterday_file = daily_dir / f"{yesterday.isoformat()}.md"
    if yesterday_file.exists():
        return []
    yesterday_file.write_text(f"# {yesterday.isoformat()}\n")
    return [f"created missing memory file: memory/daily/{yesterday.isoformat()}.md"]


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


def collect_triggers(skill_data: Path) -> tuple[list[str], list[str]]:
    """Every trigger a sensor has dropped, as prompt lines plus the file
    names, so the scheduler can remove them once the agent has seen them.

    A trigger is {"at", "source", "kind", "text"}; only "text" is required.
    A file that is not that is reported on stderr and left alone.
    """
    triggers_dir = skill_data / "triggers.d"
    if not triggers_dir.is_dir():
        return [], []
    lines: list[str] = []
    files: list[str] = []
    for path in sorted(triggers_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            print(f"warning: unreadable trigger {path.name}", file=sys.stderr)
            continue
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str) or not text.strip():
            print(f"warning: trigger {path.name} has no text", file=sys.stderr)
            continue
        source = item.get("source") if isinstance(item.get("source"), str) else path.stem
        kind = item.get("kind") if item.get("kind") in TRIGGER_KINDS else "alert"
        lines.append(f"{source} ({kind}): {text.strip()}")
        files.append(path.name)
    return lines, files


def _age(at: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return "unknown age"
    if then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // (24 * 60)}d ago"


def collect_readings(workspace: Path, now: datetime) -> list[str]:
    """The last line of every readings/<source>.jsonl, as one prompt line
    each: source, age, summary."""
    readings_dir = workspace / "readings"
    if not readings_dir.is_dir():
        return []
    lines: list[str] = []
    for path in sorted(readings_dir.glob("*.jsonl")):
        try:
            raw = path.read_text().rstrip("\n").rsplit("\n", 1)[-1]
            item = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            print(f"warning: unreadable reading {path.name}", file=sys.stderr)
            continue
        summary = item.get("summary") if isinstance(item, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            continue
        lines.append(f"{path.stem} ({_age(str(item.get('at', '')), now)}): {summary.strip()}")
    return lines


def run_watchdog(workspace: Path, skill_data: Path, tz: ZoneInfo) -> dict:
    config = load_config(skill_data)
    now = datetime.now(tz)
    all_triggers: list[str] = []
    all_fixed = check_yesterday_memory(workspace, tz)

    today = now.date().isoformat()
    all_triggers.extend(_once_a_day(
        check_morning_stamp(workspace, tz, config["morning_deadline_hour"]),
        skill_data, "morning_missed", today,
    ))
    all_triggers.extend(_once_a_day(
        check_learnings_full(workspace, config["learnings_max_entries"]),
        skill_data, "learnings_full", today,
    ))
    dropped, files = collect_triggers(skill_data)
    all_triggers.extend(dropped)

    status = "attention" if all_triggers else "clean"
    result = {
        "checked_at": now.isoformat(),
        "status": status,
        "triggers": all_triggers,
        "files": files,
        "readings": collect_readings(workspace, now),
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
