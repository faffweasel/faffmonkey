"""Output current preconscious buffer for session loading.
Sorted by effective score (C + I) descending.
Usage: read.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from buffer import load_buffer


def read_buffer(buffer_file: Path) -> str:
    data = load_buffer(buffer_file)

    if not data.get("items"):
        return "Preconscious buffer is empty."

    lines = ["## Preconscious Buffer", ""]
    for item in sorted(data["items"], key=lambda x: x["c"] + x["i"], reverse=True):
        lines.append(f"- {item['description']} [C:{item['c']}, I:{item['i']}]")
    return "\n".join(lines)


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    buffer_file = skill_data / "buffer.json"
    print(read_buffer(buffer_file))


if __name__ == "__main__":
    main()
