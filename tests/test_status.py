"""Tests for faff status."""

import json
from pathlib import Path


from faffmonkey.cli.status import run_status


def _make_config(state_dir: Path) -> None:
    config = {
        "timezone": "UTC",
        "models": {
            "main": {
                "provider": "openrouter", "model": "google/gemini-2.5-flash",
                "base_url": "http://localhost:11434/v1",
            }
        },
    }
    (state_dir / "config.json").write_text(json.dumps(config))


class TestStatus:
    def test_shows_model_config(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _make_config(state_dir)

        run_status(state_dir, workspace)
        out = capsys.readouterr().out
        assert "openrouter" in out
        assert "gemini-2.5-flash" in out

    def test_no_goal(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _make_config(state_dir)

        run_status(state_dir, workspace)
        assert "Active goal: none" in capsys.readouterr().out

    def test_with_goal(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        goal_dir = workspace / "skills-data" / "goal"
        goal_dir.mkdir(parents=True)
        (goal_dir / "current.json").write_text(json.dumps({"goal": "Build the thing"}))
        _make_config(state_dir)

        run_status(state_dir, workspace)
        assert "Build the thing" in capsys.readouterr().out

    def test_cron_runs(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _make_config(state_dir)

        log_dir = state_dir / "logs" / "cron"
        log_dir.mkdir(parents=True)
        (log_dir / "morning.jsonl").write_text(json.dumps({
            "timestamp": "2026-05-14T07:00:00+07:00",
            "status": "success",
            "duration_ms": 1500,
        }) + "\n")

        run_status(state_dir, workspace)
        out = capsys.readouterr().out
        assert "morning" in out
        assert "success" in out

    def test_cron_status_redacted(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _make_config(state_dir)

        log_dir = state_dir / "logs" / "cron"
        log_dir.mkdir(parents=True)
        (log_dir / "myjob.jsonl").write_text(json.dumps({
            "timestamp": "2026-05-14T07:00:00+07:00",
            "status": "error: sk-aaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "duration_ms": 500,
        }) + "\n")

        run_status(state_dir, workspace)
        out = capsys.readouterr().out
        assert "sk-aaaaaa" not in out
        assert "[REDACTED]" in out

    def test_heartbeat_status_redacted(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _make_config(state_dir)

        log_dir = state_dir / "logs" / "cron"
        log_dir.mkdir(parents=True)
        (log_dir / "heartbeat.jsonl").write_text(json.dumps({
            "timestamp": "2026-05-14T07:00:00+07:00",
            "status": "leaked sk-bbbbbbbbbbbbbbbbbbbbbbbbbbb",
        }) + "\n")

        run_status(state_dir, workspace)
        out = capsys.readouterr().out
        assert "sk-bbbbb" not in out
        assert "[REDACTED]" in out

    def test_no_config(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        run_status(state_dir, workspace)
        assert "No config found" in capsys.readouterr().out
