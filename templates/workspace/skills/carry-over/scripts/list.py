import json
import os
import sys
from pathlib import Path

PRIORITY_ORDER = {"urgent": 0, "normal": 1, "curious": 2, "simmering": 3}


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)
    queue_path = skill_data / "queue.json"

    if not queue_path.exists():
        print("No carry-over items.")
        return

    try:
        queue = json.loads(queue_path.read_text())
    except (json.JSONDecodeError, OSError):
        print("No carry-over items.")
        return

    pending = [item for item in queue if item.get("status") == "pending"]
    if not pending:
        print("No pending carry-over items.")
        return

    pending.sort(
        key=lambda x: (PRIORITY_ORDER.get(x.get("priority", "normal"), 1), x.get("timestamp", ""))
    )

    # Numbered, because `done` takes these numbers and there is nothing
    # else to address an item by.
    print(f"{len(pending)} pending item(s):")
    for number, item in enumerate(pending, 1):
        ts = item.get("timestamp", "unknown")
        msg = item.get("message", "")
        pri = item.get("priority", "normal")
        print(f"  {number}. [{pri}] [{ts}] {msg}")


if __name__ == "__main__":
    main()
