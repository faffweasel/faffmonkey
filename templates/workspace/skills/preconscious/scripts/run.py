"""Decay currency scores and drop expired items.
Runs standalone (no agent, no LLM) via session: "none" cron jobs.
Usage: run.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from buffer import load_buffer, save_buffer


def decay_buffer(buffer_file: Path) -> str:
    data = load_buffer(buffer_file)

    if not data.get("items"):
        return "Buffer empty, nothing to decay."

    lines: list[str] = ["Decaying:"]
    for item in data["items"]:
        lines.append(f"  {item['description']} [C:{item['c']} -> C:{item['c'] - 1}, I:{item['i']}]")

    for item in data["items"]:
        item["c"] -= 1

    dropped = [item for item in data["items"] if item["c"] <= 0 and item["i"] <= 2]
    if dropped:
        lines.append("Dropping (expired):")
        for item in dropped:
            lines.append(f"  {item['description']}")
        data["items"] = [item for item in data["items"] if not (item["c"] <= 0 and item["i"] <= 2)]

    save_buffer(buffer_file, data)
    lines.append(f"Buffer: {len(data['items'])} items remaining.")
    return "\n".join(lines)


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    buffer_file = skill_data / "buffer.json"
    print(decay_buffer(buffer_file))


if __name__ == "__main__":
    main()
