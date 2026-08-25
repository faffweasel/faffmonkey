import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from add import _validate_job
from jobs_io import locked_jobs


def main() -> None:
    """Merge a JSON patch into an existing job, validated as a whole.

    Without this the agent's only way to change a schedule was disable
    plus add, and add refused the existing id, so the job stayed
    disabled.
    """
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    jobs_path = Path(workspace_env) / "config" / "jobs.json"

    if len(sys.argv) < 3:
        print("error: job id and JSON patch required", file=sys.stderr)
        print("usage: update.py <job-id> '<json>'", file=sys.stderr)
        sys.exit(1)
    job_id = sys.argv[1].strip()
    try:
        patch = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(patch, dict):
        print("error: patch must be a JSON object", file=sys.stderr)
        sys.exit(1)
    if "id" in patch and patch["id"] != job_id:
        print("error: a job's id cannot be changed; remove it and add a new one", file=sys.stderr)
        sys.exit(1)

    with locked_jobs(jobs_path) as jobs:
        for index, job in enumerate(jobs):
            if job.get("id") == job_id:
                break
        else:
            print(f"error: job {job_id!r} not found", file=sys.stderr)
            sys.exit(1)
        # A null in the patch removes the field: that is how to switch a
        # job from schedule to at, or drop a model override.
        merged = {**job, **patch}
        merged = {k: v for k, v in merged.items() if v is not None}
        error = _validate_job(merged)
        if error:
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)
        jobs[index] = merged

    changed = ", ".join(sorted(patch))
    print(f"Updated job {job_id!r} ({changed})")


if __name__ == "__main__":
    main()
