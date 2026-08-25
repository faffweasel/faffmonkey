import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Same id rule as add.py; it also keeps the id from naming a path.
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def _display_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("TZ") or "UTC")
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def _render(raw: str, tz: ZoneInfo) -> str:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 2:
        print("error: job id required", file=sys.stderr)
        print("usage: history.py <job-id> [limit]", file=sys.stderr)
        sys.exit(1)
    job_id = sys.argv[1].strip()
    if not NAME_RE.match(job_id):
        print(f"error: invalid job id {job_id!r}", file=sys.stderr)
        sys.exit(1)
    limit = DEFAULT_LIMIT
    if len(sys.argv) > 2:
        if not sys.argv[2].isdigit() or int(sys.argv[2]) < 1:
            print("error: limit must be a positive integer", file=sys.stderr)
            sys.exit(1)
        limit = min(int(sys.argv[2]), MAX_LIMIT)

    # Run logs live beside the workspace, not in it; same layout add.py
    # relies on to read config.json.
    log_path = Path(workspace_env).parent / "state" / "logs" / "cron" / f"{job_id}.jsonl"
    if not log_path.exists():
        print(f"No runs recorded for job {job_id!r}.")
        return
    try:
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
    except OSError as e:
        print(f"error: cannot read run log: {e}", file=sys.stderr)
        sys.exit(1)
    if not lines:
        print(f"No runs recorded for job {job_id!r}.")
        return

    tz = _display_tz()
    shown = 0
    for line in reversed(lines[-limit:]):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        status = entry.get("status", "?")
        suffix = f"  {entry['error']}" if entry.get("error") else ""
        print(f"  {_render(entry.get('timestamp', ''), tz)}  {status:8s}  {entry.get('duration_ms', 0)}ms{suffix}")
        shown += 1
    print(f"{shown} run{'' if shown == 1 else 's'} shown, newest first (skipped runs carry the reason)")


if __name__ == "__main__":
    main()
