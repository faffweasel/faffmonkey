import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobs_io import locked_jobs


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    jobs_path = Path(workspace_env) / "config" / "jobs.json"

    if len(sys.argv) < 2:
        print("error: job id required", file=sys.stderr)
        print("usage: remove.py <job-id>", file=sys.stderr)
        sys.exit(1)
    job_id = sys.argv[1].strip()

    with locked_jobs(jobs_path) as jobs:
        remaining = [j for j in jobs if j.get("id") != job_id]
        if len(remaining) == len(jobs):
            print(f"error: job {job_id!r} not found", file=sys.stderr)
            sys.exit(1)
        jobs[:] = remaining

    print(f"Removed job {job_id!r}")


if __name__ == "__main__":
    main()
