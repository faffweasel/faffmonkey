"""Tests for faff update and faff update-extension."""

import hashlib
import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest


from faffmonkey.cli.update import (
    MAX_SNAPSHOTS,
    _check_contrib_staleness,
    _check_skill_staleness,
    _run_migrations,
    _snapshot,
    _sync_templates,
    run_update,
    run_update_extension,
)
from faffmonkey.runtime.session import SCHEMA_VERSION


class TestSnapshot:
    def test_creates_tarball(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"test": true}')
        backups_dir = tmp_path / "backups"

        _snapshot(tmp_path, backups_dir)

        tarballs = list(backups_dir.glob("*.tar.gz"))
        assert len(tarballs) == 1

        with tarfile.open(tarballs[0], "r:gz") as tar:
            names = tar.getnames()
            assert "state/config.json" in names

    def test_includes_db_backup(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.execute("INSERT INTO test VALUES (42)")
        conn.commit()
        conn.close()

        backups_dir = tmp_path / "backups"
        _snapshot(tmp_path, backups_dir)

        tarballs = list(backups_dir.glob("*.tar.gz"))
        with tarfile.open(tarballs[0], "r:gz") as tar:
            assert "state/sessions.db" in tar.getnames()

    def test_tarball_created_with_0600(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"test": true}')
        backups_dir = tmp_path / "backups"

        original_open = tarfile.open
        permissions_during_write: list[int] = []

        def capturing_tarfile_open(name, mode="r", **kwargs):
            if mode == "w:gz" and name is not None:
                file_mode = stat.S_IMODE(os.stat(name).st_mode)
                permissions_during_write.append(file_mode)
            return original_open(name, mode, **kwargs)

        with patch("faffmonkey.cli.backup.tarfile.open", side_effect=capturing_tarfile_open):
            _snapshot(tmp_path, backups_dir)

        assert permissions_during_write, "tarfile.open was not called"
        assert permissions_during_write[0] == 0o600

        tarballs = list(backups_dir.glob("*.tar.gz"))
        assert len(tarballs) == 1
        assert stat.S_IMODE(os.stat(tarballs[0]).st_mode) == 0o600

    def test_snap_dir_created_with_0700(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        backups_dir = tmp_path / "backups"

        dirs_created: list[tuple[Path, int]] = []
        original_mkdir = Path.mkdir

        def capturing_mkdir(self, *args, **kwargs):
            original_mkdir(self, *args, **kwargs)
            if self.parent == backups_dir:
                dirs_created.append((self, kwargs.get("mode", None)))

        with patch.object(Path, "mkdir", capturing_mkdir):
            _snapshot(tmp_path, backups_dir)

        assert dirs_created, "staging dir mkdir was not captured"
        _, mode = dirs_created[0]
        assert mode == 0o700

    def test_rotates_old_snapshots(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        for i in range(MAX_SNAPSHOTS + 2):
            ts = f"20260514T{i:06d}Z"
            (backups_dir / f"{ts}.tar.gz").write_text("fake")

        _snapshot(tmp_path, backups_dir)

        tarballs = list(backups_dir.glob("*.tar.gz"))
        assert len(tarballs) == MAX_SNAPSHOTS


class TestMigrations:
    def test_no_database(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _run_migrations(state_dir)
        assert "no database yet" in capsys.readouterr().out

    def test_current_schema(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        db_path = state_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        conn.close()

        _run_migrations(state_dir)
        assert "up to date" in capsys.readouterr().out


class TestSyncTemplates:
    def test_copies_missing_files(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        template_dir = tmp_path / "templates" / "workspace"
        template_dir.mkdir(parents=True)
        (template_dir / "NEW_TEMPLATE.md").write_text("# New template")

        monkeypatch.setattr(
            "faffmonkey.cli.update._find_template_dir",
            lambda: tmp_path / "templates",
        )

        _sync_templates(workspace)
        assert (workspace / "NEW_TEMPLATE.md").exists()
        assert "copied: NEW_TEMPLATE.md" in capsys.readouterr().out

    def test_skips_dotfiles(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        template_dir = tmp_path / "templates" / "workspace"
        template_dir.mkdir(parents=True)
        (template_dir / ".DS_Store").write_bytes(b"junk")
        (template_dir / "SOUL.md").write_text("# Soul")
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_template_dir",
            lambda: tmp_path / "templates",
        )

        _sync_templates(workspace)
        assert not (workspace / ".DS_Store").exists()
        assert (workspace / "SOUL.md").exists()
        assert ".DS_Store" not in capsys.readouterr().out

    def test_does_not_overwrite(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "SOUL.md").write_text("# My soul")

        template_dir = tmp_path / "templates" / "workspace"
        template_dir.mkdir(parents=True)
        (template_dir / "SOUL.md").write_text("# Template soul")

        monkeypatch.setattr(
            "faffmonkey.cli.update._find_template_dir",
            lambda: tmp_path / "templates",
        )

        _sync_templates(workspace)
        assert (workspace / "SOUL.md").read_text() == "# My soul"

    def test_copies_missing_skills(self, tmp_path, capsys, monkeypatch):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        template_dir = tmp_path / "templates" / "workspace"
        skill_dir = template_dir / "skills" / "new-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: new-skill\n---\n")

        monkeypatch.setattr(
            "faffmonkey.cli.update._find_template_dir",
            lambda: tmp_path / "templates",
        )

        _sync_templates(workspace)
        assert (workspace / "skills" / "new-skill" / "SKILL.md").exists()


class TestSyncBuiltinSkills:
    def _setup(self, tmp_path, monkeypatch, template_content="---\nname: sk\n---\n"):
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        skill_dir = tmp_path / "templates" / "workspace" / "skills" / "sk"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(template_content)
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_template_dir",
            lambda: tmp_path / "templates",
        )
        return workspace, skill_dir

    def _origin(self, workspace):
        return json.loads((workspace / "skills" / ".origin.json").read_text())

    def test_install_records_provenance(self, tmp_path, capsys, monkeypatch):
        workspace, _skill_dir = self._setup(tmp_path, monkeypatch)
        _sync_templates(workspace)
        entry = self._origin(workspace)["sk"]
        assert entry["source"] == "templates/workspace/skills/sk"
        assert entry["source_hash"]
        assert "installed skill: sk" in capsys.readouterr().out

    def test_pristine_copy_updated_when_template_changes(self, tmp_path, capsys, monkeypatch):
        workspace, skill_dir = self._setup(tmp_path, monkeypatch)
        _sync_templates(workspace)
        old_hash = self._origin(workspace)["sk"]["source_hash"]

        (skill_dir / "SKILL.md").write_text("---\nname: sk\n---\nv2\n")
        _sync_templates(workspace)

        assert "v2" in (workspace / "skills" / "sk" / "SKILL.md").read_text()
        assert self._origin(workspace)["sk"]["source_hash"] != old_hash
        assert "updated skill: sk" in capsys.readouterr().out

    def test_pristine_copy_untouched_when_template_unchanged(self, tmp_path, capsys, monkeypatch):
        workspace, _skill_dir = self._setup(tmp_path, monkeypatch)
        _sync_templates(workspace)
        first = self._origin(workspace)["sk"]
        _sync_templates(workspace)
        assert self._origin(workspace)["sk"] == first
        assert "all up to date" in capsys.readouterr().out

    def test_modified_copy_not_overwritten(self, tmp_path, capsys, monkeypatch):
        workspace, skill_dir = self._setup(tmp_path, monkeypatch)
        _sync_templates(workspace)

        installed = workspace / "skills" / "sk" / "SKILL.md"
        installed.write_text("---\nname: sk\n---\ncustomised\n")
        (skill_dir / "SKILL.md").write_text("---\nname: sk\n---\nv2\n")
        _sync_templates(workspace)

        assert "customised" in installed.read_text()
        out = capsys.readouterr().out
        assert "modified locally" in out
        assert "delete workspace/skills/sk" in out

    def test_modified_copy_silent_when_template_unchanged(self, tmp_path, capsys, monkeypatch):
        workspace, _skill_dir = self._setup(tmp_path, monkeypatch)
        _sync_templates(workspace)
        installed = workspace / "skills" / "sk" / "SKILL.md"
        installed.write_text("---\nname: sk\n---\ncustomised\n")
        _sync_templates(workspace)
        assert "customised" in installed.read_text()
        assert "!!" not in capsys.readouterr().out

    def test_preprovenance_identical_copy_adopted(self, tmp_path, capsys, monkeypatch):
        workspace, skill_dir = self._setup(tmp_path, monkeypatch)
        dst = workspace / "skills" / "sk"
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text((skill_dir / "SKILL.md").read_text())

        _sync_templates(workspace)
        entry = self._origin(workspace)["sk"]
        assert entry["source"] == "templates/workspace/skills/sk"

    def test_preprovenance_differing_copy_warned_not_touched(self, tmp_path, capsys, monkeypatch):
        workspace, _skill_dir = self._setup(tmp_path, monkeypatch)
        dst = workspace / "skills" / "sk"
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text("---\nname: sk\n---\nedited long ago\n")

        _sync_templates(workspace)
        assert "edited long ago" in (dst / "SKILL.md").read_text()
        assert not (workspace / "skills" / ".origin.json").exists()
        assert "no provenance" in capsys.readouterr().out

    def test_contrib_sourced_entry_skipped(self, tmp_path, capsys, monkeypatch):
        workspace, _skill_dir = self._setup(tmp_path, monkeypatch)
        skills = workspace / "skills"
        dst = skills / "sk"
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text("contrib version")
        (skills / ".origin.json").write_text(json.dumps({
            "sk": {
                "source": "contrib/skills/sk",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": "abc123def456",
            }
        }))

        _sync_templates(workspace)
        assert (dst / "SKILL.md").read_text() == "contrib version"
        assert self._origin(workspace)["sk"]["source"] == "contrib/skills/sk"

    def test_pycache_not_copied(self, tmp_path, capsys, monkeypatch):
        workspace, skill_dir = self._setup(tmp_path, monkeypatch)
        pycache = skill_dir / "scripts" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "junk.pyc").write_bytes(b"junk")
        (skill_dir / "scripts" / "run.py").write_text("print('hi')\n")

        _sync_templates(workspace)
        assert (workspace / "skills" / "sk" / "scripts" / "run.py").exists()
        assert not (workspace / "skills" / "sk" / "scripts" / "__pycache__").exists()

    def test_builtin_entries_skipped_by_contrib_staleness(self, tmp_path, capsys):
        skills = tmp_path / "workspace" / "skills"
        skills.mkdir(parents=True)
        (skills / "sk").mkdir()
        (skills / ".origin.json").write_text(json.dumps({
            "sk": {
                "source": "templates/workspace/skills/sk",
                "copied_at": "2026-01-01T00:00:00Z",
                "source_hash": "abc123def456",
            }
        }))
        _check_skill_staleness(tmp_path, tmp_path / "workspace")
        out = capsys.readouterr().out
        assert "escapes" not in out
        assert "unverifiable" not in out

    def test_contrib_skill_verified_against_checkout_not_data_root(
        self, tmp_path, capsys, monkeypatch
    ):
        # After the data-root split, contrib/ lives in the checkout and the
        # data root has none. Reinstalling could never clear "unverifiable"
        # because the check looked for contrib/ under the data root.
        from faffmonkey.cli.skill import _dir_hash

        checkout = tmp_path / "checkout"
        data_root = tmp_path / "data"
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_project_root", lambda: checkout
        )
        src = checkout / "contrib" / "skills" / "aqi"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("---\nname: aqi\n---\n")
        skills = data_root / "workspace" / "skills"
        dst = skills / "aqi"
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text((src / "SKILL.md").read_text())
        (skills / ".origin.json").write_text(json.dumps({
            "aqi": {
                "source": "contrib/skills/aqi",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": _dir_hash(src),
            }
        }))

        _check_skill_staleness(data_root, data_root / "workspace")
        out = capsys.readouterr().out
        assert "unverifiable" not in out
        assert "stale" not in out
        assert "modified" not in out

        (src / "SKILL.md").write_text("---\nname: aqi\n---\nv2\n")
        _check_skill_staleness(data_root, data_root / "workspace")
        assert "stale: skill aqi" in capsys.readouterr().out


class TestContribStaleness:
    @pytest.fixture(autouse=True)
    def _project_root_is_tmp(self, tmp_path, monkeypatch):
        # Contrib and templates resolve against the checkout; these tests
        # build theirs under tmp_path.
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_project_root", lambda: tmp_path
        )

    def test_detects_stale(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        original = b"# original version"
        install_hash = hashlib.sha256(original).hexdigest()[:12]
        (ext_dir / "channel_telegram.py").write_bytes(original)
        (contrib_dir / "channel_telegram.py").write_text("# updated")
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": install_hash,
            }
        }))

        _check_contrib_staleness(tmp_path)
        out = capsys.readouterr().out
        assert "stale" in out
        assert "update-extension" in out

    def test_detects_tampered(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        original = b"# original"
        tampered = b"# tampered by attacker"
        install_hash = hashlib.sha256(original).hexdigest()[:12]

        (contrib_dir / "channel_telegram.py").write_bytes(original)
        (ext_dir / "channel_telegram.py").write_bytes(tampered)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": install_hash,
            }
        }))

        _check_contrib_staleness(tmp_path)
        out = capsys.readouterr().out
        assert "modified since install" in out

    def test_rejects_source_traversal(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "evil.py").write_text("# evil")
        (ext_dir / ".origin.json").write_text(json.dumps({
            "evil.py": {
                "source": "../../etc/passwd",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": "abcdef123456",
            }
        }))

        _check_contrib_staleness(tmp_path)
        out = capsys.readouterr().out
        assert "escapes contrib/" in out

    def test_tampered_with_forged_install_hash(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        original = b"# original"
        tampered = b"# tampered by attacker"
        forged_hash = hashlib.sha256(tampered).hexdigest()[:12]

        (contrib_dir / "channel_telegram.py").write_bytes(original)
        (ext_dir / "channel_telegram.py").write_bytes(tampered)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": forged_hash,
            }
        }))

        _check_contrib_staleness(tmp_path)
        out = capsys.readouterr().out
        assert "stale" in out

    def test_unverifiable_no_source(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()

        content = b"# custom extension"
        (ext_dir / "custom_plugin.py").write_bytes(content)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "custom_plugin.py": {
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": hashlib.sha256(content).hexdigest()[:12],
            }
        }))

        _check_contrib_staleness(tmp_path)
        out = capsys.readouterr().out
        assert "unverifiable" in out

    def test_old_contrib_hash_entry_unverifiable(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        content = b"# telegram channel"
        (contrib_dir / "channel_telegram.py").write_bytes(content)
        (ext_dir / "channel_telegram.py").write_bytes(content)
        h = hashlib.sha256(content).hexdigest()[:12]
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_hash": h,
            }
        }))

        _check_contrib_staleness(tmp_path)
        out = capsys.readouterr().out
        assert "unverifiable" in out


class TestRunUpdate:
    @pytest.fixture(autouse=True)
    def _project_root_is_tmp(self, tmp_path, monkeypatch):
        # Contrib and templates resolve against the checkout; these tests
        # build theirs under tmp_path.
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_project_root", lambda: tmp_path
        )

    def test_idempotent(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        db_path = state_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        conn.close()

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        result1 = run_update(tmp_path)
        result2 = run_update(tmp_path)

        assert result1 == 0
        assert result2 == 0

    def test_no_state_dir(self, tmp_path, capsys):
        result = run_update(tmp_path)
        assert result == 1
        assert "faff init" in capsys.readouterr().out


class TestUpdateExtension:
    @pytest.fixture(autouse=True)
    def _project_root_is_tmp(self, tmp_path, monkeypatch):
        # Contrib and templates resolve against the checkout; these tests
        # build theirs under tmp_path.
        monkeypatch.setattr(
            "faffmonkey.cli.update._find_project_root", lambda: tmp_path
        )

    def test_read_only_extensions_dir_says_run_on_the_host(self, tmp_path, capsys):
        """Run inside the container, where extensions/ is a read-only mount,
        it got as far as creating the .bak and died with a raw OSError."""
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "channel_telegram.py").write_text("# installed")
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {"source": "contrib/channel_telegram.py"},
        }))
        os.chmod(ext_dir, 0o500)
        try:
            with pytest.raises(SystemExit) as exc:
                run_update_extension(tmp_path, "telegram")
        finally:
            os.chmod(ext_dir, 0o700)

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "./bin/faff update-extension telegram" in out
        assert "docker compose restart" in out
        assert not (ext_dir / "channel_telegram.py.bak").exists()

    def test_updates_stale_extension(self, tmp_path):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        old_content = b"# old version"
        new_content = b"# new version"
        old_install_hash = hashlib.sha256(old_content).hexdigest()[:12]

        (ext_dir / "channel_telegram.py").write_bytes(old_content)
        (contrib_dir / "channel_telegram.py").write_bytes(new_content)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": old_install_hash,
            }
        }))

        result = run_update_extension(tmp_path, "telegram")
        assert result == 0

        assert (ext_dir / "channel_telegram.py.bak").exists()
        assert (ext_dir / "channel_telegram.py.bak").read_bytes() == old_content

        assert (ext_dir / "channel_telegram.py").read_bytes() == new_content

        origin = json.loads((ext_dir / ".origin.json").read_text())
        new_source_hash = hashlib.sha256(new_content).hexdigest()[:12]
        assert origin["channel_telegram.py"]["contrib_source_hash"] == new_source_hash

    def _voice_pair(self, tmp_path):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()
        origin = {}
        for filename in ("transcriber_openai.py", "synthesiser_openai.py"):
            old = f"# old {filename}".encode()
            (ext_dir / filename).write_bytes(old)
            (contrib_dir / filename).write_bytes(f"# new {filename}".encode())
            origin[filename] = {
                "source": f"contrib/{filename}",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": hashlib.sha256(old).hexdigest()[:12],
            }
        (ext_dir / ".origin.json").write_text(json.dumps(origin))
        return ext_dir

    def test_voice_short_name_resolves(self, tmp_path):
        ext_dir = self._voice_pair(tmp_path)
        (ext_dir / "synthesiser_openai.py").unlink()
        origin = json.loads((ext_dir / ".origin.json").read_text())
        del origin["synthesiser_openai.py"]
        (ext_dir / ".origin.json").write_text(json.dumps(origin))

        result = run_update_extension(tmp_path, "openai")
        assert result == 0
        assert (ext_dir / "transcriber_openai.py").read_bytes() == b"# new transcriber_openai.py"

    def test_ambiguous_short_name_refused(self, tmp_path, capsys):
        ext_dir = self._voice_pair(tmp_path)
        result = run_update_extension(tmp_path, "openai")
        assert result == 1
        out = capsys.readouterr().out
        assert "Ambiguous" in out
        assert "transcriber_openai.py" in out
        assert (ext_dir / "transcriber_openai.py").read_bytes() == b"# old transcriber_openai.py"

    def test_full_filename_bypasses_ambiguity(self, tmp_path):
        ext_dir = self._voice_pair(tmp_path)
        result = run_update_extension(tmp_path, "transcriber_openai.py")
        assert result == 0
        assert (ext_dir / "transcriber_openai.py").read_bytes() == b"# new transcriber_openai.py"
        assert (ext_dir / "synthesiser_openai.py").read_bytes() == b"# old synthesiser_openai.py"

    def test_already_up_to_date(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        content = b"# same version"
        h = hashlib.sha256(content).hexdigest()[:12]

        (ext_dir / "channel_telegram.py").write_bytes(content)
        (contrib_dir / "channel_telegram.py").write_bytes(content)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": h,
            }
        }))

        result = run_update_extension(tmp_path, "telegram")
        assert result == 0
        assert "Already up to date" in capsys.readouterr().out

    def test_rejects_source_traversal(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "evil.py").write_text("# evil")
        (ext_dir / ".origin.json").write_text(json.dumps({
            "evil.py": {
                "source": "../../etc/passwd",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": "abcdef123456",
            }
        }))

        result = run_update_extension(tmp_path, "evil.py")
        assert result == 1
        assert "escapes contrib/" in capsys.readouterr().out

    def test_refuses_non_contrib(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / ".origin.json").write_text("{}")

        result = run_update_extension(tmp_path, "custom-thing")
        assert result == 1
        assert "didn't come from contrib" in capsys.readouterr().out

    def test_no_extensions_dir(self, tmp_path, capsys):
        result = run_update_extension(tmp_path, "telegram")
        assert result == 1

    def test_symlinked_dest_skipped(self, tmp_path, capsys):
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        contrib_dir = tmp_path / "contrib"
        contrib_dir.mkdir()

        target = tmp_path / "real_file.py"
        target.write_text("# real")
        (contrib_dir / "channel_telegram.py").write_text("# new version")
        (ext_dir / "channel_telegram.py").symlink_to(target)
        (ext_dir / ".origin.json").write_text(json.dumps({
            "channel_telegram.py": {
                "source": "contrib/channel_telegram.py",
                "copied_at": "2026-01-01T00:00:00Z",
                "contrib_source_hash": "abcdef123456",
            }
        }))

        result = run_update_extension(tmp_path, "telegram")
        assert result == 1
        out = capsys.readouterr().out
        assert "symlink" in out
        assert target.read_text() == "# real"


class TestUpdateSurvivesDamage:
    """P7-M1/L1/D23: three ways update aborted after taking the snapshot."""

    def test_database_without_schema_table_does_not_traceback(self, tmp_path, capsys):
        from faffmonkey.cli.update import _run_migrations

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "sessions.db").write_bytes(b"")

        _run_migrations(state_dir)

        assert "unreadable" in capsys.readouterr().out

    def test_symlinked_workspace_is_a_skipped_step_not_a_crash(self, tmp_path, capsys):
        from faffmonkey.cli.update import run_update

        base = tmp_path / "project"
        (base / "state").mkdir(parents=True)
        real = base / "ws-real"
        real.mkdir()
        (base / "workspace").symlink_to(real)

        assert run_update(base) == 0
        assert "skipped" in capsys.readouterr().out

    def test_extension_backups_are_versioned(self, tmp_path):
        from faffmonkey.cli.update import _next_backup_path

        ext = tmp_path / "channel_telegram.py"
        ext.write_text("v1")
        first = _next_backup_path(ext)
        assert first.name == "channel_telegram.py.bak"
        first.write_text("v1")
        second = _next_backup_path(ext)
        assert second.name == "channel_telegram.py.bak2"


class TestDataRootMigration:
    """2026-08-24: a deploy rsync deleted workspace/, state/ and the backups
    inside state/ because they all lived in the checkout. faff update moves
    a legacy in-checkout install to the data root, one time, with
    confirmation."""

    def _legacy_install(self, tmp_path):
        legacy = tmp_path / "checkout"
        (legacy / "state" / "backups").mkdir(parents=True)
        (legacy / "state" / "config.json").write_text("{}")
        (legacy / "state" / "backups" / "old.tar.gz").write_text("snap")
        (legacy / "state" / "backups" / "checkpoint_x").mkdir()
        (legacy / "workspace" / "memory").mkdir(parents=True)
        (legacy / "workspace" / "memory" / "note.md").write_text("hi")
        (legacy / "extensions").mkdir()
        (legacy / "requirements.extra.txt").write_text("dep==1\n")
        return legacy

    def test_moves_everything_and_relocates_snapshots(self, tmp_path, capsys, monkeypatch):
        legacy = self._legacy_install(tmp_path)
        root = tmp_path / "data"
        monkeypatch.setattr("faffmonkey.cli.update._find_project_root", lambda: legacy)
        monkeypatch.setattr("builtins.input", lambda *a: "y")

        assert run_update(root) == 0

        assert (root / "state" / "config.json").is_file()
        assert (root / "workspace" / "memory" / "note.md").read_text() == "hi"
        assert (root / "requirements.extra.txt").read_text() == "dep==1\n"
        # operator snapshots move out of state/; compaction checkpoints
        # stay where the container mounts them
        assert (root / "backups" / "old.tar.gz").is_file()
        assert not (root / "state" / "backups" / "old.tar.gz").exists()
        assert (root / "state" / "backups" / "checkpoint_x").is_dir()
        # the checkout keeps only the disposable build mirror
        assert not (legacy / "state").exists()
        assert not (legacy / "workspace").exists()
        assert (legacy / "requirements.extra.txt").read_text() == "dep==1\n"

    def test_decline_moves_nothing(self, tmp_path, capsys, monkeypatch):
        legacy = self._legacy_install(tmp_path)
        root = tmp_path / "data"
        monkeypatch.setattr("faffmonkey.cli.update._find_project_root", lambda: legacy)
        monkeypatch.setattr("builtins.input", lambda *a: "n")

        assert run_update(root) == 1
        assert (legacy / "state" / "config.json").is_file()
        assert not (root / "state").exists()

    def test_no_migration_when_root_already_has_state(self, tmp_path, capsys, monkeypatch):
        legacy = self._legacy_install(tmp_path)
        root = tmp_path / "data"
        (root / "state").mkdir(parents=True)
        (root / "state" / "config.json").write_text("{}")
        (root / "workspace").mkdir()

        monkeypatch.setattr("faffmonkey.cli.update._find_project_root", lambda: legacy)

        def _refuse(*args):
            raise AssertionError("migration prompt fired for a migrated install")
        monkeypatch.setattr("builtins.input", _refuse)

        assert run_update(root) == 0
        assert (legacy / "state" / "config.json").is_file()
