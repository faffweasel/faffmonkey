"""Tests for faff cron list/history CLI commands."""

import json
from pathlib import Path


from faffmonkey.cli.cron import run_cron_history, run_cron_list


def _make_config_file(state_dir: Path) -> None:
    config = {
        "timezone": "Asia/Bangkok",
        "models": {
            "main": {
                "provider": "test", "model": "test-model",
                "base_url": "http://localhost:11434/v1",
            }
        },
    }
    (state_dir / "config.json").write_text(json.dumps(config))


class TestCronList:
    def test_lists_jobs_with_next_fire(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_config_file(state_dir)

        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        jobs = [
            {"id": "morning", "schedule": "0 7 * * *", "prompt": "Good morning",
             "session": "isolated", "enabled": True},
            {"id": "disabled-job", "schedule": "0 12 * * *", "prompt": "noon",
             "enabled": False},
        ]
        (workspace / "config" / "jobs.json").write_text(json.dumps(jobs))

        run_cron_list(state_dir, workspace)
        out = capsys.readouterr().out
        assert "morning" in out
        assert "disabled-job" in out
        assert "[enabled]" in out
        assert "[disabled]" in out
        assert "next:" in out

    def test_no_jobs(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_config_file(state_dir)

        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text("[]")

        run_cron_list(state_dir, workspace)
        assert "No cron jobs" in capsys.readouterr().out

    def test_one_shot_job(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_config_file(state_dir)

        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        jobs = [
            {"id": "reminder", "at": "2099-12-31 23:59", "prompt": "New Year!",
             "enabled": True},
        ]
        (workspace / "config" / "jobs.json").write_text(json.dumps(jobs))

        run_cron_list(state_dir, workspace)
        out = capsys.readouterr().out
        assert "reminder" in out
        assert "at 2099-12-31 23:59" in out


class TestCronHistory:
    def test_shows_last_20(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        log_dir = state_dir / "logs" / "cron"
        log_dir.mkdir(parents=True)

        log_path = log_dir / "test-job.jsonl"
        lines = []
        for i in range(25):
            lines.append(json.dumps({
                "timestamp": f"2026-05-14T{i:02d}:00:00+07:00",
                "status": "success",
                "duration_ms": 100 + i,
            }))
        log_path.write_text("\n".join(lines) + "\n")

        run_cron_history(state_dir, "test-job")
        out = capsys.readouterr().out
        output_lines = [l for l in out.strip().split("\n") if l.strip()]
        assert len(output_lines) == 20

    def test_no_history(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        run_cron_history(state_dir, "nonexistent")
        assert "No history" in capsys.readouterr().out

    def test_shows_errors(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        log_dir = state_dir / "logs" / "cron"
        log_dir.mkdir(parents=True)

        log_path = log_dir / "failing-job.jsonl"
        log_path.write_text(json.dumps({
            "timestamp": "2026-05-14T10:00:00+07:00",
            "status": "error",
            "duration_ms": 50,
            "error": "connection refused",
        }) + "\n")

        run_cron_history(state_dir, "failing-job")
        out = capsys.readouterr().out
        assert "error" in out
        assert "connection refused" in out


class TestCronRunKeepsOneShots:
    """D24: testing a reminder used to destroy it."""

    def test_manual_run_does_not_delete_a_one_shot(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_config_file(state_dir)
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "SOUL.md").write_text("agent")
        jobs_path = workspace / "config" / "jobs.json"
        jobs_path.write_text(json.dumps([
            {"id": "reminder", "at": "2030-01-01 09:00", "prompt": "remind me",
             "deliver": {"mode": "none"}, "enabled": True},
        ]))

        runtime = MagicMock()
        runtime.config = __import__(
            "faffmonkey.config", fromlist=["load_config"],
        ).load_config(state_dir / "config.json")
        runtime.search_provider = None
        provider = MagicMock()
        provider.complete.return_value = __import__(
            "faffmonkey.types", fromlist=["CompletionResponse"],
        ).CompletionResponse(text="here is your reminder", model="test")
        runtime.resolve_provider = lambda m: provider

        from faffmonkey.cli.cron import run_cron_run

        with patch("faffmonkey.wiring.wire", return_value=runtime), \
             patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            code = run_cron_run(state_dir, workspace, "reminder")

        assert code == 0
        remaining = json.loads(jobs_path.read_text())
        assert [j["id"] for j in remaining] == ["reminder"]
        assert "kept" in capsys.readouterr().out


class TestCronListReportsRejects:
    """P7-M4: a broken jobs.json printed the same line as an empty one."""

    def _setup(self, tmp_path, jobs_text):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_config_file(state_dir)
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "config" / "jobs.json").write_text(jobs_text)
        return state_dir, workspace

    def test_unparseable_file_is_reported(self, tmp_path, capsys):
        state_dir, workspace = self._setup(tmp_path, "{not json")
        run_cron_list(state_dir, workspace)
        assert "unreadable" in capsys.readouterr().out

    def test_rejected_entries_are_counted(self, tmp_path, capsys):
        state_dir, workspace = self._setup(tmp_path, json.dumps([
            {"id": "good", "schedule": "0 7 * * *", "prompt": "hi"},
            {"id": "bad hour", "schedule": "0 25 * * *", "prompt": "hi"},
        ]))
        run_cron_list(state_dir, workspace)
        out = capsys.readouterr().out
        assert "1 job(s) in jobs.json were rejected" in out
        assert "good" in out

    def test_healthy_file_says_nothing_extra(self, tmp_path, capsys):
        state_dir, workspace = self._setup(tmp_path, json.dumps([
            {"id": "good", "schedule": "0 7 * * *", "prompt": "hi"},
        ]))
        run_cron_list(state_dir, workspace)
        assert "rejected" not in capsys.readouterr().out


class TestCronRunPrintsOutput:
    """A manual run has no channels, so an announce job was recorded as
    'could not deliver to channel' and the operator never saw what the
    job produced."""

    def test_announce_job_prints_output_and_succeeds(self, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_config_file(state_dir)
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        (workspace / "SOUL.md").write_text("agent")
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "briefing", "schedule": "0 7 * * *", "prompt": "brief me",
             "session": "isolated",
             "deliver": {"mode": "announce", "channel": "discord"}, "enabled": True},
        ]))

        runtime = MagicMock()
        runtime.config = __import__(
            "faffmonkey.config", fromlist=["load_config"],
        ).load_config(state_dir / "config.json")
        runtime.search_provider = None
        provider = MagicMock()
        provider.complete.return_value = __import__(
            "faffmonkey.types", fromlist=["CompletionResponse"],
        ).CompletionResponse(text="the briefing sk-abcdefghijklmnopqrstuvwxyz123456", model="test")
        runtime.resolve_provider = lambda m: provider

        from faffmonkey.cli.cron import run_cron_run

        with patch("faffmonkey.wiring.wire", return_value=runtime), \
             patch("faffmonkey.runtime.scheduler.provider_preflight", return_value=True):
            code = run_cron_run(state_dir, workspace, "briefing")

        out = capsys.readouterr().out
        assert code == 0
        assert "briefing: success" in out
        assert "the briefing" in out
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
        assert "Not delivered" in out and "'discord'" in out
        history = (state_dir / "logs" / "cron" / "briefing.jsonl").read_text()
        assert "could not deliver" not in history
