from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from faffmonkey.config import (
    Config,
    ConfigError,
    ModelConfig,
    read_json_object,
    write_json_object,
)
from faffmonkey.runtime.compaction import FLUSH_FAILED, FLUSH_NOTHING, FLUSH_SAVED
from faffmonkey.runtime.goal import (
    GoalState,
    check_goal_done,
    handle_goal_command,
    make_continuation_prompt,
)
from faffmonkey.runtime.ingest import flag_response, scan_patterns, strip_invisible
from faffmonkey.runtime.redaction import redact
from faffmonkey.runtime.retry import retry_with_fallback, run_with_timeout
from faffmonkey.runtime.scheduler import parse_timestamp, recent_cron_runs
from faffmonkey.runtime.session import SessionStore
from faffmonkey.runtime.skills import invoke as skill_invoke, load_full as skill_load_full, scan_skills
from faffmonkey.runtime.tools import ToolRegistry
from faffmonkey.seams.channel import Channel
from faffmonkey.seams.provider import Provider
from faffmonkey.seams.synthesiser import NoopSynthesiser, Synthesiser
from faffmonkey.seams.transcriber import NoopTranscriber, Transcriber
from faffmonkey.types import (
    CompletionRequest,
    CompletionResponse,
    ContextLengthError,
    InboundMessage,
    Message,
    OutboundMessage,
    RetryableError,
    TokenUsage,
    ToolCall,
    ToolResult,
)

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_TURN = 50
MAX_LLM_CALLS_PER_TURN = 20
INACTIVITY_TIMEOUT = 600
MAX_TURN_DURATION = 3600
# How many images a single request may carry. Older ones degrade to a
# text reference, so a long conversation full of photos cannot quietly
# grow into a megabyte of base64 per turn.
MAX_REQUEST_IMAGES = 4
EMPTY_RESPONSE_RETRIES = 3
# /model validates the name: a slot pointing at a model that does not
# resolve stops every cron job listed after it.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
EMPTY_RESPONSE_NUDGE = "(Please continue -- that last turn had no visible response.)"

SLASH_COMMANDS = {
    "/help": "list available slash commands",
    "/status": "show runtime status",
    "/new": "save memory, then start new session",
    "/clear": "start new session (no memory flush)",
    "/model": "show model config, or /model <slot> [provider] <model> to switch",
    "/compact": "force compaction now",
    "/skill": "invoke a skill: /skill <name>",
    "/cron": "list cron jobs, or /cron history <job-id>",
    "/goal": "start, check, or stop an autonomous goal",
}


def _handle_help() -> str:
    lines = ["Available commands:"]
    for cmd, desc in SLASH_COMMANDS.items():
        lines.append(f"  {cmd:12s} {desc}")
    return "\n".join(lines)


def _cron_health_line(state_dir: Path, tz: ZoneInfo) -> str:
    runs = recent_cron_runs(state_dir, limit=None)
    if not runs:
        return "Cron: no runs recorded"

    cutoff = datetime.now(tz) - timedelta(hours=24)
    recent = []
    for run in runs:
        ts = parse_timestamp(run.timestamp, tz)
        if ts is None:
            continue
        if ts >= cutoff:
            recent.append((ts.astimezone(tz), run))
    recent.sort(key=lambda pair: pair[0], reverse=True)

    if not recent:
        return "Cron: no runs in last 24h"

    count = f"{len(recent)} run{'' if len(recent) == 1 else 's'}"
    failed = [(ts, run) for ts, run in recent if run.status == "error"]
    if not failed:
        return f"Cron: {count} in last 24h, all ok"

    shown = ", ".join(
        f"{run.job_id} {ts.strftime('%H:%M')}" for ts, run in failed[:3]
    )
    more = f", +{len(failed) - 3} more" if len(failed) > 3 else ""
    return f"Cron: {count} in last 24h, {len(failed)} failed ({shown}{more})"


def _format_status(
    config: Config,
    session_id: str | None,
    message_count: int | None,
    usage: TokenUsage,
    state_dir: Path | None,
) -> str:
    lines = ["Status:"]

    for task, slot in config.routing.items():
        mc = config.models.get(slot)
        model = mc.model if mc else "(unconfigured)"
        lines.append(f"  {task}: {model} [{slot}]")

    if session_id is None:
        lines.append("  Session: none (not persisted)")
    else:
        count = "unknown" if message_count is None else str(message_count)
        lines.append(f"  Session: {session_id} ({count} messages)")

    lines.append(
        f"  Tokens this session: {usage.total_tokens}"
        f" ({usage.prompt_tokens} in, {usage.completion_tokens} out)"
    )

    if state_dir is not None:
        lines.append(f"  {_cron_health_line(state_dir, config.timezone)}")

    return "\n".join(lines)


def _handle_status(status_fn: Callable[[], str] | None) -> str:
    if status_fn is None:
        return "Status unavailable."
    return status_fn()


def _find_provider_preset(provider_key: str) -> dict | None:
    """The contrib preset for a provider key, or None."""
    from faffmonkey.cli.init import _find_project_root
    try:
        preset_dir = _find_project_root() / "contrib" / "providers" / "openai-compatible"
    except RuntimeError:
        return None
    if not preset_dir.is_dir():
        return None
    for path in sorted(preset_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and data.get("provider_key") == provider_key and data.get("base_url"):
            return data
    return None


def _handle_model(args: str, config: Config, state_dir: Path | None = None) -> str:
    parts = args.split()
    if not parts:
        lines = ["Model configuration:"]
        for slot, mc in config.models.items():
            lines.append(f"  {slot}: {mc.model} ({mc.provider}, {mc.base_url})")
        lines.append("")
        lines.append("Routing:")
        for task, slot in config.routing.items():
            lines.append(f"  {task} -> {slot}")
        return "\n".join(lines)

    if len(parts) not in (2, 3):
        return "Usage: /model <slot> <model>, or /model <slot> <provider> <model>"

    if len(parts) == 2:
        slot, new_model = parts
        provider_key = None
    else:
        slot, provider_key, new_model = parts
    mc = config.models.get(slot)
    if mc is None:
        available = ", ".join(config.models.keys())
        return f"Unknown slot {slot!r}. Available: {available}"

    if not _MODEL_NAME_RE.match(new_model):
        return (
            f"Invalid model name {new_model!r}. Nothing was changed. "
            f"A slot pointing at a model that does not resolve stops every "
            f"cron job listed after it."
        )

    old_model = mc.model
    old_provider = mc.provider
    raw_extra: dict[str, object] = {}
    if provider_key is None or provider_key == mc.provider:
        config.models[slot] = dataclasses.replace(mc, model=new_model)
    else:
        # Switching provider live: resolve_provider builds from the slot's
        # ModelConfig on every call, so no restart is needed as long as the
        # provider's API key is already in the environment. Connection
        # details come from another slot on that provider, else from the
        # contrib preset.
        donor_name = next(
            (n for n, m in config.models.items() if m.provider == provider_key),
            None,
        )
        if donor_name is not None:
            donor = config.models[donor_name]
            config.models[slot] = dataclasses.replace(
                mc, provider=donor.provider, base_url=donor.base_url,
                api_key=donor.api_key, module=donor.module,
                allow_insecure=donor.allow_insecure, model=new_model,
            )
            raw_extra = {"base_url": donor.base_url, "_donor": donor_name}
        else:
            preset = _find_provider_preset(provider_key)
            if preset is None:
                known = sorted({m.provider for m in config.models.values()})
                return (
                    f"Unknown provider {provider_key!r}. Nothing was changed. "
                    f"Slots use: {', '.join(known)}; presets live in "
                    f"contrib/providers/openai-compatible/."
                )
            api_key_env = preset.get("api_key_env", "")
            api_key = os.environ.get(api_key_env, "").strip() if api_key_env else ""
            if api_key_env and not api_key:
                return (
                    f"{api_key_env} is not in the environment, so "
                    f"{provider_key} cannot be reached from this process. "
                    f"Add it to state/.env and restart, or switch from a "
                    f"slot already on {provider_key}. Nothing was changed."
                )
            config.models[slot] = dataclasses.replace(
                mc, provider=provider_key, base_url=preset["base_url"],
                api_key=api_key, module="", model=new_model,
            )
            raw_extra = {
                "base_url": preset["base_url"], "_api_key_env": api_key_env,
            }

    # Persist, or the switch reverts on restart with nothing to say so.
    # Slots stay global on purpose, so a cron job labelled "main" follows
    # the main model, which is the point of having slots.
    if state_dir is None:
        return (
            f"Switched {slot}: {old_model} -> {new_model}\n"
            f"(this session only; no state directory to persist to)"
        )
    switched = (
        f"Switched {slot}: {old_model} on {old_provider} -> "
        f"{new_model} on {config.models[slot].provider}"
    )
    config_path = state_dir / "config.json"
    try:
        raw = read_json_object(config_path, "config.json")
        models_raw = raw.setdefault("models", {})
        entry = models_raw.setdefault(slot, {})
        entry["model"] = new_model
        if raw_extra:
            entry["provider"] = config.models[slot].provider
            entry["base_url"] = raw_extra["base_url"]
            donor_name = raw_extra.get("_donor")
            if donor_name is not None:
                donor_raw = models_raw.get(donor_name, {})
                for key in ("api_key_env", "module", "allow_insecure"):
                    if key in donor_raw:
                        entry[key] = donor_raw[key]
                    else:
                        entry.pop(key, None)
            else:
                api_key_env = raw_extra.get("_api_key_env", "")
                if api_key_env:
                    entry["api_key_env"] = api_key_env
                else:
                    entry.pop("api_key_env", None)
                entry.pop("module", None)
        write_json_object(config_path, raw)
    except (ConfigError, OSError) as e:
        return f"{switched}\n(not saved: {e}; it will revert on restart)"
    return f"{switched} (saved)"


def _handle_compact(compact_fn: Callable[[], dict] | None) -> str:
    if compact_fn is None:
        return "Compaction not available (no session store)."
    stats = compact_fn()
    if stats.get("aborted"):
        return f"Compaction aborted: {stats.get('reason', 'unknown')}"
    if stats.get("skipped"):
        return "Nothing to compact (the protected tail covers the whole session)."
    return (
        f"Compaction complete.\n"
        f"  Before: {stats['before_tokens']} tokens ({stats['before_messages']} messages)\n"
        f"  After:  {stats['after_tokens']} tokens ({stats['after_messages']} messages)"
    )


def _handle_skill(
    args: str,
    workspace: Path | None = None,
    tz: str = "UTC",
    state_dir: Path | None = None,
) -> str:
    # shlex, not str.split: the same quoting rule the skill_invoke tool
    # follows, so /skill and the tool behave identically.
    try:
        parts = shlex.split(args.strip())
    except ValueError as e:
        return f"could not parse skill input: {e}"
    if not parts:
        return "Usage: /skill <name> [action] [args...]"

    name = parts[0]

    if workspace is None:
        return f"error: no workspace configured for skill invocation"

    full_md = skill_load_full(workspace, name)
    if full_md is None:
        available = scan_skills(workspace, state_dir)
        if available:
            names = ", ".join(n for n, _ in available)
            return f"skill not found: {name}. Available: {names}"
        return f"skill not found: {name}. No skills installed."

    if len(parts) < 2:
        return f"[SKILL.md for {name}]\n\n{full_md}"

    action = parts[1]
    action_args = parts[2:] if len(parts) > 2 else []

    output, attachments, is_error = skill_invoke(
        workspace, name, action, action_args, tz=tz, state_dir=state_dir,
    )
    result_parts = [output]
    if attachments:
        result_parts.append("Attachments: " + ", ".join(str(a) for a in attachments))
    return "\n".join(result_parts)


def _handle_cron(args: str, workspace: Path | None = None, state_dir: Path | None = None) -> str:
    """Deterministic cron visibility: no LLM call and no skill invocation,
    so it works even when the agent or its model is the thing being
    debugged. Reuses the faff cron renderers via stdout capture rather than
    duplicating their formatting."""
    if workspace is None or state_dir is None:
        return "error: cron inspection needs a workspace and state directory"
    import io
    from contextlib import redirect_stdout

    from faffmonkey.cli.cron import run_cron_history, run_cron_list

    parts = args.split()
    buf = io.StringIO()
    try:
        if not parts:
            with redirect_stdout(buf):
                run_cron_list(state_dir, workspace)
        elif parts[0] == "history" and len(parts) == 2:
            with redirect_stdout(buf):
                run_cron_history(state_dir, parts[1])
        else:
            return "Usage: /cron, or /cron history <job-id>"
    except Exception as e:
        return f"error reading cron state: {e}"
    out = buf.getvalue().strip()
    return out or "No cron output."


def handle_slash_command(
    text: str,
    config: Config,
    clear_history: Callable[[], None],
    new_session: Callable[[], None] | None = None,
    workspace: Path | None = None,
    compact_fn: Callable[[], dict] | None = None,
    memory_flush_fn: Callable[[], str] | None = None,
    status_fn: Callable[[], str] | None = None,
    state_dir: Path | None = None,
) -> str | None:
    text = text.strip()
    if not text.startswith("/"):
        return None

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return _handle_help()
    if cmd == "/status":
        return _handle_status(status_fn)
    if cmd == "/new":
        outcome = FLUSH_FAILED
        if memory_flush_fn is not None:
            try:
                outcome = memory_flush_fn()
            except Exception:
                logger.warning("memory flush failed during /new, proceeding anyway")
        clear_history()
        if new_session is not None:
            new_session()
        if outcome == FLUSH_SAVED:
            return "Session saved and reset."
        if outcome == FLUSH_NOTHING:
            return "Session reset (nothing new to save)."
        return "Session reset (memory was not saved)."
    if cmd == "/clear":
        clear_history()
        if new_session is not None:
            new_session()
        return "Session reset."
    if cmd == "/model":
        return _handle_model(args, config, state_dir)
    if cmd == "/compact":
        return _handle_compact(compact_fn)
    if cmd == "/skill":
        return _handle_skill(
            args, workspace=workspace, tz=str(config.timezone), state_dir=state_dir,
        )
    if cmd == "/cron":
        return _handle_cron(args, workspace=workspace, state_dir=state_dir)

    return f"Unknown command: {cmd}. Type /help for available commands."


def _provider_complete_with_timeout(
    provider: Provider,
    request: CompletionRequest,
    timeout: float,
) -> CompletionResponse:
    try:
        return run_with_timeout(
            lambda: provider.complete(request), timeout, "provider.complete()"
        )
    except TimeoutError as e:
        raise RetryableError(str(e)) from None


class AgentLoop:
    def __init__(
        self,
        resolve_provider: Callable[[ModelConfig], Provider],
        config: Config,
        channel: Channel,
        system_prompt: str = "",
        context_window: int = 128000,
        db_path: Path | None = None,
        channel_id: str = "cli",
        tool_registry: ToolRegistry | None = None,
        workspace: Path | None = None,
        debug: bool = False,
        allow_overflow: bool = False,
        bootstrap_file_tokens: dict[str, int] | None = None,
        session_lock: threading.RLock | None = None,
        session_rotated: threading.Event | None = None,
        history_dirty: threading.Event | None = None,
        history_dirty_peers: list[threading.Event] | None = None,
        session_key: str | None = None,
        on_activity: Callable[[str], None] | None = None,
        transcriber: Transcriber | None = None,
        synthesiser: Synthesiser | None = None,
        state_dir: Path | None = None,
        system_prompt_fn: Callable[[], str] | None = None,
        conversation_slot: str | None = None,
        config_readonly: bool = False,
    ) -> None:
        self.resolve_provider = resolve_provider
        self.config = config
        self._conversation_slot = conversation_slot
        self._config_readonly = config_readonly
        # Callers that are not a person (cron) need to tell an answer from
        # the "provider gave us nothing" message, which is ordinary text.
        self.last_response_empty = False
        self.channel = channel
        self.system_prompt = system_prompt
        self._system_prompt_fn = system_prompt_fn
        self._workspace = workspace
        self._context_window = context_window
        self.history: list[Message] = []
        self._db_path = db_path
        self._state_dir = state_dir
        self._store: SessionStore | None = None
        self._channel_id = channel_id
        # Which conversation this loop is in. Defaults to the channel name;
        # faff run passes MAIN_SESSION_KEY so every channel shares one.
        self._session_key = session_key or channel_id
        self._active_key = self._session_key
        self._session_id: str | None = None
        self._tools = tool_registry
        self._debug = debug
        self._turn_start: float = time.monotonic()
        self._last_activity: float = self._turn_start
        self._turn_attachments: list[Path] = []
        self._goal: GoalState | None = None
        self._session_lock = session_lock
        self._session_rotated = session_rotated or threading.Event()
        self._history_dirty = history_dirty or threading.Event()
        # The other loops on the same session. Set after every write so
        # they reload at their next turn boundary.
        self._history_dirty_peers = list(history_dirty_peers or [])
        self._on_activity = on_activity
        self._abandoned_threads: int = 0
        self._transcriber: Transcriber = transcriber or NoopTranscriber()
        self._synthesiser: Synthesiser = synthesiser or NoopSynthesiser()
        self.usage_total = TokenUsage()

        if system_prompt:
            from faffmonkey.runtime.tokens import check_budget

            result = check_budget(system_prompt, context_window)
            if not result.ok and not allow_overflow:
                by_size = sorted(
                    (bootstrap_file_tokens or {}).items(),
                    key=lambda x: x[1],
                    reverse=True,
                )
                for path, tokens in by_size:
                    logger.warning("bootstrap file: %s = %d tokens", path, tokens)
                largest = ", ".join(f"{p} ({t})" for p, t in by_size[:3])
                raise ConfigError(
                    f"Bootstrap exceeds context budget"
                    f" ({result.total_tokens} / {result.max_tokens} tokens)."
                    f" Largest files: {largest}."
                    f" Trim bootstrap files or use a larger model."
                    f" Override with --allow-overflow."
                )
            elif not result.ok:
                logger.warning(
                    "bootstrap exceeds context budget: %d/%d tokens"
                    " (--allow-overflow active)",
                    result.total_tokens,
                    result.max_tokens,
                )

    def _ensure_db(self) -> None:
        if self._store is not None or self._db_path is None:
            return
        self._store = SessionStore(self._db_path)
        session = self._store.get_or_create_main_session(self._active_key)
        self._session_id = session.id
        self.history = self._store.get_history(session.id)

    def _switch_session(self, key: str) -> None:
        """Move this loop to another conversation, e.g. a guild room.

        Every direct message shares the main session, so a group message
        needs somewhere else to go; the key follows the message.
        """
        if key == self._active_key:
            return
        self._active_key = key
        if self._store is not None:
            session = self._store.get_or_create_main_session(key)
            self._session_id = session.id
            self.history = self._store.get_history(session.id)

    def _notify_peers(self) -> None:
        for event in self._history_dirty_peers:
            event.set()

    def clear_history(self) -> None:
        if self._store is not None and self._session_id is not None:
            self._store.deactivate_session(self._session_id)
            self._session_id = None
            self._notify_peers()
        self.history = []
        # /status calls this "Tokens this session", so a session reset has
        # to reset it. Otherwise a brand-new session reported the whole
        # process total.
        self.usage_total = TokenUsage()

    def _new_session(self) -> None:
        if self._store is not None:
            session = self._store.get_or_create_main_session(self._active_key)
            self._session_id = session.id
            self._notify_peers()

    def _mark_activity(self) -> None:
        self._last_activity = time.monotonic()

    def _check_turn_duration(self) -> bool:
        """True when the turn must stop.

        Two timers, not one. The inactivity clock resets whenever the turn
        actually makes progress, so a long chain of quick tools does not
        look like a hang; the absolute cap stops a turn that is genuinely
        making progress from running all day.
        """
        now = time.monotonic()
        if (now - self._last_activity) >= INACTIVITY_TIMEOUT:
            return True
        return (now - self._turn_start) >= MAX_TURN_DURATION

    _MAX_ABANDONED_THREADS = 5

    def _dispatch_with_timeout(self, tc: ToolCall, timeout: float = 120.0) -> ToolResult:
        try:
            return run_with_timeout(
                lambda: self._tools.dispatch(tc), timeout, "tool dispatch"
            )
        except TimeoutError:
            pass
        self._abandoned_threads += 1
        logger.warning(
            "tool dispatch timed out, abandoned thread #%d",
            self._abandoned_threads,
        )
        if self._abandoned_threads >= self._MAX_ABANDONED_THREADS:
            logger.error(
                "too many abandoned tool threads (%d), aborting turn",
                self._abandoned_threads,
            )
            return ToolResult(
                id=tc.id,
                content="internal error: too many hung tools, aborting",
                is_error=True,
            )
        return ToolResult(
            id=tc.id,
            content=f"tool dispatch timed out ({timeout}s)",
            is_error=True,
        )

    def _do_compact(self) -> dict:
        from faffmonkey.runtime.compaction import compact

        stats = compact(
            self._store, self._session_id, self.config,
            self._workspace, self.resolve_provider,
            self._context_window,
        )
        if not stats.get("aborted"):
            self.history = self._store.get_history(self._session_id)
        return stats

    def _do_status(self) -> str:
        message_count: int | None = None
        if self._store is not None and self._session_id is not None:
            try:
                message_count = self._store.message_count(self._session_id)
            except sqlite3.Error as e:
                logger.warning("status: message count failed: %s", e)
        return _format_status(
            self.config, self._session_id, message_count,
            self.usage_total, self._state_dir,
        )

    def _maybe_compact(self) -> None:
        """Compact if needed, and never lose a turn because it failed.

        This runs after the model has already produced a reply. A session
        that is too large is a problem for the next turn; a reply thrown
        away is a problem now, and on the goal path it would also cancel
        the active goal.
        """
        if not (self._store and self._session_id and self._workspace):
            return
        from faffmonkey.runtime.compaction import should_compact

        try:
            if should_compact(
                self._store, self._session_id,
                self.config.compaction, self._context_window,
            ):
                self._do_compact()
        except Exception:
            logger.exception("compaction failed; continuing with the turn")

    def _maybe_daily_note(self) -> None:
        """Record the day from the loop, not from the model's goodwill.

        AGENTS.md asked the agent to append to today's log as things
        happened; over a full day of conversation it never did once, and
        the evening job that was meant to catch what it missed failed on
        a provider error, so the day was lost. Runs after the reply for
        the same reason compaction does.
        """
        if not (self._store and self._session_id and self._workspace):
            return
        from faffmonkey.runtime.compaction import daily_note, daily_note_due

        try:
            if daily_note_due(self._store, self._session_id, self.config.daily_note):
                daily_note(
                    self._store, self._session_id, self._workspace,
                    self.resolve_provider, self.config,
                )
        except Exception:
            logger.exception("daily note failed; continuing with the turn")

    def _live_image_cutoff(self) -> int:
        """Index of the last user message: only its images are live.

        An image's durable value is extracted in the turn it arrives (the
        model's own reading of it persists as text). Resending the base64
        afterwards costs its token weight on every turn and forces the
        vision route.
        """
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].role == "user":
                return i
        return len(self.history)

    def _build_messages(self) -> list[Message]:
        messages = list(self.history)
        cutoff = self._live_image_cutoff()
        for i, m in enumerate(messages):
            if not m.images:
                continue
            if i >= cutoff and len(m.images) <= MAX_REQUEST_IMAGES:
                continue
            keep = m.images[-MAX_REQUEST_IMAGES:] if i >= cutoff else []
            refs = ", ".join(im for im in m.images if im not in keep)
            messages[i] = dataclasses.replace(
                m,
                content=f"{m.content}\n[earlier images, not resent: {refs}]".strip(),
                images=keep,
            )
        if self.system_prompt:
            messages.insert(0, Message(role="system", content=self.system_prompt))
        return messages

    def _turn_task(self) -> str:
        """Which routing task this turn needs.

        Only the current turn's images ride the request (see
        _live_image_cutoff), so only they force the vision route.
        """
        cutoff = self._live_image_cutoff()
        if any(m.images for m in self.history[cutoff:]):
            # The route key is always present because DEFAULT_ROUTING carries
            # it, so testing for the key alone made this fallback dead code.
            # What actually fails is a route pointing at a slot with no model
            # behind it, which raised ConfigError instead of falling back.
            slot = self.config.routing.get("image_understanding")
            if slot is not None and slot in self.config.models:
                return "image_understanding"
            logger.warning(
                "conversation contains images but no usable image_understanding "
                "route (slot %r); falling back to conversation", slot,
            )
        return "conversation"

    def _resolve_turn_model(self, task: str) -> ModelConfig:
        """Resolve a turn's model, honouring a caller-supplied slot.

        A caller that already chose the slot (cron) passes it in. Image turns
        still route through image_understanding, because a slot chosen for
        text cannot necessarily read a picture.
        """
        if self._conversation_slot is not None and task == "conversation":
            return self.config.resolve_model(task, override=self._conversation_slot)
        return self.config.resolve_model(task)

    def _complete_once(self, model_config: ModelConfig) -> CompletionResponse:
        messages = self._build_messages()
        request = CompletionRequest(
            messages=messages,
            model=model_config.model,
            tools=self._tools.schemas() if self._tools is not None else None,
        )

        if self._debug:
            tool_count = len(request.tools) if request.tools else 0
            print(
                f"[debug] request: model={request.model}"
                f" tools={tool_count > 0} tool_count={tool_count}",
                file=sys.stderr,
            )

        provider = self.resolve_provider(model_config)
        timeout = float(model_config.timeout)

        fallbacks: list[Callable[[], CompletionResponse]] = []
        for fb_model in self.config.fallback_models:
            fb_provider = self.resolve_provider(fb_model)
            fb_request = CompletionRequest(
                messages=messages,
                model=fb_model.model,
                tools=self._tools.schemas() if self._tools is not None else None,
            )
            fb_timeout = float(fb_model.timeout)
            fallbacks.append(lambda p=fb_provider, r=fb_request, t=fb_timeout: _provider_complete_with_timeout(p, r, t))

        response = retry_with_fallback(
            lambda: _provider_complete_with_timeout(provider, request, timeout),
            fallbacks,
        )

        usage = response.usage
        self.usage_total = self.usage_total + usage

        if self._debug:
            tc = response.tool_calls
            tc_count = len(tc) if tc else 0
            print(
                f"[debug] response: text_len={len(response.text)}"
                f" tool_calls={tc_count > 0} tool_calls_count={tc_count}",
                file=sys.stderr,
            )

        return response

    def _persist_message(self, role: str, content: str | None = None, tool_calls: list[ToolCall] | None = None, tool_call_id: str | None = None, images: list[str] | None = None) -> None:
        if self._store is not None and self._session_id is not None:
            self._store.append_message(
                self._session_id, role, content, tool_calls, tool_call_id,
                images=images,
            )
            self._notify_peers()

    def _check_session_rotated(self) -> None:
        """Adopt the store's active session if this loop's is no longer it.

        The store is the authority, not the rotated event: the event only
        reaches loops in this process, and a rotation can come from another
        one (`faff cron run` of a rotate_session job). The event is cleared
        for whoever set it.
        """
        self._session_rotated.clear()
        if self._store is None:
            return
        session = self._store.get_or_create_main_session(self._active_key)
        if session.id == self._session_id:
            return
        self._session_id = session.id
        self.history = self._store.get_history(session.id)

    def _check_history_dirty(self) -> None:
        """Reload a session another thread has written to.

        Distinct from rotation: the session id does not change. This is how
        a cron delivery becomes visible to a conversation that is already
        running, so replying to a morning briefing lands in a history that
        contains the briefing.
        """
        if not self._history_dirty.is_set():
            return
        self._history_dirty.clear()
        if self._store is not None and self._session_id is not None:
            self.history = self._store.get_history(self._session_id)

    def _refresh_history(self) -> None:
        """Sync in-memory history with the store, at a turn boundary only.

        Called before the inbound message is appended, never mid-turn:
        replacing self.history after the user's message has been persisted
        to the old session drops that message.
        """
        self._check_session_rotated()
        self._check_history_dirty()

    def _complete(self, model_config: ModelConfig) -> str:
        self._turn_start = time.monotonic()
        self._last_activity = self._turn_start
        self._turn_attachments = []
        self._abandoned_threads = 0
        tool_call_count = 0
        llm_call_count = 0
        compacted_this_turn = False

        while True:
            if self._check_turn_duration():
                return "Turn ended: inactivity timeout."
            llm_call_count += 1
            if llm_call_count > MAX_LLM_CALLS_PER_TURN:
                # history[-1] is usually a tool result at this point, and
                # handing raw tool output back as if the model had said it
                # is worse than saying nothing.
                last = self.history[-1] if self.history else None
                prefix = last.content if last is not None and last.role == "assistant" else ""
                return (prefix or "") + "\n[Turn ended: too many LLM round-trips]"

            try:
                response = self._complete_once(model_config)
                self._mark_activity()
            except ContextLengthError:
                if compacted_this_turn:
                    raise
                if not (self._store and self._session_id and self._workspace):
                    raise
                logger.warning("context length exceeded, triggering emergency compaction")
                stats = self._do_compact()
                if stats.get("aborted"):
                    raise
                compacted_this_turn = True
                continue

            tool_calls = response.tool_calls or []

            if not tool_calls and not response.text.strip():
                for attempt in range(EMPTY_RESPONSE_RETRIES):
                    llm_call_count += 1
                    if llm_call_count > MAX_LLM_CALLS_PER_TURN:
                        break
                    logger.warning(
                        "empty response, retry %d/%d",
                        attempt + 1, EMPTY_RESPONSE_RETRIES,
                    )
                    nudge = Message(role="system", content=EMPTY_RESPONSE_NUDGE)
                    self.history.append(nudge)
                    try:
                        response = self._complete_once(model_config)
                    finally:
                        if self.history and self.history[-1] is nudge:
                            self.history.pop()
                    tool_calls = response.tool_calls or []
                    if response.text.strip() or tool_calls:
                        break

                if not response.text.strip() and not tool_calls:
                    self.last_response_empty = True
                    error_text = (
                        "The model returned an empty response."
                        " Try again or check your provider."
                    )
                    _lock = self._session_lock or nullcontext()
                    with _lock:
                        self.history.append(
                            Message(role="assistant", content=error_text),
                        )
                        self._persist_message("assistant", error_text)
                    return error_text

            response_text = response.text
            history_text, scan_hit = flag_response(response_text, "<provider>", "provider response")
            if scan_hit is not None:
                response_text = f"[WARNING: provider response flagged: {scan_hit}]\n{response_text}"

            _lock = self._session_lock or nullcontext()

            if not tool_calls or self._tools is None:
                with _lock:
                    self.history.append(Message(role="assistant", content=history_text))
                    self._persist_message("assistant", history_text)
                return response_text

            with _lock:
                self.history.append(Message(
                    role="assistant",
                    content=history_text,
                    tool_calls=response.tool_calls,
                ))
                self._persist_message(
                    "assistant", history_text or None, tool_calls=response.tool_calls,
                )

            for idx, tc in enumerate(tool_calls):
                tool_call_count += 1
                if tool_call_count > MAX_TOOL_CALLS_PER_TURN:
                    with _lock:
                        err = "tool call limit exceeded (50 per turn)"
                        self.history.append(Message(role="tool", content=err, tool_call_id=tc.id))
                        self._persist_message("tool", err, tool_call_id=tc.id)
                    continue

                if self._check_turn_duration():
                    # Every remaining call in the batch needs a result too.
                    # Returning here left the persisted assistant message
                    # carrying tool_calls that nothing ever answered, which
                    # strict providers reject on every later turn.
                    with _lock:
                        # Slice by position. index(tc) matches by value, so a
                        # batch containing two identical tool calls restubbed
                        # from the first one and answered it twice.
                        for remaining in tool_calls[idx:]:
                            err = "turn killed: inactivity timeout"
                            self.history.append(Message(role="tool", content=err, tool_call_id=remaining.id))
                            self._persist_message("tool", err, tool_call_id=remaining.id)
                    return "Turn ended: inactivity timeout."

                tc_hit = scan_patterns(tc.name, path="<provider:tool_call>")
                if tc_hit is None:
                    tc_hit = scan_patterns(
                        json.dumps(tc.arguments), path="<provider:tool_call>",
                    )
                if tc_hit is not None:
                    logger.warning(
                        "tool call blocked: %s (reason: %s)", tc.name, tc_hit,
                    )
                    with _lock:
                        err = f"tool call blocked: {tc_hit}"
                        self.history.append(Message(role="tool", content=err, tool_call_id=tc.id))
                        self._persist_message("tool", err, tool_call_id=tc.id)
                    continue

                try:
                    # Fakes and mocks in tests answer hasattr for anything,
                    # so trust only a numeric answer.
                    try:
                        dispatch_ceiling = self._tools.dispatch_timeout(tc)
                    except Exception:
                        dispatch_ceiling = 120.0
                    if not isinstance(dispatch_ceiling, (int, float)):
                        dispatch_ceiling = 120.0
                    result = self._dispatch_with_timeout(tc, dispatch_ceiling)
                    self._mark_activity()
                    # A skill's MEDIA: files reach the channel with the reply.
                    self._turn_attachments.extend(result.attachments)
                    redacted_content = redact(result.content)
                except Exception as e:
                    # Every tool_call in the persisted assistant message must
                    # get a result. An exception escaping this loop left
                    # orphaned tool_calls in the session, which strict
                    # providers reject on every later turn: one failure
                    # poisoned the conversation permanently.
                    logger.exception("tool %s raised", tc.name)
                    redacted_content = f"tool error: {redact(str(e))}"

                with _lock:
                    self.history.append(Message(role="tool", content=redacted_content, tool_call_id=tc.id))
                    self._persist_message("tool", redacted_content, tool_call_id=tc.id)

            if tool_call_count > MAX_TOOL_CALLS_PER_TURN:
                return response_text or "Tool call limit exceeded."

    def _do_memory_flush(self) -> str:
        if not (self._store and self._session_id and self._workspace):
            return FLUSH_FAILED
        from faffmonkey.runtime.compaction import memory_flush

        return memory_flush(
            self._store, self._session_id, self._workspace,
            self.resolve_provider, self.config,
        )

    def _goal_state_path(self) -> Path | None:
        if self._workspace is None:
            return None
        return self._workspace / "skills-data" / "goal" / "current.json"

    def _persist_goal(self) -> None:
        """Mirror goal state to workspace so `faff status` can see it.

        The file was read by faff status and written by nothing, so an
        operator checking on a running goal was told there was none, and
        started a second overlapping one.
        """
        path = self._goal_state_path()
        if path is None:
            return
        try:
            if self._goal is None or not self._goal.active:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "goal": self._goal.text,
                "channel": self._channel_id,
                "turns": self._goal.turn_count,
                "max_turns": self._goal.max_turns,
                # Whoever is running it. A goal file left behind by a
                # process that died is indistinguishable from a live one
                # without this, so faff status reported a goal as active
                # that nothing was working on.
                "pid": os.getpid(),
            }, indent=2) + "\n")
        except OSError as e:
            logger.warning("could not record goal state: %s", e)

    def _handle_goal(self, args: str) -> str:
        response, new_goal = handle_goal_command(args, self._goal)
        if new_goal is not None:
            self._goal = new_goal
        self._persist_goal()
        return response

    def _goal_turn(self) -> str | None:
        if self._goal is None or not self._goal.active:
            return None

        if self._goal.turn_count >= self._goal.max_turns:
            self._goal.active = False
            self._persist_goal()
            return (
                f"Goal budget exhausted ({self._goal.max_turns} turns).\n"
                f"Goal was: {self._goal.text}"
            )

        self._refresh_history()
        prompt = make_continuation_prompt(self._goal.text)
        self.history.append(Message(role="user", content=prompt))
        self._persist_message("user", prompt)

        try:
            model_config = self._resolve_turn_model("conversation")
        except ConfigError as e:
            self._goal.active = False
            self._persist_goal()
            return f"Goal aborted: config error: {e}"

        result = self._complete(model_config)
        self._goal.turn_count += 1

        self._maybe_compact()

        if check_goal_done(result):
            self._goal.active = False
            self._persist_goal()
            return f"{result}\n\nGoal completed in {self._goal.turn_count} turns."

        self._persist_goal()
        return result

    def _refresh_system_prompt(self) -> None:
        """Rebuild the prompt so the agent's clock and memory keep moving.

        system_prompt was assigned once in __init__ and never reassigned, so
        the current time, today's daily log and MEMORY.md were frozen at
        process start. A long-running container still believed it was the
        day it booted, weeks later. Session rotation does not help: it swaps
        the session id and reuses the same prompt string.

        Rebuilt once per turn, not per LLM call, so a turn making twenty
        tool calls still reads the workspace files once.
        """
        if self._system_prompt_fn is None:
            return
        try:
            self.system_prompt = self._system_prompt_fn()
        except Exception:
            # A broken workspace file must not take the turn down; the
            # previous prompt is stale but usable.
            logger.exception("could not rebuild system prompt, keeping previous")

    def handle_message(self, text: str, images: list[str] | None = None) -> str:
        self.last_response_empty = False
        self._ensure_db()
        self._refresh_system_prompt()
        self._refresh_history()
        parts = text.strip().split(None, 1)
        if parts and parts[0].lower() == "/goal":
            return self._handle_goal(parts[1] if len(parts) > 1 else "")

        compact_fn = (
            self._do_compact
            if (self._store and self._session_id and self._workspace)
            else None
        )
        memory_flush_fn = (
            self._do_memory_flush
            if (self._store and self._session_id and self._workspace)
            else None
        )
        slash_result = handle_slash_command(
            text, self.config, self.clear_history, self._new_session,
            workspace=self._workspace,
            compact_fn=compact_fn,
            memory_flush_fn=memory_flush_fn,
            status_fn=self._do_status,
            state_dir=None if self._config_readonly else self._state_dir,
        )
        if slash_result is not None:
            return slash_result

        self.history.append(Message(role="user", content=text, images=images or []))
        self._persist_message("user", text, images=images or None)

        try:
            model_config = self._resolve_turn_model(self._turn_task())
        except ConfigError as e:
            return f"Config error: {e}"

        result = self._complete(model_config)

        self._maybe_compact()
        self._maybe_daily_note()

        return result

    def _resolve_inbound_text(self, msg: InboundMessage) -> tuple[str | None, bool]:
        """Inbound text, or None when a voice message could not be read.

        None is not a transcript. Substituting a placeholder persisted it
        as the user's own words, so the conversation recorded them saying
        "[transcription not configured]" and the model answered it.
        """
        if msg.audio is None:
            return msg.text, False
        try:
            transcript = self._transcriber.transcribe(msg.audio, msg.audio_mime or "")
        except Exception as e:
            logger.warning("transcription failed: %s", e)
            return None, True
        if not transcript or not transcript.strip():
            return None, True
        cleaned = strip_invisible(transcript.strip())
        hit = scan_patterns(cleaned, path="<transcription>")
        if hit is not None:
            logger.warning("transcription blocked: %s", hit)
            return None, True
        return cleaned, True

    def _make_outbound(
        self, response_text: str, was_voice: bool, group_id: str | None = None,
    ) -> OutboundMessage:
        out = OutboundMessage(
            text=redact(response_text),
            attachments=list(self._turn_attachments),
            group_id=group_id,
        )
        if was_voice:
            try:
                result = self._synthesiser.synthesise(out.text)
            except Exception as e:
                logger.warning("speech synthesis failed: %s", e)
                result = None
            if result is not None:
                out.audio, out.audio_mime = result
        return out

    def _turn(self, msg: InboundMessage) -> None:
        """One inbound message, start to reply.

        Errors are contained here, not allowed out: an exception escaping
        run() kills this channel's thread for the lifetime of the process
        while the others carry on, so Docker's restart policy never fires.
        """
        text, was_voice = self._resolve_inbound_text(msg)
        if text is not None and was_voice:
            # The transcript arrived looking like typed text, so the agent
            # looked for an audio file to transcribe and said it could not
            # send voice, then the reply was synthesised anyway.
            text = f"{text}\n[voice note, transcribed]"
        images = [str(p) for p in msg.images]
        if text is not None and msg.attachments:
            # A document was saved to the inbox and the path stopped there,
            # so the agent was told a file had arrived and had no way to
            # address it short of guessing. It is not an image part; naming
            # the path is what makes file_read usable on it.
            refs = ", ".join(str(p) for p in msg.attachments)
            text = f"{text}\n[file saved to: {refs}]"
        if text is None:
            try:
                self.channel.send(OutboundMessage(
                    text="I could not transcribe that voice message. Send it as text?",
                    group_id=msg.group_id,
                ))
            except Exception:
                logger.exception("failed to send transcription notice")
            return
        key = (
            f"{self._channel_id}:{msg.group_id}" if msg.group_id
            else self._session_key
        )
        # One turn at a time across every loop on the shared session: two
        # channels answering at once would interleave two tool-call
        # sequences in one history. RLock, because the persist calls inside
        # the turn take the same lock.
        _lock = self._session_lock or nullcontext()
        try:
            with _lock:
                self._switch_session(key)
                if self._on_activity is not None and not msg.group_id:
                    self._on_activity(self._channel_id)
                response_text = self.handle_message(text, images=images)
        except Exception as e:
            logger.exception("turn failed on channel %s", self._channel_id)
            response_text = f"Sorry, that turn failed: {e}"
        try:
            self.channel.send(self._make_outbound(response_text, was_voice, msg.group_id))
        except Exception:
            logger.exception("failed to send reply on channel %s", self._channel_id)

    def run(self) -> None:
        try:
            self._ensure_db()
            self.channel.start()
            while True:
                if self._goal is not None and self._goal.active:
                    msg = self.channel.poll()
                    if msg is not None and self.channel.is_allowed(msg.sender_id):
                        self._turn(msg)
                        continue
                    try:
                        with (self._session_lock or nullcontext()):
                            result = self._goal_turn()
                    except Exception as e:
                        logger.exception("goal turn failed on channel %s", self._channel_id)
                        self._goal.active = False
                        self._persist_goal()
                        result = f"Goal stopped: {e}"
                    if result is not None:
                        try:
                            self.channel.send(OutboundMessage(text=redact(result)))
                        except Exception:
                            logger.exception("failed to send goal update")
                            continue
                    continue

                msg = self.channel.receive()
                if msg is None:
                    # None means "nothing yet". Only is_closed() ends the
                    # loop. Queue-backed channels return None on every idle
                    # poll, so breaking here ended the session one second
                    # after start.
                    if self.channel.is_closed():
                        break
                    continue
                if not self.channel.is_allowed(msg.sender_id):
                    logger.debug("denied message from %s", msg.sender_id)
                    continue
                self._turn(msg)
        finally:
            self.channel.stop()
            if self._store is not None:
                self._store.close()
