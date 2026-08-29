"""Drop a trigger by hand so the next heartbeat tick wakes the agent.

    poke [text]

Scheduled at fixed times it is the "look around" occasion: the agent wakes
with every current reading and decides whether anything is worth saying.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def main() -> None:
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    text = " ".join(sys.argv[1:]).strip() or "Look around: is anything worth telling the user?"
    now = datetime.now(ZoneInfo(os.environ.get("TZ", "UTC")))
    triggers_dir = Path(skill_data_env) / "triggers.d"
    triggers_dir.mkdir(parents=True, exist_ok=True)
    path = triggers_dir / f"poke-{now.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps({
        "at": now.isoformat(timespec="seconds"),
        "source": "poke",
        "kind": "occasion",
        "text": text,
    }, indent=2) + "\n")
    print(f"trigger written: {text}")


if __name__ == "__main__":
    main()
