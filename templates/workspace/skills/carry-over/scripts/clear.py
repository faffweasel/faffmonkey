import os
import sys
from pathlib import Path

from _queue import locked_queue


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)
    queue_path = skill_data / "queue.json"

    clear_all = "--all" in sys.argv[1:]

    if not queue_path.exists():
        print("No carry-over items to clear.")
        return

    with locked_queue(queue_path) as (queue, write):
        if clear_all:
            count = len(queue)
            if count == 0:
                print("No carry-over items to clear.")
                return
            write([])
            print(f"Cleared all {count} item(s).")
            return

        count = 0
        for item in queue:
            if item.get("status") == "pending":
                item["status"] = "done"
                count += 1

        if count == 0:
            print("No pending items to clear.")
            return

        write(queue)
        print(f"Marked all {count} pending item(s) done.")


if __name__ == "__main__":
    main()
