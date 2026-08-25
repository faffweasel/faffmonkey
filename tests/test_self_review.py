import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "templates" / "workspace" / "skills" / "self-review" / "scripts"
)


def _run_script(workspace, script_name, args=None):
    env = {
        "WORKSPACE": str(workspace),
        "SKILL_DATA": str(workspace / "skills-data" / "self-review"),
        "TZ": "UTC",
    }
    cmd = [sys.executable, str(SCRIPTS_DIR / script_name)]
    if args:
        cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class TestAdd:
    def test_creates_learnings_file(self, tmp_path):
        result = _run_script(tmp_path, "add.py", [
            "--tag", "LRN", "--summary", "Test learning",
        ])
        assert result.returncode == 0
        assert (tmp_path / "LEARNINGS.md").exists()
        assert "Logged LRN-" in result.stdout

    def test_correct_entry_format(self, tmp_path):
        _run_script(tmp_path, "add.py", [
            "--tag", "LRN",
            "--summary", "Use urllib not requests",
            "--priority", "high",
            "--area", "backend",
            "--details", "stdlib-only project",
        ])
        text = (tmp_path / "LEARNINGS.md").read_text()
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert f"## [LRN-{today}-001] learning" in text
        assert "**Status**: pending" in text
        assert "**Priority**: high" in text
        assert "**Area**: backend" in text
        assert "**Summary**: Use urllib not requests" in text
        assert "**Details**: stdlib-only project" in text

    def test_sequential_ids_same_day(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "First"])
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "Second"])
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "Third"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert f"[LRN-{today}-001]" in text
        assert f"[LRN-{today}-002]" in text
        assert f"[LRN-{today}-003]" in text

    def test_different_tags_independent_ids(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "Learning"])
        _run_script(tmp_path, "add.py", ["--tag", "ERR", "--summary", "Error"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert f"[LRN-{today}-001]" in text
        assert f"[ERR-{today}-001]" in text

    def test_err_tag_label(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "ERR", "--summary", "Build failed"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "] error" in text

    def test_feat_tag_label(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "FEAT", "--summary", "Need CSV export"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "] feature" in text

    def test_default_priority_is_medium(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "Something"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "**Priority**: medium" in text

    def test_optional_area_omitted(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "No area"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "**Area**" not in text

    def test_optional_details_omitted(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "No details"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "**Details**" not in text

    def test_rejects_missing_tag(self, tmp_path):
        result = _run_script(tmp_path, "add.py", ["--summary", "No tag"])
        assert result.returncode != 0
        assert "--tag required" in result.stderr

    def test_rejects_invalid_tag(self, tmp_path):
        result = _run_script(tmp_path, "add.py", ["--tag", "BUG", "--summary", "Invalid"])
        assert result.returncode != 0
        assert "invalid tag" in result.stderr

    def test_rejects_missing_summary(self, tmp_path):
        result = _run_script(tmp_path, "add.py", ["--tag", "LRN"])
        assert result.returncode != 0
        assert "--summary required" in result.stderr

    def test_rejects_invalid_priority(self, tmp_path):
        result = _run_script(tmp_path, "add.py", [
            "--tag", "LRN", "--summary", "Test", "--priority", "urgent",
        ])
        assert result.returncode != 0
        assert "invalid priority" in result.stderr

    def test_file_heading_on_creation(self, tmp_path):
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "First"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert text.startswith("# Learnings\n")

    def test_appends_to_existing_file(self, tmp_path):
        (tmp_path / "LEARNINGS.md").write_text("# Learnings\n\n## [LRN-20260101-001] learning\n**Status**: pending\n**Priority**: medium\n**Summary**: Old entry\n")
        _run_script(tmp_path, "add.py", ["--tag", "LRN", "--summary", "New entry"])
        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "Old entry" in text
        assert "New entry" in text
        assert text.count("# Learnings") == 1


class TestReview:
    def test_detects_duplicates(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Always use urllib.request instead of requests library\n\n"
            f"## [LRN-{today}-002] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Use urllib.request not requests for HTTP calls\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "Duplicates" in result.stdout

    def test_detects_stale_entries(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{old_date}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Old entry about database connection pooling settings\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "Stale" in result.stdout

    def test_no_stale_for_recent(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Recent entry about unique specific database migration patterns\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "Stale" not in result.stdout

    def test_no_stale_for_promoted(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{old_date}-001] learning\n"
            "**Status**: promoted\n"
            "**Promoted**: AGENTS.md\n"
            "**Priority**: medium\n"
            "**Summary**: Already promoted entry about something\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "Stale" not in result.stdout

    def test_detects_internalised(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Always use concise formal communication style responses\n"
        )
        (tmp_path / "AGENTS.md").write_text(
            "Keep responses concise and use formal communication style.\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "Internalised" in result.stdout

    def test_detects_promotion_candidates(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Always use urllib.request instead of requests library\n\n"
            f"## [LRN-{today}-002] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Use urllib.request not requests for HTTP calls\n\n"
            f"## [ERR-{today}-001] error\n"
            "**Status**: pending\n"
            "**Priority**: high\n"
            "**Summary**: Import requests failed, must use urllib.request\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "Promotion Candidates" in result.stdout

    def test_clean_report(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Unique specific entry about database migration patterns\n\n"
            f"## [ERR-{today}-001] error\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Completely different topic about frontend accessibility testing\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "No Issues Found" in result.stdout

    def test_missing_learnings(self, tmp_path):
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "No LEARNINGS.md found" in result.stdout

    def test_empty_learnings(self, tmp_path):
        (tmp_path / "LEARNINGS.md").write_text("")
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "empty" in result.stdout

    def test_no_entries_just_heading(self, tmp_path):
        (tmp_path / "LEARNINGS.md").write_text("# Learnings\n\n")
        result = _run_script(tmp_path, "review.py")
        assert result.returncode == 0
        assert "No entries found" in result.stdout

    def test_entry_count(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: First unique entry about specific topic alpha\n\n"
            f"## [LRN-{today}-002] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Second completely different entry about topic beta\n\n"
            f"## [LRN-{today}-003] learning\n"
            "**Status**: promoted\n"
            "**Promoted**: AGENTS.md\n"
            "**Priority**: medium\n"
            "**Summary**: Third already promoted entry about gamma\n"
        )
        result = _run_script(tmp_path, "review.py")
        assert "Total entries: 3 (2 pending)" in result.stdout


class TestPromote:
    def test_promotes_to_agents(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: high\n"
            "**Summary**: Always regenerate API client after spec changes\n"
        )
        result = _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "agents",
        ])
        assert result.returncode == 0
        assert "Promoted" in result.stdout
        assert "AGENTS.md" in result.stdout

        agents_text = (tmp_path / "AGENTS.md").read_text()
        assert "Always regenerate API client after spec changes" in agents_text

        learnings_text = (tmp_path / "LEARNINGS.md").read_text()
        assert "**Status**: promoted" in learnings_text
        assert "**Promoted**: AGENTS.md" in learnings_text

    def test_promotes_to_memory(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Project uses pnpm not npm\n"
        )
        result = _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "memory",
        ])
        assert result.returncode == 0

        memory_text = (tmp_path / "MEMORY.md").read_text()
        assert "Project uses pnpm not npm" in memory_text

        learnings_text = (tmp_path / "LEARNINGS.md").read_text()
        assert "**Promoted**: MEMORY.md" in learnings_text

    def test_appends_to_existing_target(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "AGENTS.md").write_text("# Agent Rules\n\n- Existing rule\n")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: New rule to add\n"
        )
        _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "agents",
        ])
        agents_text = (tmp_path / "AGENTS.md").read_text()
        assert "Existing rule" in agents_text
        assert "New rule to add" in agents_text

    def test_refuses_already_promoted(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: promoted\n"
            "**Promoted**: AGENTS.md\n"
            "**Priority**: medium\n"
            "**Summary**: Already promoted rule\n"
        )
        result = _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "memory",
        ])
        assert result.returncode != 0
        assert "already promoted" in result.stderr

    def test_refuses_nonexistent_id(self, tmp_path):
        (tmp_path / "LEARNINGS.md").write_text("# Learnings\n\n")
        result = _run_script(tmp_path, "promote.py", [
            "--id", "LRN-20260101-999", "--target", "agents",
        ])
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_refuses_missing_learnings(self, tmp_path):
        result = _run_script(tmp_path, "promote.py", [
            "--id", "LRN-20260101-001", "--target", "agents",
        ])
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_refuses_invalid_target(self, tmp_path):
        result = _run_script(tmp_path, "promote.py", [
            "--id", "LRN-20260101-001", "--target", "tools",
        ])
        assert result.returncode != 0
        assert "invalid target" in result.stderr

    def test_preserves_other_entries(self, tmp_path):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        (tmp_path / "LEARNINGS.md").write_text(
            "# Learnings\n\n"
            f"## [LRN-{today}-001] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: First entry to promote\n\n"
            f"## [LRN-{today}-002] learning\n"
            "**Status**: pending\n"
            "**Priority**: medium\n"
            "**Summary**: Second entry stays pending\n"
        )
        _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "agents",
        ])
        learnings_text = (tmp_path / "LEARNINGS.md").read_text()
        assert "Second entry stays pending" in learnings_text

        lines_with_status = [l for l in learnings_text.splitlines() if "**Status**:" in l]
        assert lines_with_status[0].strip() == "**Status**: promoted"
        assert lines_with_status[1].strip() == "**Status**: pending"


class TestLifecycle:
    def test_add_review_promote_cycle(self, tmp_path):
        _run_script(tmp_path, "add.py", [
            "--tag", "LRN", "--summary", "Always use urllib.request for HTTP",
            "--priority", "high", "--area", "backend",
        ])
        _run_script(tmp_path, "add.py", [
            "--tag", "ERR", "--summary", "Build failed on ARM64 platform",
            "--area", "infra",
        ])

        review_result = _run_script(tmp_path, "review.py")
        assert review_result.returncode == 0
        assert "Total entries: 2" in review_result.stdout

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        promote_result = _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "agents",
        ])
        assert promote_result.returncode == 0

        text = (tmp_path / "LEARNINGS.md").read_text()
        assert "**Status**: promoted" in text
        assert "**Promoted**: AGENTS.md" in text

        agents_text = (tmp_path / "AGENTS.md").read_text()
        assert "urllib.request" in agents_text

        second_promote = _run_script(tmp_path, "promote.py", [
            "--id", f"LRN-{today}-001", "--target", "memory",
        ])
        assert second_promote.returncode != 0
        assert "already promoted" in second_promote.stderr
