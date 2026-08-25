import json
import os
import stat
from unittest.mock import patch

import pytest

from faffmonkey.cli.init import (
    _parse_active_hours,
    _check_no_symlink_components,
    _find_project_root,
    _write_if_missing,
    ensure_extensions_writable,
    run_init,
)


@pytest.fixture
def project_dir(tmp_path):
    return tmp_path / "myproject"


def _run_init_noninteractive(base_path):
    # "" accepts the detected timezone and skips every identity question
    with patch("builtins.input", return_value=""):
        run_init(base_path)


class TestInit:
    def test_creates_directories(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        for d in [
            "workspace",
            "workspace/memory",
            "workspace/memory/daily",
            "workspace/skills",
            "workspace/skills-data",
            "workspace/shared",
            "workspace/shared/inbox",
            "workspace/config",
            "workspace/documents",
            "workspace/tmp",
            "state",
            "state/backups",
            "extensions",
            "backups",
        ]:
            assert (project_dir / d).is_dir(), f"{d} not created"

    def test_does_not_create_contrib(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        assert not (project_dir / "contrib").exists(), "contrib/ should not be created by init"

    def test_creates_config_json(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        config_path = project_dir / "state" / "config.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert "timezone" in config
        assert config["models"] == {}
        assert config["tools"]["shell_exec"] == "ask"

    def test_creates_env_file(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        env_path = project_dir / "state" / ".env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "OPENROUTER_API_KEY" in content

    def test_creates_jobs_json_in_config(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        jobs_path = project_dir / "workspace" / "config" / "jobs.json"
        assert jobs_path.exists(), "jobs.json should be in workspace/config/"
        assert json.loads(jobs_path.read_text()) == []
        assert not (project_dir / "workspace" / "jobs.json").exists(), (
            "jobs.json should not be at workspace/ root"
        )

    def test_creates_requirements_extra(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        assert (project_dir / "requirements.extra.txt").exists()

    def test_creates_origin_json(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        origin = project_dir / "extensions" / ".origin.json"
        assert origin.exists()
        assert json.loads(origin.read_text()) == {}

    def test_copies_workspace_templates(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        for name in ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "HEARTBEAT.md"]:
            assert (project_dir / "workspace" / name).exists(), f"{name} not copied"

    def test_idempotent_never_overwrites(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        config_path = project_dir / "state" / "config.json"
        config_path.write_text('{"custom": true}')

        jobs_path = project_dir / "workspace" / "config" / "jobs.json"
        jobs_path.write_text('[{"id": "test"}]')

        soul_path = project_dir / "workspace" / "SOUL.md"
        soul_path.write_text("my custom soul")

        _run_init_noninteractive(project_dir)

        config = json.loads(config_path.read_text())
        assert config["custom"] is True
        assert "timezone" in config
        assert jobs_path.read_text() == '[{"id": "test"}]'
        assert soul_path.read_text() == "my custom soul"

    def test_runs_twice_without_error(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        _run_init_noninteractive(project_dir)

    def test_invalid_timezone_rejected_and_reprompted(self, project_dir):
        project_dir.mkdir()
        inputs = iter(["n", "Nope/Fake", "Europe/London", "", "", "", "", "", ""])
        with patch("builtins.input", side_effect=inputs):
            run_init(project_dir)

        config = json.loads((project_dir / "state" / "config.json").read_text())
        assert config["timezone"] == "Europe/London"

    def test_timezone_updated_in_existing_config(self, project_dir):
        project_dir.mkdir()
        state_dir = project_dir / "state"
        state_dir.mkdir(parents=True)
        config_path = state_dir / "config.json"
        config_path.write_text(json.dumps({"timezone": "UTC", "custom": 42}))

        inputs = iter(["n", "Asia/Tokyo", "", "", "", "", "", ""])
        with patch("builtins.input", side_effect=inputs):
            run_init(project_dir)

        config = json.loads(config_path.read_text())
        assert config["timezone"] == "Asia/Tokyo"
        assert config["custom"] == 42

    def test_ctrl_c_aborts_instead_of_skipping(self, project_dir, capsys):
        project_dir.mkdir()
        with patch("builtins.input", side_effect=["", "", "Alex", KeyboardInterrupt]):
            with pytest.raises(SystemExit) as exc:
                run_init(project_dir)
        assert exc.value.code == 1
        assert "Aborted" in capsys.readouterr().out
        assert not (project_dir / "state" / "config.json").exists()
        assert not (project_dir / "workspace" / "IDENTITY.md").exists()

    def test_ctrl_c_at_timezone_prompt_aborts(self, project_dir):
        project_dir.mkdir()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                run_init(project_dir)
        assert not (project_dir / "state" / "config.json").exists()

    def test_closed_stdin_still_skips(self, project_dir):
        # Non-interactive init (stdin not a tty) must keep working.
        project_dir.mkdir()
        with patch("builtins.input", side_effect=EOFError):
            run_init(project_dir)
        assert (project_dir / "state" / "config.json").exists()

    def test_gibberish_confirmation_reprompts(self, project_dir):
        project_dir.mkdir()
        inputs = iter(["gfdgd", "y", "", "", "", "", "", ""])
        with patch("builtins.input", side_effect=inputs) as mock_input:
            run_init(project_dir)
        assert mock_input.call_count == 8

    def test_import_does_not_call_find_project_root(self):
        import importlib
        import faffmonkey.cli.init
        import faffmonkey.cli.setup_provider
        importlib.reload(faffmonkey.cli.init)
        importlib.reload(faffmonkey.cli.setup_provider)

    def test_template_dir_resolves_to_project_root(self):
        project_root = _find_project_root()
        template_dir = project_root / "templates"
        assert template_dir.is_dir(), f"templates dir does not exist: {template_dir}"
        assert (project_root / "pyproject.toml").exists(), (
            "project root should contain pyproject.toml"
        )

    def test_env_file_mode_0600(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        env_path = project_dir / "state" / ".env"
        mode = stat.S_IMODE(os.stat(env_path).st_mode)
        assert mode == 0o600

    def test_state_dir_mode_0700(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        state_path = project_dir / "state"
        mode = stat.S_IMODE(os.stat(state_path).st_mode)
        assert mode == 0o700

    def test_extensions_dir_mode_0700(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        extensions_path = project_dir / "extensions"
        mode = stat.S_IMODE(os.stat(extensions_path).st_mode)
        assert mode == 0o700

    def test_backups_dir_mode_0700(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        backups_path = project_dir / "state" / "backups"
        mode = stat.S_IMODE(os.stat(backups_path).st_mode)
        assert mode == 0o700


class TestSymlinkRefusal:
    def test_write_if_missing_refuses_symlink(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("original")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        result = _write_if_missing(link, "overwritten")
        assert result is False
        assert real.read_text() == "original"

    def test_init_symlink_at_state_dir_raises(self, tmp_path):
        base = tmp_path / "project"
        base.mkdir()
        attacker_dir = tmp_path / "attacker"
        attacker_dir.mkdir()
        (base / "state").symlink_to(attacker_dir)
        with pytest.raises(RuntimeError, match="is a symlink"):
            _run_init_noninteractive(base)

    def test_init_existing_dir_remediates_permissions(self, tmp_path):
        base = tmp_path / "project"
        base.mkdir()
        state_dir = base / "state"
        state_dir.mkdir()
        os.chmod(state_dir, 0o755)
        assert stat.S_IMODE(os.stat(state_dir).st_mode) == 0o755
        _run_init_noninteractive(base)
        assert stat.S_IMODE(os.stat(state_dir).st_mode) == 0o700

    def test_init_survives_readonly_sensitive_dir(self, tmp_path, capsys):
        base = tmp_path / "project"
        base.mkdir()
        ext_dir = base / "extensions"
        ext_dir.mkdir()

        real_chmod = os.chmod

        def failing_chmod(path, mode, **kwargs):
            if str(path) == str(ext_dir):
                raise OSError(30, "Read-only file system", str(path))
            real_chmod(path, mode, **kwargs)

        with patch("faffmonkey.cli.init.os.chmod", side_effect=failing_chmod):
            _run_init_noninteractive(base)
        assert "cannot chmod" in capsys.readouterr().out
        assert (base / "workspace" / "SOUL.md").exists()

    def test_ensure_extensions_writable_passes(self, tmp_path):
        d = tmp_path / "extensions"
        d.mkdir()
        ensure_extensions_writable(d)

    def test_ensure_extensions_writable_missing_dir_passes(self, tmp_path):
        ensure_extensions_writable(tmp_path / "extensions")

    def test_ensure_extensions_writable_read_only_exits(self, tmp_path, capsys):
        d = tmp_path / "extensions"
        d.mkdir()
        os.chmod(d, 0o500)
        try:
            with pytest.raises(SystemExit):
                ensure_extensions_writable(d)
        finally:
            os.chmod(d, 0o700)
        assert "on the host" in capsys.readouterr().out

    def test_check_no_symlink_components_clean_path(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        (root / "workspace").mkdir()
        _check_no_symlink_components(root / "workspace" / "file.txt", root)

    def test_check_no_symlink_components_detects_symlink(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (root / "workspace").symlink_to(attacker)
        with pytest.raises(RuntimeError, match="is a symlink"):
            _check_no_symlink_components(root / "workspace" / "file.txt", root)


class TestInitIdentity:
    _ANSWERS = ["y", "", "Alex", "Scout", "trip planner", "concise", "Based in Lisbon"]

    def test_identity_values_written(self, project_dir):
        project_dir.mkdir()
        with patch("builtins.input", side_effect=iter(self._ANSWERS)):
            run_init(project_dir)

        identity = (project_dir / "workspace" / "IDENTITY.md").read_text()
        assert "Scout" in identity
        assert "trip planner" in identity
        assert "I'm Scout, your trip planner." in identity

        user = (project_dir / "workspace" / "USER.md").read_text()
        assert "Alex" in user
        assert "Communication style: concise." in user
        assert "Timezone:" in user

        memory = (project_dir / "workspace" / "MEMORY.md").read_text()
        assert "- Based in Lisbon" in memory
        assert "<!-- Persistent facts" not in memory
        assert "## Active projects" in memory

    def test_empty_answers_keep_placeholders(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        identity = (project_dir / "workspace" / "IDENTITY.md").read_text()
        assert "<!-- Your agent's name -->" in identity
        assert "personal assistant" in identity

        user = (project_dir / "workspace" / "USER.md").read_text()
        assert "<!-- Your name -->" in user
        assert "Communication style: normal." in user

        memory = (project_dir / "workspace" / "MEMORY.md").read_text()
        assert "## Key facts" in memory
        assert "<!-- Persistent facts" in memory

    def test_rerun_skips_identity_prompts(self, project_dir):
        project_dir.mkdir()
        with patch("builtins.input", side_effect=iter(self._ANSWERS)):
            run_init(project_dir)

        with patch("builtins.input", side_effect=iter(["y", ""])) as mock_input:
            run_init(project_dir)

        assert mock_input.call_count == 2
        assert "Scout" in (project_dir / "workspace" / "IDENTITY.md").read_text()
        memory = (project_dir / "workspace" / "MEMORY.md").read_text()
        assert memory.count("Based in Lisbon") == 1

    def test_installs_builtin_skills(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        skills = project_dir / "workspace" / "skills"
        for name in [
            "carry-over", "cron-manager", "heartbeat", "memory-search",
            "morning-routine", "preconscious", "self-review", "skill-writer",
        ]:
            assert (skills / name / "SKILL.md").exists(), f"{name} not installed"

    def test_rerun_does_not_overwrite_skills(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)

        skill_md = project_dir / "workspace" / "skills" / "heartbeat" / "SKILL.md"
        skill_md.write_text("customised")
        _run_init_noninteractive(project_dir)

        assert skill_md.read_text() == "customised"


class TestInitCommandsFile:
    def test_creates_empty_commands_json(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        commands_path = project_dir / "state" / "commands.json"
        assert commands_path.read_text() == "{}\n"

    def test_rerun_does_not_overwrite_commands_json(self, project_dir):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        commands_path = project_dir / "state" / "commands.json"
        commands_path.write_text('{"IMAGE_GEN_CMD": "custom"}')
        _run_init_noninteractive(project_dir)
        assert commands_path.read_text() == '{"IMAGE_GEN_CMD": "custom"}'


class TestInitSurvivesDamage:
    """H2/D30: init is the documented repair, and it crashed on the input."""

    def test_corrupt_config_is_kept_aside_and_recreated(self, project_dir):
        project_dir.mkdir()
        (project_dir / "state").mkdir()
        config_path = project_dir / "state" / "config.json"
        config_path.write_text('{"models": {"main": {"provider":"x"')

        _run_init_noninteractive(project_dir)

        assert json.loads(config_path.read_text())["models"] == {}
        assert (project_dir / "state" / "config.json.corrupt").exists()

    def test_non_dict_config_is_kept_aside(self, project_dir):
        project_dir.mkdir()
        (project_dir / "state").mkdir()
        config_path = project_dir / "state" / "config.json"
        config_path.write_text('["not", "an", "object"]')

        _run_init_noninteractive(project_dir)

        assert isinstance(json.loads(config_path.read_text()), dict)
        assert (project_dir / "state" / "config.json.corrupt").exists()

    def test_configured_timezone_survives_a_rerun(self, project_dir):
        project_dir.mkdir()
        (project_dir / "state").mkdir()
        config_path = project_dir / "state" / "config.json"
        config_path.write_text(json.dumps({
            "timezone": "Europe/London", "models": {},
        }))

        _run_init_noninteractive(project_dir)

        assert json.loads(config_path.read_text())["timezone"] == "Europe/London"

    def test_invalid_configured_timezone_is_not_reused(self, project_dir):
        project_dir.mkdir()
        (project_dir / "state").mkdir()
        config_path = project_dir / "state" / "config.json"
        config_path.write_text(json.dumps({
            "timezone": "Mars/Olympus_Mons", "models": {},
        }))

        _run_init_noninteractive(project_dir)

        tz = json.loads(config_path.read_text())["timezone"]
        assert tz != "Mars/Olympus_Mons"


def _answering(active_hours: list[str]):
    """input() that answers the active-hours prompt from a list and
    everything else with Enter."""
    answers = iter(active_hours)

    def fake_input(prompt: str) -> str:
        return next(answers) if "Active hours" in prompt else ""
    return fake_input


class TestActiveHours:
    """The 09:00-22:00 window was a hardcoded default that init never
    asked about, so nobody outside that window ever got a heartbeat."""

    def test_default_is_offered_and_kept_on_enter(self, project_dir, capsys):
        project_dir.mkdir()
        _run_init_noninteractive(project_dir)
        config = json.loads((project_dir / "state" / "config.json").read_text())
        assert config["heartbeat"]["active_hours"] == [9, 22]
        assert "heartbeat active hours: 09:00-22:00" in capsys.readouterr().out

    def test_overnight_range_is_written(self, project_dir):
        project_dir.mkdir()
        with patch("builtins.input", side_effect=_answering(["22-7"])):
            run_init(project_dir)
        config = json.loads((project_dir / "state" / "config.json").read_text())
        assert config["heartbeat"]["active_hours"] == [22, 7]

    def test_bad_ranges_are_reprompted(self, project_dir):
        project_dir.mkdir()
        bad_then_good = ["9:30-22", "25-3", "9-9", "nine-ten", "08:00-23:00"]
        with patch("builtins.input", side_effect=_answering(bad_then_good)):
            run_init(project_dir)
        config = json.loads((project_dir / "state" / "config.json").read_text())
        assert config["heartbeat"]["active_hours"] == [8, 23]

    def test_configured_hours_are_the_default_on_rerun(self, project_dir, capsys):
        project_dir.mkdir()
        state_dir = project_dir / "state"
        state_dir.mkdir(parents=True)
        config_path = state_dir / "config.json"
        config_path.write_text(json.dumps({
            "timezone": "Europe/London",
            "heartbeat": {"enabled": True, "active_hours": [7, 20], "ack_max_chars": 300},
        }))
        _run_init_noninteractive(project_dir)
        config = json.loads(config_path.read_text())
        assert config["heartbeat"]["active_hours"] == [7, 20]
        assert config["heartbeat"]["ack_max_chars"] == 300
        assert "heartbeat active hours: 07:00-20:00" in capsys.readouterr().out

    def test_config_without_heartbeat_block_gains_one(self, project_dir):
        project_dir.mkdir()
        state_dir = project_dir / "state"
        state_dir.mkdir(parents=True)
        config_path = state_dir / "config.json"
        config_path.write_text(json.dumps({"timezone": "UTC", "custom": 42}))
        with patch("builtins.input", side_effect=_answering(["6-21"])):
            run_init(project_dir)
        config = json.loads(config_path.read_text())
        assert config["heartbeat"]["active_hours"] == [6, 21]
        assert config["heartbeat"]["enabled"] is True
        assert config["custom"] == 42


@pytest.mark.parametrize("text, expected", [
    ("9-22", (9, 22)),
    ("09:00-22:00", (9, 22)),
    ("22 - 7", (22, 7)),
    ("0-23", (0, 23)),
    ("9-24", None),
    ("9:15-22", None),
    ("9", None),
    ("9-22-23", None),
    ("12-12", None),
    ("-9", None),
])
def test_parse_active_hours(text, expected):
    assert _parse_active_hours(text) == expected
