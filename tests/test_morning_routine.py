"""Tests for the morning-routine skill scripts."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "templates" / "workspace" / "skills" / "morning-routine" / "scripts"
)


def _run_script(workspace, script_name, tz="UTC"):
    env = {
        "WORKSPACE": str(workspace),
        "TZ": tz,
    }
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _today(tz="UTC"):
    return datetime.now(ZoneInfo(tz)).date().isoformat()


@pytest.fixture
def workspace(ws):
    return ws


class TestPrepare:
    def test_creates_today_memory_file(self, workspace):
        result = _run_script(workspace, "prepare.py")
        assert result.returncode == 0
        today_file = workspace / "memory" / "daily" / f"{_today()}.md"
        assert today_file.read_text() == f"# {_today()}\n"

    def test_ready_when_not_stamped(self, workspace):
        result = _run_script(workspace, "prepare.py")
        assert "READY" in result.stdout
        assert "ALREADY_RUN" not in result.stdout

    def test_already_run_when_stamped(self, workspace):
        memory = workspace / "memory" / "daily"
        memory.mkdir(parents=True)
        (memory / f"{_today()}.md").write_text(
            f"# {_today()}\n\nMorning message sent 07:05\n"
        )
        result = _run_script(workspace, "prepare.py")
        assert result.returncode == 0
        assert "ALREADY_RUN" in result.stdout
        assert "READY" not in result.stdout.splitlines()

    def test_prints_pending_carry_over(self, workspace):
        queue_dir = workspace / "skills-data" / "carry-over"
        queue_dir.mkdir(parents=True)
        (queue_dir / "queue.json").write_text(json.dumps([
            {"message": "renew the visa", "priority": "urgent", "status": "pending"},
            {"message": "old news", "priority": "normal", "status": "delivered"},
        ]))
        result = _run_script(workspace, "prepare.py")
        assert "renew the visa" in result.stdout
        assert "[urgent]" in result.stdout
        assert "old news" not in result.stdout

    def test_prints_preconscious_buffer_sorted(self, workspace):
        buffer_dir = workspace / "skills-data" / "preconscious"
        buffer_dir.mkdir(parents=True)
        (buffer_dir / "buffer.json").write_text(json.dumps({"items": [
            {"description": "low item", "c": 1, "i": 1},
            {"description": "high item", "c": 5, "i": 4},
        ]}))
        result = _run_script(workspace, "prepare.py")
        assert result.stdout.index("high item") < result.stdout.index("low item")

    def test_skips_missing_and_corrupt_sources(self, workspace):
        queue_dir = workspace / "skills-data" / "carry-over"
        queue_dir.mkdir(parents=True)
        (queue_dir / "queue.json").write_text("not json")
        result = _run_script(workspace, "prepare.py")
        assert result.returncode == 0
        assert "READY" in result.stdout

    def test_requires_workspace_env(self, workspace):
        cmd = [sys.executable, str(SCRIPTS_DIR / "prepare.py")]
        result = subprocess.run(cmd, capture_output=True, text=True, env={"TZ": "UTC"})
        assert result.returncode == 1

    def test_respects_timezone(self, workspace):
        tz = "Pacific/Kiritimati"
        result = _run_script(workspace, "prepare.py", tz=tz)
        assert result.returncode == 0
        assert (workspace / "memory" / "daily" / f"{_today(tz)}.md").exists()


class TestStamp:
    def test_appends_stamp(self, workspace):
        memory = workspace / "memory" / "daily"
        memory.mkdir(parents=True)
        (memory / f"{_today()}.md").write_text(f"# {_today()}\n")
        result = _run_script(workspace, "stamp.py")
        assert result.returncode == 0
        content = (memory / f"{_today()}.md").read_text()
        assert "Morning message sent" in content
        assert content.startswith(f"# {_today()}\n")

    def test_creates_file_when_missing(self, workspace):
        result = _run_script(workspace, "stamp.py")
        assert result.returncode == 0
        content = (workspace / "memory" / "daily" / f"{_today()}.md").read_text()
        assert content.startswith(f"# {_today()}\n")
        assert "Morning message sent" in content

    def test_idempotent(self, workspace):
        _run_script(workspace, "stamp.py")
        result = _run_script(workspace, "stamp.py")
        assert "already stamped" in result.stdout
        content = (workspace / "memory" / "daily" / f"{_today()}.md").read_text()
        assert content.count("Morning message sent") == 1


class TestRoundTrip:
    def test_prepare_stamp_prepare(self, workspace):
        first = _run_script(workspace, "prepare.py")
        assert "READY" in first.stdout
        _run_script(workspace, "stamp.py")
        second = _run_script(workspace, "prepare.py")
        assert "ALREADY_RUN" in second.stdout

    def test_stamp_satisfies_watchdog_check(self, workspace):
        _run_script(workspace, "stamp.py")
        content = (workspace / "memory" / "daily" / f"{_today()}.md").read_text()
        assert "morning" in content.lower()
