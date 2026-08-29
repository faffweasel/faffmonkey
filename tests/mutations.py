#!/usr/bin/env python3
"""Break the source on purpose and see whether the suite notices.

Coverage says a line ran. This says a line mattered. Each entry below is a
change that makes the code wrong in a way an operator would eventually
feel; a suite worth keeping fails on every one of them.

Run:  .venv/bin/python tests/mutations.py            # every mutation
      .venv/bin/python tests/mutations.py -k compact # matching ones
      .venv/bin/python tests/mutations.py --list

A mutation whose `old` text is no longer in the file is an ERROR, never a
pass. Otherwise a refactor would silently retire the check and the report
would still read green.

Add one entry per defect found from here on. That is the point: the file
grows by exactly the mistakes this project has actually made, and a
regression has to get past a test that was written because it happened.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTEST = ROOT / ".venv" / "bin" / "pytest"


@dataclass
class Mutation:
    name: str
    path: str
    old: str
    new: str
    why: str
    tests: str = field(default="tests/")


MUTATIONS: list[Mutation] = [
    # -- the two that shipped --
    Mutation(
        name="compaction-disconnected",
        path="src/faffmonkey/runtime/loop.py",
        old="    def _maybe_compact(self) -> None:\n",
        new="    def _maybe_compact(self) -> None:\n        return\n",
        why="Compaction never runs; context grows until the provider refuses.",
    ),
    Mutation(
        name="cron-delivery-not-recorded",
        path="src/faffmonkey/runtime/scheduler.py",
        old='    from faffmonkey.runtime.session import SessionStore\n\n    with _main_session_lock:\n        store = SessionStore(state_dir / "sessions.db")',
        new='    from faffmonkey.runtime.session import SessionStore\n\n    return\n    with _main_session_lock:\n        store = SessionStore(state_dir / "sessions.db")',
        why="C8: the agent has no record of the briefing it just sent you.",
    ),
    # -- seam contracts --
    Mutation(
        name="channel-missing-poll",
        path="contrib/channel_telegram.py",
        old="    def poll(self)",
        new="    def _poll_disabled(self)",
        why="TelegramChannel shipped without poll() for three months.",
    ),
    Mutation(
        name="search-drops-default",
        path="contrib/search_provider_brave.py",
        old="def search(self, query: str, max_results: int = 5)",
        new="def search(self, query: str, max_results: int)",
        why="Callers omit max_results; a required parameter breaks them.",
    ),
    # -- security boundaries --
    Mutation(
        name="workspace-containment-off",
        path="src/faffmonkey/runtime/tools.py",
        old='    workspace_resolved = workspace.resolve()\n    if not str(resolved).startswith(str(workspace_resolved) + "/") and resolved != workspace_resolved:\n        return None\n    return resolved',
        new="    return resolved",
        why="File tools could reach state/, where .env lives.",
    ),
    Mutation(
        name="redaction-off",
        path="src/faffmonkey/runtime/redaction.py",
        old="def redact(text: str) -> str:\n",
        new="def redact(text: str) -> str:\n    return text\n",
        why="API keys reach the channel in plain text.",
    ),
    Mutation(
        name="instruction-policy-missing",
        path="src/faffmonkey/runtime/bootstrap.py",
        old="            sections.append(INSTRUCTION_SOURCE_POLICY)",
        new="            pass",
        why="Nothing tells the agent which sources may instruct it.",
    ),
    Mutation(
        name="tool-summary-missing",
        path="src/faffmonkey/runtime/bootstrap.py",
        old="            sections.append(tool_text)",
        new="            pass",
        why="The model is never told what tools it has.",
    ),
    # -- cron correctness --
    Mutation(
        name="agent-mode-no-stale-ack",
        path="src/faffmonkey/runtime/scheduler.py",
        old='    if _is_stale_ack(text, ack_max_chars=config.heartbeat.ack_max_chars):\n        logger.info("stale ack detected for job %s (agent session), re-prompting", job.id)',
        new='    if False:\n        logger.info("stale ack detected for job %s (agent session), re-prompting", job.id)',
        why='A cron job answers "on it" and that is what gets delivered.',
    ),
    Mutation(
        name="agent-mode-empty-is-success",
        path="src/faffmonkey/runtime/scheduler.py",
        old="    text = loop.handle_message(cleaned)\n    # The loop answers a person, so it turns \"provider returned nothing\" into\n    # readable text. Delivered as a job result that reads as success.\n    if loop.last_response_empty:\n        return \"\", loop.usage_total",
        new='    text = loop.handle_message(cleaned)',
        why="A dead provider is logged as a successful run and never backs off.",
    ),
    Mutation(
        name="heartbeat-watchdog-not-refreshed",
        path="src/faffmonkey/runtime/scheduler.py",
        old="    _refresh_triggers(workspace, state_dir)\n    triggers = _load_triggers(workspace)",
        new="    triggers = _load_triggers(workspace)",
        why="The heartbeat reads yesterday's triggers.",
    ),
    Mutation(
        name="heartbeat-wakes-on-clean",
        path="src/faffmonkey/runtime/scheduler.py",
        old='    if triggers.get("status") != "attention" or not trigger_lines:\n        logger.info("heartbeat: clean")\n        return "", TokenUsage(), "clean"',
        new='    if False:\n        logger.info("heartbeat: clean")\n        return "", TokenUsage(), "clean"',
        why="A quiet tick costs an agent turn every five minutes.",
    ),
    Mutation(
        name="heartbeat-forgets-to-consume-triggers",
        path="src/faffmonkey/runtime/scheduler.py",
        old='    _consume_triggers(workspace, triggers.get("files", []))',
        new="    pass",
        why="Every trigger wakes the agent on every tick until someone deletes the file.",
    ),
    Mutation(
        name="heartbeat-hides-recent-deliveries",
        path="src/faffmonkey/runtime/scheduler.py",
        old='    if recent:\n        sections.append("Sent by the heartbeat recently:\\n" + _format_recent(recent, config))',
        new='    if False:\n        sections.append("Sent by the heartbeat recently:\\n" + _format_recent(recent, config))',
        why="The wake cannot know what it already said, so it says it again.",
    ),
    # -- setup wizards --
    Mutation(
        name="config-merge-skips-validation",
        path="src/faffmonkey/cli/setup_provider.py",
        old="        warnings = validate_config_schema(config)\n        for w in warnings:\n            print(f\"  Warning: {w}\")\n        if warnings:\n            raise SystemExit(1)",
        new="        validate_config_schema(config)",
        why="A wizard writes over a config the runtime refuses to load.",
    ),
    Mutation(
        name="search-wizard-wrong-class",
        path="src/faffmonkey/cli/setup_search.py",
        old='"class": "BraveSearchProvider",',
        new='"class": "BraveSearch",',
        why="The config names a class that does not exist; wiring crashes.",
    ),
    # -- probes into areas nothing here has verified by hand --
    Mutation(
        name="blocklist-never-blocks",
        path="src/faffmonkey/runtime/blocklist.py",
        old="def check_blocklist(command: str) -> bool:\n",
        new="def check_blocklist(command: str) -> bool:\n    return False\n",
        why="The hardline blocklist stops nothing.",
    ),
    Mutation(
        name="invisible-chars-not-stripped",
        path="src/faffmonkey/runtime/ingest.py",
        old="def strip_invisible(text: str) -> str:\n    return _INVISIBLE.sub('', text)",
        new="def strip_invisible(text: str) -> str:\n    return text",
        why="Zero-width and bidi characters survive into the prompt.",
    ),
    Mutation(
        name="injection-scan-never-fires",
        path="src/faffmonkey/runtime/ingest.py",
        old="def scan_patterns(text: str, path: str = \"<unknown>\") -> str | None:\n",
        new="def scan_patterns(text: str, path: str = \"<unknown>\") -> str | None:\n    return None\n",
        why="Injection patterns in untrusted content are never flagged.",
    ),
    Mutation(
        name="token-count-always-zero",
        path="src/faffmonkey/runtime/tokens.py",
        old="def count_tokens(text: str) -> int:\n",
        new="def count_tokens(text: str) -> int:\n    return 0\n",
        why="No budget ever trips: bootstrap overflow and compaction go blind.",
    ),
    Mutation(
        name="cron-catchup-window-zero",
        path="src/faffmonkey/runtime/scheduler.py",
        old="CATCHUP_MINUTES = 6",
        new="CATCHUP_MINUTES = 0",
        why="A job owed a fire across the stagger or a restart never runs.",
    ),
    Mutation(
        name="wal-mode-off",
        path="src/faffmonkey/runtime/session.py",
        old='self._conn.execute("PRAGMA journal_mode=WAL")',
        new='self._conn.execute("PRAGMA journal_mode=DELETE")',
        why="Scheduler and loop threads contend on writes.",
    ),
    Mutation(
        name="protected-tail-ignored",
        path="src/faffmonkey/runtime/compaction.py",
        old="    if len(messages) <= protect_last_n:\n        return list(messages)\n\n    tail_start = len(messages) - protect_last_n",
        new="    if len(messages) <= protect_last_n:\n        return list(messages)\n\n    tail_start = len(messages)",
        why="Compaction summarises the turn the user is mid-way through.",
    ),
    Mutation(
        name="checkpoint-failure-does-not-abort",
        path="src/faffmonkey/runtime/compaction.py",
        old='    checkpoint_path = _checkpoint(session_store, state_dir)\n    if checkpoint_path is None:\n        logger.error("compaction aborted: checkpoint failed")\n        return {"aborted": True, "reason": "checkpoint_failed"}',
        new="    checkpoint_path = _checkpoint(session_store, state_dir)",
        why="History is destroyed with no backup when the checkpoint fails.",
    ),
    Mutation(
        name="stub-timestamp-not-adjacent",
        path="src/faffmonkey/runtime/compaction.py",
        old="                        timestamp=_just_after(m.timestamp),",
        new="                        timestamp=None,",
        why="A repair stub lands at the end of history, not beside its call.",
    ),
    Mutation(
        name="fallback-chain-not-walked",
        path="src/faffmonkey/runtime/retry.py",
        old="    for i, fallback in enumerate(fallbacks):",
        new="    for i, fallback in enumerate([]):",
        why="fallback_models is configured and never used.",
    ),
    Mutation(
        name="trust-hash-always-matches",
        path="src/faffmonkey/runtime/trust.py",
        old="    return _sha256(full) == store[filepath].hash",
        new="    return True",
        why="An edited workspace file still loads as trusted.",
    ),
    Mutation(
        name="approval-not-bound-to-command",
        path="src/faffmonkey/runtime/tools.py",
        old='def _approval_key(command: str, cwd: str) -> str:\n    raw = f"{command}\\x00{cwd}"',
        new='def _approval_key(command: str, cwd: str) -> str:\n    raw = f"{cwd}"',
        why="Approval granted for one command authorises any other.",
    ),
    Mutation(
        name="backoff-not-reset-on-success",
        path="src/faffmonkey/runtime/scheduler.py",
        old="    def record_success(self) -> None:\n        self.failure_count = 0",
        new="    def record_success(self) -> None:\n        self.failure_count = self.failure_count",
        why="A job that recovers stays backed off forever.",
    ),
    Mutation(
        name="one-shot-dropped-without-firing",
        path="src/faffmonkey/runtime/scheduler.py",
        old="        _delete_job(self.workspace, job.id)\n        jobs_path = self.workspace / \"config\" / \"jobs.json\"",
        new="        jobs_path = self.workspace / \"config\" / \"jobs.json\"",
        why="A fired one-shot reminder repeats forever.",
    ),
    Mutation(
        # Mutate the check, not the constant: test_tool_call_cap_enforced
        # sizes its own batches from MAX_TOOL_CALLS_PER_TURN, so raising the
        # constant makes the test build millions of calls and hang instead
        # of failing.
        name="tool-call-budget-not-enforced",
        path="src/faffmonkey/runtime/loop.py",
        old="                if tool_call_count > MAX_TOOL_CALLS_PER_TURN:",
        new="                if False:",
        why="A looping model runs tools until the timeout instead of the cap.",
    ),
    Mutation(
        name="llm-roundtrip-budget-not-enforced",
        path="src/faffmonkey/runtime/loop.py",
        old="MAX_LLM_CALLS_PER_TURN = 20",
        new="MAX_LLM_CALLS_PER_TURN = 10_000_000",
        why="One turn can bill indefinitely.",
    ),
    Mutation(
        name="telegram-writes-env-before-validating",
        path="src/faffmonkey/cli/setup_telegram.py",
        old='    print("\\nValidating bot token...")\n    if not _validate_token(token):\n        print("\\nToken validation failed. Check the token and try again.")\n        raise SystemExit(1)\n\n    env_path = state_dir / ".env"\n    _append_env_var(env_path, "TELEGRAM_BOT_TOKEN", token)',
        new='    env_path = state_dir / ".env"\n    _append_env_var(env_path, "TELEGRAM_BOT_TOKEN", token)\n    print("\\nValidating bot token...")\n    if not _validate_token(token):\n        print("\\nToken validation failed. Check the token and try again.")\n        raise SystemExit(1)',
        why="A rejected token is written to .env anyway.",
    ),
]


# Mutations that survived and were then shown NOT to be defects. Kept so
# nobody re-derives them, and so a later change to the reasoning is visible.
RETIRED: dict[str, str] = {
    "history-not-time-ordered": (
        "session.py ORDER BY timestamp -> ORDER BY rowid. Survives, and "
        "correctly. The invariant that matters is that a repair stub reads "
        "back adjacent to the call it answers, and insertion order satisfies "
        "it: _strip_orphaned_tool_messages puts the stub in list position and "
        "compaction re-appends the list in order. Only three call sites pass "
        "an explicit timestamp, all in compaction, all ascending. Removing "
        "BOTH this and _just_after() leaves the suite green, which shows the "
        "asymmetry: ORDER BY timestamp is what makes the stub's timestamp "
        "matter at all, not a guard on top of it. Open design question, not a "
        "test gap: timestamps collide at microsecond resolution and ORDER BY "
        "timestamp is unstable across ties, where rowid is not."
    ),
}


def _apply(mutation: Mutation) -> str:
    path = ROOT / mutation.path
    original = path.read_text()
    if mutation.old not in original:
        raise LookupError(
            f"{mutation.name}: anchor text not found in {mutation.path}. "
            f"The code moved; update or retire this mutation."
        )
    if original.count(mutation.old) > 1:
        raise LookupError(
            f"{mutation.name}: anchor text appears "
            f"{original.count(mutation.old)} times in {mutation.path}; "
            f"make it unique."
        )
    path.write_text(original.replace(mutation.old, mutation.new, 1))
    return original


SUITE_TIMEOUT = 300


def _run_suite(tests: str) -> tuple[str, str]:
    """Returns (outcome, first_failing_test).

    A mutation can remove the only thing that terminates a loop, so the run
    is bounded. A timeout is its own outcome: the suite neither passed nor
    failed, it stopped being a test.
    """
    try:
        proc = subprocess.run(
            [str(PYTEST), tests, "-x", "-q", "--tb=no", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, text=True, timeout=SUITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "HUNG", f"no result in {SUITE_TIMEOUT}s"
    match = re.search(r"^FAILED (\S+)", proc.stdout, re.M)
    if match is None:
        match = re.search(r"^(\S+::\S+)", proc.stdout, re.M)
    caught_by = match.group(1) if match else ""
    return ("caught" if proc.returncode != 0 else "SURVIVED"), caught_by


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", dest="filter", default="", help="only mutations matching this substring")
    parser.add_argument("--list", action="store_true", help="list mutations and exit")
    args = parser.parse_args()

    selected = [m for m in MUTATIONS if args.filter in m.name]
    if args.list:
        for m in selected:
            print(f"  {m.name:38s} {m.path}")
        if RETIRED:
            print("\nRetired (survived, then shown not to be defects):")
            for name, reason in RETIRED.items():
                print(f"  {name}\n    {reason}\n")
        return 0
    if not selected:
        print(f"no mutations match {args.filter!r}")
        return 2

    # Two runs against one working tree revert each other's mutations
    # mid-suite. The failure is silent and biased toward "caught".
    lock = ROOT / ".mutations.lock"
    try:
        lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(f"another mutation run holds {lock}. Wait, or delete it if stale.")
        return 2
    os.write(lock_fd, str(os.getpid()).encode())
    os.close(lock_fd)

    try:
        return _sweep(selected)
    finally:
        lock.unlink(missing_ok=True)


def _sweep(selected: list[Mutation]) -> int:
    print(f"Applying {len(selected)} mutations, full suite each time.\n")
    survived: list[Mutation] = []
    errored: list[tuple[Mutation, str]] = []
    rows: list[tuple[str, str, str]] = []

    for i, mutation in enumerate(selected, 1):
        path = ROOT / mutation.path
        print(f"[{i}/{len(selected)}] {mutation.name} ... ", end="", flush=True)
        try:
            original = _apply(mutation)
        except LookupError as e:
            print("ERROR")
            errored.append((mutation, str(e)))
            rows.append((mutation.name, "ERROR", "anchor not found"))
            continue
        try:
            outcome, caught_by = _run_suite(mutation.tests)
        finally:
            path.write_text(original)

        if outcome == "caught":
            print(f"caught by {caught_by}")
        else:
            print(outcome)
            survived.append(mutation)
        rows.append((mutation.name, outcome, caught_by or "-"))

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "src/", "contrib/"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip()

    print("\n| mutation | result | first catcher |")
    print("|---|---|---|")
    for name, result, caught_by in rows:
        print(f"| {name} | {result} | {caught_by} |")

    print(
        f"\n{len(rows) - len(survived) - len(errored)} caught, "
        f"{len(survived)} survived, {len(errored)} errored."
    )
    for mutation in survived:
        print(f"\nSURVIVED: {mutation.name}\n  {mutation.why}\n  {mutation.path}")
    for mutation, reason in errored:
        print(f"\nERROR: {reason}")

    if dirty:
        print(f"\nWORKING TREE NOT RESTORED:\n{dirty}")
        return 3
    return 1 if (survived or errored) else 0


if __name__ == "__main__":
    sys.exit(main())
