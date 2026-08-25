"""Morning routine preparation: idempotency check, memory file, context dump.
Prints ALREADY_RUN if today's greeting was already sent; otherwise prints
pending carry-over items, the preconscious buffer, and READY.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STAMP = "Morning message sent"


def ensure_today_file(workspace: Path, tz: ZoneInfo) -> Path:
    today = datetime.now(tz).date().isoformat()
    daily_dir = workspace / "memory" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    today_file = daily_dir / f"{today}.md"
    if not today_file.exists():
        today_file.write_text(f"# {today}\n")
    return today_file


def pending_carry_over(workspace: Path) -> list[str]:
    queue_path = workspace / "skills-data" / "carry-over" / "queue.json"
    if not queue_path.exists():
        return []
    try:
        queue = json.loads(queue_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(queue, list):
        return []
    return [
        f"- [{item.get('priority', 'normal')}] {item.get('message', '')}"
        for item in queue
        if isinstance(item, dict) and item.get("status") == "pending"
    ]


def preconscious_items(workspace: Path) -> list[str]:
    buffer_path = workspace / "skills-data" / "preconscious" / "buffer.json"
    if not buffer_path.exists():
        return []
    try:
        data = json.loads(buffer_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    valid = [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("description"), str)
        and isinstance(item.get("c"), int)
        and isinstance(item.get("i"), int)
    ]
    valid.sort(key=lambda x: x["c"] + x["i"], reverse=True)
    return [f"- {item['description']} [C:{item['c']}, I:{item['i']}]" for item in valid]


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE must be set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    tz = ZoneInfo(os.environ.get("TZ", "UTC"))

    today_file = ensure_today_file(workspace, tz)
    if STAMP in today_file.read_text():
        print("ALREADY_RUN")
        return

    carry = pending_carry_over(workspace)
    if carry:
        print("Carry-over items:")
        print("\n".join(carry))
    buffer_lines = preconscious_items(workspace)
    if buffer_lines:
        print("Preconscious buffer:")
        print("\n".join(buffer_lines))
    print("READY")


if __name__ == "__main__":
    main()
