import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from faffmonkey.config import CompactionConfig, Config, HeartbeatConfig, ModelConfig
from faffmonkey.runtime.loop import handle_slash_command
from faffmonkey.runtime.skills import (
    invoke,
    load_full,
    parse_frontmatter,
    parse_media_lines,
    scan_skills,
)
from faffmonkey.runtime.tools import ToolRegistry
from faffmonkey.types import ToolCall


def _make_config(**overrides) -> Config:
    defaults = {
        "models": {
            "main": ModelConfig(
                provider="ollama-local", model="llama3",
                base_url="http://localhost:11434/v1", api_key="",
            ),
        },
        "routing": {"conversation": "main"},
        "fallback_models": [],
        "timezone": ZoneInfo("UTC"),
        "heartbeat": HeartbeatConfig(),
        "compaction": CompactionConfig(),
        "channels": {},
        "tool_permissions": {
            "file_read": "always",
            "file_write": "always",
            "web_search": "always",
            "web_fetch": "always",
            "shell_exec": "ask",
            "skill_invoke": "always",
        },
    }
    defaults.update(overrides)
    return Config(**defaults)


def _setup_skill(workspace, name, frontmatter, script_name=None, script_content=None):
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(frontmatter)
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    if script_name and script_content:
        script = scripts_dir / script_name
        script.write_text(script_content)
    return skill_dir


class TestParseFrontmatter:
    def test_basic(self):
        text = "---\nname: weather\ndescription: Check weather\n---\nbody"
        result = parse_frontmatter(text)
        assert result["name"] == "weather"
        assert result["description"] == "Check weather"

    def test_empty_string(self):
        assert parse_frontmatter("") == {}

    def test_no_frontmatter(self):
        assert parse_frontmatter("just some text") == {}

    def test_quoted_values(self):
        text = '---\nname: "my-skill"\ndescription: \'does stuff\'\n---\n'
        result = parse_frontmatter(text)
        assert result["name"] == "my-skill"
        assert result["description"] == "does stuff"

    def test_dashes_in_key(self):
        text = "---\nlong-key: value\n---\n"
        result = parse_frontmatter(text)
        assert result["long-key"] == "value"

    def test_multiline_body_after_frontmatter(self):
        text = "---\nname: test\n---\nline1\nline2"
        result = parse_frontmatter(text)
        assert result["name"] == "test"

    def test_actions_field(self):
        text = "---\nname: cron-manager\nactions: list, add, disable, enable\n---\n"
        result = parse_frontmatter(text)
        assert result["actions"] == "list, add, disable, enable"

    def test_underscores_in_key(self):
        text = "---\nmy_key: value\n---\n"
        result = parse_frontmatter(text)
        assert result["my_key"] == "value"

    def test_ignores_indented_lines(self):
        text = "---\nname: test\n  indented: ignored\n---\n"
        result = parse_frontmatter(text)
        assert result == {"name": "test"}

    def test_colon_in_value(self):
        text = "---\nname: weather\ndescription: Check weather: AQI and temp\n---\n"
        result = parse_frontmatter(text)
        assert result["description"] == "Check weather: AQI and temp"


class TestScanSkills:
    def test_scans_multiple_skills(self, tmp_path):
        _setup_skill(tmp_path, "weather", "---\nname: weather\ndescription: Check weather\n---\n")
        _setup_skill(tmp_path, "cron", "---\nname: cron-manager\ndescription: Manage cron\n---\n")
        result = scan_skills(tmp_path)
        assert len(result) == 2
        names = [r[0] for r in result]
        assert "weather" in names
        assert "cron-manager" in names

    def test_skips_dir_without_skill_md(self, tmp_path):
        (tmp_path / "skills" / "incomplete").mkdir(parents=True)
        (tmp_path / "skills" / "incomplete" / "README.md").write_text("nothing")
        assert scan_skills(tmp_path) == []

    def test_uses_dirname_when_no_name(self, tmp_path):
        _setup_skill(tmp_path, "my-skill", "---\ndescription: does stuff\n---\n")
        result = scan_skills(tmp_path)
        assert result[0][0] == "my-skill"
        assert result[0][1] == "does stuff"

    def test_no_skills_dir(self, tmp_path):
        assert scan_skills(tmp_path) == []

    def test_nonexistent_workspace(self, tmp_path):
        assert scan_skills(tmp_path / "nope") == []

    def test_skips_files_in_skills_dir(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "stray_file.md").write_text("not a skill dir")
        assert scan_skills(tmp_path) == []

    def test_sorted_by_dirname(self, tmp_path):
        for name in ["zeta", "alpha", "mid"]:
            _setup_skill(tmp_path, name, f"---\nname: {name}\ndescription: d\n---\n")
        result = scan_skills(tmp_path)
        assert [r[0] for r in result] == ["alpha", "mid", "zeta"]

    def test_live_on_creation(self, tmp_path):
        assert scan_skills(tmp_path) == []
        _setup_skill(tmp_path, "new-skill", "---\nname: new-skill\ndescription: new\n---\n")
        result = scan_skills(tmp_path)
        assert len(result) == 1
        assert result[0][0] == "new-skill"


class TestLoadFull:
    def test_loads_full_content(self, tmp_path):
        content = "---\nname: weather\ndescription: Check weather\n---\n\n# Weather\nFull docs here."
        _setup_skill(tmp_path, "weather", content)
        result = load_full(tmp_path, "weather")
        assert result == content

    def test_returns_none_for_missing(self, tmp_path):
        assert load_full(tmp_path, "nonexistent") is None

    def test_returns_none_when_dir_exists_but_no_skill_md(self, tmp_path):
        (tmp_path / "skills" / "broken").mkdir(parents=True)
        assert load_full(tmp_path, "broken") is None


class TestParseMediaLines:
    def test_single_media_line(self, tmp_path):
        media_path = tmp_path / "shared" / "chart.png"
        output = f"some text\nMEDIA: {media_path}\nmore text"
        result = parse_media_lines(output, tmp_path)
        assert len(result) == 1
        assert result[0] == media_path.resolve()

    def test_multiple_media_lines(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.jpg"
        output = f"MEDIA: {a}\ntext\nMEDIA: {b}"
        result = parse_media_lines(output, tmp_path)
        assert len(result) == 2
        assert result[0] == a.resolve()
        assert result[1] == b.resolve()

    def test_no_media_lines(self, tmp_path):
        assert parse_media_lines("just text\nno media here", tmp_path) == []

    def test_empty_string(self, tmp_path):
        assert parse_media_lines("", tmp_path) == []

    def test_media_with_spaces_in_path(self, tmp_path):
        media_path = tmp_path / "shared" / "my chart.png"
        result = parse_media_lines(f"MEDIA: {media_path}", tmp_path)
        assert result[0] == media_path.resolve()

    def test_media_line_with_leading_spaces(self, tmp_path):
        media_path = tmp_path / "shared" / "file.png"
        result = parse_media_lines(f"  MEDIA: {media_path}", tmp_path)
        assert len(result) == 1

    def test_empty_media_path_skipped(self, tmp_path):
        assert parse_media_lines("MEDIA: ", tmp_path) == []

    def test_path_outside_workspace_rejected(self, tmp_path):
        result = parse_media_lines("MEDIA: /etc/passwd", tmp_path)
        assert result == []

    def test_traversal_path_rejected(self, tmp_path):
        result = parse_media_lines(f"MEDIA: {tmp_path}/../../../etc/passwd", tmp_path)
        assert result == []

    def test_relative_path_inside_workspace(self, tmp_path):
        result = parse_media_lines("MEDIA: shared/output.png", tmp_path)
        assert len(result) == 1
        assert result[0] == (tmp_path / "shared" / "output.png").resolve()


class TestInvoke:
    def test_runs_script_and_captures_output(self, tmp_path):
        script = "import sys; print('hello from skill')"
        _setup_skill(tmp_path, "greeter", "---\nname: greeter\n---\n", "greet.py", script)
        output, attachments, is_error = invoke(tmp_path, "greeter", "greet")
        assert "hello from skill" in output
        assert not is_error
        assert attachments == []

    def test_script_not_found(self, tmp_path):
        _setup_skill(tmp_path, "empty", "---\nname: empty\n---\n")
        output, attachments, is_error = invoke(tmp_path, "empty", "missing")
        assert "script not found" in output
        assert is_error

    def test_script_error_returns_stderr(self, tmp_path):
        script = "import sys; print('oh no', file=sys.stderr); sys.exit(1)"
        _setup_skill(tmp_path, "failing", "---\nname: failing\n---\n", "fail.py", script)
        output, attachments, is_error = invoke(tmp_path, "failing", "fail")
        assert "oh no" in output
        assert is_error
        assert "[exit code: 1]" in output

    def test_media_lines_in_output(self, tmp_path):
        media_path = tmp_path / "shared" / "out.png"
        script = f"print('result data'); print('MEDIA: {media_path}')"
        _setup_skill(tmp_path, "media-skill", "---\nname: media-skill\n---\n", "run.py", script)
        output, attachments, is_error = invoke(tmp_path, "media-skill", "run")
        assert not is_error
        assert len(attachments) == 1
        assert attachments[0] == media_path.resolve()

    def test_environment_variables(self, tmp_path):
        script = (
            "import os\n"
            "print(f'WORKSPACE={os.environ.get(\"WORKSPACE\", \"\")}')\n"
            "print(f'SKILL_DATA={os.environ.get(\"SKILL_DATA\", \"\")}')\n"
            "print(f'TZ={os.environ.get(\"TZ\", \"\")}')\n"
            "print(f'INHERITED={os.environ.get(\"_FAFFMONKEY_TEST_MARKER\", \"\")}')\n"
        )
        _setup_skill(tmp_path, "env-test", "---\nname: env-test\n---\n", "check.py", script)
        with patch.dict(os.environ, {"_FAFFMONKEY_TEST_MARKER": "present"}):
            output, _, is_error = invoke(tmp_path, "env-test", "check", tz="Asia/Ho_Chi_Minh")
        assert not is_error
        assert f"WORKSPACE={tmp_path}" in output
        assert f"SKILL_DATA={tmp_path / 'skills-data' / 'env-test'}" in output
        assert "TZ=Asia/Ho_Chi_Minh" in output
        assert "INHERITED=present" in output

    def test_creates_skill_data_dir(self, tmp_path):
        script = "print('ok')"
        _setup_skill(tmp_path, "data-test", "---\nname: data-test\n---\n", "run.py", script)
        data_dir = tmp_path / "skills-data" / "data-test"
        assert not data_dir.exists()
        invoke(tmp_path, "data-test", "run")
        assert data_dir.is_dir()

    def test_passes_args_to_script(self, tmp_path):
        script = "import sys; print(' '.join(sys.argv[1:]))"
        _setup_skill(tmp_path, "args-test", "---\nname: args-test\n---\n", "run.py", script)
        output, _, is_error = invoke(tmp_path, "args-test", "run", args=["hello", "world"])
        assert not is_error
        assert "hello world" in output

    def test_timeout(self, tmp_path):
        script = "import time; time.sleep(120)"
        _setup_skill(tmp_path, "slow", "---\nname: slow\n---\n", "run.py", script)
        with patch("faffmonkey.runtime.skills.SKILL_TIMEOUT", 1):
            output, _, is_error = invoke(tmp_path, "slow", "run")
        assert is_error
        assert "timed out" in output

    def test_timeout_clamped_to_max(self, tmp_path):
        script = "import time; time.sleep(120)"
        _setup_skill(tmp_path, "slow", "---\nname: slow\ntimeout: 9999\n---\n", "run.py", script)
        with patch("faffmonkey.runtime.skills._MAX_SKILL_TIMEOUT", 1):
            output, _, is_error = invoke(tmp_path, "slow", "run")
        assert is_error
        assert "timed out (1s)" in output

    def test_finds_script_without_extension(self, tmp_path):
        skill_dir = tmp_path / "skills" / "bare-script"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: bare-script\n---\n")
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "run"
        script.write_text("#!/usr/bin/env python3\nprint('bare')")
        script.chmod(0o755)
        output, _, is_error = invoke(tmp_path, "bare-script", "run")
        assert "bare" in output
        assert not is_error


class TestToolRegistrySkillInvoke:
    def test_skill_invoke_loads_md_when_no_action(self, tmp_path):
        content = "---\nname: weather\ndescription: Check weather\n---\n\n# Weather docs"
        _setup_skill(tmp_path, "weather", content)
        reg = ToolRegistry(
            workspace=tmp_path,
            permissions={"skill_invoke": "always"},
            shell_preapproved=[],
        )
        call = ToolCall(id="1", name="skill_invoke", arguments={"name": "weather"})
        result = reg.dispatch(call)
        assert "SKILL.md for weather" in result.content
        assert "# Weather docs" in result.content
        assert not result.is_error

    def test_skill_invoke_runs_action(self, tmp_path):
        script = "print('invoked')"
        _setup_skill(tmp_path, "runner", "---\nname: runner\n---\n", "do.py", script)
        reg = ToolRegistry(
            workspace=tmp_path,
            permissions={"skill_invoke": "always"},
            shell_preapproved=[],
        )
        call = ToolCall(id="2", name="skill_invoke", arguments={"name": "runner", "input": "do"})
        result = reg.dispatch(call)
        assert "invoked" in result.content
        assert not result.is_error

    def test_skill_invoke_not_found(self, tmp_path):
        reg = ToolRegistry(
            workspace=tmp_path,
            permissions={"skill_invoke": "always"},
            shell_preapproved=[],
        )
        call = ToolCall(id="3", name="skill_invoke", arguments={"name": "missing"})
        result = reg.dispatch(call)
        assert result.is_error
        assert "not found" in result.content

    def test_skill_invoke_with_attachments(self, tmp_path):
        media_path = tmp_path / "shared" / "out.csv"
        script = f"print('data'); print('MEDIA: {media_path}')"
        _setup_skill(tmp_path, "media", "---\nname: media\n---\n", "export.py", script)
        reg = ToolRegistry(
            workspace=tmp_path,
            permissions={"skill_invoke": "always"},
            shell_preapproved=[],
        )
        call = ToolCall(id="4", name="skill_invoke", arguments={"name": "media", "input": "export"})
        result = reg.dispatch(call)
        assert "Attachments:" in result.content
        assert "out.csv" in result.content


class TestSlashSkillCommand:
    def test_skill_loads_md(self, tmp_path):
        content = "---\nname: weather\ndescription: Check weather\n---\n\n# Full docs"
        _setup_skill(tmp_path, "weather", content)
        config = _make_config()
        result = handle_slash_command("/skill weather", config, lambda: None, workspace=tmp_path)
        assert "SKILL.md for weather" in result
        assert "# Full docs" in result

    def test_skill_runs_action(self, tmp_path):
        script = "print('from slash')"
        _setup_skill(tmp_path, "runner", "---\nname: runner\n---\n", "go.py", script)
        config = _make_config()
        result = handle_slash_command("/skill runner go", config, lambda: None, workspace=tmp_path)
        assert "from slash" in result

    def test_skill_not_found_lists_available(self, tmp_path):
        _setup_skill(tmp_path, "real-skill", "---\nname: real-skill\ndescription: exists\n---\n")
        config = _make_config()
        result = handle_slash_command("/skill bogus", config, lambda: None, workspace=tmp_path)
        assert "not found" in result
        assert "real-skill" in result

    def test_skill_not_found_no_skills(self, tmp_path):
        config = _make_config()
        result = handle_slash_command("/skill bogus", config, lambda: None, workspace=tmp_path)
        assert "not found" in result
        assert "No skills installed" in result

    def test_skill_no_args(self):
        config = _make_config()
        result = handle_slash_command("/skill", config, lambda: None)
        assert "usage" in result.lower()


class TestCronManagerTemplate:
    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path
        (ws / "config").mkdir()
        (ws / "config" / "jobs.json").write_text("[]\n")
        return ws

    def _run_script(self, workspace, action, args=None, tz="UTC"):
        script = (
            Path(__file__).resolve().parent.parent
            / "templates" / "workspace" / "skills" / "cron-manager" / "scripts"
            / f"{action}.py"
        )
        env = {
            "WORKSPACE": str(workspace),
            "SKILL_DATA": str(workspace / "skills-data" / "cron-manager"),
            "TZ": tz,
        }
        cmd = [sys.executable, str(script)]
        if args:
            cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_update_changes_fields_in_place(self, workspace):
        """The agent's only way to change a job was disable plus add, and
        add refused the existing id, so the job stayed disabled."""
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "heartbeat", "schedule": "0 * * * *", "prompt": "check",
             "session": "isolated", "context": "heartbeat",
             "deliver": {"mode": "announce", "channel": "discord"}, "enabled": True},
        ]))
        result = self._run_script(workspace, "update", [
            "heartbeat", json.dumps({"deliver": {"mode": "announce", "channel": "telegram"}}),
        ])
        assert result.returncode == 0, result.stderr
        assert "Updated job 'heartbeat' (deliver)" in result.stdout
        jobs = json.loads((workspace / "config" / "jobs.json").read_text())
        assert len(jobs) == 1
        assert jobs[0]["deliver"]["channel"] == "telegram"
        assert jobs[0]["schedule"] == "0 * * * *"
        assert jobs[0]["enabled"] is True

    def test_update_null_removes_a_field_and_result_is_validated(self, workspace):
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "j", "schedule": "0 7 * * *", "prompt": "x", "session": "isolated"},
        ]))
        # Dropping schedule without adding at leaves no trigger: rejected, untouched.
        result = self._run_script(workspace, "update", ["j", json.dumps({"schedule": None})])
        assert result.returncode != 0
        assert "schedule" in result.stderr
        assert json.loads((workspace / "config" / "jobs.json").read_text())[0]["schedule"] == "0 7 * * *"
        # Switching to a one-shot in one patch is fine.
        result = self._run_script(workspace, "update", [
            "j", json.dumps({"schedule": None, "at": "2030-01-01 09:00"}),
        ])
        assert result.returncode == 0, result.stderr
        job = json.loads((workspace / "config" / "jobs.json").read_text())[0]
        assert "schedule" not in job and job["at"] == "2030-01-01 09:00"

    def test_update_rejects_id_change_and_unknown_job(self, workspace):
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "j", "schedule": "0 7 * * *", "prompt": "x"},
        ]))
        result = self._run_script(workspace, "update", ["j", json.dumps({"id": "k"})])
        assert result.returncode != 0 and "cannot be changed" in result.stderr
        result = self._run_script(workspace, "update", ["nope", json.dumps({"prompt": "y"})])
        assert result.returncode != 0 and "not found" in result.stderr

    def test_remove_deletes_only_that_job(self, workspace):
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "a", "schedule": "0 7 * * *", "prompt": "x"},
            {"id": "b", "schedule": "0 8 * * *", "prompt": "y"},
        ]))
        result = self._run_script(workspace, "remove", ["a"])
        assert result.returncode == 0
        assert "Removed job 'a'" in result.stdout
        assert [j["id"] for j in json.loads((workspace / "config" / "jobs.json").read_text())] == ["b"]
        result = self._run_script(workspace, "remove", ["a"])
        assert result.returncode != 0 and "not found" in result.stderr

    def test_history_shows_runs_newest_first_with_skip_reason(self, tmp_path):
        """The agent had no view of the run log, so when a heartbeat never
        arrived it could only guess whether the scheduler had fired."""
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        log_dir = tmp_path / "state" / "logs" / "cron"
        log_dir.mkdir(parents=True)
        (log_dir / "heartbeat.jsonl").write_text(
            '{"timestamp": "2026-08-23T01:02:00Z", "status": "success", "duration_ms": 900, "tokens": {}}\n'
            '{"timestamp": "2026-08-23T02:02:00Z", "status": "skipped", "duration_ms": 1, "error": "outside-active-hours"}\n'
            'not json\n'
            '{"timestamp": "2026-08-23T03:02:00Z", "status": "error", "duration_ms": 5000, "error": "provider timeout"}\n'
        )
        result = self._run_script(workspace, "history", ["heartbeat"])
        assert result.returncode == 0, result.stderr
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert "03:02:00  error" in lines[0] and "provider timeout" in lines[0]
        assert "02:02:00  skipped" in lines[1] and "outside-active-hours" in lines[1]
        assert "01:02:00  success" in lines[2]
        assert lines[3].startswith("3 runs shown")

        result = self._run_script(workspace, "history", ["heartbeat", "1"])
        assert result.returncode == 0
        assert "03:02:00  error" in result.stdout and "02:02:00" not in result.stdout

    def test_history_renders_in_the_skill_timezone(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        log_dir = tmp_path / "state" / "logs" / "cron"
        log_dir.mkdir(parents=True)
        (log_dir / "j.jsonl").write_text(
            '{"timestamp": "2026-08-23T01:02:00Z", "status": "success", "duration_ms": 1, "tokens": {}}\n'
        )
        result = self._run_script(workspace, "history", ["j"], tz="Asia/Ho_Chi_Minh")
        assert result.returncode == 0, result.stderr
        assert "2026-08-23 08:02:00  success" in result.stdout

    def test_history_no_log_and_bad_id(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "config").mkdir(parents=True)
        result = self._run_script(workspace, "history", ["never-ran"])
        assert result.returncode == 0
        assert "No runs recorded" in result.stdout
        # The id names a file under state/; a traversal must not become a path.
        result = self._run_script(workspace, "history", ["../../config"])
        assert result.returncode != 0 and "invalid job id" in result.stderr
        result = self._run_script(workspace, "history", ["j", "0"])
        assert result.returncode != 0 and "limit" in result.stderr

    def test_list_shows_where_each_job_delivers(self, workspace):
        """A reminder job ran "successfully" and nothing arrived; the agent
        could not see from list that the job had no channel."""
        (workspace / "config" / "jobs.json").write_text(json.dumps([
            {"id": "a", "schedule": "0 7 * * *", "prompt": "x",
             "deliver": {"mode": "announce", "channel": "last"}},
            {"id": "b", "schedule": "0 8 * * *", "prompt": "y", "deliver": {"mode": "none"}},
            {"id": "c", "schedule": "0 9 * * *", "prompt": "z"},
        ]))
        out = self._run_script(workspace, "list").stdout
        assert "a: " in out and "deliver: last" in out
        assert "deliver: none" in out
        assert "NO CHANNEL" in out

    def test_list_empty(self, workspace):
        result = self._run_script(workspace, "list")
        assert result.returncode == 0
        assert "No jobs configured" in result.stdout

    def test_add_valid_job(self, workspace):
        job = json.dumps({
            "id": "test-job",
            "schedule": "0 7 * * *",
            "prompt": "do something",
            "session": "isolated",
        })
        result = self._run_script(workspace, "add", [job])
        assert result.returncode == 0
        assert "Added job" in result.stdout

        jobs = json.loads((workspace / "config" / "jobs.json").read_text())
        assert len(jobs) == 1
        assert jobs[0]["id"] == "test-job"
        assert jobs[0]["enabled"] is True

    def test_add_missing_id(self, workspace):
        job = json.dumps({"schedule": "0 7 * * *", "prompt": "do something"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "missing required field: id" in result.stderr

    def test_add_missing_schedule_and_at(self, workspace):
        job = json.dumps({"id": "bad", "prompt": "do something"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "schedule" in result.stderr

    def test_add_both_schedule_and_at(self, workspace):
        job = json.dumps({"id": "bad", "schedule": "0 7 * * *", "at": "2026-05-15 09:00", "prompt": "x"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0

    def test_add_missing_prompt_and_skill(self, workspace):
        job = json.dumps({"id": "bad", "schedule": "0 7 * * *"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "prompt" in result.stderr or "skill" in result.stderr

    def test_add_both_prompt_and_skill(self, workspace):
        job = json.dumps({"id": "bad", "schedule": "0 7 * * *", "prompt": "x", "skill": "y"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0

    def test_add_duplicate_id(self, workspace):
        job = json.dumps({"id": "dup", "schedule": "0 7 * * *", "prompt": "first"})
        self._run_script(workspace, "add", [job])
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "already exists" in result.stderr

    def test_add_one_shot_job(self, workspace):
        job = json.dumps({
            "id": "reminder",
            "at": "2026-05-15 09:00",
            "prompt": "remind me",
            "session": "isolated",
        })
        result = self._run_script(workspace, "add", [job])
        assert result.returncode == 0

    def test_add_skill_job_with_none_session(self, workspace):
        job = json.dumps({
            "id": "watchdog",
            "schedule": "*/30 * * * *",
            "skill": "aqi-weather",
            "session": "none",
        })
        result = self._run_script(workspace, "add", [job])
        assert result.returncode == 0

    def test_add_skill_job_with_isolated_session_rejected(self, workspace):
        job = json.dumps({
            "id": "bad-combo",
            "schedule": "0 7 * * *",
            "skill": "aqi-weather",
            "session": "isolated",
        })
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0

    def test_add_invalid_cron(self, workspace):
        job = json.dumps({"id": "bad-cron", "schedule": "invalid", "prompt": "do"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "cron expression" in result.stderr

    def test_add_invalid_at_format(self, workspace):
        job = json.dumps({"id": "bad-at", "at": "tomorrow", "prompt": "do"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0

    def test_omitted_session_runs_as_the_mode_add_validated(self, workspace):
        """add.py and load_jobs have to agree on the default.

        They did not: add.py validated an omitted session as 'isolated' and
        the scheduler ran it as 'agent', so a job created the documented way
        was tool-capable when the operator was told it would not be.
        """
        from faffmonkey.runtime.scheduler import load_jobs

        job = json.dumps({"id": "no-session", "schedule": "0 7 * * *", "prompt": "do"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode == 0, result.stderr

        loaded = load_jobs(workspace)
        assert [j.session for j in loaded] == ["agent"]

    def test_add_invalid_session(self, workspace):
        job = json.dumps({"id": "bad-session", "schedule": "0 7 * * *", "prompt": "do", "session": "custom"})
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "session" in result.stderr

    def test_list_shows_jobs(self, workspace):
        job = json.dumps({"id": "morning", "schedule": "0 7 * * *", "prompt": "briefing", "session": "isolated"})
        self._run_script(workspace, "add", [job])
        result = self._run_script(workspace, "list")
        assert result.returncode == 0
        assert "morning" in result.stdout
        assert "0 7 * * *" in result.stdout

    def test_disable_job(self, workspace):
        job = json.dumps({"id": "toggle-me", "schedule": "0 7 * * *", "prompt": "test"})
        self._run_script(workspace, "add", [job])
        result = self._run_script(workspace, "disable", ["toggle-me"])
        assert result.returncode == 0
        assert "Disabled" in result.stdout

        jobs = json.loads((workspace / "config" / "jobs.json").read_text())
        assert jobs[0]["enabled"] is False

    def test_enable_job(self, workspace):
        job = json.dumps({"id": "toggle-me", "schedule": "0 7 * * *", "prompt": "test", "enabled": False})
        (workspace / "config" / "jobs.json").write_text(json.dumps([json.loads(job)]))
        result = self._run_script(workspace, "enable", ["toggle-me"])
        assert result.returncode == 0
        assert "Enabled" in result.stdout

        jobs = json.loads((workspace / "config" / "jobs.json").read_text())
        assert jobs[0]["enabled"] is True

    def test_disable_nonexistent_job(self, workspace):
        result = self._run_script(workspace, "disable", ["nope"])
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_enable_nonexistent_job(self, workspace):
        result = self._run_script(workspace, "enable", ["nope"])
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_add_with_deliver(self, workspace):
        job = json.dumps({
            "id": "announced",
            "schedule": "0 7 * * *",
            "prompt": "briefing",
            "deliver": {"mode": "announce", "channel": "telegram"},
        })
        result = self._run_script(workspace, "add", [job])
        assert result.returncode == 0

    def test_add_with_deliver_announce_missing_channel(self, workspace):
        job = json.dumps({
            "id": "bad-deliver",
            "schedule": "0 7 * * *",
            "prompt": "briefing",
            "deliver": {"mode": "announce"},
        })
        result = self._run_script(workspace, "add", [job])
        assert result.returncode != 0
        assert "channel" in result.stderr


class TestLoadCommands:
    def _write(self, tmp_path, content: str):
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        (state / "commands.json").write_text(content)
        return state

    def test_missing_file_returns_empty(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        assert load_commands(tmp_path / "state") == {}

    def test_valid_commands_loaded(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        state = self._write(tmp_path, '{"IMAGE_GEN_CMD": "python3 gen.py"}')
        assert load_commands(state) == {"IMAGE_GEN_CMD": "python3 gen.py"}

    def test_invalid_json_returns_empty(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        state = self._write(tmp_path, "not json")
        assert load_commands(state) == {}

    def test_non_object_returns_empty(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        state = self._write(tmp_path, '["IMAGE_GEN_CMD"]')
        assert load_commands(state) == {}

    def test_invalid_key_skipped(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        state = self._write(
            tmp_path,
            '{"lowercase": "x", "1BAD": "y", "GOOD_CMD": "z"}',
        )
        assert load_commands(state) == {"GOOD_CMD": "z"}

    def test_reserved_keys_skipped(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        state = self._write(
            tmp_path,
            '{"WORKSPACE": "/evil", "PATH": "/evil", "TZ": "evil", "OK_CMD": "fine"}',
        )
        assert load_commands(state) == {"OK_CMD": "fine"}

    def test_non_string_and_empty_values_skipped(self, tmp_path):
        from faffmonkey.runtime.skills import load_commands
        state = self._write(
            tmp_path,
            '{"NUM_CMD": 42, "EMPTY_CMD": "  ", "OK_CMD": "fine"}',
        )
        assert load_commands(state) == {"OK_CMD": "fine"}


class TestInvokeCommandsEnv:
    def test_commands_injected_into_skill_env(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / "commands.json").write_text(
            '{"IMAGE_GEN_CMD": "python3 gen.py"}'
        )
        script = (
            "import os\n"
            "print(f'CMD={os.environ.get(\"IMAGE_GEN_CMD\", \"\")}')\n"
        )
        _setup_skill(tmp_path, "cmd-test", "---\nname: cmd-test\n---\n", "check.py", script)
        output, _attachments, is_error = invoke(
            tmp_path, "cmd-test", "check", state_dir=state,
        )
        assert not is_error
        assert "CMD=python3 gen.py" in output

    def test_reserved_key_cannot_override_runtime_env(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / "commands.json").write_text('{"WORKSPACE": "/evil"}')
        script = (
            "import os\n"
            "print(f'WS={os.environ.get(\"WORKSPACE\", \"\")}')\n"
        )
        _setup_skill(tmp_path, "ws-test", "---\nname: ws-test\n---\n", "check.py", script)
        output, _attachments, is_error = invoke(
            tmp_path, "ws-test", "check", state_dir=state,
        )
        assert not is_error
        assert "/evil" not in output
        assert f"WS={tmp_path}" in output

    def test_default_state_dir_is_workspace_sibling(self, tmp_path):
        base = tmp_path / "base"
        workspace = base / "workspace"
        workspace.mkdir(parents=True)
        state = base / "state"
        state.mkdir()
        (state / "commands.json").write_text('{"SIBLING_CMD": "found"}')
        script = (
            "import os\n"
            "print(f'CMD={os.environ.get(\"SIBLING_CMD\", \"\")}')\n"
        )
        _setup_skill(workspace, "sib-test", "---\nname: sib-test\n---\n", "check.py", script)
        output, _attachments, is_error = invoke(workspace, "sib-test", "check")
        assert not is_error
        assert "CMD=found" in output


class TestRequiresGating:
    """D1: 20 of 22 shipped skills declared `requires` and nothing read it."""

    def _skill(self, tmp_path, name, metadata):
        d = tmp_path / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: does {name}\n"
            f"metadata: '{metadata}'\n---\n\nbody\n"
        )
        return d

    def test_missing_env_hides_the_skill(self, tmp_path, monkeypatch):
        self._skill(tmp_path, "weather", '{"faffmonkey":{"requires":{"env":["WEATHER_KEY"]}}}')
        monkeypatch.delenv("WEATHER_KEY", raising=False)
        assert scan_skills(tmp_path) == []

    def test_present_env_offers_the_skill(self, tmp_path, monkeypatch):
        self._skill(tmp_path, "weather", '{"faffmonkey":{"requires":{"env":["WEATHER_KEY"]}}}')
        monkeypatch.setenv("WEATHER_KEY", "set")
        assert [n for n, _ in scan_skills(tmp_path)] == ["weather"]

    def test_missing_binary_hides_the_skill(self, tmp_path):
        self._skill(tmp_path, "conv", '{"faffmonkey":{"requires":{"bins":["definitely-not-a-real-binary"]}}}')
        assert scan_skills(tmp_path) == []

    def test_present_binary_offers_the_skill(self, tmp_path):
        self._skill(tmp_path, "conv", '{"faffmonkey":{"requires":{"bins":["python3"]}}}')
        assert [n for n, _ in scan_skills(tmp_path)] == ["conv"]

    def test_skill_without_metadata_is_always_offered(self, tmp_path):
        d = tmp_path / "skills" / "plain"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: plain\ndescription: no requires\n---\n\nbody\n")
        assert [n for n, _ in scan_skills(tmp_path)] == ["plain"]

    def test_unparseable_metadata_does_not_hide_the_skill(self, tmp_path):
        self._skill(tmp_path, "broken", "{not json")
        assert [n for n, _ in scan_skills(tmp_path)] == ["broken"]

    def test_empty_env_var_counts_as_missing(self, tmp_path, monkeypatch):
        self._skill(tmp_path, "weather", '{"faffmonkey":{"requires":{"env":["WEATHER_KEY"]}}}')
        monkeypatch.setenv("WEATHER_KEY", "")
        assert scan_skills(tmp_path) == []

    def test_command_seam_key_satisfies_requirement(self, tmp_path, monkeypatch):
        """2026-08-25: selfie required IMAGE_EDIT_CMD, which only
        commands.json supplied. The catalog check read os.environ alone,
        hid the skill, and the agent generated a portrait instead of
        editing the reference one. invoke() merges commands.json into
        the subprocess env, so the catalog must count it as present."""
        workspace = tmp_path / "workspace"
        self._skill(workspace, "selfie", '{"faffmonkey":{"requires":{"env":["IMAGE_EDIT_CMD"]}}}')
        monkeypatch.delenv("IMAGE_EDIT_CMD", raising=False)
        state = tmp_path / "state"
        state.mkdir()
        (state / "commands.json").write_text(
            json.dumps({"IMAGE_EDIT_CMD": "python3 skills/venice-ai-media/scripts/venice-edit.py"})
        )
        assert [n for n, _ in scan_skills(workspace)] == ["selfie"]
        assert [n for n, _ in scan_skills(workspace, state)] == ["selfie"]


class TestDeclaredActionsAreAnAllowList:
    """P6-L1: every shared module in scripts/ was reachable as an action."""

    def _skill(self, tmp_path):
        d = tmp_path / "skills" / "preconscious"
        (d / "scripts").mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: preconscious\ndescription: buffer\nactions: add, read\n---\n\nbody\n"
        )
        (d / "scripts" / "add.py").write_text("print('added')\n")
        (d / "scripts" / "buffer.py").write_text("def helper():\n    return 1\n")
        return tmp_path

    def test_declared_action_runs(self, tmp_path):
        ws = self._skill(tmp_path)
        output, _, is_error = invoke(ws, "preconscious", "add")
        assert not is_error
        assert "added" in output

    def test_shared_module_is_not_an_action(self, tmp_path):
        ws = self._skill(tmp_path)
        output, _, is_error = invoke(ws, "preconscious", "buffer")
        assert is_error
        assert "unknown action" in output
        assert "add, read" in output

    def test_skill_without_an_actions_list_is_unrestricted(self, tmp_path):
        d = tmp_path / "skills" / "loose"
        (d / "scripts").mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: loose\ndescription: x\n---\n\nbody\n")
        (d / "scripts" / "anything.py").write_text("print('ran')\n")

        output, _, is_error = invoke(tmp_path, "loose", "anything")
        assert not is_error
        assert "ran" in output
