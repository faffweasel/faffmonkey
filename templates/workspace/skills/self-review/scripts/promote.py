import os
import re
import sys
from pathlib import Path

VALID_TARGETS = {"agents": "AGENTS.md", "memory": "MEMORY.md"}


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)

    entry_id = None
    target = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--id" and i + 1 < len(args):
            entry_id = args[i + 1]
            i += 2
        elif args[i] == "--target" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    if not entry_id:
        print("error: --id required", file=sys.stderr)
        sys.exit(1)
    if not target:
        print("error: --target required (agents, memory)", file=sys.stderr)
        sys.exit(1)
    if target not in VALID_TARGETS:
        print(
            f"error: invalid target '{target}' (use agents, memory)",
            file=sys.stderr,
        )
        sys.exit(1)

    target_file = VALID_TARGETS[target]
    learnings_path = workspace / "LEARNINGS.md"

    if not learnings_path.exists():
        print("error: LEARNINGS.md not found", file=sys.stderr)
        sys.exit(1)

    text = learnings_path.read_text()
    lines = text.splitlines()

    header_idx = None
    for idx, line in enumerate(lines):
        if line.startswith(f"## [{entry_id}]"):
            header_idx = idx
            break

    if header_idx is None:
        print(f"error: entry {entry_id} not found", file=sys.stderr)
        sys.exit(1)

    end_idx = len(lines)
    for idx in range(header_idx + 1, len(lines)):
        if re.match(r"^## \[", lines[idx]):
            end_idx = idx
            break

    summary = ""
    status_idx = None
    for idx in range(header_idx + 1, end_idx):
        stripped = lines[idx].strip()
        if stripped.startswith("**Status**:"):
            status_value = stripped.split(":", 1)[1].strip()
            if status_value == "promoted":
                print(f"error: entry {entry_id} is already promoted", file=sys.stderr)
                sys.exit(1)
            status_idx = idx
        elif stripped.startswith("**Summary**:"):
            summary = stripped.split(":", 1)[1].strip()

    if not summary:
        print(f"error: entry {entry_id} has no summary", file=sys.stderr)
        sys.exit(1)

    target_path = workspace / target_file
    if target_path.exists():
        target_text = target_path.read_text()
        separator = "" if target_text.endswith("\n") else "\n"
        target_path.write_text(target_text + separator + f"- {summary}\n")
    else:
        target_path.write_text(f"- {summary}\n")

    new_lines = []
    for idx, line in enumerate(lines):
        if idx == status_idx:
            new_lines.append("**Status**: promoted")
            new_lines.append(f"**Promoted**: {target_file}")
        else:
            new_lines.append(line)

    trailing = "\n" if text.endswith("\n") else ""
    learnings_path.write_text("\n".join(new_lines) + trailing)

    print(f"Promoted {entry_id} to {target_file}: {summary}")


if __name__ == "__main__":
    main()
