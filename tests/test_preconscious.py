"""Tests for the preconscious skill scripts."""
from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parent.parent / "templates" / "workspace" / "skills" / "preconscious" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from add import add_item, MAX_ITEMS
from run import decay_buffer
from drop_lowest import drop_lowest
from read import read_buffer


# --- Helpers ---


def make_item(desc: str, c: int, i: int) -> dict:
    return {"description": desc, "c": c, "i": i, "added": "2026-05-13T00:00:00+00:00"}


def write_buffer(buffer_file: Path, items: list[dict]) -> None:
    buffer_file.parent.mkdir(parents=True, exist_ok=True)
    buffer_file.write_text(json.dumps({"items": items}, indent=2) + "\n")


# --- Add ---


class TestAdd:
    def test_add_to_empty_buffer(self, tmp_path):
        bf = tmp_path / "buffer.json"
        result = add_item(bf, "test item", 5, 3)
        assert "Added [C:5, I:3]" in result
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["description"] == "test item"
        assert data["items"][0]["c"] == 5
        assert data["items"][0]["i"] == 3

    def test_add_clamps_values(self, tmp_path):
        bf = tmp_path / "buffer.json"
        add_item(bf, "clamped", 10, -1)
        data = json.loads(bf.read_text())
        assert data["items"][0]["c"] == 5
        assert data["items"][0]["i"] == 1

    def test_duplicate_updates_existing(self, tmp_path):
        bf = tmp_path / "buffer.json"
        add_item(bf, "auth bug", 5, 3)
        result = add_item(bf, "auth bug", 4, 5)
        assert "Updated existing item" in result
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["c"] == 4
        assert data["items"][0]["i"] == 5
        assert "updated" in data["items"][0]

    def test_duplicate_does_not_increase_count(self, tmp_path):
        bf = tmp_path / "buffer.json"
        for n in range(5):
            add_item(bf, f"item {n}", 5, 3)
        add_item(bf, "item 0", 3, 4)
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 5

    def test_buffer_cap_drops_lowest(self, tmp_path):
        bf = tmp_path / "buffer.json"
        items = [make_item(f"item {n}", 5, 3) for n in range(5)]
        write_buffer(bf, items)

        result = add_item(bf, "new high item", 5, 5)
        assert "Dropped" in result
        data = json.loads(bf.read_text())
        assert len(data["items"]) == MAX_ITEMS
        descriptions = [it["description"] for it in data["items"]]
        assert "new high item" in descriptions

    def test_buffer_cap_drops_correct_item(self, tmp_path):
        bf = tmp_path / "buffer.json"
        items = [
            make_item("high", 5, 5),
            make_item("medium", 3, 3),
            make_item("low", 1, 1),
            make_item("mid-high", 4, 4),
            make_item("mid", 3, 2),
        ]
        write_buffer(bf, items)

        result = add_item(bf, "newcomer", 5, 3)
        data = json.loads(bf.read_text())
        descriptions = [it["description"] for it in data["items"]]
        assert "low" not in descriptions
        assert "newcomer" in descriptions

    def test_creates_skill_data_dir(self, tmp_path):
        bf = tmp_path / "nested" / "dir" / "buffer.json"
        add_item(bf, "test", 5, 3)
        assert bf.exists()


# --- Decay ---


class TestDecay:
    def test_decay_decrements_currency(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("test", 5, 3)])
        decay_buffer(bf)
        data = json.loads(bf.read_text())
        assert data["items"][0]["c"] == 4

    def test_decay_drops_expired_low_importance(self, tmp_path):
        bf = tmp_path / "buffer.json"
        items = [
            make_item("expiring", 1, 2),
            make_item("surviving", 1, 3),
        ]
        write_buffer(bf, items)
        result = decay_buffer(bf)
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["description"] == "surviving"
        assert "Dropping (expired)" in result
        assert "expiring" in result

    def test_decay_preserves_high_importance(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("important", 1, 5)])
        decay_buffer(bf)
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["c"] == 0
        assert data["items"][0]["description"] == "important"

    def test_decay_preserves_importance_3(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("moderate", 1, 3)])
        decay_buffer(bf)
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 1

    def test_decay_empty_buffer(self, tmp_path):
        bf = tmp_path / "buffer.json"
        result = decay_buffer(bf)
        assert "empty" in result.lower()

    def test_decay_multiple_cycles(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("fleeting", 3, 1)])
        for _ in range(3):
            decay_buffer(bf)
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 0

    def test_decay_standalone_no_env(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [
            make_item("a", 3, 4),
            make_item("b", 1, 1),
        ])
        result = decay_buffer(bf)
        assert "2 -> 2" in result or "C:2" in result
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["description"] == "a"


# --- Read ---


class TestRead:
    def test_read_empty_buffer(self, tmp_path):
        bf = tmp_path / "buffer.json"
        result = read_buffer(bf)
        assert "empty" in result.lower()

    def test_read_sorted_by_effective_score(self, tmp_path):
        bf = tmp_path / "buffer.json"
        items = [
            make_item("low", 1, 1),
            make_item("high", 5, 5),
            make_item("mid", 3, 3),
        ]
        write_buffer(bf, items)
        result = read_buffer(bf)
        lines = [line for line in result.split("\n") if line.startswith("- ")]
        assert len(lines) == 3
        assert "high" in lines[0]
        assert "mid" in lines[1]
        assert "low" in lines[2]

    def test_read_includes_scores(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("test", 4, 3)])
        result = read_buffer(bf)
        assert "[C:4, I:3]" in result

    def test_read_outputs_markdown_header(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("test", 5, 3)])
        result = read_buffer(bf)
        assert "## Preconscious Buffer" in result

    def test_read_missing_file(self, tmp_path):
        bf = tmp_path / "nonexistent" / "buffer.json"
        result = read_buffer(bf)
        assert "empty" in result.lower()


# --- Drop Lowest ---


class TestDropLowest:
    def test_drop_removes_lowest(self, tmp_path):
        bf = tmp_path / "buffer.json"
        items = [
            make_item("high", 5, 5),
            make_item("low", 1, 1),
            make_item("mid", 3, 3),
        ]
        write_buffer(bf, items)
        result = drop_lowest(bf)
        assert "low" in result
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 2
        descriptions = [it["description"] for it in data["items"]]
        assert "low" not in descriptions

    def test_drop_empty_buffer(self, tmp_path):
        bf = tmp_path / "buffer.json"
        result = drop_lowest(bf)
        assert "empty" in result.lower()

    def test_drop_single_item(self, tmp_path):
        bf = tmp_path / "buffer.json"
        write_buffer(bf, [make_item("only", 3, 3)])
        drop_lowest(bf)
        data = json.loads(bf.read_text())
        assert len(data["items"]) == 0


# --- Buffer cap integration ---


class TestBufferCap:
    def test_never_exceeds_max(self, tmp_path):
        bf = tmp_path / "buffer.json"
        for n in range(10):
            add_item(bf, f"item {n}", 5, n % 5 + 1)
        data = json.loads(bf.read_text())
        assert len(data["items"]) <= MAX_ITEMS

    def test_highest_scores_survive(self, tmp_path):
        bf = tmp_path / "buffer.json"
        add_item(bf, "low1", 1, 1)
        add_item(bf, "low2", 1, 2)
        add_item(bf, "mid", 3, 3)
        add_item(bf, "high1", 5, 4)
        add_item(bf, "high2", 5, 5)
        add_item(bf, "new_high", 5, 5)
        data = json.loads(bf.read_text())
        descriptions = [it["description"] for it in data["items"]]
        assert "low1" not in descriptions
        assert "new_high" in descriptions
        assert "high2" in descriptions


class TestBufferShapeIsUntrusted:
    """P6-M1/L3/L4: the agent can write this file, and the shape was trusted."""

    def _add(self, tmp_path, *args):
        from importlib import util
        spec = util.spec_from_file_location(
            "pre_add",
            Path(__file__).resolve().parent.parent
            / "templates" / "workspace" / "skills" / "preconscious" / "scripts" / "add.py",
        )
        mod = util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.add_item(tmp_path / "buffer.json", *args)

    def test_object_without_items_does_not_traceback(self, tmp_path):
        (tmp_path / "buffer.json").write_text("{}")
        assert "Added" in self._add(tmp_path, "note", 5, 3)

    def test_list_instead_of_object_does_not_traceback(self, tmp_path):
        (tmp_path / "buffer.json").write_text("[]")
        assert "Added" in self._add(tmp_path, "note", 5, 3)

    def test_malformed_items_are_dropped_not_indexed(self, tmp_path):
        (tmp_path / "buffer.json").write_text(
            json.dumps({"items": ["not an object", {"description": "ok", "c": 3, "i": 3}]}),
        )
        assert "Added" in self._add(tmp_path, "note", 5, 3)

    def test_lowest_scoring_newcomer_is_not_reported_as_added(self, tmp_path):
        (tmp_path / "buffer.json").write_text(json.dumps({"items": [
            {"description": f"item{n}", "c": 5, "i": 5} for n in range(5)
        ]}))

        result = self._add(tmp_path, "minor note", 1, 1)

        assert "Not added" in result
        assert "Dropped" not in result
        stored = json.loads((tmp_path / "buffer.json").read_text())
        assert all(i["description"] != "minor note" for i in stored["items"])
