"""Shared buffer read/write for preconscious scripts."""

from __future__ import annotations

import json
from pathlib import Path


def load_buffer(buffer_file: Path) -> dict:
    """Always a dict with a list of well-formed items.

    The agent has file_write over skills-data/, and the runtime's own
    reader of this file already validates it. Trusting the shape on this
    side turned a hand-written {} into a KeyError traceback delivered to
    the user as the tool result.
    """
    if not buffer_file.exists():
        return {"items": []}
    try:
        data = json.loads(buffer_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"items": []}
    if not isinstance(data, dict):
        return {"items": []}
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
        return data
    data["items"] = [
        item for item in items
        if isinstance(item, dict)
        and isinstance(item.get("description"), str)
        and isinstance(item.get("c"), int)
        and isinstance(item.get("i"), int)
    ]
    return data


def save_buffer(buffer_file: Path, data: dict) -> None:
    buffer_file.parent.mkdir(parents=True, exist_ok=True)
    buffer_file.write_text(json.dumps(data, indent=2) + "\n")
