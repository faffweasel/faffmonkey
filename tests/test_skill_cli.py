import json
from pathlib import Path
from unittest.mock import patch

import pytest

from faffmonkey.cli.skill import (
    _dir_hash,
    run_skill_install,
    run_skill_list,
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    skills = root / "contrib" / "skills"
    weather = skills / "weather"
    (weather / "scripts").mkdir(parents=True)
    (weather / "SKILL.md").write_text(
        "---\nname: weather\ndescription: Weather lookups\n---\nbody\n"
    )
    (weather / "HUMAN.md").write_text("# Weather setup\n")
    (weather / "scripts" / "weather.py").write_text("print('rain')\n")
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "base" / "workspace"
    (ws / "skills").mkdir(parents=True)
    return ws


def _install(project_root, workspace, name, force=False):
    with patch("faffmonkey.cli.skill._find_project_root", return_value=project_root):
        return run_skill_install(workspace, name, force=force)


class TestDirHash:
    def test_stable_and_content_sensitive(self, project_root):
        src = project_root / "contrib" / "skills" / "weather"
        h1 = _dir_hash(src)
        assert h1 == _dir_hash(src)
        (src / "scripts" / "weather.py").write_text("print('sun')\n")
        assert _dir_hash(src) != h1

    def test_ignores_pycache_and_dotfiles(self, project_root):
        src = project_root / "contrib" / "skills" / "weather"
        h1 = _dir_hash(src)
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "x.pyc").write_text("junk")
        (src / ".DS_Store").write_text("junk")
        assert _dir_hash(src) == h1


class TestSkillInstall:
    def test_installs_and_records_provenance(self, project_root, workspace):
        assert _install(project_root, workspace, "weather") == 0

        dest = workspace / "skills" / "weather"
        assert (dest / "SKILL.md").is_file()
        assert (dest / "scripts" / "weather.py").is_file()

        origin = json.loads((workspace / "skills" / ".origin.json").read_text())
        assert origin["weather"]["source"] == "contrib/skills/weather"
        assert origin["weather"]["contrib_source_hash"] == _dir_hash(
            project_root / "contrib" / "skills" / "weather"
        )

    def test_unknown_skill_fails(self, project_root, workspace, capsys):
        assert _install(project_root, workspace, "nonexistent") == 1
        out = capsys.readouterr().out
        assert "not found" in out
        assert "weather" in out

    def test_invalid_name_fails(self, project_root, workspace):
        assert _install(project_root, workspace, "../evil") == 1
        assert _install(project_root, workspace, "Weather") == 1

    def test_refuses_non_contrib_collision(self, project_root, workspace, capsys):
        user_skill = workspace / "skills" / "weather"
        user_skill.mkdir()
        (user_skill / "SKILL.md").write_text("mine")

        assert _install(project_root, workspace, "weather") == 1
        assert "did not come from contrib" in capsys.readouterr().out
        assert (user_skill / "SKILL.md").read_text() == "mine"

    def test_clean_reinstall_updates(self, project_root, workspace):
        assert _install(project_root, workspace, "weather") == 0
        src_script = project_root / "contrib" / "skills" / "weather" / "scripts" / "weather.py"
        src_script.write_text("print('updated')\n")

        assert _install(project_root, workspace, "weather") == 0
        deployed = workspace / "skills" / "weather" / "scripts" / "weather.py"
        assert deployed.read_text() == "print('updated')\n"

    def test_modified_install_requires_force(self, project_root, workspace, capsys):
        assert _install(project_root, workspace, "weather") == 0
        deployed = workspace / "skills" / "weather" / "scripts" / "weather.py"
        deployed.write_text("print('local change')\n")

        assert _install(project_root, workspace, "weather") == 1
        assert "--force" in capsys.readouterr().out
        assert deployed.read_text() == "print('local change')\n"

        assert _install(project_root, workspace, "weather", force=True) == 0
        assert deployed.read_text() == "print('rain')\n"

    def test_refuses_symlinked_source(self, project_root, workspace, tmp_path):
        src = project_root / "contrib" / "skills" / "weather"
        outside = tmp_path / "outside.py"
        outside.write_text("evil")
        (src / "scripts" / "link.py").symlink_to(outside)

        assert _install(project_root, workspace, "weather") == 1
        assert not (workspace / "skills" / "weather").exists()


class TestSkillList:
    def test_lists_installed_and_available(self, project_root, workspace, capsys):
        _install(project_root, workspace, "weather")
        with patch(
            "faffmonkey.cli.skill._find_project_root", return_value=project_root,
        ):
            assert run_skill_list(workspace) == 0

        out = capsys.readouterr().out
        assert "weather" in out
        assert "(installed)" in out
        assert "Weather lookups" in out

    def test_empty_everything(self, tmp_path, capsys):
        ws = tmp_path / "ws"
        ws.mkdir()
        with patch(
            "faffmonkey.cli.skill._find_project_root",
            return_value=tmp_path / "no-project",
        ):
            assert run_skill_list(ws) == 0
        assert "none" in capsys.readouterr().out


class TestUpdateSkillStaleness:
    @pytest.fixture(autouse=True)
    def _project_root_is_deploy(self, tmp_path, monkeypatch):
        # The staleness check resolves contrib/ against the checkout, not
        # the data root; these tests put both under tmp_path/deploy.
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_project_root",
            lambda: tmp_path / "deploy",
        )

    def _base_with_install(self, tmp_path):
        base = tmp_path / "deploy"
        skills = base / "contrib" / "skills"
        w = skills / "weather"
        (w / "scripts").mkdir(parents=True)
        (w / "SKILL.md").write_text("---\nname: weather\n---\n")
        (w / "scripts" / "weather.py").write_text("print('rain')\n")
        workspace = base / "workspace"
        (workspace / "skills").mkdir(parents=True)
        with patch(
            "faffmonkey.cli.skill._find_project_root", return_value=base,
        ):
            assert run_skill_install(workspace, "weather") == 0
        return base, workspace

    def test_fresh_install_not_stale(self, tmp_path, capsys):
        from faffmonkey.cli.update import _check_skill_staleness
        base, workspace = self._base_with_install(tmp_path)
        capsys.readouterr()
        _check_skill_staleness(base, workspace)
        assert "stale: skill" not in capsys.readouterr().out

    def test_contrib_change_reported_stale(self, tmp_path, capsys):
        from faffmonkey.cli.update import _check_skill_staleness
        base, workspace = self._base_with_install(tmp_path)
        (base / "contrib" / "skills" / "weather" / "scripts" / "weather.py").write_text(
            "print('new version')\n"
        )
        _check_skill_staleness(base, workspace)
        out = capsys.readouterr().out
        assert "stale: skill weather" in out
        assert "faff skill install weather" in out

    def test_local_modification_reported(self, tmp_path, capsys):
        from faffmonkey.cli.update import _check_skill_staleness
        base, workspace = self._base_with_install(tmp_path)
        (workspace / "skills" / "weather" / "scripts" / "weather.py").write_text(
            "print('hacked')\n"
        )
        _check_skill_staleness(base, workspace)
        assert "modified since install" in capsys.readouterr().out

    def test_source_escape_rejected(self, tmp_path, capsys):
        from faffmonkey.cli.update import _check_skill_staleness
        base, workspace = self._base_with_install(tmp_path)
        origin_path = workspace / "skills" / ".origin.json"
        origin = json.loads(origin_path.read_text())
        origin["weather"]["source"] = "../../../etc"
        origin_path.write_text(json.dumps(origin))
        _check_skill_staleness(base, workspace)
        assert "escapes contrib/skills/" in capsys.readouterr().out


class TestSkillInstallGuardsBeforeDeleting:
    """P7-M3: rmtree ran four lines before the guard meant to prevent it."""

    def test_symlinked_skills_dir_destroys_nothing(self, tmp_path):
        from faffmonkey.cli.skill import run_skill_install

        real_skills = tmp_path / "elsewhere"
        (real_skills / "weather").mkdir(parents=True)
        (real_skills / "weather" / "SKILL.md").write_text("# do not delete me")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "skills").symlink_to(real_skills)

        with pytest.raises(RuntimeError, match="symlink"):
            run_skill_install(workspace, "weather")

        assert (real_skills / "weather" / "SKILL.md").read_text() == "# do not delete me"


class TestInstallIsAtomic:
    """P6-M4/M5: a half-copied tree was reported as a local modification."""

    def _contrib(self, base, name="weather"):
        src = base / "contrib" / "skills" / name
        (src / "scripts").mkdir(parents=True)
        (src / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n\nbody\n")
        (src / "scripts" / "run.py").write_text("print('ran')\n")
        return src

    def test_failed_copy_leaves_the_previous_install_intact(self, tmp_path, monkeypatch):
        from faffmonkey.cli import skill as skill_mod

        base = tmp_path / "project"
        self._contrib(base)
        workspace = base / "workspace"
        dest = workspace / "skills" / "weather"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# previous install")
        (workspace / "skills" / ".origin.json").write_text(json.dumps({
            "weather": {"source": "contrib/skills/weather", "contrib_source_hash": "abc"},
        }))

        def boom(*a, **kw):
            raise OSError("ENOSPC")

        monkeypatch.setattr(skill_mod.shutil, "copytree", boom)
        with patch("faffmonkey.cli.skill._find_project_root", return_value=base), \
             pytest.raises(OSError):
            skill_mod.run_skill_install(workspace, "weather", force=True)

        assert (dest / "SKILL.md").read_text() == "# previous install"

    def test_builtin_provenance_key_is_honoured(self, tmp_path):
        from faffmonkey.cli import skill as skill_mod

        base = tmp_path / "project"
        self._contrib(base)
        workspace = base / "workspace"
        dest = workspace / "skills" / "weather"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# locally edited")
        (workspace / "skills" / ".origin.json").write_text(json.dumps({
            "weather": {"source": "templates/weather", "source_hash": "deadbeef"},
        }))

        with patch("faffmonkey.cli.skill._find_project_root", return_value=base):
            code = skill_mod.run_skill_install(workspace, "weather")

        assert code == 1
        assert (dest / "SKILL.md").read_text() == "# locally edited"


class TestInstallSetupChecklist:
    """2026-08-24: "installed" meant "files copied"; the operator found
    missing API keys and command wiring one runtime error at a time."""

    def _make_skill(self, project_root, name, metadata=None, human=""):
        skill = project_root / "contrib" / "skills" / name
        (skill / "scripts").mkdir(parents=True)
        meta_line = f"metadata: '{metadata}'\n" if metadata else ""
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n{meta_line}---\nbody\n"
        )
        if human:
            (skill / "HUMAN.md").write_text(human)
        (skill / "scripts" / "run.py").write_text("print('x')\n")

    def test_missing_env_key_is_reported(self, project_root, workspace, capsys, monkeypatch):
        monkeypatch.delenv("FAKE_SKILL_KEY", raising=False)
        self._make_skill(
            project_root, "needy",
            metadata='{"faffmonkey":{"requires":{"env":["FAKE_SKILL_KEY"]}}}',
        )
        assert _install(project_root, workspace, "needy") == 0
        out = capsys.readouterr().out
        assert "still needs" in out
        assert "FAKE_SKILL_KEY in state/.env" in out

    def test_key_in_state_env_counts_as_satisfied(self, project_root, workspace, capsys, monkeypatch):
        monkeypatch.delenv("FAKE_SKILL_KEY", raising=False)
        state = workspace.parent / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / ".env").write_text("FAKE_SKILL_KEY=abc123\n")
        self._make_skill(
            project_root, "needy",
            metadata='{"faffmonkey":{"requires":{"env":["FAKE_SKILL_KEY"]}}}',
        )
        assert _install(project_root, workspace, "needy") == 0
        out = capsys.readouterr().out
        assert "All declared requirements are satisfied" in out

    def test_command_seam_wiring_is_pointed_out(self, project_root, workspace, capsys):
        self._make_skill(
            project_root, "imagey",
            human="# setup\nAdd to `state/commands.json`:\n",
        )
        assert _install(project_root, workspace, "imagey") == 0
        out = capsys.readouterr().out
        assert "state/commands.json" in out

    def test_no_requirements_reports_satisfied(self, project_root, workspace, capsys):
        assert _install(project_root, workspace, "weather") == 0
        out = capsys.readouterr().out
        assert "All declared requirements are satisfied" in out
