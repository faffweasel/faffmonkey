import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


from faffmonkey.config import CompactionConfig, Config, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.scheduler import CronJob, Scheduler, _load_triggers, _run_heartbeat
from faffmonkey.types import CompletionResponse

import importlib


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        name.removesuffix(".py"),
        Path(__file__).resolve().parent.parent
        / "templates" / "workspace" / "skills" / "heartbeat" / "scripts" / name,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watchdog = _load_script("run.py")


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

    def test_custom_config(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({
            "morning_deadline_hour": 10,
        }))
        config = watchdog.load_config(tmp_path)
        assert config["morning_deadline_hour"] == 10
        assert config["learnings_max_entries"] == 30

    def test_invalid_json_uses_defaults(self, tmp_path):
        (tmp_path / "config.json").write_text("not json")
        config = watchdog.load_config(tmp_path)
        assert config == watchdog.DEFAULT_CONFIG


class TestWatchdogYesterdayMemory:
    def test_creates_missing_memory_file(self, tmp_path):
        tz = ZoneInfo("UTC")
        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        fixed = watchdog.check_yesterday_memory(tmp_path, tz)
        memory_file = tmp_path / "memory" / "daily" / f"{yesterday.isoformat()}.md"
        assert memory_file.exists()
        assert f"# {yesterday.isoformat()}" in memory_file.read_text()
        assert len(fixed) == 1
        assert "created missing memory file" in fixed[0]

    def test_existing_memory_file_untouched(self, tmp_path):
        tz = ZoneInfo("UTC")
        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        memory_dir = tmp_path / "memory" / "daily"
        memory_dir.mkdir(parents=True)
        memory_file = memory_dir / f"{yesterday.isoformat()}.md"
        memory_file.write_text("# existing content")
        fixed = watchdog.check_yesterday_memory(tmp_path, tz)
        assert memory_file.read_text() == "# existing content"
        assert fixed == []


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

    def test_learnings_full_is_raised_once_a_day(self, tmp_path):
        """A full LEARNINGS.md stays full until someone runs self-review,
        so without the stamp the heartbeat escalated about it every hour."""
        tz = ZoneInfo("UTC")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        skill_data = tmp_path / "skill_data"
        lines = ["# L\n"] + [f"- item {i}\n" for i in range(40)]
        (workspace / "LEARNINGS.md").write_text("".join(lines))

        first = watchdog.run_watchdog(workspace, skill_data, tz)
        second = watchdog.run_watchdog(workspace, skill_data, tz)
        assert first["status"] == "attention"
        assert second["status"] == "clean"
        assert second["triggers"] == []


class TestWatchdogTray:
    """Sensors talk to the heartbeat by dropping files. The watchdog turns
    them into prompt lines and names them, so the wake can remove exactly
    what it saw."""

    def test_collects_triggers_and_names_their_files(self, tmp_path):
        tray = tmp_path / "triggers.d"
        tray.mkdir()
        (tray / "aqi-high.json").write_text(json.dumps(
            {"source": "aqi", "kind": "alert", "text": "AQI 192, above your 180 threshold"},
        ))
        (tray / "poke-1.json").write_text(json.dumps({"text": "look around"}))

        lines, files = watchdog.collect_triggers(tmp_path)

        assert lines == [
            "aqi (alert): AQI 192, above your 180 threshold",
            "poke-1 (alert): look around",
        ]
        assert files == ["aqi-high.json", "poke-1.json"]

    def test_bad_files_are_reported_and_skipped(self, tmp_path, capsys):
        tray = tmp_path / "triggers.d"
        tray.mkdir()
        (tray / "broken.json").write_text("{not json")
        (tray / "empty.json").write_text(json.dumps({"source": "x"}))
        (tray / "notes.txt").write_text("ignored")

        lines, files = watchdog.collect_triggers(tmp_path)

        assert lines == [] and files == []
        err = capsys.readouterr().err
        assert "broken.json" in err and "empty.json" in err

    def test_no_tray_is_clean(self, tmp_path):
        assert watchdog.collect_triggers(tmp_path) == ([], [])

    def test_tray_trigger_makes_the_run_attention_with_files(self, tmp_path):
        tz = ZoneInfo("UTC")
        workspace = tmp_path / "workspace"
        (workspace / "memory" / "daily").mkdir(parents=True)
        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        (workspace / "memory" / "daily" / f"{yesterday.isoformat()}.md").write_text("# exists")
        skill_data = tmp_path / "skill_data"
        tray = skill_data / "triggers.d"
        tray.mkdir(parents=True)
        (tray / "weather-rain.json").write_text(json.dumps(
            {"source": "weather", "kind": "alert", "text": "rain within 2h"},
        ))

        with patch.object(watchdog, "check_morning_stamp", return_value=[]):
            result = watchdog.run_watchdog(workspace, skill_data, tz)

        assert result["status"] == "attention"
        assert result["triggers"] == ["weather (alert): rain within 2h"]
        assert result["files"] == ["weather-rain.json"]
        assert (tray / "weather-rain.json").exists()


class TestWatchdogReadings:
    def test_last_line_of_each_reading_with_its_age(self, tmp_path):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 8, 29, 15, 10, tzinfo=tz)
        readings = tmp_path / "readings"
        readings.mkdir()
        (readings / "aqi.jsonl").write_text(
            json.dumps({"at": "2026-08-29T14:00:00+00:00", "summary": "AQI 150"}) + "\n"
            + json.dumps({"at": "2026-08-29T15:00:00+00:00", "summary": "AQI 192 at Hoan Kiem"}) + "\n"
        )
        (readings / "weather.jsonl").write_text(
            json.dumps({"at": "2026-08-27T15:00:00+00:00", "summary": "35C, humid"}) + "\n"
        )

        assert watchdog.collect_readings(tmp_path, now) == [
            "aqi (10m ago): AQI 192 at Hoan Kiem",
            "weather (2d ago): 35C, humid",
        ]

    def test_unreadable_or_summaryless_readings_are_skipped(self, tmp_path, capsys):
        tz = ZoneInfo("UTC")
        readings = tmp_path / "readings"
        readings.mkdir()
        (readings / "broken.jsonl").write_text("{not json\n")
        (readings / "bare.jsonl").write_text(json.dumps({"at": "2026-08-29T15:00:00+00:00"}) + "\n")

        assert watchdog.collect_readings(tmp_path, datetime.now(tz)) == []
        assert "broken.jsonl" in capsys.readouterr().err

    def test_no_readings_dir(self, tmp_path):
        assert watchdog.collect_readings(tmp_path, datetime.now(ZoneInfo("UTC"))) == []


class TestPoke:
    def test_writes_an_occasion_trigger(self, tmp_path, monkeypatch, capsys):
        poke = _load_script("poke.py")
        monkeypatch.setenv("SKILL_DATA", str(tmp_path))
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.setattr("sys.argv", ["poke.py", "Check", "the", "afternoon."])

        poke.main()

        files = list((tmp_path / "triggers.d").glob("poke-*.json"))
        assert len(files) == 1
        item = json.loads(files[0].read_text())
        assert item["kind"] == "occasion" and item["source"] == "poke"
        assert item["text"] == "Check the afternoon."
        assert "trigger written" in capsys.readouterr().out

    def test_default_text_when_none_given(self, tmp_path, monkeypatch):
        poke = _load_script("poke.py")
        monkeypatch.setenv("SKILL_DATA", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["poke.py"])

        poke.main()

        item = json.loads(next((tmp_path / "triggers.d").glob("poke-*.json")).read_text())
        assert item["text"]


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

def _fake_watchdog(triggers, files=(), readings=()):
    """A stand-in for the heartbeat skill's run action: writes triggers.json
    the way the real watchdog does, without a skill install in tmp_path."""
    def invoke(ws, name, action, **kwargs):
        path = ws / "skills-data" / "heartbeat" / "triggers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "status": "attention" if triggers else "clean",
            "triggers": list(triggers),
            "files": list(files),
            "readings": list(readings),
        }))
        return ("ok", [], False)
    return invoke


class TestHeartbeatTick:
    """The contract: the watchdog decides whether to wake, a wake is one
    agent turn with tools, and a wake consumes the triggers it was handed."""

    def _scheduler(self, tmp_path, answer="noted"):
        config = _make_config(tool_permissions={"file_read": "always"})
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "SOUL.md").write_text("You are a test agent.")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text=answer, model="test")
        scheduler = Scheduler(
            config=config, workspace=workspace, state_dir=state_dir,
            resolve_provider=lambda m: provider,
            channels={"telegram": MagicMock()},
        )
        return scheduler, provider, workspace

    def _job(self, **overrides):
        fields = dict(
            id="heartbeat", schedule="*/5 * * * *", prompt=None, context="heartbeat",
            session="agent", deliver_mode="announce", deliver_channel="telegram",
        )
        fields.update(overrides)
        return CronJob(**fields)

    def _run(self, scheduler, job, watchdog_fn):
        with (
            patch("faffmonkey.runtime.skills.invoke", side_effect=watchdog_fn),
            patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True),
        ):
            return scheduler.run_job(job)

    def test_clean_tick_calls_no_model_and_writes_no_log_row(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path)
        (workspace / "HEARTBEAT.md").write_text("- be brief")

        result = self._run(scheduler, self._job(), _fake_watchdog([]))

        assert result.status == "skipped" and result.error == "clean"
        provider.complete.assert_not_called()
        assert not (scheduler.state_dir / "logs" / "cron" / "heartbeat.jsonl").exists()

    def test_trigger_wakes_one_agent_turn_with_tools_and_delivers(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path, "AQI is 192, stay in.")

        result = self._run(
            scheduler, self._job(),
            _fake_watchdog(["aqi (alert): AQI 192, above your 180 threshold"]),
        )

        assert result.status == "success"
        provider.complete.assert_called_once()
        request = provider.complete.call_args[0][0]
        assert request.tools
        assert "aqi (alert): AQI 192" in request.messages[-1].content
        scheduler.channels["telegram"].send.assert_called_once()

    def test_no_reply_wake_is_success_and_sends_nothing(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path, "NO_REPLY")

        result = self._run(scheduler, self._job(), _fake_watchdog(["poke (occasion): look"]))

        assert result.status == "success"
        scheduler.channels["telegram"].send.assert_not_called()

    def test_wake_consumes_only_the_triggers_it_was_handed(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path, "NO_REPLY")
        tray = workspace / "skills-data" / "heartbeat" / "triggers.d"
        tray.mkdir(parents=True)
        (tray / "aqi-high.json").write_text("{}")
        (tray / "weather-rain.json").write_text("{}")

        self._run(
            scheduler, self._job(),
            _fake_watchdog(["aqi (alert): AQI 192"], files=["aqi-high.json"]),
        )

        assert not (tray / "aqi-high.json").exists()
        assert (tray / "weather-rain.json").exists()

    def test_failed_wake_keeps_its_triggers_for_the_retry(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path)
        tray = workspace / "skills-data" / "heartbeat" / "triggers.d"
        tray.mkdir(parents=True)
        (tray / "aqi-high.json").write_text("{}")

        with patch("faffmonkey.runtime.scheduler._run_agent", side_effect=RuntimeError("provider down")):
            result = self._run(
                scheduler, self._job(),
                _fake_watchdog(["aqi (alert): AQI 192"], files=["aqi-high.json"]),
            )

        assert result.status == "error"
        assert (tray / "aqi-high.json").exists()

    def test_trigger_file_names_outside_the_tray_are_ignored(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path, "NO_REPLY")
        victim = workspace / "SOUL.md"

        self._run(
            scheduler, self._job(),
            _fake_watchdog(["x (alert): y"], files=["../../SOUL.md", "/etc/passwd"]),
        )

        assert victim.exists()

    def test_missing_heartbeat_file_still_wakes(self, tmp_path):
        scheduler, provider, workspace = self._scheduler(tmp_path, "NO_REPLY")

        result = self._run(scheduler, self._job(), _fake_watchdog(["poke (occasion): look"]))

        assert result.status == "success"
        provider.complete.assert_called_once()
        assert "Standing instructions" not in provider.complete.call_args[0][0].messages[-1].content

    def test_load_jobs_parses_context(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        jobs_data = [{
            "id": "heartbeat",
            "schedule": "*/5 * * * *",
            "prompt": "check",
            "context": "heartbeat",
            "session": "agent",
        }]
        (workspace / "config" / "jobs.json").write_text(json.dumps(jobs_data))
        from faffmonkey.runtime.scheduler import load_jobs
        jobs = load_jobs(workspace)
        assert jobs[0].context == "heartbeat"


class TestHeartbeatFileTrust:
    """HEARTBEAT.md is always-trusted, so the symlink rejection the bootstrap
    applies to SOUL.md and friends must hold on the path that actually reads
    it every hour. It was only ever tested against a bootstrap mode nothing
    called."""

    def test_symlinked_heartbeat_is_ignored(self, tmp_path, caplog):
        config = _make_config()
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text="NO_REPLY", model="test")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "other.md"
        target.write_text("SYMLINKED_HEARTBEAT")
        (workspace / "HEARTBEAT.md").symlink_to(target)
        job = CronJob(
            id="hb", schedule="*/5 * * * *", context="heartbeat",
            deliver_mode="none",
        )

        with (
            patch("faffmonkey.runtime.skills.invoke", side_effect=_fake_watchdog(["poke (occasion): look"])),
            caplog.at_level(logging.WARNING),
        ):
            text, usage, skip = _run_heartbeat(
                job, config, lambda m: provider, workspace, tmp_path,
                now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
            )

        assert skip is None
        sent = provider.complete.call_args[0][0].messages[-1].content
        assert "SYMLINKED_HEARTBEAT" not in sent
        assert any("HEARTBEAT.md failed trust check" in r.message for r in caplog.records)


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

    def test_watchdog_failure_leaves_the_last_triggers_in_force(self, tmp_path):
        """A broken watchdog must not silence a trigger that was already
        written; the tick reads whatever triggers.json is there."""
        config = _make_config()
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text="NO_REPLY", model="test")
        workspace = self._workspace(tmp_path)
        _fake_watchdog(["aqi (alert): 192"])(workspace, "heartbeat", "run")

        with patch(
            "faffmonkey.runtime.skills.invoke", side_effect=OSError("no such skill"),
        ):
            text, usage, skip = _run_heartbeat(
                self._job(), config, lambda m: provider, workspace, tmp_path,
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

        with patch("faffmonkey.runtime.skills.invoke", side_effect=_fake_watchdog(["poke (occasion): look"])):
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


class TestHeartbeatModelSlot:
    """A wake runs on the `heartbeat` route when one is configured, so the
    slot that costs every wake is the one the operator chose for it."""

    def _config(self, routing):
        return _make_config(
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
            routing=routing,
            heartbeat=HeartbeatConfig(active_hours=(0, 24)),
        )

    def _wake(self, tmp_path, config, job):
        seen = []

        def resolver(mc):
            seen.append(mc.model)
            provider = MagicMock()
            provider.complete.return_value = CompletionResponse(text="NO_REPLY", model=mc.model)
            return provider

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with patch("faffmonkey.runtime.skills.invoke", side_effect=_fake_watchdog(["poke (occasion): look"])):
            _run_heartbeat(
                job, config, resolver, workspace, tmp_path,
                now=datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc),
            )
        return seen

    def test_wake_runs_on_the_heartbeat_route(self, tmp_path):
        config = self._config({"conversation": "main", "cron_default": "main", "heartbeat": "cheap"})
        seen = self._wake(tmp_path, config, CronJob(id="hb", schedule="*/5 * * * *", context="heartbeat"))
        assert seen == ["cheap-model"]

    def test_job_model_override_wins(self, tmp_path):
        config = self._config({"conversation": "main", "cron_default": "cheap", "heartbeat": "cheap"})
        seen = self._wake(
            tmp_path, config, CronJob(id="hb", schedule="*/5 * * * *", context="heartbeat", model="main"),
        )
        assert seen == ["expensive"]

    def test_without_a_heartbeat_route_cron_default_is_used(self, tmp_path):
        config = self._config({"conversation": "main", "cron_default": "main"})
        seen = self._wake(tmp_path, config, CronJob(id="hb", schedule="*/5 * * * *", context="heartbeat"))
        assert seen == ["expensive"]


class TestRecentDeliveries:
    """A wake is shown what the heartbeat already sent, so "already told
    them" is evidence in the prompt rather than something the model has
    to remember across turns it never sees."""

    def _scheduler(self, tmp_path, answer):
        config = _make_config()
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "SOUL.md").write_text("You are a test agent.")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        provider = MagicMock()
        provider.complete.return_value = CompletionResponse(text=answer, model="test")
        return Scheduler(
            config=config, workspace=workspace, state_dir=state_dir,
            resolve_provider=lambda m: provider, channels={"telegram": MagicMock()},
        ), provider

    def _job(self):
        return CronJob(
            id="heartbeat", schedule="*/5 * * * *", context="heartbeat", session="agent",
            deliver_mode="announce", deliver_channel="telegram",
        )

    def test_delivered_message_is_remembered_persisted_and_reloaded(self, tmp_path):
        scheduler, provider = self._scheduler(tmp_path, "AQI is 192, stay in.")
        with (
            patch("faffmonkey.runtime.skills.invoke", side_effect=_fake_watchdog(["aqi (alert): 192"])),
            patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True),
        ):
            scheduler.run_job(self._job())

        assert scheduler._recent["heartbeat"][0]["text"] == "AQI is 192, stay in."
        saved = json.loads((scheduler.state_dir / "cron-state.json").read_text())
        assert saved["jobs"]["heartbeat"]["recent"][0]["text"] == "AQI is 192, stay in."

        fresh = Scheduler(
            config=scheduler.config, workspace=scheduler.workspace, state_dir=scheduler.state_dir,
            resolve_provider=scheduler.resolve_provider, channels={},
        )
        fresh.load_state()
        assert fresh._recent["heartbeat"][0]["text"] == "AQI is 192, stay in."

    def test_next_wake_is_shown_what_was_sent(self, tmp_path):
        scheduler, provider = self._scheduler(tmp_path, "NO_REPLY")
        scheduler._recent["heartbeat"] = [{"at": "2026-05-14T09:00:00Z", "text": "AQI is 192, stay in."}]
        with (
            patch("faffmonkey.runtime.skills.invoke", side_effect=_fake_watchdog(["aqi (alert): 195"])),
            patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True),
        ):
            scheduler.run_job(self._job())

        sent = provider.complete.call_args[0][0].messages[-1].content
        assert "Sent by the heartbeat recently:" in sent
        assert "AQI is 192, stay in." in sent

    def test_kept_to_ten_and_two_days(self, tmp_path):
        scheduler, _provider = self._scheduler(tmp_path, "unused")
        for i in range(12):
            scheduler._remember_delivery("heartbeat", f"message {i}")
        assert [e["text"] for e in scheduler._recent["heartbeat"]] == [f"message {i}" for i in range(2, 12)]

        stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
        scheduler._recent["other"] = [{"at": stale, "text": "old news"}]
        scheduler._save_state()
        saved = json.loads((scheduler.state_dir / "cron-state.json").read_text())
        assert "other" not in saved["jobs"]
        assert len(saved["jobs"]["heartbeat"]["recent"]) == 10
