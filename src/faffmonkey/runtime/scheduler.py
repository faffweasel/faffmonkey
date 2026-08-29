from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import dataclasses
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from faffmonkey.config import Config, ConfigError, ModelConfig
from faffmonkey.runtime.ingest import scan_patterns, strip_invisible
from faffmonkey.runtime.retry import run_with_timeout
from faffmonkey.runtime.session import MAIN_SESSION_KEY
from faffmonkey.runtime.trust import load_trust_store, read_and_check_trust
from faffmonkey.seams.channel import Channel
from faffmonkey.seams.search_provider import SearchProvider
from faffmonkey.types import CompletionRequest, CompletionResponse, Message, OutboundMessage, TokenUsage

logger = logging.getLogger(__name__)

BACKOFF_STEPS = [30, 60, 300, 900, 3600]
EMPTY_RESPONSE_RETRIES = 3
PREFLIGHT_CACHE_SECONDS = 300
PREFLIGHT_NEGATIVE_CACHE_SECONDS = 60
STALE_ACK_PATTERNS = ["on it", "checking", "let me", "pulling"]

# A no-tools completion (isolated, main, heartbeat escalation) that tries to
# call a tool writes the call as text, and delivering that verbatim put raw
# "<function_calls>" XML in the operator's Telegram. Conservative markers
# only: prose does not contain these.
_TOOL_SYNTAX_MARKERS = ("<function_calls>", "<invoke name=", "<tool_call>", "<|tool_call")

_VALID_SESSIONS = frozenset({"isolated", "main", "none", "agent"})
_VALID_DELIVER_MODES = frozenset({"announce", "none"})
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")

# deliver.channel value meaning "wherever the user last spoke".
LAST_CHANNEL = "last"

# What the heartbeat's agent turn is told when the job has no prompt of its
# own. The setup wizard writes the same text into the job it creates.
HEARTBEAT_PROMPT = (
    "The heartbeat woke you because something needs a decision. Below are "
    "the triggers that woke you, the latest sensor readings, your standing "
    "instructions from HEARTBEAT.md, and what you have already sent "
    "recently. Decide whether the user should hear anything now. If so, "
    "write it plainly as one message. If not, respond with exactly NO_REPLY."
)

# How much of what a job delivered is remembered for its next wake: enough
# to know what was already said, not a transcript.
RECENT_DELIVERIES_KEEP = 10
RECENT_DELIVERIES_HOURS = 48
RECENT_DELIVERY_CHARS = 500

# How much of a job's prompt is recorded beside its delivered message. The
# line is replayed on every later turn, so the whole prompt would be paid
# for indefinitely.
MAX_RECORDED_PROMPT_CHARS = 200

_MAX_LOG_BYTES = 1 * 1024 * 1024
_MAX_LOG_LINES_KEEP = 500
_MAX_LOG_LINES_READ = 200

# How far back a tick will look for a fire it owes. Wide enough to cover
# the maximum stagger plus a tick interval, and a DST transition minute,
# and narrow enough that a restart after a long outage does not replay a
# morning's worth of jobs at once.
CATCHUP_MINUTES = 6
_MAX_STAGGER_SECONDS = 300


class SkillFailed(RuntimeError):
    pass


@dataclass
class CronJob:
    id: str
    schedule: str | None = None
    at: str | None = None
    prompt: str | None = None
    skill: str | None = None
    session: str = "agent"
    context: str | None = None
    model: str | None = None
    deliver_mode: str = "announce"
    deliver_channel: str | None = None
    enabled: bool = True
    rotate_session: bool = False


@dataclass
class BackoffState:
    failure_count: int = 0
    next_retry_after: float = 0.0
    # The same deadline as an absolute UTC instant. monotonic cannot
    # survive a restart and wall clock cannot survive an NTP step, so the
    # in-process decision uses monotonic and only persistence uses this.
    next_retry_at: datetime | None = None

    def record_failure(self) -> None:
        idx = min(self.failure_count, len(BACKOFF_STEPS) - 1)
        step = BACKOFF_STEPS[idx]
        self.next_retry_after = time.monotonic() + step
        self.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=step)
        self.failure_count += 1

    def record_success(self) -> None:
        self.failure_count = 0
        self.next_retry_after = 0.0
        self.next_retry_at = None

    def is_backed_off(self) -> bool:
        if self.failure_count == 0:
            return False
        return time.monotonic() < self.next_retry_after


@dataclass
class RunLog:
    timestamp: str
    job_id: str
    status: str
    duration_ms: int = 0
    tokens: dict = field(default_factory=dict)
    error: str | None = None
    # The job's text, for a manual run to print. Not written to the log.
    output: str | None = None


def _stagger_offset(job_id: str, max_seconds: int = 300) -> int:
    h = hashlib.md5(job_id.encode()).hexdigest()
    return int(h, 16) % max_seconds


def _cross_field_error(
    session: str, context: str | None, prompt: str | None, skill: str | None,
    rotate_session: bool = False,
) -> str | None:
    """Reject job shapes that run but cannot do what the author meant.

    Only `session: "none"` reads `skill`; every other mode sends
    `prompt or ""`, so `{"skill": "watchdog"}` on the default session sent
    an empty user turn to the model every five minutes, delivered whatever
    came back, and logged success.
    """
    # Documented as main-only in architecture.md and in cron-manager's
    # SKILL.md, and accepted on any mode. An isolated job carrying it
    # flushed memory and rotated a main session it had never run in.
    if rotate_session and session != "main":
        return f"rotate_session is only valid on session 'main', not {session!r}"
    if context == "heartbeat":
        return None
    if session == "none":
        if not skill:
            return "session 'none' requires a skill"
        return None
    if not prompt:
        if skill:
            return (
                f"session {session!r} does not run skills; use session 'none',"
                f" or give the job a prompt"
            )
        return "job has neither 'prompt' nor 'skill'"
    return None


def load_jobs(workspace: Path) -> list[CronJob]:
    jobs_path = workspace / "config" / "jobs.json"
    if not jobs_path.exists():
        return []
    try:
        raw = json.loads(jobs_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error("failed to load jobs.json: %s", e)
        return []
    jobs: list[CronJob] = []
    for entry in raw:
        try:
            job_id = entry.get("id")
            if not job_id:
                logger.error("skipping job entry with missing or empty id")
                continue
            if not _SAFE_NAME_RE.match(job_id):
                logger.error("skipping job with invalid id: %r", job_id)
                continue
            session = entry.get("session", "agent")
            if session not in _VALID_SESSIONS:
                logger.error("skipping job %r: invalid session %r", job_id, session)
                continue
            deliver = entry.get("deliver", {})
            deliver_mode = deliver.get("mode", "announce")
            if deliver_mode not in _VALID_DELIVER_MODES:
                logger.error("skipping job %r: invalid deliver_mode %r", job_id, deliver_mode)
                continue
            deliver_channel = deliver.get("channel")
            if deliver_channel is not None and not _SAFE_NAME_RE.match(deliver_channel):
                logger.error("skipping job %r: invalid deliver_channel %r", job_id, deliver_channel)
                continue
            skill = entry.get("skill")
            if skill is not None and not _SAFE_NAME_RE.match(skill):
                logger.error("skipping job %r: invalid skill name %r", job_id, skill)
                continue
            schedule = entry.get("schedule")
            if schedule is not None:
                try:
                    parse_cron(schedule)
                except ValueError as e:
                    logger.error("skipping job %r: invalid schedule %r: %s", job_id, schedule, e)
                    continue
            at = entry.get("at")
            if at is not None:
                try:
                    datetime.strptime(at, "%Y-%m-%d %H:%M")
                except ValueError:
                    logger.error("skipping job %r: invalid at %r", job_id, at)
                    continue
            enabled = entry.get("enabled", True)
            rotate_session = entry.get("rotate_session", False)
            # "false" is truthy, and LLM-written JSON produces it. Every
            # other field here is validated; these two were not.
            if not isinstance(enabled, bool):
                logger.error("skipping job %r: enabled must be true or false, got %r", job_id, enabled)
                continue
            if not isinstance(rotate_session, bool):
                logger.error(
                    "skipping job %r: rotate_session must be true or false, got %r",
                    job_id, rotate_session,
                )
                continue
            prompt = entry.get("prompt")
            reason = _cross_field_error(
                session, entry.get("context"), prompt, skill, rotate_session,
            )
            if reason is not None:
                logger.error("skipping job %r: %s", job_id, reason)
                continue
            jobs.append(CronJob(
                id=job_id,
                schedule=schedule,
                at=at,
                prompt=prompt,
                skill=skill,
                session=session,
                context=entry.get("context"),
                model=entry.get("model"),
                deliver_mode=deliver_mode,
                deliver_channel=deliver_channel,
                enabled=enabled,
                rotate_session=rotate_session,
            ))
        except (KeyError, TypeError, AttributeError) as e:
            logger.error("skipping bad job entry: %s", e)
            continue
    return jobs


@contextmanager
def _file_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive flock on <path>.lock for the duration of the block.

    Yields False when the lock could not be taken, so callers can decide
    whether to proceed unlocked or give up.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError as e:
        logger.warning("cannot open lock for %s: %s", path, e)
        yield False
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield True
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _delete_job(workspace: Path, job_id: str) -> None:
    """Remove a job from jobs.json as a locked read-modify-write.

    The cron-manager skill writes this file from a channel turn while the
    scheduler thread can be deleting a fired one-shot.
    """
    jobs_path = workspace / "config" / "jobs.json"
    if not jobs_path.exists():
        return
    with _file_lock(jobs_path):
        try:
            raw = json.loads(jobs_path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        raw = [j for j in raw if j.get("id") != job_id]
        tmp = jobs_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        os.replace(tmp, jobs_path)
    logger.info("deleted one-shot job %s", job_id)


def utc_now_iso() -> str:
    """Run-log timestamps are UTC with a Z suffix, so the string sort in
    recent_cron_runs holds across timezone changes.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def render_timestamp(raw: str, tz: ZoneInfo, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Render a stored run-log timestamp in the display timezone.

    Entries written before the switch to UTC carry a local offset or none
    at all; both still render, so old history stays readable.
    """
    dt = parse_timestamp(raw, tz)
    if dt is None:
        return raw
    return dt.astimezone(tz).strftime(fmt)


def parse_timestamp(raw: str, tz: tzinfo) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def _run_sort_key(raw: str) -> datetime:
    """Order run logs by instant, not by the text of the timestamp.

    Sorting the strings is only correct while every entry carries the same
    suffix. Entries written before the switch to UTC carry a local offset,
    and "+07:00" sorts before "Z" in ASCII whatever instant it names, so a
    history spanning the switch came back interleaved. An unparseable
    timestamp sorts oldest rather than landing somewhere arbitrary.
    """
    return parse_timestamp(raw, timezone.utc) or datetime.min.replace(tzinfo=timezone.utc)


def _log_run(state_dir: Path, run: RunLog) -> None:
    log_dir = state_dir / "logs" / "cron"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run.job_id}.jsonl"
    entry = {
        "timestamp": run.timestamp,
        "status": run.status,
        "duration_ms": run.duration_ms,
        "tokens": run.tokens,
    }
    if run.error is not None:
        entry["error"] = run.error
    with _file_lock(log_path):
        if log_path.exists() and log_path.stat().st_size > _MAX_LOG_BYTES:
            lines = log_path.read_text().splitlines()
            log_path.write_text("\n".join(lines[-_MAX_LOG_LINES_KEEP:]) + "\n")
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")


def prune_cron_logs(state_dir: Path, live_job_ids: set[str]) -> list[str]:
    """Delete logs belonging to jobs that no longer exist, or every job ever
    deleted keeps costing a file read on every /status.
    """
    log_dir = state_dir / "logs" / "cron"
    if not log_dir.is_dir():
        return []
    removed: list[str] = []
    for log_file in sorted(log_dir.iterdir()):
        if log_file.suffix != ".jsonl" or log_file.stem in live_job_ids:
            continue
        try:
            log_file.unlink()
        except OSError as e:
            logger.warning("failed to delete stale cron log %s: %s", log_file.name, e)
            continue
        removed.append(log_file.stem)
    if removed:
        logger.info("removed cron logs for deleted jobs: %s", ", ".join(removed))
    return removed


def recent_cron_runs(state_dir: Path, limit: int | None = 10) -> list[RunLog]:
    """Read every job log under state/logs/cron, newest first.

    The inverse of _log_run. Malformed lines are skipped, not fatal. Only
    the tail of each file is read: a caller wanting the last ten runs has
    no use for a year of history, and /status reads this on every call.
    """
    log_dir = state_dir / "logs" / "cron"
    if not log_dir.is_dir():
        return []
    runs: list[RunLog] = []
    try:
        log_files = sorted(log_dir.iterdir())
    except OSError as e:
        logger.warning("failed to list cron logs: %s", e)
        return []
    for log_file in log_files:
        if log_file.suffix != ".jsonl":
            continue
        try:
            lines = log_file.read_text().splitlines()[-_MAX_LOG_LINES_READ:]
        except OSError as e:
            logger.warning("failed to read cron log %s: %s", log_file.name, e)
            continue
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Silence here hid the loss. A kill mid-append merges a
                # partial record with the next complete one, so a single
                # bad line quietly costs two runs.
                logger.warning(
                    "unreadable entry in cron log %s line %d, skipping",
                    log_file.name, lineno,
                )
                continue
            if not isinstance(entry, dict):
                logger.warning(
                    "non-object entry in cron log %s line %d, skipping",
                    log_file.name, lineno,
                )
                continue
            tokens = entry.get("tokens")
            runs.append(RunLog(
                timestamp=str(entry.get("timestamp", "")),
                job_id=log_file.stem,
                status=str(entry.get("status", "")),
                duration_ms=entry.get("duration_ms", 0),
                tokens=tokens if isinstance(tokens, dict) else {},
                error=entry.get("error"),
            ))
    runs.sort(key=lambda r: _run_sort_key(r.timestamp), reverse=True)
    return runs if limit is None else runs[:limit]


# -- cron expression parsing --

_FIELD_NAMES = ("minute", "hour", "day_of_month", "month", "day_of_week")


def _parse_field(expr: str, min_val: int, max_val: int) -> set[int]:
    result: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = None
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if step < 1:
                raise ValueError(f"step must be >= 1, got {step}")
            if step > (max_val - min_val + 1):
                raise ValueError(f"step {step} exceeds field range {min_val}-{max_val}")
            part = base

        if part == "*":
            vals = range(min_val, max_val + 1)
        elif "-" in part:
            lo, hi = part.split("-", 1)
            lo_int, hi_int = int(lo), int(hi)
            if lo_int < min_val or lo_int > max_val:
                raise ValueError(f"value {lo_int} out of range [{min_val}, {max_val}]")
            if hi_int < min_val or hi_int > max_val:
                raise ValueError(f"value {hi_int} out of range [{min_val}, {max_val}]")
            vals = range(lo_int, hi_int + 1)
        else:
            v = int(part)
            if v < min_val or v > max_val:
                raise ValueError(f"value {v} out of range [{min_val}, {max_val}]")
            vals = [v]

        if step:
            start = min(vals) if part != "*" else min_val
            end = max(vals) + 1 if part != "*" else max_val + 1
            result.update(range(start, end, step))
        else:
            result.update(vals)
    return result


def parse_cron(expression: str) -> dict[str, set[int]]:
    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(parts)}: {expression!r}")

    # day-of-week accepts 7 as Sunday, as standard cron does, and folds it
    # onto 0.
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    parsed: dict[str, set[int]] = {}
    for i, (name, (lo, hi)) in enumerate(zip(_FIELD_NAMES, bounds)):
        parsed[name] = _parse_field(parts[i], lo, hi)
    if 7 in parsed["day_of_week"]:
        parsed["day_of_week"] = (parsed["day_of_week"] - {7}) | {0}
    return parsed


def _matches_cron(dt: datetime, fields: dict[str, set[int]]) -> bool:
    if dt.minute not in fields["minute"]:
        return False
    if dt.hour not in fields["hour"]:
        return False
    if dt.month not in fields["month"]:
        return False

    dom_wild = fields["day_of_month"] == set(range(1, 32))
    dow_wild = fields["day_of_week"] == set(range(0, 7))

    # OR semantics: when both day-of-month and day-of-week are restricted,
    # match if EITHER matches (standard cron behavior)
    if dom_wild and dow_wild:
        return True
    if dom_wild:
        return dt.weekday() in _convert_dow(fields["day_of_week"])
    if dow_wild:
        return dt.day in fields["day_of_month"]
    return dt.day in fields["day_of_month"] or dt.weekday() in _convert_dow(fields["day_of_week"])


def _convert_dow(cron_dow: set[int]) -> set[int]:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon)."""
    mapping = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    return {mapping[d] for d in cron_dow if d in mapping}


def next_fire_time(
    fields: dict[str, set[int]],
    after: datetime,
    tz: ZoneInfo,
    stagger_seconds: int = 0,
) -> datetime:
    """When this expression will next fire, as a real local instant.

    Walks UTC minutes for the same reason due_fire_time does, so what
    `faff cron list` prints is what the scheduler will actually do; walking
    wall clock can land inside the spring-forward gap, on an instant that
    does not exist.
    """
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0)
    prev_wall = candidate.astimezone(tz).replace(tzinfo=None)
    candidate += timedelta(minutes=1)
    for _ in range(525960):  # ~1 year of minutes
        local = candidate.astimezone(tz)
        wall = local.replace(tzinfo=None)
        skipped = int((wall - prev_wall).total_seconds() // 60) - 1
        for i in range(1, skipped + 1):
            if _matches_cron(prev_wall + timedelta(minutes=i), fields):
                return (candidate + timedelta(seconds=stagger_seconds)).astimezone(tz)
        prev_wall = wall
        if _matches_cron(local, fields):
            return (candidate + timedelta(seconds=stagger_seconds)).astimezone(tz)
        candidate += timedelta(minutes=1)
    raise ValueError("no matching fire time found within one year")


def _stagger_for(job_id: str, minute: int, fields: dict[str, set[int]]) -> int:
    if minute == 0 and 0 in fields["minute"]:
        return _stagger_offset(job_id)
    return 0


def due_fire_time(
    job_id: str,
    fields: dict[str, set[int]],
    now: datetime,
    tz: ZoneInfo,
    last_fire: datetime | None,
) -> datetime | None:
    """The UTC minute this job owes a run for, or None.

    Candidates are walked in UTC, not wall clock, which is what makes both
    DST transitions behave. On fall-back the repeated hour produces
    distinct instants instead of comparing equal and being suppressed; on
    spring-forward the wall-clock minutes that never occur are detected as
    a gap and matched anyway, so a reminder inside the lost hour still runs
    at the first real instant after it.

    A match is only due once `now` has passed the matched minute plus the
    job's stagger. The previous code required `now` to still be inside the
    matched minute, which any stagger of 60 or more outlived, so roughly
    80 percent of top-of-hour jobs could never fire at all.
    """
    now_minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    # The window must cover the largest stagger this job could be given,
    # plus the catch-up allowance, or a job staggered near the end of the
    # range falls outside it whenever a tick cycle runs long and is never
    # owed the fire.
    window = CATCHUP_MINUTES + (_MAX_STAGGER_SECONDS + 59) // 60
    earliest = now_minute - timedelta(minutes=window)
    if last_fire is not None:
        last_minute = last_fire.astimezone(timezone.utc).replace(second=0, microsecond=0)
        earliest = max(earliest, last_minute + timedelta(minutes=1))

    due: datetime | None = None
    prev_wall: datetime | None = None
    candidate = earliest
    while candidate <= now_minute:
        local = candidate.astimezone(tz)
        wall = local.replace(tzinfo=None)
        if prev_wall is not None:
            skipped = int((wall - prev_wall).total_seconds() // 60) - 1
            for i in range(1, skipped + 1):
                lost = prev_wall + timedelta(minutes=i)
                if _matches_cron(lost, fields):
                    due = candidate
        prev_wall = wall
        if _matches_cron(local, fields):
            # On fall-back the same wall-clock time happens twice, and
            # astimezone marks the second instance fold=1. An hourly job
            # should fire in both, because both really are that hour. A job
            # naming specific hours means once a day, so it takes the first
            # occurrence only; otherwise a 01:30 reminder arrived twice.
            names_specific_hours = len(fields["hour"]) < 24
            if not (local.fold == 1 and names_specific_hours):
                stagger = _stagger_for(job_id, local.minute, fields)
                if now >= candidate + timedelta(seconds=stagger):
                    due = candidate
        candidate += timedelta(minutes=1)
    return due


def next_one_shot_time(at_str: str, tz: ZoneInfo) -> datetime | None:
    try:
        naive = datetime.strptime(at_str, "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=tz)
    except ValueError:
        logger.error("invalid one-shot datetime: %s", at_str)
        return None


# -- provider preflight --

_preflight_cache: dict[str, tuple[bool, float]] = {}
_preflight_lock = threading.Lock()


def _is_local_endpoint(base_url: str) -> bool:
    from urllib.parse import urlsplit
    from faffmonkey.config import LOCAL_HOSTS

    return (urlsplit(base_url).hostname or "") in LOCAL_HOSTS


def provider_preflight(base_url: str) -> bool:
    """Cheap liveness probe for a local provider before a cron run.

    Only local endpoints are probed, as documented: a remote provider is
    reachable or not on its own terms, and probing it unauthenticated made
    a 401 look like an outage. For the same reason any HTTP status counts
    as reachable; only a transport error with no status means down.
    """
    if not _is_local_endpoint(base_url):
        return True

    now = time.monotonic()
    with _preflight_lock:
        cached = _preflight_cache.get(base_url)
        if cached is not None:
            ok, ts = cached
            ttl = PREFLIGHT_CACHE_SECONDS if ok else PREFLIGHT_NEGATIVE_CACHE_SECONDS
            if now - ts < ttl:
                return ok

    import urllib.request
    import urllib.error
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10):
            pass
        with _preflight_lock:
            _preflight_cache[base_url] = (True, now)
        return True
    except urllib.error.HTTPError:
        with _preflight_lock:
            _preflight_cache[base_url] = (True, now)
        return True
    except (urllib.error.URLError, OSError) as e:
        logger.warning("preflight failed for %s: %s", url, e)
        with _preflight_lock:
            _preflight_cache[base_url] = (False, now)
        return False


def clear_preflight_cache() -> None:
    with _preflight_lock:
        _preflight_cache.clear()


# -- stale ack detection --

def _is_stale_ack(text: str, ack_max_chars: int = 300) -> bool:
    if len(text) >= ack_max_chars:
        return False
    lower = text.lower()
    return any(p in lower for p in STALE_ACK_PATTERNS)


# -- session execution --

def _clean_cron_prompt(prompt: str, job_id: str) -> str | None:
    """Strip invisible characters and scan for injection patterns.

    Returns the cleaned prompt, or None if an injection pattern was detected.
    """
    cleaned = strip_invisible(prompt)
    hit = scan_patterns(cleaned, path=f"<cron:{job_id}>")
    if hit is not None:
        logger.warning("cron job %s prompt blocked: %s", job_id, hit)
        return None
    return cleaned


def _complete_with_timeout(
    provider: object,
    request: CompletionRequest,
    timeout: float = 120.0,
) -> CompletionResponse:
    return run_with_timeout(
        lambda: provider.complete(request), timeout, "provider.complete()"
    )


def _has_tool_syntax(text: str) -> bool:
    return any(m in text for m in _TOOL_SYNTAX_MARKERS)


def _ensure_plain_text(
    provider: object,
    messages: list[Message],
    model_config: ModelConfig,
    text: str,
    usage: TokenUsage,
    job_id: str,
    allow_no_reply: bool = False,
) -> tuple[str, TokenUsage]:
    """One re-prompt if a no-tools completion emitted tool-call syntax;
    raises rather than delivering raw tool XML if it does it again."""
    if not _has_tool_syntax(text):
        return text, usage
    logger.warning(
        "job %s emitted tool-call syntax in a no-tools session, re-prompting",
        job_id,
    )
    nudge = "You have no tools in this run. Answer in plain text only."
    if allow_no_reply:
        nudge += " If there is nothing to say, respond with exactly NO_REPLY."
    retry_messages = [
        *messages,
        Message(role="assistant", content=text),
        Message(role="user", content=nudge),
    ]
    request = CompletionRequest(messages=retry_messages, model=model_config.model)
    response = _complete_with_timeout(provider, request, timeout=model_config.timeout)
    usage = usage + response.usage
    if _has_tool_syntax(response.text):
        raise RuntimeError(
            f"cron job {job_id!r} kept emitting tool-call syntax in a no-tools session"
        )
    return response.text, usage


def _run_isolated(
    job: CronJob,
    config: Config,
    resolve_provider: Callable[[ModelConfig], object],
    workspace: Path,
    state_dir: Path,
) -> tuple[str, TokenUsage]:
    from faffmonkey.runtime.bootstrap import load_bootstrap

    prompt = _clean_cron_prompt(job.prompt or "", job.id)
    if prompt is None:
        raise RuntimeError(f"cron job {job.id!r} prompt blocked by injection scan")

    trust_store = load_trust_store(state_dir)
    bootstrap = load_bootstrap(workspace, config, mode="cron", wrap=True, trust_store=trust_store)
    model_config = config.resolve_model("cron_default", override=job.model)

    messages: list[Message] = []
    if bootstrap.text:
        messages.append(Message(role="system", content=bootstrap.text))
    messages.append(Message(role="user", content=prompt))

    provider = resolve_provider(model_config)
    request = CompletionRequest(
        messages=messages,
        model=model_config.model,
    )
    response = _complete_with_timeout(provider, request, timeout=model_config.timeout)
    text = response.text
    usage = response.usage

    if _is_stale_ack(text, ack_max_chars=config.heartbeat.ack_max_chars):
        logger.info("stale ack detected for job %s, re-prompting", job.id)
        messages.append(Message(role="assistant", content=text))
        messages.append(Message(role="user", content="Please provide the actual result, not just an acknowledgement."))
        request = CompletionRequest(messages=messages, model=model_config.model)
        response = _complete_with_timeout(provider, request, timeout=model_config.timeout)
        text = response.text
        usage = usage + response.usage

    text, usage = _retry_if_empty(
        provider, messages, model_config, text, usage, job.id,
    )
    text, usage = _ensure_plain_text(
        provider, messages, model_config, text, usage, job.id,
    )
    return text, usage


def _retry_if_empty(
    provider: object,
    messages: list[Message],
    model_config: ModelConfig,
    text: str,
    usage: TokenUsage,
    job_id: str,
) -> tuple[str, TokenUsage]:
    """Up to EMPTY_RESPONSE_RETRIES nudges while the reply is blank."""
    for attempt in range(EMPTY_RESPONSE_RETRIES):
        if text.strip():
            break
        logger.warning("empty response for job %s, attempt %d/%d", job_id, attempt + 1, EMPTY_RESPONSE_RETRIES)
        messages.append(Message(role="assistant", content=""))
        messages.append(Message(role="user", content="(continue)"))
        request = CompletionRequest(messages=messages, model=model_config.model)
        response = _complete_with_timeout(provider, request, timeout=model_config.timeout)
        text = response.text
        usage = usage + response.usage
    return text, usage


def _redact_for_history(text: str, job_id: str) -> str:
    from faffmonkey.runtime.ingest import flag_response
    return flag_response(text, f"<cron:{job_id}>", "cron response")[0]


def _run_main(
    job: CronJob,
    config: Config,
    resolve_provider: Callable[[ModelConfig], object],
    workspace: Path,
    state_dir: Path,
) -> tuple[str, TokenUsage]:
    from faffmonkey.runtime.bootstrap import load_bootstrap
    from faffmonkey.runtime.session import SessionStore

    prompt = _clean_cron_prompt(job.prompt or "", job.id)
    if prompt is None:
        raise RuntimeError(f"cron job {job.id!r} prompt blocked by injection scan")

    model_config = config.resolve_model("cron_default", override=job.model)
    # The live conversation is one session whichever channel it came by,
    # so a main job runs there regardless of where its output is sent.
    channel_id = MAIN_SESSION_KEY

    with _main_session_lock:
        session_store = None
        try:
            session_store = SessionStore(state_dir / "sessions.db")
            session = session_store.get_or_create_main_session(channel_id)
            history = session_store.get_history(session.id)

            trust_store = load_trust_store(state_dir)
            bootstrap = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store=trust_store)
            messages: list[Message] = []
            if bootstrap.text:
                messages.append(Message(role="system", content=bootstrap.text))
            messages.extend(history)
            messages.append(Message(role="user", content=prompt))

            provider = resolve_provider(model_config)
            request = CompletionRequest(messages=messages, model=model_config.model)
            response = _complete_with_timeout(provider, request, timeout=model_config.timeout)
            text = response.text
            usage = response.usage

            # The exchange is written to the shared session only if the
            # final reply is non-empty: a blank assistant row is replayed
            # on every later chat turn, and nothing may enter the session
            # that a provider can refuse.
            exchange: list[tuple[str, str]] = [("user", prompt)]

            if _is_stale_ack(text, ack_max_chars=config.heartbeat.ack_max_chars):
                logger.info("stale ack detected for job %s (main session), re-prompting", job.id)
                reprompt = "Please provide the actual result, not just an acknowledgement."
                exchange.append(("assistant", _redact_for_history(text, job.id)))
                exchange.append(("user", reprompt))
                messages.append(Message(role="assistant", content=text))
                messages.append(Message(role="user", content=reprompt))
                request = CompletionRequest(messages=messages, model=model_config.model)
                response = _complete_with_timeout(provider, request, timeout=model_config.timeout)
                text = response.text
                usage = usage + response.usage

            text, usage = _retry_if_empty(
                provider, messages, model_config, text, usage, job.id,
            )
            text, usage = _ensure_plain_text(
                provider, messages, model_config, text, usage, job.id,
            )
            if text.strip():
                exchange.append(("assistant", _redact_for_history(text, job.id)))
                for role, content in exchange:
                    session_store.append_message(session.id, role, content)
        finally:
            if session_store is not None:
                session_store.close()

    return text, usage


def _condense_prompt(prompt: str | None, limit: int = MAX_RECORDED_PROMPT_CHARS) -> str:
    """The job's instruction, short enough to sit in a conversation."""
    if not prompt:
        return ""
    collapsed = " ".join(prompt.split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + "..."
    return collapsed


def _cron_origin_marker(job_id: str, prompt: str | None = None) -> str:
    condensed = _condense_prompt(prompt)
    if condensed:
        return f"[delivered to you by cron job {job_id!r}, which asked: {condensed}]"
    return f"[delivered to you by cron job {job_id!r}]"


def _record_delivery(
    state_dir: Path, channel_id: str, job_id: str, text: str,
    prompt: str | None = None,
) -> None:
    """Put a delivered cron message into the channel's conversation.

    Without this the agent has no memory of the message it just sent, so
    replying to a morning briefing lands in a history containing no trace of
    the briefing. It has to happen here rather than inside a session mode:
    agent, isolated and none all deliver and none of them touches the store,
    and none has no LLM exchange to persist at all.

    The prompt goes in with it. Recording only the output left the agent able
    to quote what it sent and unable to say why, and left the operator with
    no way to reconstruct afterwards which instruction produced which
    message. It is condensed rather than stored whole, because this line is
    replayed on every later turn.
    """
    from faffmonkey.runtime.session import SessionStore

    with _main_session_lock:
        store = SessionStore(state_dir / "sessions.db")
        try:
            session = store.get_or_create_main_session(channel_id)
            store.append_message(
                session.id,
                "assistant",
                f"{_cron_origin_marker(job_id, prompt)}\n{text}",
            )
        finally:
            store.close()


def _heartbeat_slot(config: Config, job: CronJob) -> str:
    """The slot a heartbeat wake runs on: the job's own, else the
    `heartbeat` route, else cron_default."""
    return job.model or config.routing.get("heartbeat") or config.routing.get("cron_default", "main")


def _resolve_heartbeat_model(config: Config, job: CronJob) -> ModelConfig:
    return config.resolve_model("cron_default", override=_heartbeat_slot(config, job))


def _refresh_triggers(workspace: Path, state_dir: Path | None) -> None:
    """Run the watchdog so triggers.json is current before it is read.

    Zero tokens: it is a local skill script. A missing or failing watchdog
    leaves whatever triggers.json is already there, and the tick still runs.
    """
    from faffmonkey.runtime.skills import invoke as skill_invoke

    try:
        _output, _attachments, is_error = skill_invoke(
            workspace, "heartbeat", "run", state_dir=state_dir,
        )
    except Exception as e:
        logger.warning("heartbeat watchdog failed: %s", e)
        return
    if is_error:
        logger.warning("heartbeat watchdog reported an error: %s", _output.strip()[:200])


def _load_triggers(workspace: Path) -> dict | None:
    triggers_path = workspace / "skills-data" / "heartbeat" / "triggers.json"
    if not triggers_path.exists():
        return None
    try:
        return json.loads(triggers_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _is_no_reply(text: str) -> bool:
    """A bare NO_REPLY however the model dressed it: quotes, backticks,
    trailing punctuation, lowercase. An exact match let "NO_REPLY." reach
    the user as a message."""
    return text.strip().strip("`'\"*.!\t \n").casefold() == "no_reply"


def _heartbeat_skip_reason(config: Config, now: datetime) -> str | None:
    """Why this heartbeat run should not happen.

    Both settings were documented, parsed and validated, and then read by
    nothing: a disabled heartbeat still ran, and active_hours never
    stopped a 3am escalation.
    """
    if not config.heartbeat.enabled:
        return "heartbeat-disabled"
    start, end = config.heartbeat.active_hours
    hour = now.astimezone(config.timezone).hour
    inside = start <= hour < end if start <= end else (hour >= start or hour < end)
    if not inside:
        return "outside-active-hours"
    return None


def _consume_triggers(workspace: Path, files: list) -> None:
    """Delete the trigger files a wake was handed.

    Called only after the agent turn returned, so a turn that raised keeps
    its triggers for the retry.
    """
    triggers_dir = workspace / "skills-data" / "heartbeat" / "triggers.d"
    for name in files:
        if not isinstance(name, str) or not _SAFE_NAME_RE.match(name):
            continue
        try:
            (triggers_dir / name).unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("could not remove trigger %s: %s", name, e)


def _format_recent(recent: list[dict], config: Config) -> str:
    lines = []
    for entry in recent:
        at = parse_timestamp(str(entry.get("at", "")), timezone.utc)
        when = at.astimezone(config.timezone).strftime("%a %H:%M") if at else "?"
        lines.append(f"- {when}: {entry.get('text', '')}")
    return "\n".join(lines)


def _run_heartbeat(
    job: CronJob,
    config: Config,
    resolve_provider: Callable[[ModelConfig], object],
    workspace: Path,
    state_dir: Path,
    now: datetime | None = None,
    recent: list[dict] | None = None,
    search_provider: SearchProvider | None = None,
) -> tuple[str, TokenUsage, str | None]:
    """One heartbeat tick: the watchdog, then an agent turn only if it found
    something. Returns (text, usage, skip_reason)."""
    skip = _heartbeat_skip_reason(config, now or datetime.now(timezone.utc))
    if skip is not None:
        return "", TokenUsage(), skip

    _refresh_triggers(workspace, state_dir)
    triggers = _load_triggers(workspace) or {}
    trigger_lines = [
        t for t in triggers.get("triggers", []) if isinstance(t, str) and t.strip()
    ]
    if triggers.get("status") != "attention" or not trigger_lines:
        logger.info("heartbeat: clean")
        return "", TokenUsage(), "clean"
    logger.info("heartbeat: attention (%s)", "; ".join(trigger_lines))

    # The same symlink and case check the bootstrap applies to every
    # always-trusted file.
    heartbeat_read = read_and_check_trust("HEARTBEAT.md", workspace, {})
    if heartbeat_read is None:
        heartbeat_content = ""
    elif not heartbeat_read.trusted:
        logger.warning("HEARTBEAT.md failed trust check (symlink or case mismatch), ignoring")
        heartbeat_content = ""
    else:
        heartbeat_content = heartbeat_read.content.strip()

    sections = [job.prompt or HEARTBEAT_PROMPT]
    sections.append("Triggers:\n" + "\n".join(f"- {t}" for t in trigger_lines))
    readings = [
        r for r in triggers.get("readings", []) if isinstance(r, str) and r.strip()
    ]
    if readings:
        sections.append("Latest readings:\n" + "\n".join(f"- {r}" for r in readings))
    if heartbeat_content:
        sections.append(f"Standing instructions (HEARTBEAT.md):\n{heartbeat_content}")
    if recent:
        sections.append("Sent by the heartbeat recently:\n" + _format_recent(recent, config))

    text, usage = _run_agent(
        job, config, resolve_provider, workspace, state_dir,
        search_provider=search_provider,
        prompt="\n\n".join(sections),
        slot=_heartbeat_slot(config, job),
    )
    _consume_triggers(workspace, triggers.get("files", []))
    logger.info("heartbeat answered %s", _describe_answer(text))
    return text, usage, None


def _describe_answer(text: str) -> str:
    if not text.strip():
        return "nothing (empty response, treated as NO_REPLY)"
    if _is_no_reply(text):
        return "NO_REPLY"
    return f"{len(text)} chars"


# RLock: a channel loop holds it for a whole turn and the persist calls
# inside that turn take it again.
_main_session_lock = threading.RLock()


def _rotate_main_session(
    config: Config,
    resolve_provider: Callable[[ModelConfig], object],
    workspace: Path,
    state_dir: Path,
    channel_id: str,
    session_rotated_events: list[threading.Event] | None = None,
) -> None:
    from faffmonkey.runtime.compaction import memory_flush
    from faffmonkey.runtime.session import SessionStore

    if not _main_session_lock.acquire(blocking=False):
        logger.warning("skipping rotation for %s: main session in active use", channel_id)
        return

    try:
        session_store = SessionStore(state_dir / "sessions.db")
        try:
            session = session_store.get_or_create_main_session(channel_id)
            try:
                memory_flush(session_store, session.id, workspace, resolve_provider, config)
            except Exception:
                logger.warning("memory flush failed during session rotation, proceeding anyway")
            session_store.deactivate_session(session.id)
            session_store.get_or_create_main_session(channel_id)
            if session_rotated_events is not None:
                for event in session_rotated_events:
                    event.set()
        finally:
            session_store.close()
    finally:
        _main_session_lock.release()


def _run_agent(
    job: CronJob,
    config: Config,
    resolve_provider: Callable[[ModelConfig], object],
    workspace: Path,
    state_dir: Path,
    search_provider: SearchProvider | None = None,
    prompt: str | None = None,
    slot: str | None = None,
) -> tuple[str, TokenUsage]:
    """One ephemeral tool-capable AgentLoop turn.

    Nothing is persisted: db_path=None keeps SessionStore untouched and the
    in-memory history dies with the turn. Ask-level tool permissions are
    denied (no human present at cron time). The loop's existing budgets
    (tool calls, LLM round-trips, inactivity timeout) bound the turn.

    `prompt` and `slot` override the job's own; the heartbeat assembles its
    prompt from the watchdog's findings and runs on its own route.
    """
    from faffmonkey.runtime.bootstrap import load_bootstrap
    from faffmonkey.runtime.loop import AgentLoop
    from faffmonkey.runtime.tools import ToolRegistry
    from faffmonkey.seams.channel_noop import NoopChannel

    # The loop resolves the conversation route; cron picks its own slot, so
    # it is passed in rather than routed for.
    slot = slot or job.model or config.routing.get("cron_default", "main")

    trust_store = load_trust_store(state_dir)
    bootstrap = load_bootstrap(workspace, config, mode="full", wrap=True, trust_store=trust_store)
    registry = ToolRegistry(
        workspace=workspace,
        permissions=config.tool_permissions,
        shell_preapproved=config.shell_preapproved,
        prompt_fn=None,
        tz=str(config.timezone),
        wrap=True,
        search_provider=search_provider,
        state_dir=state_dir,
    )
    loop = AgentLoop(
        resolve_provider=resolve_provider,
        config=config,
        channel=NoopChannel(),
        system_prompt=bootstrap.text,
        tool_registry=registry,
        workspace=workspace,
        allow_overflow=True,
        state_dir=state_dir,
        conversation_slot=slot,
        config_readonly=True,
    )
    cleaned = _clean_cron_prompt(prompt if prompt is not None else (job.prompt or ""), job.id)
    if cleaned is None:
        raise RuntimeError(f"cron job {job.id!r} prompt blocked by injection scan")
    text = loop.handle_message(cleaned)
    # The loop answers a person, so it turns "provider returned nothing" into
    # readable text. Delivered as a job result that reads as success.
    if loop.last_response_empty:
        return "", loop.usage_total

    if _is_stale_ack(text, ack_max_chars=config.heartbeat.ack_max_chars):
        logger.info("stale ack detected for job %s (agent session), re-prompting", job.id)
        text = loop.handle_message(
            "Please provide the actual result, not just an acknowledgement."
        )
        if loop.last_response_empty:
            return "", loop.usage_total

    return text, loop.usage_total


def _run_none(
    job: CronJob,
    workspace: Path,
    state_dir: Path | None = None,
) -> tuple[str, TokenUsage]:
    """Run a skill with no LLM. Raises on skill failure, so a watchdog that
    exits 1 is logged as a failure and its stderr is not delivered as the
    answer.
    """
    if not job.skill:
        raise SkillFailed("session=none requires a skill")
    from faffmonkey.runtime.skills import invoke as skill_invoke, _MAX_SKILL_TIMEOUT
    output, _attachments, is_error = run_with_timeout(
        lambda: skill_invoke(workspace, job.skill, "run", state_dir=state_dir),
        _MAX_SKILL_TIMEOUT,
        f"skill {job.skill}",
    )
    if is_error:
        raise SkillFailed(f"skill {job.skill!r} failed: {output.strip()[:500]}")
    return output, TokenUsage()


# -- main scheduler --

def _hash_file(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except (OSError, FileNotFoundError):
        return None
    return hashlib.sha256(data).hexdigest()


def _diff_jobs(
    old: list[CronJob], new: list[CronJob],
) -> tuple[list[CronJob], list[CronJob], list[tuple[CronJob, CronJob]]]:
    old_by_id = {j.id: j for j in old}
    new_by_id = {j.id: j for j in new}

    added = [new_by_id[jid] for jid in new_by_id if jid not in old_by_id]
    removed = [old_by_id[jid] for jid in old_by_id if jid not in new_by_id]
    changed = []
    for jid in old_by_id:
        if jid in new_by_id and old_by_id[jid] != new_by_id[jid]:
            changed.append((old_by_id[jid], new_by_id[jid]))

    return added, removed, changed


def _describe_schedule(job: CronJob) -> str:
    if job.at:
        return f"at {job.at}"
    if job.schedule:
        return f"schedule {job.schedule}"
    return "no schedule"


def _build_change_summary(
    added: list[CronJob],
    removed: list[CronJob],
    changed: list[tuple[CronJob, CronJob]],
) -> str:
    parts: list[str] = []
    for j in added:
        parts.append(f"added '{j.id}' ({_describe_schedule(j)})")
    for j in removed:
        parts.append(f"removed '{j.id}'")
    for old_j, new_j in changed:
        details: list[str] = []
        if old_j.schedule != new_j.schedule:
            details.append(f"schedule: {old_j.schedule} → {new_j.schedule}")
        if old_j.at != new_j.at:
            details.append(f"at: {old_j.at} → {new_j.at}")
        if old_j.enabled != new_j.enabled:
            details.append(f"enabled: {old_j.enabled} → {new_j.enabled}")
        if old_j.prompt != new_j.prompt:
            details.append("prompt changed")
        if old_j.skill != new_j.skill:
            details.append(f"skill: {old_j.skill} → {new_j.skill}")
        if old_j.session != new_j.session:
            details.append(f"session: {old_j.session} → {new_j.session}")
        if old_j.deliver_mode != new_j.deliver_mode:
            details.append(f"deliver_mode: {old_j.deliver_mode} → {new_j.deliver_mode}")
        if old_j.deliver_channel != new_j.deliver_channel:
            details.append(f"deliver_channel: {old_j.deliver_channel} → {new_j.deliver_channel}")
        detail_str = ", ".join(details) if details else "config changed"
        parts.append(f"changed '{new_j.id}' ({detail_str})")
    return "Cron jobs modified: " + ", ".join(parts)


@dataclass
class Scheduler:
    config: Config
    workspace: Path
    state_dir: Path
    resolve_provider: Callable[[ModelConfig], object]
    channels: dict[str, Channel]
    search_provider: SearchProvider | None = None
    # False for a manual `faff cron run`: that builds a fresh Scheduler
    # holding only the one job it was asked about, so saving state wrote a
    # cron-state.json containing that job alone and wiped every other job's
    # last-fire time and backoff. A debugging command must not mutate the
    # thing being debugged.
    persist_state: bool = True
    # False for a manual `faff cron run`, which has no channels: the output
    # is returned on the RunLog for the operator to read instead of being
    # recorded as a failed delivery to a channel that only exists in
    # `faff run`.
    deliver_output: bool = True
    session_rotated_events: list[threading.Event] = field(default_factory=list)
    history_dirty_events: dict[str, threading.Event] = field(default_factory=dict)
    _backoff: dict[str, BackoffState] = field(default_factory=dict)
    _last_fire: dict[str, datetime] = field(default_factory=dict)
    # Per job, the last few delivered messages: what a heartbeat wake is
    # told it already said.
    _recent: dict[str, list[dict]] = field(default_factory=dict)
    _running: bool = False
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _run_lock: threading.Lock = field(default_factory=threading.Lock)
    _jobs_hash: str | None = field(default=None, init=False, repr=False)
    _jobs_snapshot: list[CronJob] = field(default_factory=list, init=False, repr=False)
    _last_channel: str | None = field(default=None, init=False, repr=False)

    def _get_backoff(self, job_id: str) -> BackoffState:
        if job_id not in self._backoff:
            self._backoff[job_id] = BackoffState()
        return self._backoff[job_id]

    @property
    def _state_path(self) -> Path:
        return self.state_dir / "cron-state.json"

    def load_state(self) -> None:
        """Restore last-fire and backoff from state/, so a restart neither
        re-runs a job that fired in the current minute nor clears a job's
        backoff against a dead provider.
        """
        try:
            raw = json.loads(self._state_path.read_text())
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("ignoring unreadable cron state: %s", e)
            return
        if not isinstance(raw, dict):
            return
        now_utc = datetime.now(timezone.utc)
        last_channel = raw.get("last_channel")
        if isinstance(last_channel, str) and _SAFE_NAME_RE.match(last_channel):
            self._last_channel = last_channel
        for job_id, entry in (raw.get("jobs") or {}).items():
            if not isinstance(entry, dict):
                continue
            last = parse_timestamp(str(entry.get("last_fire", "")), timezone.utc)
            if last is not None:
                self._last_fire[job_id] = last
            recent = entry.get("recent")
            if isinstance(recent, list):
                self._recent[job_id] = [
                    r for r in recent
                    if isinstance(r, dict)
                    and isinstance(r.get("at"), str) and isinstance(r.get("text"), str)
                ][-RECENT_DELIVERIES_KEEP:]
            failures = entry.get("failure_count", 0)
            if not isinstance(failures, int) or failures <= 0:
                continue
            state = self._get_backoff(job_id)
            state.failure_count = failures
            retry_at = parse_timestamp(str(entry.get("next_retry_at", "")), timezone.utc)
            if retry_at is None:
                continue
            state.next_retry_at = retry_at
            state.next_retry_after = time.monotonic() + max(
                0.0, (retry_at - now_utc).total_seconds(),
            )

    def _save_state(self) -> None:
        if not self.persist_state:
            return
        jobs: dict[str, dict] = {}
        for job_id, last in self._last_fire.items():
            jobs.setdefault(job_id, {})["last_fire"] = (
                last.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            )
        for job_id, state in self._backoff.items():
            if state.failure_count == 0:
                continue
            entry = jobs.setdefault(job_id, {})
            entry["failure_count"] = state.failure_count
            if state.next_retry_at is not None:
                entry["next_retry_at"] = (
                    state.next_retry_at.isoformat(timespec="seconds").replace("+00:00", "Z")
                )
        cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENT_DELIVERIES_HOURS)
        for job_id, entries in self._recent.items():
            kept = [
                e for e in entries
                if (parse_timestamp(e["at"], timezone.utc) or cutoff) > cutoff
            ]
            if kept:
                jobs.setdefault(job_id, {})["recent"] = kept
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            state: dict = {"jobs": jobs}
            if self._last_channel is not None:
                state["last_channel"] = self._last_channel
            tmp.write_text(json.dumps(state, indent=2) + "\n")
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning("failed to persist cron state: %s", e)

    def _check_jobs_changed(self) -> None:
        jobs_path = self.workspace / "config" / "jobs.json"
        new_hash = _hash_file(jobs_path)
        if new_hash == self._jobs_hash:
            return
        new_jobs = load_jobs(self.workspace)
        if self._jobs_hash is None:
            self._jobs_hash = new_hash
            self._jobs_snapshot = new_jobs
            return
        self._jobs_hash = new_hash
        added, removed, changed = _diff_jobs(self._jobs_snapshot, new_jobs)
        self._jobs_snapshot = new_jobs
        if not added and not removed and not changed:
            return
        summary = _build_change_summary(added, removed, changed)
        logger.info("jobs.json changed: %s", summary)
        from faffmonkey.runtime.redaction import redact
        for channel in self.channels.values():
            try:
                channel.send(OutboundMessage(text=redact(summary)))
            except Exception:
                logger.exception("failed to notify channel of jobs.json change")

    def _record_and_signal(self, job: CronJob, delivered: str) -> None:
        """Persist a delivered message, then wake the loop that owns it.

        session=main has already written the exchange to the store, so it
        only needs the signal; every other mode needs both. The signal is
        what makes either visible to a conversation that is already running.
        """
        if job.session != "main":
            try:
                _record_delivery(
                    self.state_dir, MAIN_SESSION_KEY, job.id, delivered, job.prompt,
                )
            except sqlite3.Error:
                logger.exception(
                    "job %s delivered but could not be recorded in the"
                    " conversation, so a reply will have no context", job.id,
                )
                return
        # Every channel loop shares the session, so every one reloads.
        for event in self.history_dirty_events.values():
            event.set()

    def _remember_delivery(self, job_id: str, text: str) -> None:
        entries = self._recent.setdefault(job_id, [])
        entries.append({"at": utc_now_iso(), "text": text[:RECENT_DELIVERY_CHARS]})
        del entries[:-RECENT_DELIVERIES_KEEP]
        self._save_state()

    def note_activity(self, channel: str) -> None:
        """Remember where the user last spoke, for deliver.channel "last"."""
        if channel != self._last_channel:
            self._last_channel = channel
            self._save_state()

    def _resolve_deliver_channel(self, job: CronJob) -> str | None:
        if job.deliver_channel != LAST_CHANNEL:
            return job.deliver_channel
        if self._last_channel in self.channels:
            return self._last_channel
        # Nothing said since the last restart that persisted it, or that
        # channel is not running: the first channel that is.
        return next(iter(sorted(self.channels)), None)

    def _drop_one_shot(self, job: CronJob) -> None:
        """Delete a fired one-shot without announcing it as a user edit.

        _delete_job changes jobs.json, so the next _check_jobs_changed
        diffed it against a stale snapshot and told every channel a job had
        been "removed", as if a human had edited the file.
        """
        _delete_job(self.workspace, job.id)
        jobs_path = self.workspace / "config" / "jobs.json"
        self._jobs_hash = _hash_file(jobs_path)
        self._jobs_snapshot = [j for j in self._jobs_snapshot if j.id != job.id]

    def run_job(self, job: CronJob, delete_one_shot: bool = True) -> RunLog:
        with self._run_lock:
            return self._run_job_locked(job, delete_one_shot=delete_one_shot)

    def _run_job_locked(self, job: CronJob, delete_one_shot: bool = True) -> RunLog:
        start = time.monotonic()
        now_str = utc_now_iso()
        backoff = self._get_backoff(job.id)
        one_shot = job.at is not None and delete_one_shot

        needs_provider = job.session in ("isolated", "main", "agent") or job.context == "heartbeat"
        if needs_provider:
            try:
                # A heartbeat wake routes through the "heartbeat" slot when
                # one is configured, so that is the endpoint to probe.
                if job.context == "heartbeat":
                    model_config = _resolve_heartbeat_model(self.config, job)
                else:
                    model_config = self.config.resolve_model("cron_default", override=job.model)
            except ConfigError as e:
                # A job naming a model or slot that does not resolve fails on
                # its own; raising out of run_job would kill the tick for every
                # job listed after it.
                logger.error("job %s names an unusable model: %s", job.id, e)
                backoff.record_failure()
                self._save_state()
                run_log = RunLog(
                    timestamp=now_str, job_id=job.id,
                    status="error", error=f"unusable model: {e}",
                )
                _log_run(self.state_dir, run_log)
                return run_log
            if not provider_preflight(model_config.base_url):
                # Logging and backing off matter more here than anywhere
                # else: without them a dead provider leaves no run log, no
                # backoff, and every operator surface reporting nothing
                # wrong while cron has stopped entirely.
                backoff.record_failure()
                self._save_state()
                run_log = RunLog(
                    timestamp=now_str, job_id=job.id,
                    status="skipped", error="preflight failed",
                )
                _log_run(self.state_dir, run_log)
                return run_log

        try:
            if job.context == "heartbeat":
                text, usage, skip_reason = _run_heartbeat(
                    job, self.config, self.resolve_provider,
                    self.workspace, self.state_dir,
                    now=datetime.now(self.config.timezone),
                    recent=self._recent.get(job.id, []),
                    search_provider=self.search_provider,
                )
                if skip_reason:
                    duration = int((time.monotonic() - start) * 1000)
                    run_log = RunLog(
                        timestamp=now_str, job_id=job.id,
                        status="skipped", duration_ms=duration,
                        error=skip_reason,
                    )
                    # A clean tick is the normal case several times an hour;
                    # a row for each would bury the wakes in the history.
                    if skip_reason != "clean":
                        _log_run(self.state_dir, run_log)
                    return run_log
            elif job.session == "isolated":
                text, usage = _run_isolated(
                    job, self.config, self.resolve_provider,
                    self.workspace, self.state_dir,
                )
            elif job.session == "main":
                text, usage = _run_main(
                    job, self.config, self.resolve_provider,
                    self.workspace, self.state_dir,
                )
            elif job.session == "none":
                text, usage = _run_none(job, self.workspace, state_dir=self.state_dir)
            elif job.session == "agent":
                text, usage = _run_agent(
                    job, self.config, self.resolve_provider,
                    self.workspace, self.state_dir,
                    search_provider=self.search_provider,
                )
            else:
                text, usage = f"unknown session mode: {job.session}", TokenUsage()
        except Exception as e:
            duration = int((time.monotonic() - start) * 1000)
            backoff.record_failure()
            self._save_state()
            # A one-shot is NOT deleted here. cron-manager documents that
            # one-shots retry with backoff and are deleted only on success,
            # and a reminder destroyed by a single provider hiccup is the
            # worst failure this scheduler can have.
            run_log = RunLog(
                timestamp=now_str, job_id=job.id,
                status="error", duration_ms=duration,
                error=str(e),
            )
            _log_run(self.state_dir, run_log)
            return run_log

        duration = int((time.monotonic() - start) * 1000)

        if not text.strip():
            if job.context == "heartbeat":
                run_log = RunLog(
                    timestamp=now_str, job_id=job.id,
                    status="success", duration_ms=duration,
                    tokens=dataclasses.asdict(usage),
                )
                _log_run(self.state_dir, run_log)
                return run_log
            backoff.record_failure()
            self._save_state()
            run_log = RunLog(
                timestamp=now_str, job_id=job.id,
                status="error", duration_ms=duration,
                tokens=dataclasses.asdict(usage), error="empty response after retries",
            )
            _log_run(self.state_dir, run_log)
            return run_log

        backoff.record_success()
        self._save_state()
        send_error: str | None = None

        if job.rotate_session:
            _rotate_main_session(
                self.config, self.resolve_provider,
                self.workspace, self.state_dir, MAIN_SESSION_KEY,
                session_rotated_events=self.session_rotated_events,
            )

        wants_delivery = (
            job.deliver_mode == "announce"
            and not _is_no_reply(text)
            and self.deliver_output
        )
        target = self._resolve_deliver_channel(job)
        if wants_delivery and not target:
            logger.error(
                "job %s is deliver_mode=announce with no deliver.channel, "
                "so its output goes nowhere", job.id,
            )
        elif wants_delivery and target not in self.channels:
            logger.error(
                "job %s cannot deliver: channel %r is not configured or "
                "failed to start (available: %s)",
                job.id, target, sorted(self.channels) or "none",
            )
        should_deliver = (
            wants_delivery
            and target
            and target in self.channels
        )
        if should_deliver:
            from faffmonkey.runtime.ingest import flag_response
            from faffmonkey.runtime.redaction import redact
            text, scan_hit = flag_response(text, f"<cron:{job.id}>", "cron response")
            if scan_hit is not None:
                logger.warning("cron response flagged for job %s: %s", job.id, scan_hit)
            delivered = redact(text)
            # An unguarded send propagated out of run_job and out of tick,
            # so one flaky network call skipped every job behind it, left
            # the fired one-shot in place to re-run 30 seconds later, and
            # wrote no run log at all.
            try:
                self.channels[target].send(OutboundMessage(text=delivered))
            except Exception as e:
                logger.exception("job %s failed to deliver to %s", job.id, target)
                send_error = f"delivery to {target!r} failed: {e}"
            else:
                self._record_and_signal(job, delivered)
                self._remember_delivery(job.id, delivered)

        # An announce job that produced output but could not deliver it has
        # not succeeded; recording it green hides an undelivered briefing.
        undelivered = wants_delivery and not should_deliver
        # A missing or unconfigured channel is a configuration error, not a
        # flaky network, and no amount of retrying will fix it; retrying would
        # run the model again on every attempt and never drop the one-shot.
        permanent = undelivered
        if undelivered:
            send_error = f"could not deliver to channel {target!r}"

        if one_shot and (send_error is None or permanent):
            self._drop_one_shot(job)

        if send_error is not None and not permanent:
            backoff.record_failure()
            self._save_state()
        run_log = RunLog(
            timestamp=now_str, job_id=job.id,
            status="error" if send_error is not None else "success",
            duration_ms=duration,
            tokens=dataclasses.asdict(usage),
            error=send_error,
            output=text,
        )
        _log_run(self.state_dir, run_log)
        return run_log

    def tick(self, now: datetime | None = None) -> list[RunLog]:
        if now is None:
            now = datetime.now(self.config.timezone)

        self._check_jobs_changed()
        jobs = load_jobs(self.workspace)
        results: list[RunLog] = []

        for job in jobs:
            # A tick that started before shutdown must not keep spending
            # tokens on the jobs behind it: stop_and_wait only proves the
            # run lock is free, which is also true between two jobs.
            if self._stop_event.is_set():
                logger.info("tick interrupted by shutdown")
                break

            if not job.enabled:
                continue

            backoff = self._get_backoff(job.id)
            if backoff.is_backed_off():
                continue

            if job.at is not None:
                fire_time = next_one_shot_time(job.at, self.config.timezone)
                if fire_time is None or now < fire_time:
                    continue
                results.append(self.run_job(job))
                continue

            if job.schedule is None:
                continue

            try:
                fields = parse_cron(job.schedule)
            except ValueError as e:
                logger.error("invalid cron for job %s: %s", job.id, e)
                continue

            due = due_fire_time(
                job.id, fields, now, self.config.timezone,
                self._last_fire.get(job.id),
            )
            if due is None:
                continue

            self._last_fire[job.id] = due
            self._save_state()
            results.append(self.run_job(job))

        return results

    def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        self.load_state()
        prune_cron_logs(self.state_dir, {j.id for j in load_jobs(self.workspace)})
        logger.info("cron scheduler started")
        while self._running:
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick error")
            self._stop_event.wait(30)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        logger.info("cron scheduler stopped")

    def stop_and_wait(self, timeout: float = 30.0) -> bool:
        self.stop()
        acquired = self._run_lock.acquire(timeout=timeout)
        if acquired:
            self._run_lock.release()
        return acquired
