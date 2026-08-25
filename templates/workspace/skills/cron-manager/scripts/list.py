import json
import os
import sys
from pathlib import Path


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    jobs_path = workspace / "config" / "jobs.json"

    if not jobs_path.exists():
        print("No jobs.json found.")
        return

    try:
        jobs = json.loads(jobs_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"error: cannot read jobs.json: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(jobs, list) or not all(isinstance(j, dict) for j in jobs):
        print("error: jobs.json must be a list of job objects", file=sys.stderr)
        sys.exit(1)

    if not jobs:
        print("No jobs configured.")
        return

    for job in jobs:
        job_id = job.get("id", "(no id)")
        schedule = job.get("schedule", job.get("at", "(none)"))
        session = job.get("session", "isolated")
        enabled = job.get("enabled", True)
        status = "enabled" if enabled else "disabled"
        prompt = job.get("prompt", job.get("skill", ""))
        deliver = job.get("deliver") or {}
        # Without this the agent could see a job had run and not where its
        # output went, and had to ask the user which channel they were on.
        if deliver.get("mode", "announce") == "none":
            where = "deliver: none"
        elif deliver.get("channel"):
            where = f"deliver: {deliver['channel']}"
        else:
            where = "deliver: NO CHANNEL (output goes nowhere)"
        print(f"  {job_id}: {schedule} [{session}] ({status}, {where}) - {prompt}")


if __name__ == "__main__":
    main()
