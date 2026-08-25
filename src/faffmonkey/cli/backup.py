"""faff backup — safe SQLite backup + tar of the whole data root."""

import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# Top-level members of a data-root snapshot. requirements.extra.txt rides
# along because setup wizards write it and the Docker build reads it.
_ROOT_DIRS = ("workspace", "state", "extensions")
_ROOT_FILES = ("requirements.extra.txt",)


def snapshot_data(root: Path, backups_dir: Path) -> Path:
    """Stage the data root (safe SQLite copy for sessions.db), tar with
    0o600 perms, and return the tarball path.

    Members are prefixed (state/..., workspace/...) so a snapshot is
    self-describing; restore recognises the old state-only layout by the
    absence of those prefixes.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backups_dir, 0o700)

    # Microseconds: "faff backup && faff update" on a small database makes
    # two snapshots in the same second, and they must not share a name.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    staging = backups_dir / timestamp
    staging.mkdir(mode=0o700)

    state_dir = root / "state"
    (staging / "state").mkdir(mode=0o700)
    db_path = state_dir / "sessions.db"
    if db_path.exists():
        src = sqlite3.connect(str(db_path))
        dest = sqlite3.connect(str(staging / "state" / "sessions.db"))
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        print("  backed up: state/sessions.db")

    # state/backups is skipped: on the legacy layout it holds older
    # snapshots (a snapshot must not contain its predecessors) and on both
    # layouts it holds compaction checkpoints. sessions.db* is skipped
    # because the safe copy above supersedes it (along with its -wal and
    # -shm, which are meaningless on their own).
    skip_names = {"backups", "sessions.db", "sessions.db-wal", "sessions.db-shm"}
    for item in sorted(state_dir.iterdir()):
        if item.name in skip_names or item.is_symlink():
            continue
        if item.is_dir():
            shutil.copytree(item, staging / "state" / item.name)
        else:
            shutil.copy2(item, staging / "state" / item.name)
        print(f"  backed up: state/{item.name}")

    for name in ("workspace", "extensions"):
        src_dir = root / name
        if src_dir.is_dir() and not src_dir.is_symlink():
            shutil.copytree(
                src_dir, staging / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            print(f"  backed up: {name}/")
    for name in _ROOT_FILES:
        f = root / name
        if f.is_file() and not f.is_symlink():
            shutil.copy2(f, staging / name)
            print(f"  backed up: {name}")

    tar_path = backups_dir / f"{timestamp}.tar.gz"
    fd = os.open(str(tar_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    with tarfile.open(tar_path, "w:gz") as tar:
        for item in sorted(staging.iterdir()):
            tar.add(item, arcname=item.name)

    shutil.rmtree(staging)
    return tar_path


def run_backup(base: Path) -> int:
    state_dir = base / "state"
    # base/backups, outside everything the snapshot covers, so a command
    # that deletes the data cannot take the backups with it.
    backups_dir = base / "backups"

    if not state_dir.is_dir():
        print("No state/ directory found. Nothing to back up.")
        return 1

    tar_path = snapshot_data(base, backups_dir)
    print(f"\n  Backup: {tar_path}")
    print(f"  Restore with: faff restore {tar_path.name}")
    print("  Copy it off this machine; an on-disk backup dies with the disk.")
    return 0


def run_restore(base: Path, name: str, force: bool = False) -> int:
    """Restore a snapshot over the data root.

    A backup nobody can restore from is not a backup, and there was no
    restore path at all: the operator was expected to work out the tarball
    layout themselves, on the day they had just lost a disk.

    The existing data is snapshotted first, so a restore of the wrong
    tarball is itself recoverable. Old state-only tarballs (top-level
    config.json) still restore, into state/.
    """
    state_dir = base / "state"
    backups_dir = base / "backups"
    legacy_backups = state_dir / "backups"
    tar_path = Path(name)
    if not tar_path.is_absolute():
        tar_path = backups_dir / name
        if not tar_path.is_file() and (legacy_backups / name).is_file():
            tar_path = legacy_backups / name

    if not tar_path.is_file():
        print(f"Not found: {tar_path}")
        available = sorted({
            p.name
            for d in (backups_dir, legacy_backups) if d.is_dir()
            for p in d.glob("*.tar.gz")
        })
        if available:
            print("\nAvailable snapshots:")
            for entry in available:
                print(f"  {entry}")
        return 1

    allowed_tops = _ROOT_DIRS + _ROOT_FILES
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()
        new_format = any(
            m.name == "state" or m.name.startswith("state/") for m in members
        )
        target = base if new_format else state_dir
        for member in members:
            # Never write outside the target. A tarball is untrusted input
            # even when the operator believes they made it.
            member_path = (target / member.name).resolve()
            if not member_path.is_relative_to(target.resolve()):
                print(f"  refusing path outside the restore target: {member.name}")
                return 1
            if member.issym() or member.islnk():
                print(f"  refusing link in archive: {member.name}")
                return 1
            if new_format and member.name.split("/", 1)[0] not in allowed_tops:
                print(f"  refusing unexpected member: {member.name}")
                return 1

        if state_dir.is_dir() and not force:
            safety = snapshot_data(base, backups_dir)
            print(f"  current data saved to: {safety.name}")

        # Only once the archive is known good. Extracting over the existing
        # tree made this a merge, not a restore: anything absent from the
        # tarball survived, so restoring a pre-cron backup left
        # cron-state.json in place and the scheduler honoured backoff for
        # jobs the restored config does not contain. backups/ is never
        # cleared: the safety snapshot is in it, and on the legacy layout
        # state/backups also holds compaction checkpoints.
        def _clear_state_contents(d: Path) -> None:
            for entry in d.iterdir():
                if entry.name == "backups":
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()

        if new_format:
            base.mkdir(parents=True, exist_ok=True)
            for top in allowed_tops:
                p = base / top
                if p.name == "state":
                    if p.is_dir() and not p.is_symlink():
                        _clear_state_contents(p)
                elif p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                elif p.exists() or p.is_symlink():
                    p.unlink()
            tar.extractall(base, filter="data")
        else:
            state_dir.mkdir(parents=True, exist_ok=True)
            _clear_state_contents(state_dir)
            tar.extractall(state_dir, filter="data")

    print(f"\n  Restored {tar_path.name} into {target}")
    print("  Run: faff doctor")
    return 0
