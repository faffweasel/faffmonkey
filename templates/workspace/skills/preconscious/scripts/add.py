"""Add item to preconscious buffer.
Usage: add.py "description" [C] [I]
C=currency (1-5, default 5), I=importance (1-5, default 3)
Duplicate descriptions update the existing item.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from buffer import load_buffer, save_buffer

MAX_ITEMS = 5


def add_item(buffer_file: Path, description: str, c: int, i: int) -> str:
    c = max(1, min(5, c))
    i = max(1, min(5, i))
    data = load_buffer(buffer_file)
    now = datetime.now(timezone.utc).isoformat()

    for item in data["items"]:
        if item["description"] == description:
            item["c"] = c
            item["i"] = i
            item["updated"] = now
            save_buffer(buffer_file, data)
            return f"Updated existing item: {description}"

    new_item = {
        "description": description,
        "c": c,
        "i": i,
        "added": now,
    }
    data["items"].append(new_item)

    dropped_msg = ""
    if len(data["items"]) > MAX_ITEMS:
        data["items"].sort(key=lambda x: x["c"] + x["i"], reverse=True)
        dropped = data["items"].pop()
        if dropped is new_item:
            # Reporting "Added: x" then "Dropped: x" told the agent it had
            # recorded something it had not.
            save_buffer(buffer_file, data)
            return (
                f"Not added: buffer is full and every item scores higher "
                f"than [C:{c}, I:{i}]: {description}"
            )
        dropped_msg = f"\nDropped: {dropped['description']}"

    save_buffer(buffer_file, data)
    return f"Added [C:{c}, I:{i}]: {description}{dropped_msg}"


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    if len(sys.argv) < 2:
        print("Usage: add.py \"description\" [C] [I]", file=sys.stderr)
        sys.exit(1)

    description = sys.argv[1]
    try:
        c = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        i = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    except ValueError:
        print(
            'Usage: add.py "description" [C] [I]  (C and I are 1-5)',
            file=sys.stderr,
        )
        sys.exit(1)

    buffer_file = skill_data / "buffer.json"
    print(add_item(buffer_file, description, c, i))


if __name__ == "__main__":
    main()
