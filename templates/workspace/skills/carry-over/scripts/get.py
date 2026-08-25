import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from _queue import locked_queue

PRIORITY_ORDER = {"urgent": 0, "normal": 1, "curious": 2, "simmering": 3}
SIMMER_DAYS = 3


def _promote_simmering(queue: list[dict]) -> bool:
    now = datetime.now(timezone.utc)
    changed = False
    for item in queue:
        if item.get("status") != "pending" or item.get("priority") != "simmering":
            continue
        try:
            item_dt = datetime.fromisoformat(item["timestamp"])
            if (now - item_dt).days >= SIMMER_DAYS:
                item["priority"] = "normal"
                changed = True
        except (ValueError, TypeError, KeyError):
            pass
    return changed


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)
    queue_path = skill_data / "queue.json"

    if not queue_path.exists():
        return

    with locked_queue(queue_path) as (queue, write):
        if _promote_simmering(queue):
            write(queue)

    pending = [item for item in queue if item.get("status") == "pending"]
    if not pending:
        return

    pending.sort(
        key=lambda x: (PRIORITY_ORDER.get(x.get("priority", "normal"), 1), x.get("timestamp", ""))
    )

    print("Carry-over from previous sessions:")
    for item in pending:
        ts = item.get("timestamp", "")
        msg = item.get("message", "")
        pri = item.get("priority", "normal")
        label = f"[{pri}] " if pri != "normal" else ""
        if ts:
            print(f"- {label}[{ts}] {msg}")
        else:
            print(f"- {label}{msg}")


if __name__ == "__main__":
    main()
