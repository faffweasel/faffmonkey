from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from faffmonkey.config import CompactionConfig, Config, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.compaction import (
    FLUSH_FAILED,
    FLUSH_NOTHING,
    FLUSH_SAVED,
    MAX_FLUSH_CONTENT_BYTES,
    MAX_PRESERVED_BLOB_BYTES,
    _PRESERVED_MARKER,
    _TRUNCATED_DATA_MARKER,
    _checkpoint,
    _determine_protected_tail,
    _deterministic_truncate,
    _execute_flush_writes,
    _find_existing_summary,
    _serialize_messages,
    _strip_orphaned_tool_messages,
    _summarise,
    compact,
    memory_flush,
    should_compact,
)
from faffmonkey.runtime.session import SessionStore
from faffmonkey.types import CompletionRequest, CompletionResponse, Message, ToolCall

from tests.faux_provider import FauxProvider, faux_response


def _flush_ok_response():
    return faux_response(tool_calls=[{
        "name": "file_write",
        "arguments": {"path": "MEMORY.md", "content": "flushed"},
    }])


def _make_config(**overrides) -> Config:
    defaults = {
        "models": {
            "main": ModelConfig(
                provider="faux", model="faux-main",
                base_url="http://localhost", api_key="",
            ),
            "cheap": ModelConfig(
                provider="faux", model="faux-cheap",
                base_url="http://localhost", api_key="",
            ),
        },
        "routing": {"conversation": "main", "compaction": "main"},
        "fallback_models": [],
        "timezone": ZoneInfo("UTC"),
        "heartbeat": HeartbeatConfig(),
        "compaction": CompactionConfig(),
        "channels": {},
        "tool_permissions": {},
    }
    defaults.update(overrides)
    return Config(**defaults)


def _make_store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


def _populate_session(store: SessionStore, session_id: str, count: int) -> None:
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        store.append_message(session_id, role, f"message {i}")


class TestShouldCompact:
    def test_below_threshold_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        _populate_session(store, session.id, 4)
        config = CompactionConfig(threshold=0.8, hard_message_limit=400)
        assert not should_compact(store, session.id, config, 128000)

    def test_exceeds_message_limit(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        config = CompactionConfig(hard_message_limit=10)
        _populate_session(store, session.id, 10)
        assert should_compact(store, session.id, config, 128000)

    def test_below_message_limit(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        config = CompactionConfig(hard_message_limit=10)
        _populate_session(store, session.id, 5)
        assert not should_compact(store, session.id, config, 128000)

    def test_exceeds_token_threshold(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        config = CompactionConfig(threshold=0.8)
        big_msg = "x" * 10000
        store.append_message(session.id, "user", big_msg)
        assert should_compact(store, session.id, config, 100)

    def test_empty_session_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        config = CompactionConfig()
        assert not should_compact(store, session.id, config, 128000)


class TestDetermineProtectedTail:
    def test_all_protected_when_fewer_than_n(self):
        messages = [Message(role="user", content=f"msg {i}") for i in range(5)]
        result = _determine_protected_tail(messages, 10)
        assert len(result) == 5

    def test_exact_n_returned(self):
        messages = [Message(role="user", content=f"msg {i}") for i in range(30)]
        result = _determine_protected_tail(messages, 20)
        assert len(result) == 20
        assert result[0].content == "msg 10"

    def test_tool_pair_not_split(self):
        messages = [
            Message(role="user", content="do something"),
            Message(role="assistant", content="ok", tool_calls=[
                ToolCall(id="tc1", name="file_read", arguments={}),
            ]),
            Message(role="tool", content="file contents", tool_call_id="tc1"),
            Message(role="assistant", content="here is the result"),
            Message(role="user", content="thanks"),
        ]
        result = _determine_protected_tail(messages, 3)
        assert len(result) >= 3
        for i, msg in enumerate(result):
            if msg.role == "tool" and msg.tool_call_id:
                assert i > 0, "tool result should not be first in protected tail"
                assert result[i - 1].role == "assistant"

    def test_multiple_tool_results_kept_together(self):
        messages = [
            Message(role="user", content="search"),
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="web_search", arguments={}),
                ToolCall(id="tc2", name="file_read", arguments={}),
            ]),
            Message(role="tool", content="search result", tool_call_id="tc1"),
            Message(role="tool", content="file result", tool_call_id="tc2"),
            Message(role="assistant", content="found it"),
            Message(role="user", content="great"),
        ]
        result = _determine_protected_tail(messages, 3)
        assert result[0].role == "assistant"
        assert result[0].tool_calls is not None

    def test_tail_starts_on_tool_extends_backward(self):
        messages = [
            Message(role="user", content="a"),
            Message(role="user", content="b"),
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="file_read", arguments={}),
            ]),
            Message(role="tool", content="data", tool_call_id="tc1"),
            Message(role="user", content="c"),
        ]
        result = _determine_protected_tail(messages, 2)
        assert len(result) == 3
        assert result[0].role == "assistant"


class TestDeterministicTruncate:
    def test_truncates_and_adds_marker(self):
        messages = [Message(role="user", content="hello " * 5000)]
        result = _deterministic_truncate(messages)
        assert "[Earlier conversation truncated" in result
        assert "[WARNING: conversation heavily truncated" in result
        assert len(result) < len("hello " * 5000) + 200

    def test_short_input_preserved(self):
        messages = [Message(role="user", content="short")]
        result = _deterministic_truncate(messages)
        assert "[user]: short" in result
        assert "[Earlier conversation truncated" in result
        assert "[WARNING: conversation heavily truncated" in result


class TestSerializeMessages:
    def test_basic_serialisation(self):
        messages = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ]
        result = _serialize_messages(messages)
        assert "[user]: hello" in result
        assert "[assistant]: hi" in result

    def test_tool_result_truncated(self):
        messages = [
            Message(role="tool", content="x" * 3000, tool_call_id="tc1"),
        ]
        result = _serialize_messages(messages)
        assert len(result) < 3000
        assert "..." in result

    def test_tool_calls_serialised(self):
        messages = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="file_read", arguments={}),
            ]),
        ]
        result = _serialize_messages(messages)
        assert "(tool_call: file_read)" in result


class TestCheckpoint:
    def test_creates_backup(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "hello")

        (state_dir / "config.json").write_text('{"models": {}}')

        assert _checkpoint(store, state_dir)

        backups = list((state_dir / "backups").glob("checkpoint_*"))
        assert len(backups) == 1
        assert (backups[0] / "sessions.db").exists()
        assert (backups[0] / "config.json").exists()

    def test_rotates_old_checkpoints(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")

        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        for i in range(5):
            cp = backups_dir / f"checkpoint_2025{i:02d}01T000000Z"
            cp.mkdir()
            (cp / "sessions.db").write_text("old")

        assert _checkpoint(store, state_dir)

        all_checkpoints = [
            p for p in backups_dir.glob("checkpoint_*") if p.suffix != ".tmp"
        ]
        assert len(all_checkpoints) == 5
        old_remaining = [p for p in all_checkpoints if "2025" in p.name]
        assert len(old_remaining) == 4

    def test_aborts_on_backup_failure(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = MagicMock()
        store.backup.side_effect = OSError("disk full")

        assert not _checkpoint(store, state_dir)


class TestMemoryFlush:
    def test_writes_files_via_tool_calls(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember: project deadline is Friday")

        provider = FauxProvider([
            faux_response(tool_calls=[{
                "name": "file_write",
                "arguments": {"path": "MEMORY.md", "content": "deadline: Friday"},
            }]),
        ])
        config = _make_config()

        memory_flush(store, session.id, workspace, lambda mc: provider, config)

        assert (workspace / "MEMORY.md").exists()
        assert "Friday" in (workspace / "MEMORY.md").read_text()

    def test_no_crash_on_empty_history(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        config = _make_config()

        # An empty history is nothing to save, not a failure. compact()
        # branches on this to decide whether to preserve the pre-compaction
        # head as a blob, and /new picks its message from it.
        result = memory_flush(store, session.id, workspace, lambda mc: MagicMock(), config)
        assert result == FLUSH_NOTHING

    def test_falls_back_to_cheap_model(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "test message")

        call_count = [0]

        def provider_fn(mc):
            call_count[0] += 1
            if call_count[0] == 1:
                failing = MagicMock()
                failing.complete.side_effect = RuntimeError("main model down")
                return failing
            return FauxProvider([faux_response(text="flushed ok")])

        config = _make_config()
        memory_flush(store, session.id, workspace, provider_fn, config)
        assert call_count[0] == 2

    def test_survives_all_models_failing(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "test")

        failing = MagicMock()
        failing.complete.side_effect = RuntimeError("all down")
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: failing, config)
        # FLUSH_FAILED is what makes compact() preserve the head rather than
        # discard it. A partial file written before the provider failed would
        # also be a defect, and neither was checked.
        assert result == FLUSH_FAILED
        assert list(workspace.iterdir()) == []

    def test_rejects_traversal_path(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "test")

        provider = FauxProvider([
            faux_response(tool_calls=[{
                "name": "file_write",
                "arguments": {"path": "../escape.txt", "content": "bad"},
            }]),
        ])
        config = _make_config()
        memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert not (tmp_path / "escape.txt").exists()

    def _store_with_history(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "ping?")
        store.append_message(session.id, "assistant", "pong")
        return store, session, workspace

    def test_nothing_to_save_marker_is_not_a_failure(self, tmp_path):
        store, session, workspace = self._store_with_history(tmp_path)
        provider = FauxProvider([faux_response(text="NOTHING_TO_SAVE")])

        result = memory_flush(store, session.id, workspace, lambda mc: provider, _make_config())

        assert result == FLUSH_NOTHING
        assert list(workspace.iterdir()) == []

    def test_wrong_tool_is_corrected_once_by_name(self, tmp_path):
        # The model picks tools it saw in the history rather than the one
        # offered; the correction must name what it did so it can stop.
        store, session, workspace = self._store_with_history(tmp_path)
        provider = FauxProvider([
            faux_response(tool_calls=[{"name": "file_read", "arguments": {"path": "MEMORY.md"}}]),
            faux_response(tool_calls=[{
                "name": "file_write",
                "arguments": {"path": "MEMORY.md", "content": "user says ping"},
            }]),
        ])

        result = memory_flush(store, session.id, workspace, lambda mc: provider, _make_config())

        assert result == FLUSH_SAVED
        assert "ping" in (workspace / "MEMORY.md").read_text()
        nudge = provider.calls[1].messages[-1]
        assert nudge.role == "user"
        assert "file_read" in nudge.content
        assert "file_write" in nudge.content
        assert provider.calls[1].tools == provider.calls[0].tools

    def test_prose_then_marker_is_nothing_to_save(self, tmp_path):
        store, session, workspace = self._store_with_history(tmp_path)
        provider = FauxProvider([
            faux_response(text="There is nothing here worth keeping."),
            faux_response(text="NOTHING_TO_SAVE"),
        ])

        result = memory_flush(store, session.id, workspace, lambda mc: provider, _make_config())

        assert result == FLUSH_NOTHING
        provider.assert_exhausted()

    def test_second_model_gets_a_turn_after_the_first_answers_wrongly_twice(self, tmp_path):
        store, session, workspace = self._store_with_history(tmp_path)
        providers = {
            "faux-main": FauxProvider([
                faux_response(tool_calls=[{"name": "file_edit", "arguments": {}}]),
                faux_response(tool_calls=[{"name": "file_edit", "arguments": {}}]),
            ]),
            "faux-cheap": FauxProvider([_flush_ok_response()]),
        }
        config = _make_config(routing={"conversation": "main", "compaction": "cheap"})

        result = memory_flush(
            store, session.id, workspace, lambda mc: providers[mc.model], config,
        )

        assert result == FLUSH_SAVED
        for provider in providers.values():
            provider.assert_exhausted()

    def test_same_model_is_not_asked_a_third_time(self, tmp_path):
        store, session, workspace = self._store_with_history(tmp_path)
        provider = FauxProvider([
            faux_response(text="noted"),
            faux_response(text="noted again"),
        ])

        result = memory_flush(store, session.id, workspace, lambda mc: provider, _make_config())

        assert result == FLUSH_FAILED
        assert len(provider.calls) == 2


class TestThreeTierFallback:
    def _make_config_and_store(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")
        config = _make_config(compaction=CompactionConfig(protect_last_n=5))
        return store, session, config, workspace, state_dir

    def test_tier1_succeeds(self, tmp_path):
        store, session, config, workspace, state_dir = self._make_config_and_store(tmp_path)
        provider = FauxProvider([
            _flush_ok_response(),
            faux_response(text="## Goal\ntier 1 summary"),
        ])

        stats = compact(
            store, session.id, config, workspace,
            lambda mc: provider, state_dir=state_dir,
        )
        assert not stats.get("aborted")
        history = store.get_history(session.id)
        assert history[0].role == "system"
        assert "tier 1 summary" in history[0].content

    def test_tier1_fails_tier2_succeeds(self, tmp_path):
        store, session, config, workspace, state_dir = self._make_config_and_store(tmp_path)

        def provider_fn(mc):
            if mc.model == "faux-main":
                call_count = getattr(provider_fn, "_main_calls", 0) + 1
                provider_fn._main_calls = call_count
                if call_count == 1:
                    return FauxProvider([_flush_ok_response()])
                failing = MagicMock()
                failing.complete.side_effect = RuntimeError("tier 1 down")
                return failing
            return FauxProvider([faux_response(text="cheap fallback summary")])

        stats = compact(
            store, session.id, config, workspace,
            provider_fn, state_dir=state_dir,
        )
        assert not stats.get("aborted")
        history = store.get_history(session.id)
        assert "cheap fallback summary" in history[0].content

    def test_all_tiers_fail_uses_truncation(self, tmp_path):
        store, session, config, workspace, state_dir = self._make_config_and_store(tmp_path)

        call_count = [0]

        def provider_fn(mc):
            call_count[0] += 1
            if call_count[0] == 1:
                return FauxProvider([_flush_ok_response()])
            failing = MagicMock()
            failing.complete.side_effect = RuntimeError("all tiers down")
            return failing

        stats = compact(
            store, session.id, config, workspace,
            provider_fn, state_dir=state_dir,
        )
        assert not stats.get("aborted")
        history = store.get_history(session.id)
        assert "[Earlier conversation truncated" in history[0].content


class TestCompact:
    def test_full_pipeline(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(40):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))
        provider = FauxProvider([
            _flush_ok_response(),
            faux_response(text="## Goal\ntest goal\n## Progress\n### Done\nstuff"),
        ])

        stats = compact(
            store, session.id, config, workspace,
            lambda mc: provider, state_dir=state_dir,
        )

        assert stats["before_messages"] == 40
        assert stats["after_messages"] == 6
        assert stats["after_tokens"] < stats["before_tokens"]

        history = store.get_history(session.id)
        assert history[0].role == "system"
        assert "test goal" in history[0].content
        assert len(history) == 6

    def test_aborts_on_checkpoint_failure(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {i}")
        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        with patch("faffmonkey.runtime.compaction._checkpoint", return_value=None):
            stats = compact(
                store, session.id, config, workspace,
                lambda mc: MagicMock(), state_dir=state_dir,
            )
        assert stats["aborted"]
        assert stats["reason"] == "checkpoint_failed"
        assert store.message_count(session.id) == 30

    def test_tool_pairs_preserved_in_output(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")

        for i in range(20):
            store.append_message(session.id, "user", f"padding {i}")

        tc = [ToolCall(id="tc1", name="file_read", arguments={})]
        store.append_message(session.id, "assistant", "let me check", tool_calls=tc)
        store.append_message(session.id, "tool", "file contents", tool_call_id="tc1")
        store.append_message(session.id, "assistant", "found it")
        store.append_message(session.id, "user", "thanks")
        store.append_message(session.id, "assistant", "welcome")

        config = _make_config(compaction=CompactionConfig(protect_last_n=4))
        provider = FauxProvider([
            _flush_ok_response(),
            faux_response(text="## Goal\nsummary"),
        ])

        compact(
            store, session.id, config, workspace,
            lambda mc: provider, state_dir=state_dir,
        )

        history = store.get_history(session.id)
        for i, msg in enumerate(history):
            if msg.role == "tool" and msg.tool_call_id:
                assert i > 0
                assert history[i - 1].role == "assistant"

    def test_empty_head_is_noop(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(3):
            store.append_message(session.id, "user", f"msg {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=20))
        stats = compact(
            store, session.id, config, workspace,
            lambda mc: MagicMock(), state_dir=state_dir,
        )

        assert stats["before_messages"] == 3
        assert stats["after_messages"] == 3
        assert stats.get("skipped") is True

    def test_protected_tail_exceeds_threshold_skips(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(10):
            store.append_message(session.id, "user", f"msg {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=10))
        stats = compact(
            store, session.id, config, workspace,
            lambda mc: MagicMock(), state_dir=state_dir,
        )
        assert stats["before_messages"] == 10
        assert stats["after_messages"] == 10
        assert stats.get("skipped") is True
        history = store.get_history(session.id)
        assert len(history) == 10

    def test_delete_reinsert_atomic_on_crash(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        original_append = store.append_message
        append_count = [0]

        def crashing_append(*args, **kwargs):
            append_count[0] += 1
            if append_count[0] == 3:
                raise RuntimeError("simulated crash mid-reinsert")
            return original_append(*args, **kwargs)

        provider = FauxProvider([
            _flush_ok_response(),
            faux_response(text="## Goal\nsummary"),
        ])

        with patch.object(store, "append_message", side_effect=crashing_append):
            with pytest.raises(RuntimeError, match="simulated crash"):
                compact(
                    store, session.id, config, workspace,
                    lambda mc: provider, state_dir=state_dir,
                )

        history = store.get_history(session.id)
        assert len(history) == 30

    def test_resummarisation_uses_previous_summary(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "system", "## Goal\nold summary content")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        captured_requests: list[CompletionRequest] = []

        def capturing_provider_fn(mc):
            provider = MagicMock()
            call_count = [0]

            def capture_complete(req):
                call_count[0] += 1
                captured_requests.append(req)
                return CompletionResponse(text="## Goal\nupdated summary", model="faux")

            provider.complete.side_effect = capture_complete
            return provider

        compact(
            store, session.id, config, workspace,
            capturing_provider_fn, state_dir=state_dir,
        )

        summary_req = captured_requests[-1]
        system_content = summary_req.messages[0].content
        assert "<previous-summary>" in system_content
        assert "old summary content" in system_content


class TestSessionStoreExtensions:
    def test_message_count(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        assert store.message_count(session.id) == 0
        store.append_message(session.id, "user", "hello")
        assert store.message_count(session.id) == 1
        store.append_message(session.id, "assistant", "hi")
        assert store.message_count(session.id) == 2

    def test_delete_all_messages(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        _populate_session(store, session.id, 10)
        assert store.message_count(session.id) == 10
        store.delete_all_messages(session.id)
        assert store.message_count(session.id) == 0

    def test_backup(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "hello")

        backup_path = tmp_path / "backup" / "sessions.db"
        store.backup(backup_path)
        assert backup_path.exists()

        backup_store = SessionStore(backup_path)
        assert backup_store.message_count(session.id) == 1
        backup_store.close()

    def test_db_path_property(self, tmp_path):
        db = tmp_path / "sessions.db"
        store = SessionStore(db)
        assert store.db_path == db


class TestConcurrentInsertDuringFlush:
    def test_message_inserted_during_flush_is_preserved(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        def flush_that_inserts(*args, **kwargs):
            store.append_message(session.id, "user", "concurrent message")
            return FLUSH_SAVED

        provider = FauxProvider([faux_response(text="## Goal\nsummary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_that_inserts):
            stats = compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        assert not stats.get("aborted")
        history = store.get_history(session.id)
        all_content = " ".join(m.content or "" for m in history)
        assert "concurrent" in all_content


class TestFlushFailurePreservesHead:
    def test_head_preserved_as_text_on_flush_failure(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        def flush_fails(*args, **kwargs):
            return FLUSH_FAILED

        provider = FauxProvider([faux_response(text="## Goal\nsummary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_fails):
            stats = compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        assert not stats.get("aborted")
        history = store.get_history(session.id)
        assert len(history) == 7
        assert history[0].role == "system"
        assert "summary" in history[0].content
        assert history[1].role == "system"
        assert "memory flush failed" in history[1].content
        assert "message 0" in history[1].content


class TestWorkspaceEscape:
    def test_rejects_symlink_escape_via_directory(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        secret_dir = tmp_path / "workspace-secret"
        secret_dir.mkdir()

        (workspace / "escape").symlink_to(secret_dir)

        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "escape/evil.txt",
                "content": "bad",
            }),
        ]
        _execute_flush_writes(tool_calls, workspace)
        assert not (secret_dir / "evil.txt").exists()

    def test_rejects_direct_symlink_to_outside(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("original")

        (workspace / "linked.txt").symlink_to(outside_file)

        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "linked.txt",
                "content": "overwritten",
            }),
        ]
        _execute_flush_writes(tool_calls, workspace)
        assert outside_file.read_text() == "original"


class TestCheckpointAtomic:
    def test_uses_tmp_then_rename(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "hello")

        assert _checkpoint(store, state_dir)

        backups_dir = state_dir / "backups"
        checkpoints = list(backups_dir.glob("checkpoint_*"))
        assert len(checkpoints) == 1
        assert (checkpoints[0] / "sessions.db").exists()
        tmp_dirs = list(backups_dir.glob("*.tmp"))
        assert len(tmp_dirs) == 0

    def test_failure_leaves_no_partial_checkpoint(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = MagicMock()
        store.backup.side_effect = OSError("disk full")

        assert not _checkpoint(store, state_dir)

        backups_dir = state_dir / "backups"
        checkpoints = list(backups_dir.glob("checkpoint_*"))
        assert len(checkpoints) == 0
        tmp_dirs = list(backups_dir.glob("*.tmp"))
        assert len(tmp_dirs) == 0

    def test_rotation_happens_after_create(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")

        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        for i in range(5):
            cp = backups_dir / f"checkpoint_2025{i:02d}01T000000Z"
            cp.mkdir()
            (cp / "sessions.db").write_text("old")

        assert _checkpoint(store, state_dir)

        all_checkpoints = [
            p for p in backups_dir.glob("checkpoint_*") if p.suffix != ".tmp"
        ]
        assert len(all_checkpoints) == 5
        new_ones = [p for p in all_checkpoints if "2025" not in p.name]
        assert len(new_ones) == 1
        assert (new_ones[0] / "sessions.db").exists()
        assert (new_ones[0] / "sessions.db").stat().st_size > 0


class TestEmptyHeadSkipsBeforeCheckpoint:
    def test_checkpoint_not_called_when_head_empty(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(3):
            store.append_message(session.id, "user", f"msg {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=20))

        with patch("faffmonkey.runtime.compaction._checkpoint") as mock_cp:
            stats = compact(
                store, session.id, config, workspace,
                lambda mc: MagicMock(), state_dir=state_dir,
            )

        mock_cp.assert_not_called()
        assert stats.get("skipped") is True
        assert store.message_count(session.id) == 3


class TestMemoryFlushEmptyToolCalls:
    def test_prose_twice_is_a_failure(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([
            faux_response(text="sure, noted"),
            faux_response(text="I have noted it."),
        ])
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert result == FLUSH_FAILED
        provider.assert_exhausted()

    def test_returns_true_with_tool_calls(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([_flush_ok_response()])
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert result == FLUSH_SAVED


class TestFlushWriteCounts:
    def test_all_writes_fail_oserror(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([faux_response(tool_calls=[
            {"name": "file_write", "arguments": {"path": "notes.md", "content": "data"}},
            {"name": "file_write", "arguments": {"path": "log.md", "content": "entry"}},
        ])])
        config = _make_config()

        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            result = memory_flush(store, session.id, workspace, lambda mc: provider, config)

        assert result == FLUSH_FAILED

    def test_zero_file_write_in_tool_calls(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([
            faux_response(tool_calls=[
                {"name": "web_search", "arguments": {"query": "something"}},
            ]),
            faux_response(text="searching now"),
        ])
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert result == FLUSH_FAILED
        provider.assert_exhausted()

    def test_mix_of_success_and_failure(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([faux_response(tool_calls=[
            {"name": "file_write", "arguments": {"path": "good.md", "content": "ok"}},
            {"name": "file_write", "arguments": {"path": "../escape.txt", "content": "bad"}},
        ])])
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert result == FLUSH_FAILED
        assert (workspace / "good.md").read_text() == "ok"
        assert not (tmp_path / "escape.txt").exists()

    def test_happy_path_returns_true(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([faux_response(tool_calls=[
            {"name": "file_write", "arguments": {"path": "MEMORY.md", "content": "noted"}},
        ])])
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert result == FLUSH_SAVED
        assert (workspace / "MEMORY.md").read_text() == "noted"

    def test_execute_flush_writes_returns_counts(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={"path": "a.md", "content": "a"}),
            ToolCall(id="tc2", name="file_write", arguments={"path": "../bad.md", "content": "b"}),
            ToolCall(id="tc3", name="web_search", arguments={"query": "x"}),
        ]
        attempted, succeeded = _execute_flush_writes(tool_calls, workspace)
        assert attempted == 2
        assert succeeded == 1

    def test_execute_flush_writes_empty_list(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        attempted, succeeded = _execute_flush_writes([], workspace)
        assert attempted == 0
        assert succeeded == 0


class TestFindExistingSummaryNoneContent:
    def test_none_content_does_not_crash(self):
        messages = [
            Message(role="system", content=None),
            Message(role="system", content="## Goal\nreal summary"),
            Message(role="user", content=None),
        ]
        result = _find_existing_summary(messages)
        assert result == "## Goal\nreal summary"

    def test_all_none_content(self):
        messages = [
            Message(role="system", content=None),
            Message(role="user", content=None),
        ]
        result = _find_existing_summary(messages)
        assert result is None


class TestPreservedBlobBounds:
    def _setup(self, tmp_path, head_size):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(head_size):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {'x' * 500} {i}")
        for i in range(5):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"tail {i}")
        config = _make_config(compaction=CompactionConfig(protect_last_n=5))
        return store, session, config, workspace, state_dir

    def test_preserved_blob_capped(self, tmp_path):
        store, session, config, workspace, state_dir = self._setup(tmp_path, 200)

        def flush_fails(*args, **kwargs):
            return FLUSH_FAILED

        provider = FauxProvider([faux_response(text="## Goal\nsummary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_fails):
            compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        history = store.get_history(session.id)
        preserved_msg = next(m for m in history if m.content and _PRESERVED_MARKER in m.content)
        blob_content = preserved_msg.content[len(_PRESERVED_MARKER) + 1:]
        assert len(blob_content) <= MAX_PRESERVED_BLOB_BYTES + len("\n[truncated]")

    def test_repeated_flush_failures_do_not_nest(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")

        store.append_message(session.id, "system", "## Goal\nold summary")
        store.append_message(
            session.id, "system",
            f"{_PRESERVED_MARKER}\n[user]: old preserved content",
        )
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        def flush_fails(*args, **kwargs):
            return FLUSH_FAILED

        provider = FauxProvider([faux_response(text="## Goal\nnew summary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_fails):
            compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        history = store.get_history(session.id)
        preserved_msgs = [m for m in history if m.content and _PRESERVED_MARKER in m.content]
        assert len(preserved_msgs) == 1
        assert "old preserved content" not in preserved_msgs[0].content


class TestExecuteFlushWritesContentCap:
    def test_rejects_oversized_content(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        big_content = "x" * (MAX_FLUSH_CONTENT_BYTES + 1)
        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "big.md",
                "content": big_content,
            }),
        ]
        _execute_flush_writes(tool_calls, workspace)
        assert not (workspace / "big.md").exists()

    def test_accepts_content_at_limit(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        content = "x" * MAX_FLUSH_CONTENT_BYTES
        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "ok.md",
                "content": content,
            }),
        ]
        _execute_flush_writes(tool_calls, workspace)
        assert (workspace / "ok.md").exists()


class TestConcurrentWriteDuringSummarisation:
    def test_concurrent_write_not_blocked(self, tmp_path):
        """Verify that appending a message while _summarise runs is not blocked."""
        import threading

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        summarise_entered = threading.Event()
        write_completed = threading.Event()

        def slow_summarise(messages, pf, cfg):
            summarise_entered.set()
            assert write_completed.wait(timeout=2.0), (
                "concurrent write was blocked by summarisation"
            )
            return "## Goal\nsummary from slow summarise"

        def concurrent_writer():
            summarise_entered.wait(timeout=2.0)
            writer_store = SessionStore(state_dir / "sessions.db")
            writer_store.append_message(session.id, "user", "concurrent msg")
            writer_store.close()
            write_completed.set()

        writer_thread = threading.Thread(target=concurrent_writer)
        writer_thread.start()

        with patch("faffmonkey.runtime.compaction._summarise", side_effect=slow_summarise):
            stats = compact(
                store, session.id, config, workspace,
                lambda mc: FauxProvider([_flush_ok_response()]),
                state_dir=state_dir,
            )

        writer_thread.join(timeout=3.0)
        assert not writer_thread.is_alive()

        assert stats.get("aborted") is True
        assert stats["reason"] == "concurrent_modification"

        final_history = store.get_history(session.id)
        assert len(final_history) == 31


class TestDeterministicTruncateContextWindow:
    def test_small_context_window_uses_floor(self):
        messages = [Message(role="user", content="hello " * 5000)]
        result = _deterministic_truncate(messages, context_window=1000)
        assert "[WARNING:" in result
        truncated_body = result.split("\n", 1)[1].split("\n\n[Earlier")[0]
        assert len(truncated_body) <= 4096

    def test_large_context_window_uses_proportion(self):
        messages = [Message(role="user", content="hello " * 50000)]
        result_large = _deterministic_truncate(messages, context_window=200000)
        result_small = _deterministic_truncate(messages, context_window=50000)
        assert len(result_large) > len(result_small)


class TestCheapFallbackTier:
    def _messages(self):
        return [Message(role="user", content=f"msg {i}") for i in range(20)]

    def test_tier1_succeeds_cheap_never_called(self):
        config = _make_config()
        calls: list[str] = []

        def provider_fn(mc):
            calls.append(mc.model)
            return FauxProvider([faux_response(text="tier 1 result")])

        result = _summarise(self._messages(), provider_fn, config)
        assert result == "tier 1 result"
        assert calls == ["faux-main"]

    def test_tier1_fails_cheap_succeeds(self):
        config = _make_config()
        calls: list[str] = []

        def provider_fn(mc):
            calls.append(mc.model)
            if mc.model == "faux-main":
                raise RuntimeError("tier 1 down")
            return FauxProvider([faux_response(text="cheap result")])

        result = _summarise(self._messages(), provider_fn, config)
        assert result == "cheap result"
        assert "faux-main" in calls
        assert "faux-cheap" in calls

    def test_same_model_skips_cheap(self):
        same = ModelConfig(
            provider="faux", model="faux-main",
            base_url="http://localhost", api_key="",
        )
        config = _make_config(models={"main": same, "cheap": same})
        calls: list[str] = []

        def provider_fn(mc):
            calls.append(mc.model)
            raise RuntimeError("always fails")

        result = _summarise(self._messages(), provider_fn, config)
        assert result is None
        assert calls == ["faux-main"]

    def test_both_fail_returns_none(self):
        config = _make_config()

        def provider_fn(mc):
            raise RuntimeError("all down")

        result = _summarise(self._messages(), provider_fn, config)
        assert result is None


class TestCheckpointNoCollision:
    def test_two_rapid_checkpoints_have_distinct_names(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "hello")

        path_a = _checkpoint(store, state_dir)
        path_b = _checkpoint(store, state_dir)

        assert path_a is not None
        assert path_b is not None
        assert path_a.name != path_b.name
        assert (path_a / "sessions.db").exists()
        assert (path_b / "sessions.db").exists()


class TestCorruptCheckpointNotPromoted:
    def test_empty_db_in_tmp_is_rejected(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        store = MagicMock()

        def write_empty_db(dest_path):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"")

        store.backup.side_effect = write_empty_db

        result = _checkpoint(store, state_dir)
        assert result is None

        backups_dir = state_dir / "backups"
        checkpoints = [
            p for p in backups_dir.glob("checkpoint_*") if p.suffix != ".tmp"
        ]
        assert len(checkpoints) == 0
        tmp_dirs = list(backups_dir.glob("*.tmp"))
        assert len(tmp_dirs) == 0


class TestStaleTmpPruned:
    def test_old_tmp_dirs_removed_on_next_checkpoint(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "hello")

        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        stale_tmp = backups_dir / "checkpoint_20250101T000000Z-aaaaaa.tmp"
        stale_tmp.mkdir()
        (stale_tmp / "sessions.db").write_text("stale")

        old_time = time.time() - 600
        os.utime(str(stale_tmp), (old_time, old_time))

        result = _checkpoint(store, state_dir)
        assert result is not None
        assert not stale_tmp.exists()

    def test_recent_tmp_dirs_not_removed(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "hello")

        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        fresh_tmp = backups_dir / "checkpoint_20250101T000000Z-bbbbbb.tmp"
        fresh_tmp.mkdir()
        (fresh_tmp / "sessions.db").write_text("in progress")

        result = _checkpoint(store, state_dir)
        assert result is not None
        assert fresh_tmp.exists()


class TestTimestampPreservation:
    def test_reappended_messages_keep_original_timestamps(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")

        history_before = store.get_history(session.id)
        original_timestamps = [m.timestamp for m in history_before[-5:]]
        assert all(ts is not None for ts in original_timestamps)

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))
        provider = FauxProvider([
            _flush_ok_response(),
            faux_response(text="## Goal\nsummary"),
        ])

        compact(
            store, session.id, config, workspace,
            lambda mc: provider, state_dir=state_dir,
        )

        history_after = store.get_history(session.id)
        tail_after = history_after[1:]
        assert len(tail_after) == 5
        for msg, orig_ts in zip(tail_after, original_timestamps):
            assert msg.timestamp == orig_ts


class TestOrphanedToolMessages:
    def test_orphaned_tool_messages_stripped(self):
        tail = [
            Message(role="user", content="query"),
            Message(role="tool", content="result", tool_call_id="tc_orphan"),
            Message(role="assistant", content="answer"),
            Message(role="user", content="thanks"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 3
        assert not any(m.tool_call_id == "tc_orphan" for m in result)

    def test_matched_tool_messages_kept(self):
        tail = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="search", arguments={}),
            ]),
            Message(role="tool", content="found it", tool_call_id="tc1"),
            Message(role="user", content="great"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 3

    def test_mixed_orphaned_and_matched(self):
        tail = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="search", arguments={}),
            ]),
            Message(role="tool", content="search result", tool_call_id="tc1"),
            Message(role="tool", content="orphan result", tool_call_id="tc_gone"),
            Message(role="assistant", content="done"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 3
        assert any(m.tool_call_id == "tc1" for m in result)
        assert not any(m.tool_call_id == "tc_gone" for m in result)

    def test_no_tool_messages_unchanged(self):
        tail = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 2


class TestFlushRefusesOverwrite:
    def test_memory_file_is_appended_never_replaced(self, tmp_path):
        """MEMORY.md must be updatable, and must not be destroyable.

        This previously asserted the write was refused outright, which meant
        MEMORY.md could be written once and never updated: the flush's whole
        purpose failed from the second run onward. Appending keeps the
        feature working while preserving the property the old assertion
        cared about, that injected content cannot erase what is there.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "MEMORY.md").write_text("original content")

        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "MEMORY.md",
                "content": "newly learned fact",
            }),
        ]
        attempted, succeeded = _execute_flush_writes(tool_calls, workspace)
        assert attempted == 1
        assert succeeded == 1
        written = (workspace / "MEMORY.md").read_text()
        assert "original content" in written
        assert "newly learned fact" in written

    def test_unlisted_existing_file_still_refused(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "notes.md").write_text("original content")

        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "notes.md",
                "content": "attacker content",
            }),
        ]
        attempted, succeeded = _execute_flush_writes(tool_calls, workspace)
        assert attempted == 1
        assert succeeded == 0
        assert (workspace / "notes.md").read_text() == "original content"

    def test_new_file_created(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool_calls = [
            ToolCall(id="tc1", name="file_write", arguments={
                "path": "new_note.md",
                "content": "fresh content",
            }),
        ]
        attempted, succeeded = _execute_flush_writes(tool_calls, workspace)
        assert attempted == 1
        assert succeeded == 1
        assert (workspace / "new_note.md").read_text() == "fresh content"

    def test_refused_write_counted_as_failure_in_flush(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Not one of the flush's own files, so still create-only.
        (workspace / "notes.md").write_text("existing")

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "remember this")

        provider = FauxProvider([faux_response(tool_calls=[
            {"name": "file_write", "arguments": {"path": "notes.md", "content": "overwrite"}},
        ])])
        config = _make_config()

        result = memory_flush(store, session.id, workspace, lambda mc: provider, config)
        assert result == FLUSH_FAILED
        assert (workspace / "notes.md").read_text() == "existing"


class TestOrphanedToolCalls:
    def test_assistant_with_tool_calls_no_results_gets_stubs(self):
        tail = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="file_read", arguments={}),
            ]),
            Message(role="user", content="thanks"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 3
        synthetic = [m for m in result if m.role == "tool" and m.tool_call_id == "tc1"]
        assert len(synthetic) == 1
        assert "lost" in synthetic[0].content

    def test_partial_results_stubs_only_missing(self):
        tail = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="file_read", arguments={}),
                ToolCall(id="tc2", name="web_search", arguments={}),
            ]),
            Message(role="tool", content="file contents", tool_call_id="tc1"),
            Message(role="user", content="ok"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 4
        tc2_results = [m for m in result if m.role == "tool" and m.tool_call_id == "tc2"]
        assert len(tc2_results) == 1
        assert "lost" in tc2_results[0].content
        tc1_results = [m for m in result if m.role == "tool" and m.tool_call_id == "tc1"]
        assert len(tc1_results) == 1
        assert tc1_results[0].content == "file contents"

    def test_all_results_present_no_stubs(self):
        tail = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="file_read", arguments={}),
            ]),
            Message(role="tool", content="data", tool_call_id="tc1"),
            Message(role="user", content="great"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert len(result) == 3
        assert result[1].content == "data"

    def test_mixed_orphaned_results_and_orphaned_calls(self):
        tail = [
            Message(role="tool", content="orphan result", tool_call_id="tc_gone"),
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc1", name="search", arguments={}),
            ]),
            Message(role="user", content="done"),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert not any(m.tool_call_id == "tc_gone" for m in result)
        tc1_results = [m for m in result if m.role == "tool" and m.tool_call_id == "tc1"]
        assert len(tc1_results) == 1
        assert "lost" in tc1_results[0].content

    def test_orphaned_tool_call_in_compact_produces_valid_session(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(20):
            store.append_message(session.id, "user", f"padding {i}")

        tc = [ToolCall(id="tc_crash", name="shell_exec", arguments={})]
        store.append_message(session.id, "assistant", "running", tool_calls=tc)
        store.append_message(session.id, "user", "what happened?")
        store.append_message(session.id, "assistant", "not sure")
        store.append_message(session.id, "user", "try again")

        config = _make_config(compaction=CompactionConfig(protect_last_n=4))
        provider = FauxProvider([
            _flush_ok_response(),
            faux_response(text="## Goal\nsummary"),
        ])

        compact(
            store, session.id, config, workspace,
            lambda mc: provider, state_dir=state_dir,
        )

        history = store.get_history(session.id)
        for msg in history:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    results = [m for m in history if m.tool_call_id == tc.id]
                    assert len(results) == 1, (
                        f"tool_call {tc.id} has {len(results)} results, expected 1"
                    )


class TestTruncatedCheckpointProtection:
    def test_marked_checkpoint_not_pruned(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")

        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        for i in range(5):
            cp = backups_dir / f"checkpoint_2025{i:02d}01T000000Z"
            cp.mkdir()
            (cp / "sessions.db").write_text("old")

        marked = backups_dir / "checkpoint_20250001T000000Z"
        (marked / _TRUNCATED_DATA_MARKER).write_text(marked.name)

        _checkpoint(store, state_dir)

        assert marked.exists()
        assert (marked / _TRUNCATED_DATA_MARKER).exists()

    def test_unmarked_checkpoint_still_pruned(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = SessionStore(state_dir / "sessions.db")

        backups_dir = state_dir / "backups"
        backups_dir.mkdir()
        for i in range(6):
            cp = backups_dir / f"checkpoint_2025{i:02d}01T000000Z"
            cp.mkdir()
            (cp / "sessions.db").write_text("old")

        _checkpoint(store, state_dir)

        all_cps = [
            p for p in backups_dir.glob("checkpoint_*") if p.suffix != ".tmp"
        ]
        assert len(all_cps) <= 5 + 1

    def test_truncated_blob_writes_marker_to_checkpoint(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {'x' * 500} {i}")
        for i in range(5):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"tail {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        def flush_fails(*args, **kwargs):
            return FLUSH_FAILED

        provider = FauxProvider([faux_response(text="## Goal\nsummary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_fails):
            compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        backups_dir = state_dir / "backups"
        checkpoints = [
            p for p in backups_dir.glob("checkpoint_*") if p.suffix != ".tmp"
        ]
        assert len(checkpoints) == 1
        assert (checkpoints[0] / _TRUNCATED_DATA_MARKER).exists()

    def test_small_blob_no_marker(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"message {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        def flush_fails(*args, **kwargs):
            return FLUSH_FAILED

        provider = FauxProvider([faux_response(text="## Goal\nsummary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_fails):
            compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        backups_dir = state_dir / "backups"
        checkpoints = [
            p for p in backups_dir.glob("checkpoint_*") if p.suffix != ".tmp"
        ]
        assert len(checkpoints) == 1
        assert not (checkpoints[0] / _TRUNCATED_DATA_MARKER).exists()

    def test_marked_checkpoint_survives_multiple_compactions(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")

        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"msg {'x' * 500} {i}")
        for i in range(5):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"tail {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        def flush_fails(*args, **kwargs):
            return FLUSH_FAILED

        provider = FauxProvider([faux_response(text="## Goal\nfirst summary")])

        with patch("faffmonkey.runtime.compaction.memory_flush", side_effect=flush_fails):
            compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        backups_dir = state_dir / "backups"
        marked_cps = [
            p for p in backups_dir.glob("checkpoint_*")
            if p.suffix != ".tmp" and (p / _TRUNCATED_DATA_MARKER).exists()
        ]
        assert len(marked_cps) == 1
        marked_name = marked_cps[0].name

        for run in range(6):
            for i in range(30):
                role = "user" if i % 2 == 0 else "assistant"
                store.append_message(session.id, role, f"round {run} msg {i}")
            provider = FauxProvider([
                _flush_ok_response(),
                faux_response(text=f"## Goal\nsummary round {run}"),
            ])
            compact(
                store, session.id, config, workspace,
                lambda mc: provider, state_dir=state_dir,
            )

        assert (backups_dir / marked_name).exists()
        assert (backups_dir / marked_name / _TRUNCATED_DATA_MARKER).exists()


class TestPartialFlushPreservesBlob:
    def test_partial_failure_preserves_head(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # An existing file the flush does not own, so the write is refused
        # and the flush is genuinely partial. This used to use MEMORY.md,
        # which is now appended to rather than refused, so the flush
        # succeeded and there was nothing partial left to preserve.
        (workspace / "notes.md").write_text("existing notes")

        store = SessionStore(state_dir / "sessions.db")
        session = store.get_or_create_main_session("test")
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message(session.id, role, f"important fact {i}")

        config = _make_config(compaction=CompactionConfig(protect_last_n=5))

        provider = FauxProvider([
            faux_response(tool_calls=[
                {"name": "file_write", "arguments": {"path": "new_note.md", "content": "ok"}},
                {"name": "file_write", "arguments": {"path": "notes.md", "content": "overwrite"}},
            ]),
            faux_response(text="## Goal\nsummary after partial flush"),
        ])

        stats = compact(
            store, session.id, config, workspace,
            lambda mc: provider, state_dir=state_dir,
        )

        assert not stats.get("aborted")
        history = store.get_history(session.id)
        preserved_msgs = [m for m in history if m.content and _PRESERVED_MARKER in m.content]
        assert len(preserved_msgs) == 1
        assert "important fact 0" in preserved_msgs[0].content


class TestOrphanStubOrdering:
    """P4-M2: the repair produced exactly the invalid sequence it prevents."""

    def test_stub_stays_next_to_the_call_it_answers(self, tmp_path):
        store = _make_store(tmp_path)
        session = store.get_or_create_main_session("test")
        store.append_message(session.id, "user", "go")
        store.append_message(
            session.id, "assistant", "running",
            [ToolCall(id="tc_crash", name="shell_exec", arguments={})],
        )
        store.append_message(session.id, "user", "what happened?")
        store.append_message(session.id, "assistant", "not sure")
        store.append_message(session.id, "user", "try again")

        tail = _strip_orphaned_tool_messages(store.get_history(session.id))
        store.delete_all_messages(session.id)
        for msg in tail:
            store.append_message(
                session.id, msg.role, msg.content or None,
                msg.tool_calls, msg.tool_call_id, timestamp=msg.timestamp,
            )

        history = store.get_history(session.id)
        stub_index = next(
            i for i, m in enumerate(history) if m.tool_call_id == "tc_crash"
        )
        assert history[stub_index - 1].role == "assistant"
        assert history[stub_index - 1].tool_calls is not None
        assert stub_index < len(history) - 1
        store.close()

    def test_stub_without_a_timestamp_is_left_alone(self):
        tail = [
            Message(role="assistant", tool_calls=[
                ToolCall(id="tc", name="f", arguments={}),
            ]),
        ]
        result = _strip_orphaned_tool_messages(tail)
        assert result[1].timestamp is None


class TestCheapTierResolvesTheSlot:
    """P4-M4: resolve_model("cheap") looked up a routing task that never exists."""

    def test_tier_two_runs_on_the_cheap_slot(self):
        calls: list[str] = []

        def provider_fn(mc):
            calls.append(mc.model)
            if mc.model == "faux-main":
                raise RuntimeError("tier 1 down")
            provider = MagicMock()
            provider.complete.return_value = CompletionResponse(
                text="cheap summary", model=mc.model,
            )
            return provider

        config = _make_config()
        assert "cheap" not in config.routing

        result = _summarise(
            [Message(role="user", content="hello")], provider_fn, config,
        )

        assert result == "cheap summary"
        assert calls == ["faux-main", "faux-cheap"]

    def test_missing_cheap_slot_falls_through(self):
        config = _make_config(models={
            "main": ModelConfig(
                provider="faux", model="faux-main",
                base_url="http://localhost", api_key="",
            ),
        })

        def provider_fn(mc):
            raise RuntimeError("down")

        assert _summarise([Message(role="user", content="x")], provider_fn, config) is None


class TestDailyNote:
    """2026-08-25: a full day of conversation produced no daily-log entry.
    AGENTS.md asked the agent to append as it went and it never did; the
    evening job that should have caught it failed on a provider error; and
    when asked directly it wrote into yesterday's file. The loop now owns
    the recording."""

    def _session(self, tmp_path):
        from faffmonkey.config import DailyNoteConfig
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        store = SessionStore(tmp_path / "state" / "sessions.db")
        session = store.get_or_create_main_session("test")
        config = _make_config(
            timezone=ZoneInfo("Asia/Bangkok"),
            routing={"conversation": "main", "compaction": "cheap"},
            daily_note=DailyNoteConfig(every_turns=3, every_minutes=60),
        )
        return workspace, store, session, config

    def _note_response(self, content):
        return faux_response(tool_calls=[{
            "name": "daily_note", "arguments": {"content": content},
        }])

    def test_appends_to_today_in_the_configured_timezone(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note
        workspace, store, session, config = self._session(tmp_path)
        store.append_message(session.id, "user", "the deadline moved to Friday")
        store.append_message(session.id, "assistant", "noted")
        provider = FauxProvider([self._note_response("Deadline moved to Friday.")])

        # 17:20 UTC on the 24th is 00:20 on the 25th in Bangkok.
        fixed = datetime(2026, 8, 25, 0, 20, tzinfo=ZoneInfo("Asia/Bangkok"))
        with patch("faffmonkey.runtime.compaction.datetime") as dt:
            dt.now.return_value = fixed
            dt.fromisoformat = datetime.fromisoformat
            assert daily_note(store, session.id, workspace, lambda mc: provider, config)

        daily = workspace / "memory" / "daily"
        assert sorted(p.name for p in daily.iterdir()) == ["2026-08-25.md"]
        text = (daily / "2026-08-25.md").read_text()
        assert text.startswith("# 2026-08-25\n")
        assert "- 00:20 Deadline moved to Friday." in text
        assert provider.calls[0].model == "faux-cheap"

    def test_model_never_chooses_the_path(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note
        workspace, store, session, config = self._session(tmp_path)
        store.append_message(session.id, "user", "remember this")
        provider = FauxProvider([faux_response(tool_calls=[
            {"name": "file_write", "arguments": {"path": "MEMORY.md", "content": "x"}},
            {"name": "file_write", "arguments": {"path": "memory/daily/2026-08-24.md", "content": "x"}},
            {"name": "daily_note", "arguments": {"content": "kept"}},
        ])])

        assert daily_note(store, session.id, workspace, lambda mc: provider, config)

        assert not (workspace / "MEMORY.md").exists()
        assert not (workspace / "memory" / "daily" / "2026-08-24.md").exists()
        written = list((workspace / "memory" / "daily").iterdir())
        assert len(written) == 1 and "kept" in written[0].read_text()

    def test_existing_log_is_only_ever_appended_to(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note
        workspace, store, session, config = self._session(tmp_path)
        today = datetime.now(config.timezone).date().isoformat()
        log = workspace / "memory" / "daily" / f"{today}.md"
        log.parent.mkdir(parents=True)
        log.write_text(f"# {today}\n\nMorning message sent 07:30\n")
        store.append_message(session.id, "user", "hello")
        provider = FauxProvider([self._note_response("Said hello.")])

        daily_note(store, session.id, workspace, lambda mc: provider, config)

        text = log.read_text()
        assert text.startswith(f"# {today}\n\nMorning message sent 07:30\n")
        assert text.rstrip().endswith("Said hello.")

    def test_nothing_worth_keeping_advances_the_cursor_without_writing(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note, daily_note_due
        workspace, store, session, config = self._session(tmp_path)
        for i in range(3):
            store.append_message(session.id, "user", f"small talk {i}")
        assert daily_note_due(store, session.id, config.daily_note)
        provider = FauxProvider([faux_response(text="nothing to keep")])

        assert daily_note(store, session.id, workspace, lambda mc: provider, config)

        assert not (workspace / "memory").exists()
        assert store.daily_note_at(session.id) is not None
        assert not daily_note_due(store, session.id, config.daily_note)

    def test_provider_failure_leaves_the_cursor_for_a_retry(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note
        workspace, store, session, config = self._session(tmp_path)
        store.append_message(session.id, "user", "hello")
        failing = MagicMock()
        failing.complete.side_effect = RuntimeError("connection error")

        assert daily_note(store, session.id, workspace, lambda mc: failing, config) is False
        assert store.daily_note_at(session.id) is None

    def test_idle_session_is_never_due(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note_due
        _, store, session, config = self._session(tmp_path)
        assert not daily_note_due(store, session.id, config.daily_note)
        store.append_message(session.id, "assistant", "cron delivery with no user turn")
        assert not daily_note_due(store, session.id, config.daily_note)

    def test_due_after_enough_turns_or_enough_time(self, tmp_path):
        from datetime import timezone as tz
        from faffmonkey.runtime.compaction import daily_note_due
        _, store, session, config = self._session(tmp_path)
        now = datetime(2026, 8, 25, 12, 0, tzinfo=tz.utc)

        store.append_message(
            session.id, "user", "one",
            timestamp=(now - timedelta(minutes=30)).isoformat(),
        )
        assert not daily_note_due(store, session.id, config.daily_note, now=now)
        assert daily_note_due(
            store, session.id, config.daily_note, now=now + timedelta(minutes=31),
        )

        store.append_message(session.id, "user", "two", timestamp=(now - timedelta(minutes=20)).isoformat())
        store.append_message(session.id, "user", "three", timestamp=(now - timedelta(minutes=10)).isoformat())
        assert daily_note_due(store, session.id, config.daily_note, now=now)

    def test_only_conversation_since_the_last_note_is_sent(self, tmp_path):
        from faffmonkey.runtime.compaction import daily_note
        workspace, store, session, config = self._session(tmp_path)
        store.append_message(session.id, "user", "first hour")
        provider = FauxProvider([
            faux_response(text="nothing"), faux_response(text="nothing"),
        ])
        daily_note(store, session.id, workspace, lambda mc: provider, config)
        store.append_message(session.id, "user", "second hour")
        store.append_message(
            session.id, "tool", "tool output", tool_call_id="c1",
        )

        daily_note(store, session.id, workspace, lambda mc: provider, config)

        sent = [m.content for m in provider.calls[1].messages if m.role != "system"]
        assert sent == ["second hour"]
