import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jobs_io import locked_jobs

VALID_SESSIONS = {"isolated", "main", "none", "agent"}
VALID_DELIVER_MODES = {"announce", "none"}

# The scheduler's own rules. Anything looser here reports success for a
# job load_jobs then drops with a log line nobody reads, and cron-manager
# list keeps showing it as enabled forever.
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _validate_field(expr: str, lo: int, hi: int) -> bool:
    for part in expr.split(","):
        part = part.strip()
        if not part:
            return False
        if "/" in part:
            base, _, step_str = part.partition("/")
            if not step_str.isdigit() or not 1 <= int(step_str) <= hi - lo + 1:
                return False
            part = base
        if part == "*":
            continue
        bounds = part.split("-") if "-" in part else [part]
        if len(bounds) > 2:
            return False
        for value in bounds:
            if not value.isdigit() or not lo <= int(value) <= hi:
                return False
    return True


def _validate_cron_expression(expr: str) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    return all(
        _validate_field(part, lo, hi)
        for part, (lo, hi) in zip(parts, FIELD_BOUNDS)
    )


def _configured_slots() -> set[str]:
    """Model slots this install actually has.

    Slots are user-defined, so validating against a hardcoded list would
    reject a legitimate one. Reading config.json is the only honest check.
    """
    state_dir = Path(os.environ.get("WORKSPACE", ".")).parent / "state"
    try:
        raw = json.loads((state_dir / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    models = raw.get("models")
    return set(models) if isinstance(models, dict) else set()


def _validate_job(job: dict) -> str | None:
    if not isinstance(job, dict):
        return "job must be a JSON object"

    if "id" not in job:
        return "missing required field: id"
    if not isinstance(job["id"], str) or not NAME_RE.match(job["id"]):
        return "id must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,62} (no spaces)"

    has_schedule = "schedule" in job
    has_at = "at" in job
    if not has_schedule and not has_at:
        return "one of 'schedule' or 'at' is required"
    if has_schedule and has_at:
        return "cannot have both 'schedule' and 'at'"

    if has_schedule and not _validate_cron_expression(job["schedule"]):
        return "invalid cron expression (expected 5 fields)"

    if has_at:
        try:
            datetime.strptime(job["at"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return "invalid 'at' (expected a real date as YYYY-MM-DD HH:MM)"

    has_prompt = "prompt" in job
    has_skill = "skill" in job
    if not has_prompt and not has_skill:
        return "one of 'prompt' or 'skill' is required"
    if has_prompt and has_skill:
        return "cannot have both 'prompt' and 'skill'"

    # Must match load_jobs in the scheduler. Validating against a different
    # default meant a job written without a session was checked as one thing
    # and then run as another.
    session = job.get("session", "agent")
    if session not in VALID_SESSIONS:
        return f"invalid session: {session!r} (expected: {', '.join(sorted(VALID_SESSIONS))})"

    if session == "none" and has_prompt:
        return "session 'none' requires 'skill', not 'prompt'"
    if session in ("isolated", "main", "agent") and has_skill and not has_prompt:
        return f"session {session!r} requires 'prompt', not 'skill'"

    deliver = job.get("deliver")
    if deliver is not None:
        if not isinstance(deliver, dict):
            return "deliver must be an object"
        mode = deliver.get("mode", "announce")
        if mode not in VALID_DELIVER_MODES:
            return f"invalid deliver.mode: {mode!r}"
        if mode == "announce" and "channel" not in deliver:
            return "deliver.mode 'announce' requires 'channel'"
        channel = deliver.get("channel")
        if channel is not None and not (
            isinstance(channel, str) and NAME_RE.match(channel)
        ):
            return f"invalid deliver.channel: {channel!r}"

    if has_skill and not (
        isinstance(job["skill"], str) and NAME_RE.match(job["skill"])
    ):
        return f"invalid skill name: {job['skill']!r}"

    model = job.get("model")
    if model is not None:
        if not isinstance(model, str) or model not in _configured_slots():
            slots = ", ".join(sorted(_configured_slots())) or "(none configured)"
            return f"invalid model slot: {model!r} (configured: {slots})"

    if "enabled" in job and not isinstance(job["enabled"], bool):
        return "enabled must be a boolean"
    if "rotate_session" in job:
        if not isinstance(job["rotate_session"], bool):
            return "rotate_session must be a boolean"
        # The scheduler rejects this too. Accepting it here meant an isolated
        # job could flush memory and rotate a main session it never ran in.
        if job["rotate_session"] and session != "main":
            return f"rotate_session is only valid on session 'main', not {session!r}"

    return None


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    jobs_path = workspace / "config" / "jobs.json"

    if len(sys.argv) < 2:
        print("error: job JSON required as argument", file=sys.stderr)
        print("usage: add.py '<json>'", file=sys.stderr)
        sys.exit(1)

    try:
        new_job = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    error = _validate_job(new_job)
    if error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)

    # Read, edit and write inside the scheduler's lock. Without it this
    # rewrote the whole list from a stale read and could resurrect a
    # one-shot the scheduler had just deleted.
    with locked_jobs(jobs_path) as jobs:
        existing_ids = {j.get("id") for j in jobs}
        if new_job["id"] in existing_ids:
            print(f"error: job id {new_job['id']!r} already exists", file=sys.stderr)
            sys.exit(1)

        new_job.setdefault("enabled", True)
        jobs.append(new_job)

    print(f"Added job {new_job['id']!r}")


if __name__ == "__main__":
    main()
