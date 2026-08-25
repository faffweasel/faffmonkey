"""Drop the item with the lowest effective score (C + I).
Usage: drop_lowest.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from buffer import load_buffer, save_buffer


def drop_lowest(buffer_file: Path) -> str:
    data = load_buffer(buffer_file)

    if not data.get("items"):
        return "Buffer empty, nothing to drop."

    data["items"].sort(key=lambda x: x["c"] + x["i"])
    dropped = data["items"].pop(0)

    save_buffer(buffer_file, data)
    return f"Dropped: {dropped['description']} [C:{dropped['c']}, I:{dropped['i']}]"


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    buffer_file = skill_data / "buffer.json"
    print(drop_lowest(buffer_file))


if __name__ == "__main__":
    main()
