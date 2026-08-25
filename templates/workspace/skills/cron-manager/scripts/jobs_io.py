"""Locked read-modify-write for config/jobs.json.

The scheduler thread locks this file when it deletes a fired one-shot
(`scheduler._delete_job`), but the cron-manager scripts did not, so the
lock only ever had one participant. A skill turn that read jobs.json,
edited it and wrote the whole list back would discard whatever the
scheduler wrote in between, resurrecting a one-shot that had already
fired and announcing edits the operator never made.

Both sides now take the same lock, on the same `<path>.lock` file, with
the same fcntl.flock call.
"""

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked_jobs(jobs_path: Path):
    """Yield the current job list; write back whatever the caller returns.

    Usage:

        with locked_jobs(path) as jobs:
            jobs.append(new_job)

    The list is re-read inside the lock, so a caller never edits a copy
    that went stale between validation and write.
    """
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = jobs_path.with_suffix(jobs_path.suffix + ".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        jobs: list[dict] = []
        if jobs_path.exists():
            try:
                jobs = json.loads(jobs_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                print(f"error: cannot read jobs.json: {e}", file=sys.stderr)
                sys.exit(1)
            if not isinstance(jobs, list) or not all(isinstance(j, dict) for j in jobs):
                print("error: jobs.json must be a list of job objects", file=sys.stderr)
                sys.exit(1)
        yield jobs
        tmp = jobs_path.with_suffix(jobs_path.suffix + ".tmp")
        tmp.write_text(json.dumps(jobs, indent=2) + "\n")
        os.replace(tmp, jobs_path)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
