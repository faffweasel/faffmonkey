"""faff cron — cron job management CLI."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from faffmonkey.config import load_config
from faffmonkey.runtime.redaction import redact
from faffmonkey.runtime.scheduler import (
    LAST_CHANNEL,
    Scheduler,
    load_jobs,
    next_fire_time,
    next_one_shot_time,
    parse_cron,
    render_timestamp,
)


def _rejected_count(workspace_dir: Path, loaded: int) -> int:
    """How many entries in jobs.json the scheduler refused to load."""
    jobs_path = workspace_dir / "config" / "jobs.json"
    try:
        raw = json.loads(jobs_path.read_text())
    except (OSError, json.JSONDecodeError):
        return -1
    if not isinstance(raw, list):
        return -1
    return len(raw) - loaded


def run_cron_list(state_dir: Path, workspace_dir: Path) -> None:
    config = load_config(state_dir / "config.json")
    jobs = load_jobs(workspace_dir)
    # "No cron jobs configured" was printed for an unparseable file as
    # well as an empty one, so every job stopping looked like a healthy
    # empty install.
    rejected = _rejected_count(workspace_dir, len(jobs))
    if rejected < 0:
        print("workspace/config/jobs.json is unreadable. No jobs will run.")
        return
    if rejected > 0:
        print(f"Warning: {rejected} job(s) in jobs.json were rejected and will not run.")
        print("Run faff doctor for details.\n")
    if not jobs:
        if rejected == 0:
            print("No cron jobs configured.")
        return

    tz = config.timezone
    now = datetime.now(tz)

    for job in jobs:
        status = "enabled" if job.enabled else "disabled"
        sched = job.schedule or f"at {job.at}"

        next_str = ""
        if job.enabled:
            if job.schedule:
                try:
                    fields = parse_cron(job.schedule)
                    nft = next_fire_time(fields, now, tz)
                    next_str = nft.strftime("%Y-%m-%d %H:%M %Z")
                except ValueError:
                    next_str = "(invalid schedule)"
            elif job.at:
                ost = next_one_shot_time(job.at, tz)
                if ost and ost > now:
                    next_str = ost.strftime("%Y-%m-%d %H:%M %Z")
                elif ost:
                    next_str = "(past)"
                else:
                    next_str = "(invalid)"

        print(f"  {job.id:20s}  {sched:20s}  [{status}]  session={job.session}")
        if next_str:
            print(f"  {'':20s}  next: {next_str}")


import re

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_job_id(job_id: str) -> bool:
    if not job_id or job_id in (".", ".."):
        return False
    return _JOB_ID_RE.match(job_id) is not None


def run_cron_history(state_dir: Path, job_id: str, tz: ZoneInfo | None = None) -> None:
    if not _validate_job_id(job_id):
        print(f"Invalid job ID: {job_id}")
        return
    log_path = state_dir / "logs" / "cron" / f"{job_id}.jsonl"
    if not log_path.exists():
        print(f"No history for job: {job_id}")
        return

    lines = log_path.read_text().strip().split("\n")
    lines = [l for l in lines if l.strip()]
    lines = lines[-20:]

    if not lines:
        print(f"No history for job: {job_id}")
        return

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp", "")
        status = entry.get("status", "")
        ms = entry.get("duration_ms", 0)
        err = entry.get("error", "")
        suffix = f"  error={err}" if err else ""
        shown = render_timestamp(ts, tz) if tz is not None else ts
        print(f"  {shown}  {status:8s}  {ms}ms{suffix}")


def run_cron_run(state_dir: Path, workspace_dir: Path, job_id: str) -> int:
    from faffmonkey.wiring import wire

    jobs = load_jobs(workspace_dir)
    job = next((j for j in jobs if j.id == job_id), None)
    if job is None:
        print(f"Job not found: {job_id}")
        return 1

    runtime = wire(state_dir, workspace=state_dir.parent)
    scheduler = Scheduler(
        config=runtime.config,
        workspace=workspace_dir,
        state_dir=state_dir,
        resolve_provider=runtime.resolve_provider,
        channels={},
        search_provider=runtime.search_provider,
        persist_state=False,
        deliver_output=False,
    )

    # A manual run must never destroy a one-shot: testing a reminder is
    # exactly when an operator least expects to lose it.
    result = scheduler.run_job(job, delete_one_shot=False)
    print(f"Job {job_id}: {result.status} ({result.duration_ms}ms)")
    if result.output is not None:
        print("  Output:")
        for line in redact(result.output).splitlines() or [""]:
            print(f"    {line}")
        if job.deliver_mode == "announce" and job.deliver_channel:
            where = (
                "the channel the user last spoke on"
                if job.deliver_channel == LAST_CHANNEL
                else repr(job.deliver_channel)
            )
            print(
                f"  Not delivered: a manual run prints the output here. The"
                f" scheduler inside faff run delivers this job to {where}."
            )
    elif result.status == "success":
        print(
            "  Output: none. The run had nothing to say (NO_REPLY, or an empty"
            " model response); the log lines above say which."
        )
    if job.at is not None:
        print(f"  One-shot job {job_id} kept; it will still fire at {job.at}")
    if result.error:
        print(f"  Error: {result.error}")
    return 0 if result.status == "success" else 1
