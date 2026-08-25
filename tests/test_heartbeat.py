import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


from faffmonkey.config import CompactionConfig, Config, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.scheduler import CronJob, Scheduler, _load_triggers, _run_heartbeat
from faffmonkey.types import CompletionResponse

import importlib


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "watchdog",
        Path(__file__).resolve().parent.parent
        / "templates" / "workspace" / "skills" / "heartbeat" / "scripts" / "run.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watchdog = _load_watchdog()


def _make_config(**overrides) -> Config:
    defaults = {
        "models": {
            "main": ModelConfig(
                provider="test", model="test-model",
                base_url="http://localhost:11434/v1", api_key="",
            ),
        },
        "routing": {"conversation": "main", "cron_default": "main"},
        "fallback_models": [],
        "timezone": ZoneInfo("UTC"),
        "heartbeat": HeartbeatConfig(active_hours=(0, 24)),
        "compaction": CompactionConfig(),
        "channels": {},
        "tool_permissions": {},
    }
    defaults.update(overrides)
    return Config(**defaults)


# -- watchdog checks --

class TestWatchdogConfig:
    def test_default_config(self, tmp_path):
        config = watchdog.load_config(tmp_path)
        assert config["morning_deadline_hour"] == 8
        assert config["learnings_max_entries"] == 30
        assert config["carryover_stale_days"] == 7

    def test_custom_config(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({
            "morning_deadline_hour": 10,
            "learnings_max_entries": 50,
        }))
        config = watchdog.load_config(tmp_path)
        assert config["morning_deadline_hour"] == 10
        assert config["learnings_max_entries"] == 50
        assert config["carryover_stale_days"] == 7

    def test_invalid_json_uses_defaults(self, tmp_path):
        (tmp_path / "config.json").write_text("not json")
        config = watchdog.load_config(tmp_path)
        assert config == watchdog.DEFAULT_CONFIG


class TestWatchdogYesterdayMemory:
    def test_creates_missing_memory_file(self, tmp_path):
        tz = ZoneInfo("UTC")
        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        triggers, fixed = watchdog.check_yesterday_memory(tmp_path, tz)
        memory_file = tmp_path / "memory" / "daily" / f"{yesterday.isoformat()}.md"
        assert memory_file.exists()
        assert f"# {yesterday.isoformat()}" in memory_file.read_text()
        assert len(fixed) == 1
        assert "created missing memory file" in fixed[0]
        assert len(triggers) == 0

    def test_existing_memory_file_untouched(self, tmp_path):
        tz = ZoneInfo("UTC")
        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        memory_dir = tmp_path / "memory" / "daily"
        memory_dir.mkdir(parents=True)
        memory_file = memory_dir / f"{yesterday.isoformat()}.md"
        memory_file.write_text("# existing content")
        triggers, fixed = watchdog.check_yesterday_memory(tmp_path, tz)
        assert memory_file.read_text() == "# existing content"
        assert len(fixed) == 0
        assert len(triggers) == 0


class TestMorningMissedIsRaisedOnceADay:
    """With no morning job the trigger was true on every hourly tick after
    08:00, so the heartbeat escalated and told the user about the missed
    morning every hour until midnight."""

    def test_second_tick_on_the_same_day_is_quiet(self, tmp_path):
        skill_data = tmp_path / "skills-data" / "heartbeat"
        found = ["morning_missed: no morning routine stamp after 08:00"]
        assert watchdog._once_a_day(found, skill_data, "morning_missed", "2026-08-23") == found
        assert watchdog._once_a_day(found, skill_data, "morning_missed", "2026-08-23") == []
        assert watchdog._once_a_day(found, skill_data, "morning_missed", "2026-08-24") == found

    def test_nothing_found_writes_nothing(self, tmp_path):
        skill_data = tmp_path / "skills-data" / "heartbeat"
        assert watchdog._once_a_day([], skill_data, "morning_missed", "2026-08-23") == []
        assert not (skill_data / "reported.json").exists()


class TestWatchdogMorningStamp:
    def test_flags_morning_missed_after_deadline(self, tmp_path):
        tz = ZoneInfo("UTC")
        today = datetime.now(tz).date()
        memory_dir = tmp_path / "memory" / "daily"
        memory_dir.mkdir(parents=True)
        (memory_dir / f"{today.isoformat()}.md").write_text("# just a date")
        triggers = watchdog.check_morning_stamp(tmp_path, tz, deadline_hour=0)
        assert len(triggers) == 1
        assert "morning_missed" in triggers[0]

    def test_no_flag_before_deadline(self, tmp_path):
        tz = ZoneInfo("UTC")
        triggers = watchdog.check_morning_stamp(tmp_path, tz, deadline_hour=25)
        assert len(triggers) == 0

    def test_no_flag_when_stamp_present(self, tmp_path):
        tz = ZoneInfo("UTC")
        today = datetime.now(tz).date()
        memory_dir = tmp_path / "memory" / "daily"
        memory_dir.mkdir(parents=True)
        (memory_dir / f"{today.isoformat()}.md").write_text("# Morning routine done")
        triggers = watchdog.check_morning_stamp(tmp_path, tz, deadline_hour=0)
        assert len(triggers) == 0

    def test_flags_when_no_today_file(self, tmp_path):
        tz = ZoneInfo("UTC")
        (tmp_path / "memory" / "daily").mkdir(parents=True)
        triggers = watchdog.check_morning_stamp(tmp_path, tz, deadline_hour=0)
        assert len(triggers) == 1
        assert "morning_missed" in triggers[0]


class TestWatchdogCarryoverStale:
    def test_flags_stale_items(self, tmp_path):
        tz = ZoneInfo("UTC")
        queue_dir = tmp_path / "skills-data" / "carry-over"
        queue_dir.mkdir(parents=True)
        old_ts = (datetime.now(tz) - timedelta(days=10)).isoformat()
        queue = [{"status": "pending", "timestamp": old_ts, "message": "old item"}]
        (queue_dir / "queue.json").write_text(json.dumps(queue))
        triggers = watchdog.check_carryover_stale(tmp_path, tz, stale_days=7)
        assert len(triggers) == 1
        assert "carryover_stale" in triggers[0]

    def test_no_flag_for_fresh_items(self, tmp_path):
        tz = ZoneInfo("UTC")
        queue_dir = tmp_path / "skills-data" / "carry-over"
        queue_dir.mkdir(parents=True)
        fresh_ts = datetime.now(tz).isoformat()
        queue = [{"status": "pending", "timestamp": fresh_ts, "message": "fresh"}]
        (queue_dir / "queue.json").write_text(json.dumps(queue))
        triggers = watchdog.check_carryover_stale(tmp_path, tz, stale_days=7)
        assert len(triggers) == 0

    def test_ignores_delivered_items(self, tmp_path):
        tz = ZoneInfo("UTC")
        queue_dir = tmp_path / "skills-data" / "carry-over"
        queue_dir.mkdir(parents=True)
        old_ts = (datetime.now(tz) - timedelta(days=10)).isoformat()
        queue = [{"status": "delivered", "timestamp": old_ts, "message": "done"}]
        (queue_dir / "queue.json").write_text(json.dumps(queue))
        triggers = watchdog.check_carryover_stale(tmp_path, tz, stale_days=7)
        assert len(triggers) == 0

    def test_no_queue_file(self, tmp_path):
        tz = ZoneInfo("UTC")
        triggers = watchdog.check_carryover_stale(tmp_path, tz, stale_days=7)
        assert len(triggers) == 0


class TestWatchdogLearningsFull:
    def test_flags_over_threshold(self, tmp_path):
        lines = ["# LEARNINGS\n"] + [f"- learning {i}\n" for i in range(35)]
        (tmp_path / "LEARNINGS.md").write_text("".join(lines))
        triggers = watchdog.check_learnings_full(tmp_path, max_entries=30)
        assert len(triggers) == 1
        assert "learnings_full" in triggers[0]
        assert "35" in triggers[0]

    def test_counts_self_review_entries(self, tmp_path):
        """self-review's add writes the TEMPLATES.md heading format, not
        bullets, so a file it filled counted as zero entries."""
        entry = (
            "## [LRN-20260824-{n:03d}] label\n**Status**: pending\n"
            "**Priority**: low\n**Area**: docs\n**Summary**: one line\n\n"
        )
        text = "# LEARNINGS\n\n" + "".join(entry.format(n=i) for i in range(31))
        (tmp_path / "LEARNINGS.md").write_text(text)
        triggers = watchdog.check_learnings_full(tmp_path, max_entries=30)
        assert len(triggers) == 1 and "31" in triggers[0]

    def test_no_flag_under_threshold(self, tmp_path):
        lines = ["# LEARNINGS\n"] + [f"- learning {i}\n" for i in range(10)]
        (tmp_path / "LEARNINGS.md").write_text("".join(lines))
        triggers = watchdog.check_learnings_full(tmp_path, max_entries=30)
        assert len(triggers) == 0

    def test_no_learnings_file(self, tmp_path):
        triggers = watchdog.check_learnings_full(tmp_path, max_entries=30)
        assert len(triggers) == 0

    def test_exact_threshold_no_flag(self, tmp_path):
        lines = ["# LEARNINGS\n"] + [f"- learning {i}\n" for i in range(30)]
        (tmp_path / "LEARNINGS.md").write_text("".join(lines))
        triggers = watchdog.check_learnings_full(tmp_path, max_entries=30)
        assert len(triggers) == 0


class TestWatchdogRunFull:
    def test_writes_clean_triggers(self, tmp_path):
        tz = ZoneInfo("UTC")
        fixed_time = datetime(2025, 6, 15, 6, 0, 0, tzinfo=tz)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        skill_data = tmp_path / "skill_data"

        yesterday = (fixed_time - timedelta(days=1)).date()
        memory_dir = workspace / "memory" / "daily"
        memory_dir.mkdir(parents=True)
        (memory_dir / f"{yesterday.isoformat()}.md").write_text("# exists")

        with patch.object(watchdog, "datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time
            result = watchdog.run_watchdog(workspace, skill_data, tz)
        assert result["status"] == "clean"
        assert result["triggers"] == []
        triggers_path = skill_data / "triggers.json"
        assert triggers_path.exists()
        saved = json.loads(triggers_path.read_text())
        assert saved["status"] == "clean"

    def test_writes_attention_triggers(self, tmp_path):
        tz = ZoneInfo("UTC")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        skill_data = tmp_path / "skill_data"

        lines = ["# L\n"] + [f"- item {i}\n" for i in range(40)]
        (workspace / "LEARNINGS.md").write_text("".join(lines))

        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        memory_dir = workspace / "memory" / "daily"
        memory_dir.mkdir(parents=True)
        (memory_dir / f"{yesterday.isoformat()}.md").write_text("# exists")

        result = watchdog.run_watchdog(workspace, skill_data, tz)
        assert result["status"] == "attention"
        assert any("learnings_full" in t for t in result["triggers"])


# -- triggers loading --

class TestLoadTriggers:
    def test_loads_valid_triggers(self, tmp_path):
        triggers_dir = tmp_path / "skills-data" / "heartbeat"
        triggers_dir.mkdir(parents=True)
        data = {"status": "attention", "triggers": ["test trigger"]}
        (triggers_dir / "triggers.json").write_text(json.dumps(data))
        result = _load_triggers(tmp_path)
        assert result is not None
        assert result["status"] == "attention"

    def test_returns_none_when_missing(self, tmp_path):
        assert _load_triggers(tmp_path) is None

    def test_returns_none_on_bad_json(self, tmp_path):
        triggers_dir = tmp_path / "skills-data" / "heartbeat"
        triggers_dir.mkdir(parents=True)
        (triggers_dir / "triggers.json").write_text("not json")
        assert _load_triggers(tmp_path) is None


# -- heartbeat scheduler context --

class TestHeartbeatContext:
    def _make_scheduler(self, tmp_path, provider_response="test output"):
        config = _make_config()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "config").mkdir()
        (workspace / "SOUL.md").write_text("You are a test agent.")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(
            text=provider_response, model="test",
        )

        scheduler = Scheduler(
            config=config, workspace=workspace, state_dir=state_dir,
            resolve_provider=lambda m: provider,
            channels={"telegram": MagicMock()},
        )
        return scheduler, provider, workspace

    def test_empty_heartbeat_skips_with_zero_llm(self, tmp_path):
        scheduler, provider, workspace = self._make_scheduler(tmp_path)
        job = CronJob(
            id="heartbeat", schedule="0 * * * *", prompt="check",
            context="heartbeat", session="isolated",
            deliver_mode="announce", deliver_channel="telegram",
        )
        with patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            result = scheduler.run_job(job)
        assert result.status == "skipped"
        assert result.error == "empty-heartbeat-file"
        provider.complete.assert_not_called()

    def test_missing_heartbeat_file_skips(self, tmp_path):
        scheduler, provider, workspace = self._make_scheduler(tmp_path)
        job = CronJob(
            id="heartbeat", schedule="0 * * * *", prompt="check",
            context="heartbeat", session="isolated",
            deliver_mode="none",
        )
        with patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            result = scheduler.run_job(job)
        assert result.status == "skipped"
        assert result.error == "empty-heartbeat-file"
        provider.complete.assert_not_called()

    def test_clean_triggers_and_heartbeat_calls_cheap_gate(self, tmp_path):
        scheduler, provider, workspace = self._make_scheduler(tmp_path, "NO_REPLY")
        (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n- Check things")

        job = CronJob(
            id="heartbeat", schedule="0 * * * *", prompt="check heartbeat",
            context="heartbeat", session="isolated",
            deliver_mode="announce", deliver_channel="telegram",
        )
        with patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            result = scheduler.run_job(job)
        assert result.status == "success"
        provider.complete.assert_called_once()
        scheduler.channels["telegram"].send.assert_not_called()

    def test_cheap_gate_escalates_on_attention(self, tmp_path):
        config = _make_config()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "config").mkdir()
        (workspace / "SOUL.md").write_text("You are a test agent.")
        (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n- Check things")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        provider = MagicMock()
        provider.complete.side_effect = [
            CompletionResponse(text="User hasn't checked in. Send a nudge.", model="test"),
            CompletionResponse(text="Hey, just checking in!", model="test"),
        ]
        channel = MagicMock()

        scheduler = Scheduler(
            config=config, workspace=workspace, state_dir=state_dir,
            resolve_provider=lambda m: provider,
            channels={"telegram": channel},
        )

        job = CronJob(
            id="heartbeat", schedule="0 * * * *", prompt="check",
            context="heartbeat", session="isolated",
            deliver_mode="announce", deliver_channel="telegram",
        )
        with patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            result = scheduler.run_job(job)
        assert result.status == "success"
        assert provider.complete.call_count == 2
        channel.send.assert_called_once()

    def test_attention_triggers_skip_cheap_gate(self, tmp_path):
        scheduler, provider, workspace = self._make_scheduler(tmp_path, "Handling triggers")
        triggers_dir = workspace / "skills-data" / "heartbeat"
        triggers_dir.mkdir(parents=True)
        triggers_data = {
            "status": "attention",
            "triggers": ["morning_missed: past deadline"],
        }
        (triggers_dir / "triggers.json").write_text(json.dumps(triggers_data))
        (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n- Check things")

        job = CronJob(
            id="heartbeat", schedule="0 * * * *", prompt="check heartbeat",
            context="heartbeat", session="isolated",
            deliver_mode="announce", deliver_channel="telegram",
        )
        with patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            result = scheduler.run_job(job)
        assert result.status == "success"
        provider.complete.assert_called_once()
        call_args = provider.complete.call_args[0][0]
        assert "morning_missed" in call_args.messages[-1].content

    def test_load_jobs_parses_context(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        jobs_data = [{
            "id": "heartbeat",
            "schedule": "0 * * * *",
            "prompt": "check",
            "context": "heartbeat",
            "session": "isolated",
        }]
        (workspace / "config" / "jobs.json").write_text(json.dumps(jobs_data))
        from faffmonkey.runtime.scheduler import load_jobs
        jobs = load_jobs(workspace)
        assert jobs[0].context == "heartbeat"

    def test_no_reply_heartbeat_not_an_error(self, tmp_path):
        """Empty/NO_REPLY from heartbeat is success, not error."""
        scheduler, provider, workspace = self._make_scheduler(tmp_path, "NO_REPLY")
        (workspace / "HEARTBEAT.md").write_text("# Heartbeat\n- Check things")

        job = CronJob(
            id="heartbeat", schedule="0 * * * *", prompt="check",
            context="heartbeat", session="isolated",
            deliver_mode="none",
        )
        with patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            result = scheduler.run_job(job)
        assert result.status == "success"


class TestHeartbeatConfigIsHonoured:
    """D2: enabled and active_hours were parsed, validated and never read."""

    def _job(self):
        return CronJob(
            id="hb", schedule="*/30 * * * *", context="heartbeat",
            deliver_mode="none",
        )

    def _workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "HEARTBEAT.md").write_text("- check the thing")
        return workspace

    def test_watchdog_runs_before_triggers_are_read(self, tmp_path):
        """Triggers written by the inline watchdog are seen by the same run."""
        config = _make_config()
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text="noted", model="test")
        workspace = self._workspace(tmp_path)

        def fake_watchdog(ws, name, action, **kwargs):
            path = ws / "skills-data" / "heartbeat" / "triggers.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"status": "attention", "triggers": ["morning_missed"]}
            ))
            return ("ok", [], False)

        with patch("faffmonkey.runtime.skills.invoke", side_effect=fake_watchdog):
            text, usage, skip = _run_heartbeat(
                self._job(), config, lambda m: provider, workspace, tmp_path,
                now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
            )

        assert skip is None
        # attention short-circuits the gate: one full call, not gate + escalate
        provider.complete.assert_called_once()
        sent = provider.complete.call_args[0][0].messages[-1].content
        assert "morning_missed" in sent

    def test_watchdog_failure_does_not_stop_the_heartbeat(self, tmp_path):
        config = _make_config()
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text="NO_REPLY", model="test")

        with patch(
            "faffmonkey.runtime.skills.invoke", side_effect=OSError("no such skill"),
        ):
            text, usage, skip = _run_heartbeat(
                self._job(), config, lambda m: provider,
                self._workspace(tmp_path), tmp_path,
                now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
            )

        assert skip is None
        provider.complete.assert_called_once()

    def test_disabled_heartbeat_does_not_run(self, tmp_path):
        config = _make_config(heartbeat=HeartbeatConfig(enabled=False))
        provider = MagicMock()

        text, usage, skip = _run_heartbeat(
            self._job(), config, lambda m: provider,
            self._workspace(tmp_path), tmp_path,
            now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        )

        assert skip == "heartbeat-disabled"
        provider.complete.assert_not_called()

    def test_outside_active_hours_does_not_run(self, tmp_path):
        config = _make_config(heartbeat=HeartbeatConfig(active_hours=(9, 22)))
        provider = MagicMock()

        text, usage, skip = _run_heartbeat(
            self._job(), config, lambda m: provider,
            self._workspace(tmp_path), tmp_path,
            now=datetime(2026, 5, 14, 3, 0, tzinfo=timezone.utc),
        )

        assert skip == "outside-active-hours"
        provider.complete.assert_not_called()

    def test_inside_active_hours_runs(self, tmp_path):
        config = _make_config(heartbeat=HeartbeatConfig(active_hours=(9, 22)))
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text="NO_REPLY", model="test")

        text, usage, skip = _run_heartbeat(
            self._job(), config, lambda m: provider,
            self._workspace(tmp_path), tmp_path,
            now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        )

        assert skip is None
        provider.complete.assert_called_once()

    def test_active_hours_wrapping_midnight(self, tmp_path):
        from faffmonkey.runtime.scheduler import _heartbeat_skip_reason

        config = _make_config(heartbeat=HeartbeatConfig(active_hours=(22, 6)))
        night = datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc)
        day = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)

        assert _heartbeat_skip_reason(config, night) is None
        assert _heartbeat_skip_reason(config, day) == "outside-active-hours"


class TestHeartbeatGateSlot:
    """D3: the documented cheap gate billed the main model on every run."""

    def test_gate_resolves_the_heartbeat_route(self, tmp_path):
        seen = []

        def resolver(mc):
            seen.append(mc.model)
            provider = MagicMock()
            provider.complete.return_value = CompletionResponse(text="NO_REPLY", model=mc.model)
            return provider

        config = _make_config(
            models={
                "main": ModelConfig(
                    provider="test", model="expensive",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
                "cheap": ModelConfig(
                    provider="test", model="cheap-model",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
            },
            routing={"conversation": "main", "cron_default": "main", "heartbeat": "cheap"},
            heartbeat=HeartbeatConfig(active_hours=(0, 24)),
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "HEARTBEAT.md").write_text("- check the thing")

        _run_heartbeat(
            CronJob(id="hb", schedule="*/30 * * * *", context="heartbeat"),
            config, resolver, workspace, tmp_path,
            now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        )

        assert seen == ["cheap-model"]

    def test_escalation_uses_cron_default_not_the_gate_model(self, tmp_path):
        seen = []

        def resolver(mc):
            seen.append(mc.model)
            provider = MagicMock()
            provider.complete.return_value = CompletionResponse(
                text="the visa appointment is in 48 hours", model=mc.model,
            )
            return provider

        config = _make_config(
            models={
                "main": ModelConfig(
                    provider="test", model="expensive",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
                "cheap": ModelConfig(
                    provider="test", model="cheap-model",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
            },
            routing={"conversation": "main", "cron_default": "main", "heartbeat": "cheap"},
            heartbeat=HeartbeatConfig(active_hours=(0, 24)),
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "HEARTBEAT.md").write_text("- any deadline within 48 hours?")

        _run_heartbeat(
            CronJob(id="hb", schedule="*/30 * * * *", context="heartbeat"),
            config, resolver, workspace, tmp_path,
            now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        )

        # Gate detects on cheap; escalation composes on cron_default.
        assert seen == ["cheap-model", "expensive"]

    def test_job_model_override_still_wins(self, tmp_path):
        seen = []

        def resolver(mc):
            seen.append(mc.model)
            provider = MagicMock()
            provider.complete.return_value = CompletionResponse(text="NO_REPLY", model=mc.model)
            return provider

        config = _make_config(
            models={
                "main": ModelConfig(
                    provider="test", model="expensive",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
                "cheap": ModelConfig(
                    provider="test", model="cheap-model",
                    base_url="http://localhost:11434/v1", api_key="",
                ),
            },
            routing={"conversation": "main", "cron_default": "cheap", "heartbeat": "cheap"},
            heartbeat=HeartbeatConfig(active_hours=(0, 24)),
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "HEARTBEAT.md").write_text("- check the thing")

        _run_heartbeat(
            CronJob(id="hb", schedule="*/30 * * * *", context="heartbeat", model="main"),
            config, resolver, workspace, tmp_path,
            now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
        )

        assert seen == ["expensive"]
