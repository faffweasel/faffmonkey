import os
import sys
from pathlib import Path

from _queue import locked_queue

PRIORITY_ORDER = {"urgent": 0, "normal": 1, "curious": 2, "simmering": 3}


def _sorted_pending(queue: list[dict]) -> list[dict]:
    """The same order list.py prints, so its numbers mean something here."""
    pending = [item for item in queue if item.get("status") == "pending"]
    pending.sort(
        key=lambda x: (PRIORITY_ORDER.get(x.get("priority", "normal"), 1), x.get("timestamp", ""))
    )
    return pending


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    queue_path = Path(skill_data_env) / "queue.json"

    args = sys.argv[1:]
    if not args:
        print("error: at least one item number required", file=sys.stderr)
        print("usage: done.py N [N ...]   (numbers come from list)", file=sys.stderr)
        sys.exit(1)

    numbers: list[int] = []
    for arg in args:
        try:
            number = int(arg)
        except ValueError:
            print(f"error: not a number: {arg!r}", file=sys.stderr)
            sys.exit(1)
        if number < 1:
            print(f"error: item numbers start at 1, got {number}", file=sys.stderr)
            sys.exit(1)
        numbers.append(number)

    if not queue_path.exists():
        print("No carry-over items.")
        return

    with locked_queue(queue_path) as (queue, write):
        pending = _sorted_pending(queue)
        if not pending:
            print("No pending carry-over items.")
            return

        # Read the whole selection before changing anything, so a bad number
        # does not leave half the request applied.
        out_of_range = [n for n in numbers if n > len(pending)]
        if out_of_range:
            listed = ", ".join(str(n) for n in out_of_range)
            print(
                f"error: no item {listed} ({len(pending)} pending)",
                file=sys.stderr,
            )
            sys.exit(1)

        marked = []
        for number in sorted(set(numbers)):
            item = pending[number - 1]
            item["status"] = "done"
            marked.append(item.get("message", ""))
        write(queue)

    remaining = sum(1 for item in queue if item.get("status") == "pending")
    for message in marked:
        print(f"Done: {message}")
    print(f"{remaining} pending item(s) left.")


if __name__ == "__main__":
    main()
