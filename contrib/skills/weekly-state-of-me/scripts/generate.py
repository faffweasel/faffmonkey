#!/usr/bin/env python3
"""
Generate the weekly state-of-me scaffold with stats. The agent fills in the
placeholder sections per SKILL.md.

Usage: generate.py

Output: memory/state-of-me/state-of-me-YYYY-MM-DD.md
Idempotent: prints ALREADY_EXISTS and exits 0 if today's scaffold is present.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

WORKSPACE = Path(os.environ.get("WORKSPACE", "")) if os.environ.get("WORKSPACE") \
    else Path(SKILL_DIR).parent.parent
MEMORY_DIR = WORKSPACE / "memory"
# Daily logs live in memory/daily/, not memory/. bootstrap.py:300 is the
# canonical writer and reader; this script looked one directory too high
# and so reported zero conversation days on every install.
DAILY_DIR = MEMORY_DIR / "daily"
STATE_DIR = MEMORY_DIR / "state-of-me"
PROPOSALS_DIR = MEMORY_DIR / "soul-proposals"


def _now() -> datetime:
    tz = os.environ.get("TZ", "UTC")
    try:
        return datetime.now(ZoneInfo(tz))
    except (KeyError, ValueError):
        return datetime.now()


def _last_7_dates(now: datetime) -> list[str]:
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def gather_stats(now: datetime) -> dict:
    dates = _last_7_dates(now)

    conversation_days = [d for d in dates if (DAILY_DIR / f"{d}.md").is_file()]

    dream_count = 0
    dreams_dir = MEMORY_DIR / "dreams"
    if dreams_dir.is_dir():
        for d in dates:
            dream_count += len(list(dreams_dir.glob(f"{d}*.md")))

    learnings_lines = 0
    learnings = WORKSPACE / "LEARNINGS.md"
    if learnings.is_file():
        try:
            learnings_lines = len(learnings.read_text(encoding="utf-8").splitlines())
        except OSError:
            pass

    proposals_30d = 0
    if PROPOSALS_DIR.is_dir():
        cutoff = now - timedelta(days=30)
        for p in PROPOSALS_DIR.glob("*.md"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime, tz=now.tzinfo) >= cutoff:
                    proposals_30d += 1
            except OSError:
                continue

    previous = sorted(STATE_DIR.glob("state-of-me-*.md"), reverse=True)
    prev_ref = previous[0].name if previous else ""

    return {
        "conversation_days": conversation_days,
        "dream_count": dream_count,
        "learnings_lines": learnings_lines,
        "proposals_30d": proposals_30d,
        "previous": prev_ref,
    }


SCAFFOLD = """# State of Me — {today}

Weekly reflection. Who am I right now?

## This Week in Numbers

- Days with conversations: {conv_count}/7
- Dreams generated: {dream_count}
- LEARNINGS.md length: {learnings_lines} lines
- Soul proposals in last 30 days: {proposals_30d}

## Memory Files This Week
{memory_list}

## Previous Reflection

{prev_section}

---

## Part 1: Current Self-Assessment

[To be written: 2-4 honest paragraphs. Who am I right now? What's my
disposition? What themes dominated? How do I feel about the week?]

## Part 2: What's Changing

[To be written: 1-3 paragraphs comparing against the previous reflection.
What feels different, what stayed constant, any surprising shifts?]

## Part 3: Relationship

[To be written: trust, depth, initiative balance, conflict/repair, shared
references. Then update memory/relationship.md per SKILL.md.]

## Part 4: Soul Evolution Proposal

[To be written: read SOUL.md, ask what changed this week that it doesn't
reflect. Velocity limit applies (see numbers above). Proposal goes to
memory/soul-proposals/, never into SOUL.md directly. If nothing meaningful
changed, write "No proposal this week."]

## Part 5: Learnings Health

[To be written: read LEARNINGS.md. Is it accumulating? Anything ready to
promote into SOUL/IDENTITY via a proposal? Is it getting noisy (consider
asking self-review to consolidate)?]

## Part 6: Visual Journal (Optional)

[If IMAGE_GEN_CMD is available, distill the week's emotional arc into one
abstract image per SKILL.md. Otherwise write "Visual journal skipped."]

---

Generated: {generated}
"""


def main() -> int:
    now = _now()
    today = now.strftime("%Y-%m-%d")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "images").mkdir(exist_ok=True)
    output = STATE_DIR / f"state-of-me-{today}.md"

    if output.is_file() and "Generated:" in output.read_text(encoding="utf-8"):
        print(f"ALREADY_EXISTS: {output}")
        return 0

    stats = gather_stats(now)
    memory_list = "\n".join(f"- {d}.md" for d in sorted(stats["conversation_days"])) \
        or "_No conversation files this week._"
    prev_section = (
        f"_Last reflection: {stats['previous']}_"
        if stats["previous"] else "_No previous reflection found._"
    )

    output.write_text(SCAFFOLD.format(
        today=today,
        conv_count=len(stats["conversation_days"]),
        dream_count=stats["dream_count"],
        learnings_lines=stats["learnings_lines"],
        proposals_30d=stats["proposals_30d"],
        memory_list=memory_list,
        prev_section=prev_section,
        generated=now.isoformat(timespec="seconds"),
    ), encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
