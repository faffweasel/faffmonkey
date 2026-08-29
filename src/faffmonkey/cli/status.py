"""faff status — runtime state at a glance."""

import json
import os
from pathlib import Path

from faffmonkey.config import load_config
from faffmonkey.runtime.redaction import redact
from faffmonkey.runtime.scheduler import recent_cron_runs, render_timestamp


def _process_alive(pid: int) -> bool:
    """Whether pid is still running. Signal 0 checks without delivering."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Still alive.
        return True
    except OSError:
        return False
    return True


def run_status(state_dir: Path, workspace_dir: Path) -> None:
    config_path = state_dir / "config.json"
    if not config_path.exists():
        print('No config found. Run "faff init" to get started.')
        return

    config = load_config(config_path)

    print("faffmonkey status\n")

    # model config
    print("Model config:")
    for slot, mc in config.models.items():
        print(f"  {slot}: {mc.provider} ({mc.model})")
    print()

    # active goal
    goal_path = workspace_dir / "skills-data" / "goal" / "current.json"
    if goal_path.exists():
        try:
            goal = json.loads(goal_path.read_text())
            goal_text = goal.get("goal", "(no text)")
            # A goal file outlives the process that wrote it. Reporting it
            # as active regardless meant a goal interrupted by a crash or
            # a restart looked like it was still being worked on, and the
            # operator was told nothing was wrong.
            pid = goal.get("pid")
            if isinstance(pid, int) and not _process_alive(pid):
                print(f"Interrupted goal: {redact(goal_text)}")
                print(
                    f"  The process running it (pid {pid}) is gone. It was "
                    f"not resumed. Restart it with /goal, or clear it by "
                    f"deleting {goal_path}."
                )
            else:
                print(f"Active goal: {redact(goal_text)}")
        except (json.JSONDecodeError, OSError):
            print("Active goal: (error reading goal file)")
    else:
        print("Active goal: none")
    print()

    # Clean heartbeat ticks are not logged, so the log's last row is the
    # last wake; the tick itself is the scheduler's last-fire time.
    try:
        cron_state = json.loads((state_dir / "cron-state.json").read_text())
        last_tick = cron_state.get("jobs", {}).get("heartbeat", {}).get("last_fire")
    except (json.JSONDecodeError, OSError, AttributeError):
        last_tick = None
    if isinstance(last_tick, str):
        print(f"Last heartbeat tick: {render_timestamp(last_tick, config.timezone)}")
    else:
        print("Last heartbeat tick: none")
    heartbeat_log = state_dir / "logs" / "cron" / "heartbeat.jsonl"
    if heartbeat_log.exists():
        try:
            lines = heartbeat_log.read_text().strip().split("\n")
            if lines and lines[-1]:
                last = json.loads(lines[-1])
                ts = last.get("timestamp", "unknown")
                status = last.get("status", "unknown")
                print(f"Last heartbeat wake: {render_timestamp(ts, config.timezone)} [{redact(status)}]")
            else:
                print("Last heartbeat wake: none")
        except (json.JSONDecodeError, OSError):
            print("Last heartbeat wake: (error reading log)")
    else:
        print("Last heartbeat wake: none")
    print()

    # last 10 cron runs (across all job logs)
    runs = recent_cron_runs(state_dir, limit=10)
    if runs:
        print("Last 10 cron runs:")
        for run in runs:
            ts = render_timestamp(run.timestamp, config.timezone)
            print(f"  {ts}  {run.job_id:20s}  {redact(run.status)}  {run.duration_ms}ms")
    else:
        print("No cron runs recorded.")
