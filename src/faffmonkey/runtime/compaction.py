from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from faffmonkey.config import CompactionConfig, Config, ConfigError, DailyNoteConfig, ModelConfig
from faffmonkey.runtime.session import SessionStore
from faffmonkey.runtime.tokens import count_tokens
from faffmonkey.seams.provider import Provider
from faffmonkey.types import CompletionRequest, Message, ToolCall

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 2000
MAX_CHECKPOINTS = 5
MAX_FLUSH_CONTENT_BYTES = 256 * 1024
MAX_PRESERVED_BLOB_BYTES = 64 * 1024
_PRESERVED_MARKER = "[pre-compaction history — memory flush failed]"
_TRUNCATED_DATA_MARKER = "has_truncated_data"

_FLUSH_SYSTEM_PROMPT = (
    "Review the conversation history. Write anything important to "
    "the appropriate memory file (MEMORY.md, daily log, person file, "
    "project file) before this history is summarised. Focus on facts, "
    "decisions, preferences, and commitments that would be lost."
)

_FLUSH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_write",
        "description": "Write a file within workspace/.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

_SUMMARY_PROMPT = (
    "Summarise this conversation for context continuity.\n"
    'Use these sections (write "(none)" if empty):\n\n'
    "## Goal\n"
    "## Constraints and Preferences\n"
    "## Progress\n"
    "### Done\n"
    "### In Progress\n"
    "### Blocked\n"
    "## Key Decisions\n"
    "## Next Steps\n"
    "## Critical Context\n\n"
    "Keep each section concise. Preserve exact file paths, function "
    "names, error messages, and technical specifics."
)

_RESUMARY_TEMPLATE = (
    "<previous-summary>\n"
    "{existing_summary}\n"
    "</previous-summary>\n\n"
    "Update the summary:\n"
    "- PRESERVE existing information still relevant\n"
    "- ADD new progress, decisions, context\n"
    "- UPDATE Progress and Next Steps\n"
    "- REMOVE items no longer relevant\n"
    "- Use the same section structure"
)

_AGGRESSIVE_PROMPT = (
    "Summarise this conversation very briefly.\n"
    'Sections (write "(none)" if empty): Goal, Progress, Key Decisions, Next Steps.\n'
    "One sentence per section maximum. Preserve file paths and function names."
)

_TRUNCATION_MARKER = "[Earlier conversation truncated for context management]"


def _serialize_messages(messages: list[Message]) -> str:
    lines: list[str] = []
    for msg in messages:
        content = msg.content
        if content and msg.role == "tool" and len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + "..."
        if content:
            lines.append(f"[{msg.role}]: {content}")
        if msg.tool_calls:
            for tc in msg.tool_calls:
                lines.append(f"[{msg.role}]: (tool_call: {tc.name})")
    return "\n".join(lines)


def _find_existing_summary(messages: list[Message]) -> str | None:
    for msg in messages:
        if msg.role == "system" and msg.content and "## Goal" in msg.content:
            return msg.content
    return None


def _may_overwrite(path_str: str) -> bool:
    """The memory files the flush owns. Everything else stays create-only.

    MEMORY.md and the daily log must be updatable on every flush, or the
    "save my facts before you summarise" contract only holds once.
    """
    normalised = path_str.lstrip("./")
    if normalised in ("MEMORY.md", "LEARNINGS.md"):
        return True
    prefix = "memory/daily/"
    if not (normalised.startswith(prefix) and normalised.endswith(".md")):
        return False
    return "/" not in normalised[len(prefix):]


_FLUSH_BLOCKED_PREFIXES = ("skills/", "config/", "extensions/")


def _execute_flush_writes(tool_calls: list[ToolCall], workspace: Path) -> tuple[int, int]:
    """Execute file_write tool calls. Returns (attempted, succeeded) counts."""
    if not tool_calls:
        return (0, 0)
    attempted = 0
    succeeded = 0
    ws_resolved = workspace.resolve()
    for tc in tool_calls:
        if tc.name != "file_write":
            continue
        attempted += 1
        args = tc.arguments
        path_str = args.get("path", "")
        content = args.get("content", "")
        if not path_str or not isinstance(path_str, str) or not isinstance(content, str):
            continue
        if len(content) > MAX_FLUSH_CONTENT_BYTES:
            logger.warning("flush: content too large (%d bytes) for %s", len(content), path_str)
            continue
        if ".." in path_str:
            logger.warning("flush: rejected traversal in path %s", path_str)
            continue
        normalised = path_str.lstrip("./").casefold()
        if any(normalised.startswith(p) for p in _FLUSH_BLOCKED_PREFIXES):
            logger.warning("flush: rejected write to protected prefix: %s", path_str)
            continue
        target_raw = workspace / path_str
        if target_raw.is_symlink():
            logger.warning("flush: rejected symlink: %s", path_str)
            continue
        target = target_raw.resolve()
        if not target.is_relative_to(ws_resolved):
            logger.warning("flush: path escaped workspace: %s", path_str)
            continue
        append = False
        if target.exists():
            if not _may_overwrite(path_str):
                logger.warning("flush: refused to overwrite existing file: %s", path_str)
                continue
            # Append, never replace. The flush is driven by the model, and
            # the model can be steered by injected content, so replacing
            # MEMORY.md wholesale would let untrusted text destroy the
            # operator's memory. Appending lets the flush add what it
            # learned and leaves everything already there intact.
            append = True
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if append:
                with target.open("a") as f:
                    f.write("\n" + content.rstrip("\n") + "\n")
                logger.info("flush: appended to %s", path_str)
            else:
                target.write_text(content)
                logger.info("flush: wrote %s", path_str)
            succeeded += 1
        except OSError as e:
            logger.warning("flush: write failed for %s: %s", path_str, e)
    return (attempted, succeeded)


def memory_flush(
    session_store: SessionStore,
    session_id: str,
    workspace: Path,
    provider_fn: Callable[[ModelConfig], Provider],
    config: Config,
) -> bool:
    history = session_store.get_history(session_id)
    if not history:
        return True

    messages = [Message(role="system", content=_FLUSH_SYSTEM_PROMPT), *history]

    for task in ("conversation", "compaction"):
        try:
            model_config = config.resolve_model(task)
        except Exception:
            continue
        try:
            provider = provider_fn(model_config)
            request = CompletionRequest(
                messages=messages,
                model=model_config.model,
                tools=[_FLUSH_TOOL_SCHEMA],
            )
            response = provider.complete(request)
            if not response.tool_calls:
                logger.warning("memory_flush: provider returned no tool calls, treating as failure")
                return False
            attempted, succeeded = _execute_flush_writes(response.tool_calls, workspace)
            if attempted == 0:
                logger.warning("memory_flush: response had tool calls but no file_write entries")
                return False
            if succeeded == 0:
                logger.warning("memory_flush: all %d file writes failed", attempted)
                return False
            if succeeded < attempted:
                logger.warning("memory_flush: %d of %d writes failed", attempted - succeeded, attempted)
                return False
            return True
        except Exception as e:
            logger.warning("memory_flush failed with %s model: %s", task, e)

    logger.error("memory_flush: all models failed, proceeding without flush")
    return False


MAX_DAILY_NOTE_CHARS = 2000

_DAILY_NOTE_PROMPT = (
    "These are the latest messages of a conversation between the user and "
    "their assistant. If anything in them belongs in today's daily log (a "
    "decision, a fact about the user, a task done or promised, a "
    "preference, something that happened), call daily_note with one or "
    "two short lines saying what. Write in the third person about the "
    "user and the assistant. If nothing is worth keeping, do not call the "
    "tool."
)

_DAILY_NOTE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "daily_note",
        "description": "Append a short note to today's daily log.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "One or two short lines worth keeping.",
                },
            },
            "required": ["content"],
        },
    },
}


def _pending_since_note(
    session_store: SessionStore, session_id: str,
) -> list[Message]:
    """Conversation since the last daily note: user and assistant text
    only. Tool traffic is noise for a note and may start mid-exchange."""
    cursor = session_store.daily_note_at(session_id)
    return [
        m for m in session_store.get_history(session_id)
        if m.role in ("user", "assistant")
        and m.content
        and m.timestamp is not None
        and (cursor is None or m.timestamp > cursor)
    ]


def daily_note_due(
    session_store: SessionStore,
    session_id: str,
    config: DailyNoteConfig,
    now: datetime | None = None,
) -> bool:
    """Whether the loop should ask for a daily note this turn.

    Fires on whichever comes first, a run of user turns or an hour of
    them, and never when nobody has said anything since the last note:
    an idle session costs no calls.
    """
    pending = _pending_since_note(session_store, session_id)
    user_turns = [m for m in pending if m.role == "user"]
    if not user_turns:
        return False
    if len(user_turns) >= config.every_turns:
        return True
    since = session_store.daily_note_at(session_id) or user_turns[0].timestamp
    try:
        since_dt = datetime.fromisoformat(since)
    except ValueError:
        return True
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - since_dt >= timedelta(minutes=config.every_minutes)


def _append_daily_note(workspace: Path, content: str, now: datetime) -> Path:
    """The runtime picks the file (today, in the user's timezone) and only
    ever appends. The model wrote to yesterday's log when it was left to
    choose, because that was the file with content in it."""
    path = workspace / "memory" / "daily" / f"{now.date().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = content.strip().splitlines()
    body = f"- {now.strftime('%H:%M')} " + "\n  ".join(line.strip() for line in lines if line.strip())
    with path.open("a", encoding="utf-8") as f:
        if path.stat().st_size == 0:
            f.write(f"# {now.date().isoformat()}\n")
        f.write(f"\n{body}\n")
    return path


def daily_note(
    session_store: SessionStore,
    session_id: str,
    workspace: Path,
    provider_fn: Callable[[ModelConfig], Provider],
    config: Config,
) -> bool:
    """Ask the cheap model whether the conversation since the last note
    holds anything for today's daily log, and append it if so.

    Storage only: history is untouched, no compaction, no new session.
    The cursor advances on any answer, including "nothing to keep", so
    a quiet stretch is not re-read every turn. A provider failure leaves
    the cursor where it was and the next due turn tries again.
    """
    pending = _pending_since_note(session_store, session_id)
    if not pending:
        return True
    latest = max(m.timestamp for m in pending if m.timestamp is not None)
    messages = [
        Message(role="system", content=_DAILY_NOTE_PROMPT),
        *[Message(role=m.role, content=m.content) for m in pending],
    ]
    # The cheap slot first; the conversation model if the config has no
    # compaction routing, the same fall-through memory_flush uses.
    response = None
    for task in ("compaction", "conversation"):
        try:
            model_config = config.resolve_model(task)
        except ConfigError:
            continue
        try:
            provider = provider_fn(model_config)
            response = provider.complete(CompletionRequest(
                messages=messages,
                model=model_config.model,
                tools=[_DAILY_NOTE_TOOL_SCHEMA],
            ))
            break
        except Exception as e:
            logger.warning("daily_note: %s model failed: %s", task, e)
    if response is None:
        return False

    now = datetime.now(config.timezone)
    for tc in response.tool_calls or []:
        if tc.name != "daily_note":
            logger.warning("daily_note: ignoring tool call %r", tc.name)
            continue
        content = tc.arguments.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > MAX_DAILY_NOTE_CHARS:
            content = content[:MAX_DAILY_NOTE_CHARS]
        path = _append_daily_note(workspace, content, now)
        logger.info("daily_note: appended to %s", path.name)
    session_store.set_daily_note_at(session_id, latest)
    return True


def should_compact(
    session_store: SessionStore,
    session_id: str,
    config: CompactionConfig,
    context_window: int,
) -> bool:
    count = session_store.message_count(session_id)
    if count >= config.hard_message_limit:
        return True
    history = session_store.get_history(session_id)
    tokens = count_tokens(_serialize_messages(history))
    return tokens >= int(context_window * config.threshold)


def _prune_stale_tmp(backups_dir: Path) -> None:
    if not backups_dir.is_dir():
        return
    for entry in backups_dir.iterdir():
        if entry.is_dir() and entry.name.endswith(".tmp"):
            age = time.time() - entry.stat().st_mtime
            if age > 300:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info("pruned stale checkpoint tmp: %s", entry.name)


def _checkpoint(session_store: SessionStore, state_dir: Path) -> Path | None:
    backups_dir = state_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    _prune_stale_tmp(backups_dir)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cp_dir = backups_dir / f"checkpoint_{ts}-{uuid.uuid4().hex[:6]}"
    tmp_dir = cp_dir.with_suffix(".tmp")

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()

    try:
        session_store.backup(tmp_dir / "sessions.db")
    except Exception as e:
        logger.error("checkpoint: database backup failed: %s", e)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    config_src = state_dir / "config.json"
    if config_src.exists():
        try:
            shutil.copy2(str(config_src), str(tmp_dir / "config.json"))
        except OSError as e:
            logger.warning("checkpoint: config snapshot failed: %s", e)

    db_in_tmp = tmp_dir / "sessions.db"
    if not db_in_tmp.exists() or db_in_tmp.stat().st_size == 0:
        logger.error("checkpoint: verification failed, db missing or empty in tmp")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    os.rename(str(tmp_dir), str(cp_dir))

    existing = sorted(backups_dir.glob("checkpoint_*"), key=lambda p: p.name)
    existing = [p for p in existing if p.suffix != ".tmp"]
    to_prune = len(existing) - MAX_CHECKPOINTS
    pruned = 0
    for cp in list(existing):
        if pruned >= to_prune:
            break
        if cp.is_dir() and (cp / _TRUNCATED_DATA_MARKER).exists():
            logger.info(
                "checkpoint: skipping prune of %s (contains truncated unflushed data)",
                cp.name,
            )
            continue
        if cp.is_dir():
            shutil.rmtree(cp)
        else:
            cp.unlink()
        pruned += 1

    logger.info("checkpoint: %s", cp_dir.name)
    return cp_dir


def _determine_protected_tail(
    messages: list[Message],
    protect_last_n: int,
) -> list[Message]:
    if len(messages) <= protect_last_n:
        return list(messages)

    tail_start = len(messages) - protect_last_n

    while 0 < tail_start < len(messages):
        msg = messages[tail_start]
        if msg.role == "tool" and msg.tool_call_id:
            tail_start -= 1
        else:
            break

    return list(messages[tail_start:])


def _just_after(timestamp: str | None) -> str | None:
    """One microsecond after a stored timestamp.

    get_history orders by timestamp, so a stub result built with no
    timestamp got the current wall clock on re-insert and reappeared at
    the end of the conversation, detached from the call it answers. That
    is precisely the invalid sequence the repair exists to prevent, and
    strict providers reject it on every subsequent turn.
    """
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    return (dt + timedelta(microseconds=1)).isoformat()


def _strip_orphaned_tool_messages(tail: list[Message]) -> list[Message]:
    tail_assistant_tc_ids: set[str] = set()
    for m in tail:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                tail_assistant_tc_ids.add(tc.id)

    orphaned = {
        m.tool_call_id
        for m in tail
        if m.role == "tool" and m.tool_call_id
        and m.tool_call_id not in tail_assistant_tc_ids
    }

    if orphaned:
        logger.warning(
            "compaction: stripping %d orphaned tool result messages from tail",
            len(orphaned),
        )
        tail = [
            m for m in tail
            if not (m.role == "tool" and m.tool_call_id in orphaned)
        ]

    tail_result_ids: set[str] = set()
    for m in tail:
        if m.role == "tool" and m.tool_call_id:
            tail_result_ids.add(m.tool_call_id)

    orphaned_calls: dict[int, list[ToolCall]] = {}
    for i, m in enumerate(tail):
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                if tc.id not in tail_result_ids:
                    orphaned_calls.setdefault(i, []).append(tc)

    if orphaned_calls:
        total = sum(len(v) for v in orphaned_calls.values())
        logger.warning(
            "compaction: stubbing %d orphaned tool calls with error results",
            total,
        )
        result: list[Message] = []
        for i, m in enumerate(tail):
            result.append(m)
            if i in orphaned_calls:
                for tc in orphaned_calls[i]:
                    result.append(Message(
                        role="tool",
                        content="[error: tool result lost during compaction]",
                        tool_call_id=tc.id,
                        timestamp=_just_after(m.timestamp),
                    ))
        return result

    return tail


def _summarise(
    messages: list[Message],
    provider_fn: Callable[[ModelConfig], Provider],
    config: Config,
) -> str | None:
    serialised = _serialize_messages(messages)
    existing = _find_existing_summary(messages)

    prompt = (
        _RESUMARY_TEMPLATE.format(existing_summary=existing)
        if existing
        else _SUMMARY_PROMPT
    )

    tier1_mc: ModelConfig | None = None
    try:
        tier1_mc = config.resolve_model("compaction")
        provider = provider_fn(tier1_mc)
        resp = provider.complete(CompletionRequest(
            messages=[
                Message(role="system", content=prompt),
                Message(role="user", content=serialised),
            ],
            model=tier1_mc.model,
            temperature=0.2,
            max_tokens=1200,
        ))
        if resp.text:
            logger.info("compaction: summarised with %s", tier1_mc.model)
            return resp.text
    except Exception as e:
        logger.warning("compaction tier 1 (normal) failed: %s", e)

    try:
        # "cheap" is a model slot, not a routing task. resolve_model looks
        # its argument up in config.routing, so this raised ConfigError on
        # every real config and the documented three-tier ladder was two.
        cheap_mc = config.models.get("cheap")
        if cheap_mc is None:
            raise ConfigError("no 'cheap' model slot configured")
        if tier1_mc and cheap_mc.provider == tier1_mc.provider and cheap_mc.model == tier1_mc.model:
            logger.info("compaction: cheap model same as tier-1, skipping")
        else:
            provider = provider_fn(cheap_mc)
            resp = provider.complete(CompletionRequest(
                messages=[
                    Message(role="system", content=_AGGRESSIVE_PROMPT),
                    Message(role="user", content=serialised),
                ],
                model=cheap_mc.model,
                temperature=0.1,
                max_tokens=600,
            ))
            if resp.text:
                logger.info("compaction: summarised with cheap fallback")
                return resp.text
    except Exception as e:
        logger.warning("compaction tier 2 (cheap fallback) failed: %s", e)

    return None


def _deterministic_truncate(messages: list[Message], context_window: int = 128000) -> str:
    serialised = _serialize_messages(messages)
    char_budget = max(int(context_window * 0.3), 4096)
    truncated = serialised[:char_budget]
    return (
        "[WARNING: conversation heavily truncated due to repeated compaction failures]\n"
        f"{truncated}\n\n{_TRUNCATION_MARKER}"
    )


def compact(
    session_store: SessionStore,
    session_id: str,
    config: Config,
    workspace: Path,
    provider_fn: Callable[[ModelConfig], Provider],
    context_window: int = 128000,
    state_dir: Path | None = None,
) -> dict:
    if state_dir is None:
        state_dir = session_store.db_path.parent

    history = session_store.get_history(session_id)
    before_count = len(history)
    before_tokens = count_tokens(_serialize_messages(history))

    protected = _determine_protected_tail(history, config.compaction.protect_last_n)
    head = history[: len(history) - len(protected)]

    if not head:
        logger.warning(
            "compaction: protected tail (%d messages) exceeds threshold, "
            "nothing to summarise, skipping",
            len(protected),
        )
        return {
            "before_messages": before_count,
            "after_messages": before_count,
            "before_tokens": before_tokens,
            "after_tokens": before_tokens,
            "skipped": True,
        }

    checkpoint_path = _checkpoint(session_store, state_dir)
    if checkpoint_path is None:
        logger.error("compaction aborted: checkpoint failed")
        return {"aborted": True, "reason": "checkpoint_failed"}

    flush_ok = memory_flush(session_store, session_id, workspace, provider_fn, config)

    # Re-read after flush (flush may have triggered appends).
    history = session_store.get_history(session_id)
    pre_summary_count = len(history)
    protected = _determine_protected_tail(history, config.compaction.protect_last_n)
    head = history[: len(history) - len(protected)]
    protected = _strip_orphaned_tool_messages(protected)

    if not head:
        return {
            "before_messages": before_count,
            "after_messages": len(history),
            "before_tokens": before_tokens,
            "after_tokens": count_tokens(_serialize_messages(history)),
            "skipped": True,
        }

    # Summarise OUTSIDE any transaction -- this makes a network call.
    summary_text = _summarise(head, provider_fn, config)
    if summary_text is None:
        summary_text = _deterministic_truncate(head, context_window)
        logger.info("compaction: fell back to deterministic truncation")

    # Acquire write lock ONLY for the atomic swap.
    flush_preserved = ""
    session_store.begin()
    try:
        current_count = session_store.message_count(session_id)
        if current_count != pre_summary_count:
            session_store.rollback()
            logger.warning(
                "compaction aborted: message count changed during summarisation "
                "(%d -> %d), will retry next cycle",
                pre_summary_count,
                current_count,
            )
            return {"aborted": True, "reason": "concurrent_modification"}

        session_store.delete_all_messages(session_id)
        summary_ts = head[0].timestamp if head and head[0].timestamp else None
        session_store.append_message(
            session_id, "system", summary_text, timestamp=summary_ts,
        )
        if not flush_ok:
            head_without_old = [
                m for m in head
                if not (m.content and _PRESERVED_MARKER in m.content)
            ]
            flush_preserved = _serialize_messages(head_without_old)
            if len(flush_preserved) > MAX_PRESERVED_BLOB_BYTES:
                logger.warning(
                    "pre-compaction history truncated from %d to %d bytes; "
                    "full data preserved in checkpoint %s",
                    len(flush_preserved), MAX_PRESERVED_BLOB_BYTES,
                    checkpoint_path.name,
                )
                flush_preserved = flush_preserved[:MAX_PRESERVED_BLOB_BYTES] + "\n[truncated]"
                try:
                    (checkpoint_path / _TRUNCATED_DATA_MARKER).write_text(
                        checkpoint_path.name,
                    )
                except OSError as e:
                    logger.warning(
                        "checkpoint: failed to write truncation marker: %s", e,
                    )
            preserved_ts = head[-1].timestamp if head and head[-1].timestamp else None
            session_store.append_message(
                session_id,
                "system",
                f"{_PRESERVED_MARKER}\n{flush_preserved}",
                timestamp=preserved_ts,
            )
        for msg in protected:
            session_store.append_message(
                session_id,
                msg.role,
                msg.content or None,
                msg.tool_calls,
                msg.tool_call_id,
                timestamp=msg.timestamp,
                # images= keeps the protected tail's image references through the
                # rewrite.
                images=msg.images or None,
            )
        session_store.commit()
    except Exception:
        session_store.rollback()
        raise

    after_count = (2 if not flush_ok else 1) + len(protected)
    after_text = summary_text
    if flush_preserved:
        after_text += flush_preserved
    after_text += _serialize_messages(protected)
    after_tokens = count_tokens(after_text)

    logger.info(
        "compaction: %d -> %d messages, %d -> %d tokens",
        before_count, after_count, before_tokens, after_tokens,
    )

    return {
        "before_messages": before_count,
        "after_messages": after_count,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
    }
