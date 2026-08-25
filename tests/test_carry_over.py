import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "templates" / "workspace" / "skills" / "carry-over" / "scripts"
)


def _run_script(skill_data, script_name, args=None):
    env = {
        "WORKSPACE": str(skill_data.parent.parent),
        "SKILL_DATA": str(skill_data),
        "TZ": "UTC",
    }
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)]
    if args:
        cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


@pytest.fixture
def skill_data(tmp_path):
    sd = tmp_path / "skills-data" / "carry-over"
    sd.mkdir(parents=True)
    return sd


class TestAdd:
    def test_adds_item(self, skill_data):
        result = _run_script(skill_data, "add.py", ["hello from the past"])
        assert result.returncode == 0
        assert "1 pending" in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert len(queue) == 1
        assert queue[0]["message"] == "hello from the past"
        assert queue[0]["status"] == "pending"
        assert queue[0]["priority"] == "normal"
        assert "timestamp" in queue[0]

    def test_adds_multiple_items(self, skill_data):
        _run_script(skill_data, "add.py", ["first message"])
        result = _run_script(skill_data, "add.py", ["second message"])
        assert result.returncode == 0
        assert "2 pending" in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert len(queue) == 2
        assert queue[0]["message"] == "first message"
        assert queue[1]["message"] == "second message"

    def test_joins_multiple_args(self, skill_data):
        result = _run_script(skill_data, "add.py", ["hello", "world", "test"])
        assert result.returncode == 0

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["message"] == "hello world test"

    def test_rejects_empty_message(self, skill_data):
        result = _run_script(skill_data, "add.py", ["  "])
        assert result.returncode != 0
        assert "empty" in result.stderr

    def test_rejects_no_args(self, skill_data):
        result = _run_script(skill_data, "add.py")
        assert result.returncode != 0
        assert "message required" in result.stderr

    def test_creates_queue_file(self, skill_data):
        assert not (skill_data / "queue.json").exists()
        _run_script(skill_data, "add.py", ["test"])
        assert (skill_data / "queue.json").exists()

    def test_handles_corrupt_queue(self, skill_data):
        (skill_data / "queue.json").write_text("not json")
        result = _run_script(skill_data, "add.py", ["recovery"])
        assert result.returncode == 0
        queue = json.loads((skill_data / "queue.json").read_text())
        assert len(queue) == 1
        assert queue[0]["message"] == "recovery"

    def test_priority_flag(self, skill_data):
        result = _run_script(skill_data, "add.py", ["--priority", "urgent", "fix now"])
        assert result.returncode == 0
        assert "[urgent]" in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "urgent"

    def test_priority_simmering(self, skill_data):
        result = _run_script(skill_data, "add.py", ["--priority", "simmering", "slow burn"])
        assert result.returncode == 0
        assert "[simmering]" in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "simmering"

    def test_priority_curious(self, skill_data):
        result = _run_script(skill_data, "add.py", ["--priority", "curious", "interesting find"])
        assert result.returncode == 0
        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "curious"

    def test_invalid_priority(self, skill_data):
        result = _run_script(skill_data, "add.py", ["--priority", "critical", "nope"])
        assert result.returncode != 0
        assert "invalid priority" in result.stderr

    def test_default_priority_is_normal(self, skill_data):
        _run_script(skill_data, "add.py", ["just a note"])
        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "normal"

    def test_priority_flag_position(self, skill_data):
        result = _run_script(skill_data, "add.py", ["message first", "--priority", "urgent"])
        assert result.returncode == 0
        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "urgent"
        assert queue[0]["message"] == "message first"


class TestList:
    def test_lists_pending(self, skill_data):
        _run_script(skill_data, "add.py", ["item one"])
        _run_script(skill_data, "add.py", ["item two"])
        result = _run_script(skill_data, "list.py")
        assert result.returncode == 0
        assert "2 pending" in result.stdout
        assert "item one" in result.stdout
        assert "item two" in result.stdout

    def test_skips_delivered(self, skill_data):
        queue = [
            {"message": "delivered", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
            {"message": "still pending", "timestamp": "2026-01-02T00:00:00+00:00", "status": "pending", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "list.py")
        assert "1 pending" in result.stdout
        assert "still pending" in result.stdout

    def test_empty_queue(self, skill_data):
        result = _run_script(skill_data, "list.py")
        assert result.returncode == 0
        assert "No carry-over items" in result.stdout

    def test_all_delivered(self, skill_data):
        queue = [
            {"message": "done", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "list.py")
        assert "No pending" in result.stdout

    def test_does_not_consume(self, skill_data):
        _run_script(skill_data, "add.py", ["persist test"])
        _run_script(skill_data, "list.py")
        _run_script(skill_data, "list.py")
        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["status"] == "pending"

    def test_shows_priority(self, skill_data):
        _run_script(skill_data, "add.py", ["--priority", "urgent", "fix this"])
        result = _run_script(skill_data, "list.py")
        assert "[urgent]" in result.stdout

    def test_sorted_by_priority(self, skill_data):
        queue = [
            {"message": "low", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "curious"},
            {"message": "high", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "urgent"},
            {"message": "mid", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "list.py")
        lines = [
            l.strip() for l in result.stdout.splitlines()
            if l.startswith("  ")
        ]
        assert lines[0].startswith("1. [urgent]")
        assert lines[1].startswith("2. [normal]")
        assert lines[2].startswith("3. [curious]")


class TestGet:
    def test_get_does_not_mark_delivered(self, skill_data):
        _run_script(skill_data, "add.py", ["important note"])
        result = _run_script(skill_data, "get.py")
        assert result.returncode == 0
        assert "Carry-over from previous sessions:" in result.stdout
        assert "important note" in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["status"] == "pending"

    def test_get_empty_no_output(self, skill_data):
        result = _run_script(skill_data, "get.py")
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_get_all_delivered_no_output(self, skill_data):
        queue = [
            {"message": "done", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        assert result.stdout.strip() == ""

    def test_get_only_pending(self, skill_data):
        queue = [
            {"message": "old", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
            {"message": "new", "timestamp": "2026-01-02T00:00:00+00:00", "status": "pending", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        assert "new" in result.stdout
        lines = [l for l in result.stdout.splitlines() if l.startswith("- ")]
        assert len(lines) == 1

    def test_get_repeatable(self, skill_data):
        _run_script(skill_data, "add.py", ["still here"])
        result1 = _run_script(skill_data, "get.py")
        result2 = _run_script(skill_data, "get.py")
        assert "still here" in result1.stdout
        assert "still here" in result2.stdout

    def test_priority_ordering(self, skill_data):
        recent_ts = datetime.now(timezone.utc).isoformat()
        queue = [
            {"message": "curious item", "timestamp": recent_ts, "status": "pending", "priority": "curious"},
            {"message": "urgent item", "timestamp": recent_ts, "status": "pending", "priority": "urgent"},
            {"message": "normal item", "timestamp": recent_ts, "status": "pending", "priority": "normal"},
            {"message": "simmering item", "timestamp": recent_ts, "status": "pending", "priority": "simmering"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        lines = [l for l in result.stdout.splitlines() if l.startswith("- ")]
        assert "urgent item" in lines[0]
        assert "normal item" in lines[1]
        assert "curious item" in lines[2]
        assert "simmering item" in lines[3]

    def test_no_emoji_in_output(self, skill_data):
        queue = [
            {"message": "fire", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "urgent"},
            {"message": "thought", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "curious"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        assert "\U0001f525" not in result.stdout
        assert "\U0001f4ad" not in result.stdout

    def test_urgent_label_in_output(self, skill_data):
        queue = [
            {"message": "fix it", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "urgent"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        assert "[urgent]" in result.stdout

    def test_normal_no_label(self, skill_data):
        queue = [
            {"message": "just a note", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        assert "[normal]" not in result.stdout
        assert "just a note" in result.stdout


class TestSimmering:
    def test_simmering_promotes_after_3_days(self, skill_data):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        queue = [
            {"message": "old idea", "timestamp": old_ts, "status": "pending", "priority": "simmering"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        assert result.returncode == 0

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "normal"

    def test_simmering_does_not_promote_before_3_days(self, skill_data):
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        queue = [
            {"message": "fresh idea", "timestamp": recent_ts, "status": "pending", "priority": "simmering"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        _run_script(skill_data, "get.py")

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "simmering"

    def test_simmering_exactly_3_days(self, skill_data):
        ts_3_days = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        queue = [
            {"message": "boundary", "timestamp": ts_3_days, "status": "pending", "priority": "simmering"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        _run_script(skill_data, "get.py")

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "normal"

    def test_simmering_only_promotes_pending(self, skill_data):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        queue = [
            {"message": "already done", "timestamp": old_ts, "status": "delivered", "priority": "simmering"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        _run_script(skill_data, "get.py")

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["priority"] == "simmering"

    def test_promoted_simmering_sorts_as_normal(self, skill_data):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        queue = [
            {"message": "curious thing", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "curious"},
            {"message": "promoted idea", "timestamp": old_ts, "status": "pending", "priority": "simmering"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "get.py")
        lines = [l for l in result.stdout.splitlines() if l.startswith("- ")]
        assert "promoted idea" in lines[0]
        assert "curious thing" in lines[1]


class TestClear:
    def test_clears_pending(self, skill_data):
        _run_script(skill_data, "add.py", ["one"])
        _run_script(skill_data, "add.py", ["two"])
        result = _run_script(skill_data, "clear.py")
        assert result.returncode == 0
        assert "Marked all 2 pending item(s) done." in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert all(item["status"] == "done" for item in queue)

    def test_clear_empty(self, skill_data):
        result = _run_script(skill_data, "clear.py")
        assert result.returncode == 0
        assert "No carry-over items to clear" in result.stdout

    def test_clear_already_delivered(self, skill_data):
        queue = [
            {"message": "done", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "clear.py")
        assert "No pending" in result.stdout

    def test_clear_all_removes_everything(self, skill_data):
        queue = [
            {"message": "pending one", "timestamp": "2026-01-01T00:00:00+00:00", "status": "pending", "priority": "normal"},
            {"message": "delivered one", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "clear.py", ["--all"])
        assert result.returncode == 0
        assert "Cleared all 2" in result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue == []

    def test_clear_all_empty_queue(self, skill_data):
        (skill_data / "queue.json").write_text("[]")
        result = _run_script(skill_data, "clear.py", ["--all"])
        assert "No carry-over items to clear" in result.stdout

    def test_clear_preserves_delivered(self, skill_data):
        queue = [
            {"message": "already done", "timestamp": "2026-01-01T00:00:00+00:00", "status": "delivered", "priority": "normal"},
            {"message": "still pending", "timestamp": "2026-01-02T00:00:00+00:00", "status": "pending", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        _run_script(skill_data, "clear.py")
        queue = json.loads((skill_data / "queue.json").read_text())
        assert len(queue) == 2
        # The legacy status still counts as resolved: only "pending" shows.
        assert [item["status"] for item in queue] == ["delivered", "done"]


class TestDone:
    """The action that replaced auto-clearing.

    Items used to be marked delivered after the agent's first successful
    reply, so the list emptied itself whether or not anything had been done
    about an item. Nothing resolves an item now except this.
    """

    def _queue(self, skill_data, messages):
        queue = [
            {"message": m, "timestamp": f"2026-01-{i + 1:02d}T00:00:00+00:00",
             "status": "pending", "priority": "normal"}
            for i, m in enumerate(messages)
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))

    def _statuses(self, skill_data):
        queue = json.loads((skill_data / "queue.json").read_text())
        return {item["message"]: item["status"] for item in queue}

    def test_marks_one_item_by_its_list_number(self, skill_data):
        self._queue(skill_data, ["first", "second", "third"])
        result = _run_script(skill_data, "done.py", ["2"])
        assert result.returncode == 0
        assert "Done: second" in result.stdout
        assert "2 pending item(s) left." in result.stdout
        assert self._statuses(skill_data) == {
            "first": "pending", "second": "done", "third": "pending",
        }

    def test_marks_several_items(self, skill_data):
        self._queue(skill_data, ["first", "second", "third"])
        assert _run_script(skill_data, "done.py", ["1", "3"]).returncode == 0
        assert self._statuses(skill_data) == {
            "first": "done", "second": "pending", "third": "done",
        }

    def test_numbering_follows_the_list_order_not_the_file_order(self, skill_data):
        """list sorts by priority then age, and done has to agree with it."""
        queue = [
            {"message": "low", "timestamp": "2026-01-01T00:00:00+00:00",
             "status": "pending", "priority": "curious"},
            {"message": "high", "timestamp": "2026-01-02T00:00:00+00:00",
             "status": "pending", "priority": "urgent"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        _run_script(skill_data, "done.py", ["1"])
        assert self._statuses(skill_data) == {"high": "done", "low": "pending"}

    def test_resolved_items_are_not_numbered(self, skill_data):
        queue = [
            {"message": "already", "timestamp": "2026-01-01T00:00:00+00:00",
             "status": "done", "priority": "normal"},
            {"message": "outstanding", "timestamp": "2026-01-02T00:00:00+00:00",
             "status": "pending", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        _run_script(skill_data, "done.py", ["1"])
        assert self._statuses(skill_data) == {
            "already": "done", "outstanding": "done",
        }

    def test_an_out_of_range_number_changes_nothing(self, skill_data):
        """Partial application would be worse than refusing the whole request."""
        self._queue(skill_data, ["first", "second"])
        result = _run_script(skill_data, "done.py", ["1", "9"])
        assert result.returncode != 0
        assert "no item 9" in result.stderr
        assert self._statuses(skill_data) == {
            "first": "pending", "second": "pending",
        }

    def test_rejects_a_non_number(self, skill_data):
        self._queue(skill_data, ["first"])
        result = _run_script(skill_data, "done.py", ["second"])
        assert result.returncode != 0
        assert "not a number" in result.stderr
        assert self._statuses(skill_data) == {"first": "pending"}

    def test_rejects_zero_and_negative(self, skill_data):
        self._queue(skill_data, ["first"])
        for arg in ("0", "-1"):
            result = _run_script(skill_data, "done.py", [arg])
            assert result.returncode != 0
            assert "start at 1" in result.stderr
        assert self._statuses(skill_data) == {"first": "pending"}

    def test_requires_an_argument(self, skill_data):
        self._queue(skill_data, ["first"])
        result = _run_script(skill_data, "done.py")
        assert result.returncode != 0
        assert "at least one item number" in result.stderr

    def test_repeated_number_is_not_counted_twice(self, skill_data):
        self._queue(skill_data, ["first", "second"])
        result = _run_script(skill_data, "done.py", ["1", "1"])
        assert result.returncode == 0
        assert result.stdout.count("Done:") == 1
        assert self._statuses(skill_data) == {"first": "done", "second": "pending"}

    def test_no_queue_file(self, skill_data):
        result = _run_script(skill_data, "done.py", ["1"])
        assert result.returncode == 0
        assert "No carry-over items." in result.stdout

    def test_nothing_pending(self, skill_data):
        queue = [
            {"message": "old", "timestamp": "2026-01-01T00:00:00+00:00",
             "status": "done", "priority": "normal"},
        ]
        (skill_data / "queue.json").write_text(json.dumps(queue))
        result = _run_script(skill_data, "done.py", ["1"])
        assert result.returncode == 0
        assert "No pending carry-over items." in result.stdout


class TestItemsPersistUntilDone:
    """The behaviour the redesign is for."""

    def test_get_repeated_across_sessions_keeps_returning_the_item(self, skill_data):
        _run_script(skill_data, "add.py", ["chase the invoice"])
        for _ in range(3):
            result = _run_script(skill_data, "get.py")
            assert "chase the invoice" in result.stdout

        _run_script(skill_data, "done.py", ["1"])
        assert "chase the invoice" not in _run_script(skill_data, "get.py").stdout


class TestLifecycle:
    def test_add_list_get_clear_cycle(self, skill_data):
        _run_script(skill_data, "add.py", ["message one"])
        _run_script(skill_data, "add.py", ["--priority", "urgent", "message two"])

        list_result = _run_script(skill_data, "list.py")
        assert "2 pending" in list_result.stdout

        get_result = _run_script(skill_data, "get.py")
        assert "message one" in get_result.stdout
        assert "message two" in get_result.stdout

        queue = json.loads((skill_data / "queue.json").read_text())
        assert all(item["status"] == "pending" for item in queue)

        clear_result = _run_script(skill_data, "clear.py")
        assert "Marked all 2 pending item(s) done." in clear_result.stdout

        list_final = _run_script(skill_data, "list.py")
        assert "No pending" in list_final.stdout

    def test_queue_persistence(self, skill_data):
        _run_script(skill_data, "add.py", ["persisted"])
        queue = json.loads((skill_data / "queue.json").read_text())
        assert len(queue) == 1
        assert queue[0]["message"] == "persisted"
        assert queue[0]["status"] == "pending"

        _run_script(skill_data, "get.py")
        queue = json.loads((skill_data / "queue.json").read_text())
        assert queue[0]["status"] == "pending"

        _run_script(skill_data, "add.py", ["second"])
        queue = json.loads((skill_data / "queue.json").read_text())
        assert len(queue) == 2
        assert queue[0]["status"] == "pending"
        assert queue[1]["status"] == "pending"
