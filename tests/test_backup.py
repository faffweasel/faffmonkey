"""Tests for faff backup."""

import os
import sqlite3
import stat
import tarfile
from pathlib import Path
from unittest.mock import patch


from faffmonkey.cli.backup import run_backup, run_restore, snapshot_data


class TestBackup:
    def test_creates_tarball(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"test": true}')

        result = run_backup(tmp_path)
        assert result == 0

        backups_dir = tmp_path / "backups"
        tarballs = list(backups_dir.glob("*.tar.gz"))
        assert len(tarballs) == 1

        with tarfile.open(tarballs[0], "r:gz") as tar:
            names = tar.getnames()
            assert "state/config.json" in names

    def test_includes_sqlite_backup(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        db_path = state_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test_data (id INTEGER, value TEXT)")
        conn.execute("INSERT INTO test_data VALUES (1, 'hello')")
        conn.commit()
        conn.close()

        result = run_backup(tmp_path)
        assert result == 0

        tarballs = list((tmp_path / "backups").glob("*.tar.gz"))
        with tarfile.open(tarballs[0], "r:gz") as tar:
            assert "state/sessions.db" in tar.getnames()
            tar.extractall(tmp_path / "extract")

        restored = sqlite3.connect(str(tmp_path / "extract" / "state" / "sessions.db"))
        row = restored.execute("SELECT value FROM test_data WHERE id = 1").fetchone()
        restored.close()
        assert row[0] == "hello"

    def test_covers_the_whole_data_root(self, tmp_path, capsys):
        """2026-08-24: the state-only backup would have restored config and
        history and none of the agent's memory."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "note.md").write_text("irreplaceable")
        (tmp_path / "extensions").mkdir()
        (tmp_path / "extensions" / "channel_x.py").write_text("# ext")
        (tmp_path / "requirements.extra.txt").write_text("somepkg==1\n")

        assert run_backup(tmp_path) == 0

        tarballs = list((tmp_path / "backups").glob("*.tar.gz"))
        with tarfile.open(tarballs[0], "r:gz") as tar:
            names = tar.getnames()
        assert "workspace/memory/note.md" in names
        assert "extensions/channel_x.py" in names
        assert "requirements.extra.txt" in names

    def test_no_state_dir(self, tmp_path, capsys):
        result = run_backup(tmp_path)
        assert result == 1

    def test_tarball_permissions(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        run_backup(tmp_path)

        backups_dir = tmp_path / "backups"
        tarballs = list(backups_dir.glob("*.tar.gz"))
        assert len(tarballs) == 1
        mode = tarballs[0].stat().st_mode & 0o777
        assert mode == 0o600

        dir_mode = backups_dir.stat().st_mode & 0o777
        assert dir_mode == 0o700

    def test_tarball_created_with_0600_no_window(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        original_open = tarfile.open
        permissions_during_write: list[int] = []

        def capturing_tarfile_open(name, mode="r", **kwargs):
            if mode == "w:gz" and name is not None:
                file_mode = stat.S_IMODE(os.stat(name).st_mode)
                permissions_during_write.append(file_mode)
            return original_open(name, mode, **kwargs)

        with patch("faffmonkey.cli.backup.tarfile.open", side_effect=capturing_tarfile_open):
            run_backup(tmp_path)

        assert permissions_during_write, "tarfile.open was not called"
        assert permissions_during_write[0] == 0o600

    def test_staging_dir_created_with_0700(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        dirs_created: list[tuple[Path, int]] = []
        original_mkdir = Path.mkdir

        def capturing_mkdir(self, *args, **kwargs):
            original_mkdir(self, *args, **kwargs)
            backups_dir = tmp_path / "backups"
            if self.parent == backups_dir:
                dirs_created.append((self, kwargs.get("mode", None)))

        with patch.object(Path, "mkdir", capturing_mkdir):
            run_backup(tmp_path)

        assert dirs_created, "staging dir mkdir was not captured"
        _, mode = dirs_created[0]
        assert mode == 0o700

    def test_no_staging_dir_left(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")

        run_backup(tmp_path)

        backups_dir = tmp_path / "backups"
        # only .tar.gz files, no staging directories
        entries = list(backups_dir.iterdir())
        for entry in entries:
            assert entry.suffix == ".gz"


class TestSnapshotCollision:
    """P7-M2: two snapshots in the same second truncated each other."""

    def test_two_snapshots_in_quick_succession_both_survive(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        backups_dir = tmp_path / "backups"

        first = snapshot_data(tmp_path, backups_dir)
        second = snapshot_data(tmp_path, backups_dir)

        assert first != second
        assert len(list(backups_dir.glob("*.tar.gz"))) == 2


class TestRestore:
    """A restore has to return the data root to the backed-up point in time.

    extractall over the existing tree made it a merge: anything absent from
    the tarball survived, so a pre-cron backup left cron-state.json in place
    and the scheduler honoured backoff for jobs the restored config does not
    contain. There were no tests for run_restore at all.
    """

    def _snapshot(self, tmp_path) -> str:
        assert run_backup(tmp_path) == 0
        tarballs = list((tmp_path / "backups").glob("*.tar.gz"))
        assert len(tarballs) == 1
        return tarballs[0].name

    def test_files_absent_from_the_snapshot_do_not_survive(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"timezone": "UTC"}')
        name = self._snapshot(tmp_path)

        (state_dir / "cron-state.json").write_text('{"backoff": {}}')
        (state_dir / "logs").mkdir()
        (state_dir / "logs" / "cron.jsonl").write_text("{}\n")
        (state_dir / "config.json").write_text('{"timezone": "Asia/Bangkok"}')

        assert run_restore(tmp_path, name, force=True) == 0

        assert (state_dir / "config.json").read_text() == '{"timezone": "UTC"}'
        assert not (state_dir / "cron-state.json").exists()
        assert not (state_dir / "logs").exists()

    def test_workspace_round_trips(self, tmp_path, capsys):
        """2026-08-24: memory files were the irreplaceable loss; the backup
        did not contain them at all."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        workspace = tmp_path / "workspace"
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "note.md").write_text("irreplaceable")
        (tmp_path / "requirements.extra.txt").write_text("somepkg==1\n")
        name = self._snapshot(tmp_path)

        (workspace / "memory" / "note.md").unlink()
        (workspace / "stray.md").write_text("post-snapshot file")
        (tmp_path / "requirements.extra.txt").unlink()

        assert run_restore(tmp_path, name, force=True) == 0

        assert (workspace / "memory" / "note.md").read_text() == "irreplaceable"
        assert not (workspace / "stray.md").exists()
        assert (tmp_path / "requirements.extra.txt").read_text() == "somepkg==1\n"

    def test_backups_dir_survives_the_clear(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        name = self._snapshot(tmp_path)

        # Without force a safety snapshot is taken first, and it lands in
        # backups/. Clearing that would destroy the escape route.
        assert run_restore(tmp_path, name) == 0
        assert (tmp_path / "backups" / name).is_file()
        assert len(list((tmp_path / "backups").glob("*.tar.gz"))) == 2

    def test_a_rejected_archive_leaves_state_alone(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"keep": true}')
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        hostile = backups_dir / "hostile.tar.gz"
        payload = tmp_path / "payload"
        payload.write_text("x")
        with tarfile.open(hostile, "w:gz") as tar:
            tar.add(payload, arcname="../escaped.json")

        assert run_restore(tmp_path, "hostile.tar.gz", force=True) == 1
        # The clear must not run before the archive is known good.
        assert (state_dir / "config.json").read_text() == '{"keep": true}'

    def test_unexpected_top_level_member_rejected(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"keep": true}')
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        odd = backups_dir / "odd.tar.gz"
        payload = tmp_path / "payload2"
        payload.write_text("x")
        with tarfile.open(odd, "w:gz") as tar:
            tar.add(payload, arcname="state/config.json")
            tar.add(payload, arcname="not-a-data-dir/file.txt")

        assert run_restore(tmp_path, "odd.tar.gz", force=True) == 1
        assert (state_dir / "config.json").read_text() == '{"keep": true}'


class TestLegacyRestore:
    """Old snapshots hold state/ contents at the top level. They must keep
    restoring, into state/, or every backup made before the data root
    existed is dead."""

    def test_state_only_tarball_restores_into_state(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"timezone": "Asia/Bangkok"}')
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        old_config = tmp_path / "old_config.json"
        old_config.write_text('{"timezone": "UTC"}')
        legacy = backups_dir / "legacy.tar.gz"
        with tarfile.open(legacy, "w:gz") as tar:
            tar.add(old_config, arcname="config.json")

        assert run_restore(tmp_path, "legacy.tar.gz", force=True) == 0
        assert (state_dir / "config.json").read_text() == '{"timezone": "UTC"}'

    def test_legacy_backups_location_still_searched(self, tmp_path, capsys):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "config.json").write_text("{}")
        legacy_dir = state_dir / "backups"
        legacy_dir.mkdir()

        old_config = tmp_path / "old_config.json"
        old_config.write_text('{"from": "legacy"}')
        with tarfile.open(legacy_dir / "old.tar.gz", "w:gz") as tar:
            tar.add(old_config, arcname="config.json")

        assert run_restore(tmp_path, "old.tar.gz", force=True) == 0
        assert (state_dir / "config.json").read_text() == '{"from": "legacy"}'
