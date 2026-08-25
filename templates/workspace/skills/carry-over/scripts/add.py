import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from _queue import locked_queue

VALID_PRIORITIES = ("urgent", "normal", "curious", "simmering")


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    priority = "normal"
    message_args: list[str] = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--priority" and i + 1 < len(args):
            priority = args[i + 1]
            i += 2
        else:
            message_args.append(args[i])
            i += 1

    if not message_args:
        print("error: message required as argument", file=sys.stderr)
        print('usage: add.py [--priority LEVEL] "message text"', file=sys.stderr)
        sys.exit(1)

    message = " ".join(message_args)
    if not message.strip():
        print("error: message cannot be empty", file=sys.stderr)
        sys.exit(1)

    if priority not in VALID_PRIORITIES:
        print(
            f"error: invalid priority '{priority}' "
            f"(use {', '.join(VALID_PRIORITIES)})",
            file=sys.stderr,
        )
        sys.exit(1)

    queue_path = skill_data / "queue.json"
    with locked_queue(queue_path) as (queue, write):
        queue.append({
            "message": message.strip(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "priority": priority,
            "status": "pending",
        })
        write(queue)

    pending_count = sum(1 for q in queue if q.get("status") == "pending")
    print(f"Queued carry-over item [{priority}] ({pending_count} pending)")


if __name__ == "__main__":
    main()
