import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VALID_TAGS = ("LRN", "ERR", "FEAT")
VALID_PRIORITIES = ("low", "medium", "high", "critical")
TAG_LABELS = {"LRN": "learning", "ERR": "error", "FEAT": "feature"}


def _next_id(text: str, tag: str, date_str: str) -> str:
    pattern = re.compile(rf"\[{tag}-{date_str}-(\d{{3}})\]")
    numbers = [int(m.group(1)) for m in pattern.finditer(text)]
    next_num = max(numbers, default=0) + 1
    return f"{tag}-{date_str}-{next_num:03d}"


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)

    tag = None
    summary = None
    details = None
    priority = "medium"
    area = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--tag" and i + 1 < len(args):
            tag = args[i + 1].upper()
            i += 2
        elif args[i] == "--summary" and i + 1 < len(args):
            summary = args[i + 1]
            i += 2
        elif args[i] == "--details" and i + 1 < len(args):
            details = args[i + 1]
            i += 2
        elif args[i] == "--priority" and i + 1 < len(args):
            priority = args[i + 1]
            i += 2
        elif args[i] == "--area" and i + 1 < len(args):
            area = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    if not tag:
        print("error: --tag required (LRN, ERR, FEAT)", file=sys.stderr)
        sys.exit(1)
    if tag not in VALID_TAGS:
        print(
            f"error: invalid tag '{tag}' (use {', '.join(VALID_TAGS)})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not summary:
        print("error: --summary required", file=sys.stderr)
        sys.exit(1)
    if not summary.strip():
        print("error: summary cannot be empty", file=sys.stderr)
        sys.exit(1)
    if priority not in VALID_PRIORITIES:
        print(
            f"error: invalid priority '{priority}' "
            f"(use {', '.join(VALID_PRIORITIES)})",
            file=sys.stderr,
        )
        sys.exit(1)

    learnings_path = workspace / "LEARNINGS.md"

    existing = ""
    if learnings_path.exists():
        existing = learnings_path.read_text()

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    entry_id = _next_id(existing, tag, date_str)
    label = TAG_LABELS[tag]

    lines = [
        f"## [{entry_id}] {label}",
        f"**Status**: pending",
        f"**Priority**: {priority}",
    ]
    if area:
        lines.append(f"**Area**: {area}")
    lines.append(f"**Summary**: {summary.strip()}")
    if details:
        lines.append(f"**Details**: {details.strip()}")

    entry_block = "\n".join(lines) + "\n"

    if not existing:
        learnings_path.write_text(f"# Learnings\n\n{entry_block}")
    else:
        separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        learnings_path.write_text(existing + separator + entry_block)

    print(f"Logged {entry_id}: {summary.strip()}")


if __name__ == "__main__":
    main()
