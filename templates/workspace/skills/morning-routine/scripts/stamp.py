"""Stamp today's memory file after the morning greeting is composed.
The stamp is what prepare.py and the heartbeat watchdog check for.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STAMP = "Morning message sent"


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE must be set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    now = datetime.now(tz)
    today = now.date().isoformat()
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    today_file = daily_dir / f"{today}.md"
    existing = today_file.read_text() if today_file.exists() else f"# {today}\n"
    if STAMP in existing:
        print("already stamped")
        return
    if not existing.endswith("\n"):
        existing += "\n"
    today_file.write_text(f"{existing}\n{STAMP} {now.strftime('%H:%M')}\n")
    print(f"stamped: {STAMP} {now.strftime('%H:%M')}")


if __name__ == "__main__":
    main()
