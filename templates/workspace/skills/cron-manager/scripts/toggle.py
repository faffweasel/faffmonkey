"""Shared enable/disable logic for cron-manager."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobs_io import locked_jobs


def set_enabled(enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    past = "Enabled" if enabled else "Disabled"

    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    jobs_path = workspace / "config" / "jobs.json"

    if len(sys.argv) < 2:
        print("error: job id required", file=sys.stderr)
        print(f"usage: {action}.py <job-id>", file=sys.stderr)
        sys.exit(1)

    job_id = sys.argv[1].strip()

    if not jobs_path.exists():
        print("error: jobs.json not found", file=sys.stderr)
        sys.exit(1)

    # Same lock the scheduler takes, so a toggle cannot discard a
    # concurrent one-shot deletion by writing back a stale list.
    with locked_jobs(jobs_path) as jobs:
        found = False
        for job in jobs:
            if job.get("id") == job_id:
                job["enabled"] = enabled
                found = True
                break

        if not found:
            print(f"error: job {job_id!r} not found", file=sys.stderr)
            sys.exit(1)

    print(f"{past} job {job_id!r}")
