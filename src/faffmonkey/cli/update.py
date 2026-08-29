"""faff update: pre-upgrade migration and template sync."""

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from faffmonkey.cli.backup import snapshot_data
from faffmonkey.cli.init import _check_no_symlink_components, _find_project_root


MAX_SNAPSHOTS = 5


def _snapshot(root: Path, backups_dir: Path) -> None:
    tar_path = snapshot_data(root, backups_dir)
    print(f"  snapshot: {tar_path}")

    # rotate: keep last N
    snapshots = sorted(backups_dir.glob("*.tar.gz"))
    while len(snapshots) > MAX_SNAPSHOTS:
        oldest = snapshots.pop(0)
        oldest.unlink()
        print(f"  rotated: {oldest.name}")


def _run_migrations(state_dir: Path) -> None:
    db_path = state_dir / "sessions.db"
    if not db_path.exists():
        print("  database: no database yet (will be created on first run)")
        return

    from faffmonkey.runtime.session import SCHEMA_VERSION
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as e:
        print(f"  database: unreadable ({e}); skipping migration check")
        return
    try:
        # doctor already degrades gracefully here; this copy did not, and
        # it is the one in the command that mutates state. A zero-byte
        # sessions.db made faff update unusable entirely.
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.Error as e:
        print(f"  database: unreadable ({e}); skipping migration check")
        conn.close()
        return
    try:
        current = row[0] if row else 0
        if current >= SCHEMA_VERSION:
            print(f"  database: schema v{current} (up to date)")
        else:
            print(f"  database: schema v{current}, expected v{SCHEMA_VERSION}")
            print("  no migrations implemented yet; manual intervention required")
    finally:
        conn.close()


def _find_template_dir() -> Path | None:
    try:
        template_dir = _find_project_root() / "templates"
    except RuntimeError:
        return None
    return template_dir if template_dir.is_dir() else None


def _sync_templates(workspace: Path) -> None:
    template_dir = _find_template_dir()
    if template_dir is None:
        print("  templates: template directory not found")
        return

    template_workspace = template_dir / "workspace"
    if not template_workspace.is_dir():
        return

    root = workspace.parent
    count = 0
    for src in sorted(template_workspace.iterdir()):
        if src.is_file():
            if src.name.startswith("."):
                # .DS_Store and friends, as the skill copy below ignores.
                continue
            if src.is_symlink():
                print(f"  skipped (symlink source): {src.name}")
                continue
            dst = workspace / src.name
            _check_no_symlink_components(dst, root)
            if dst.is_symlink():
                print(f"  skipped (symlink): {src.name}")
                continue
            if not dst.exists():
                shutil.copy2(src, dst)
                print(f"  copied: {src.name}")
                count += 1

    # sync skill directories
    template_skills = template_workspace / "skills"
    if template_skills.is_dir():
        count += _sync_builtin_skills(template_skills, workspace, root)

    if count == 0:
        print("  templates: all up to date")


# Prompts the wizard wrote before 0.2.0. A job still carrying one has not
# been edited by the operator, so it is safe to rewrite.
_OLD_HEARTBEAT_PROMPTS = frozenset({
    "Check HEARTBEAT.md items. If nothing needs attention, respond with NO_REPLY",
    "Go through HEARTBEAT.md. If any line asks you to report something now, "
    "or anything on it needs attention, write what the user should hear. "
    "Only if there is nothing to say, respond with exactly NO_REPLY",
})


def _migrate_heartbeat_job(workspace: Path) -> None:
    """A pre-0.2.0 heartbeat job ran a tool-less gate hourly; a wake is an
    agent turn, and a quiet tick is free, so the job runs every five
    minutes. Rewritten in place when it still has the old shape."""
    from faffmonkey.runtime.scheduler import HEARTBEAT_PROMPT

    jobs_path = workspace / "config" / "jobs.json"
    try:
        jobs = json.loads(jobs_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(jobs, list):
        return
    changed: list[str] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("context") != "heartbeat":
            continue
        if job.get("session") == "agent":
            continue
        job["session"] = "agent"
        if job.get("schedule") == "0 * * * *":
            job["schedule"] = "*/5 * * * *"
        if str(job.get("prompt") or "").strip().rstrip(".") in _OLD_HEARTBEAT_PROMPTS:
            job["prompt"] = HEARTBEAT_PROMPT
        changed.append(str(job.get("id")))
    if not changed:
        print("  heartbeat job: up to date")
        return
    jobs_path.write_text(json.dumps(jobs, indent=2) + "\n")
    print(f"  heartbeat job: rewrote {', '.join(changed)} as an agent wake every five minutes")


def _migrate_heartbeat_file(workspace: Path) -> None:
    """HEARTBEAT.md was a checklist a gate evaluated; it is now standing
    instructions read on a wake. The unmodified old template is replaced;
    a file with the user's own lines is left, with a note."""
    path = workspace / "HEARTBEAT.md"
    if path.is_symlink():
        return
    try:
        text = path.read_text()
    except OSError:
        return
    if "Checks evaluated on every heartbeat tick" not in text:
        return
    if "No checks are configured yet: respond with exactly NO_REPLY." in text:
        template_dir = _find_template_dir()
        src = template_dir / "workspace" / "HEARTBEAT.md" if template_dir else None
        if src is None or not src.is_file():
            print("  HEARTBEAT.md: old checklist template; no template found to replace it")
            return
        path.write_text(src.read_text())
        print("  HEARTBEAT.md: replaced the checklist template with standing instructions")
        return
    print(
        "  HEARTBEAT.md: still worded as a checklist. It is now standing "
        "instructions read when the heartbeat wakes; anything that needs "
        "checking belongs in a sensor job (see the heartbeat skill's HUMAN.md)"
    )


def _builtin_origin_entry(name: str, source_hash: str) -> dict:
    return {
        "source": f"templates/workspace/skills/{name}",
        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_hash": source_hash,
    }


def _sync_builtin_skills(template_skills: Path, workspace: Path, root: Path) -> int:
    from faffmonkey.cli.skill import _dir_hash, _load_origin

    skills_dir = workspace / "skills"
    _check_no_symlink_components(skills_dir, root)
    skills_dir.mkdir(exist_ok=True)
    origin_path = skills_dir / ".origin.json"
    origin = _load_origin(origin_path)
    origin_changed = False
    count = 0

    for skill_src in sorted(template_skills.iterdir()):
        if not skill_src.is_dir():
            continue
        name = skill_src.name
        skill_dst = skills_dir / name
        _check_no_symlink_components(skill_dst, root)
        template_hash = _dir_hash(skill_src)

        if not skill_dst.exists():
            shutil.copytree(
                skill_src, skill_dst,
                ignore=shutil.ignore_patterns("__pycache__", ".*"),
            )
            origin[name] = _builtin_origin_entry(name, template_hash)
            origin_changed = True
            print(f"  installed skill: {name}")
            count += 1
            continue

        entry = origin.get(name) or {}
        if entry.get("source", "").startswith("contrib/"):
            continue

        recorded_hash = entry.get("source_hash", "")
        deployed_hash = _dir_hash(skill_dst)

        if not recorded_hash:
            if deployed_hash == template_hash:
                origin[name] = _builtin_origin_entry(name, template_hash)
                origin_changed = True
            else:
                print(
                    f"  !! skill {name}: no provenance and differs from the "
                    f"current template (customised or stale; delete "
                    f"workspace/skills/{name} to re-sync)"
                )
            continue

        if deployed_hash == recorded_hash:
            if template_hash != recorded_hash:
                from faffmonkey.cli.skill import _replace_tree
                _replace_tree(skill_src, skill_dst)
                origin[name] = _builtin_origin_entry(name, template_hash)
                origin_changed = True
                print(f"  updated skill: {name}")
                count += 1
        elif template_hash != recorded_hash:
            print(
                f"  !! skill {name}: modified locally and the template has "
                f"changed (delete workspace/skills/{name} to re-sync)"
            )

    if origin_changed:
        origin_path.write_text(json.dumps(origin, indent=2) + "\n")
    return count


_EXT_PREFIXES = ("channel_", "search_provider_", "transcriber_", "synthesiser_")


def _ext_short_name(filename: str) -> str:
    stem = filename[:-3] if filename.endswith(".py") else filename
    for prefix in _EXT_PREFIXES:
        if stem.startswith(prefix):
            return stem[len(prefix):]
    return stem


def _contrib_root(base: Path) -> Path:
    """Where contrib/ lives: the checkout, not the data root.

    Origin entries record sources relative to the project root. Resolving
    them against base only worked while the two were the same directory;
    after the data-root split every contrib install read as unverifiable.
    """
    try:
        return _find_project_root()
    except RuntimeError:
        return base


def classify_origin(base: Path, origin: dict) -> dict[str, list]:
    """Classify .origin.json entries against deployed and contrib files.

    Returns lists keyed bad_source [(filename, reason)], unverifiable
    [filename], tampered [filename], stale [(filename, short_name)].
    An entry can be both tampered and stale.
    """
    extensions_dir = base / "extensions"
    project_root = _contrib_root(base)
    contrib_base = (project_root / "contrib").resolve()
    result: dict[str, list] = {
        "bad_source": [], "unverifiable": [], "tampered": [], "stale": [],
    }
    for filename, info in origin.items():
        source = info.get("source", "")
        install_hash = info.get("contrib_source_hash", "")

        if source:
            if Path(source).is_absolute():
                # Written by an older wizard. It is not evidence of
                # tampering, just a value this install cannot verify.
                result["unverifiable"].append(
                    (filename, "absolute source path (reinstall to enable verification)"),
                )
                continue
            contrib_path = (project_root / source).resolve()
            if not str(contrib_path).startswith(str(contrib_base) + "/") and contrib_path != contrib_base:
                result["bad_source"].append(
                    (filename, "source path escapes contrib/ directory"),
                )
                continue
            if not contrib_path.is_file():
                result["bad_source"].append(
                    (filename, f"source file does not exist: {source}"),
                )
                continue
        else:
            contrib_path = None

        ext_path = extensions_dir / filename
        if not ext_path.exists():
            continue

        if not contrib_path or not install_hash:
            result["unverifiable"].append(filename)
            continue

        deployed_hash = hashlib.sha256(ext_path.read_bytes()).hexdigest()[:12]
        if deployed_hash != install_hash:
            result["tampered"].append(filename)

        if contrib_path.exists():
            current_contrib_hash = hashlib.sha256(contrib_path.read_bytes()).hexdigest()[:12]
            if current_contrib_hash != install_hash:
                short = _ext_short_name(filename)
                if sum(1 for f in origin if _ext_short_name(f) == short) > 1:
                    short = filename
                result["stale"].append((filename, short))
    return result


def _check_contrib_staleness(base: Path) -> None:
    origin_path = base / "extensions" / ".origin.json"
    if not origin_path.exists():
        return

    try:
        origin = json.loads(origin_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    classified = classify_origin(base, origin)
    for filename, reason in classified["bad_source"]:
        print(f"  !! {filename}: {reason}")
    for filename in classified["unverifiable"]:
        print(f"  !! {filename}: unverifiable (reinstall to enable verification)")
    for filename in classified["tampered"]:
        print(f"  !! {filename}: deployed file modified since install")
    for filename, short in classified["stale"]:
        print(f"  stale: {filename}")
        print(f"    Run on the host: ./bin/faff update-extension {short}")


def _check_skill_staleness(base: Path, workspace: Path) -> None:
    from faffmonkey.cli.skill import _dir_hash

    origin_path = workspace / "skills" / ".origin.json"
    if not origin_path.exists():
        return

    try:
        origin = json.loads(origin_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    project_root = _contrib_root(base)
    contrib_skills = (project_root / "contrib" / "skills").resolve()
    for name, info in origin.items():
        source = info.get("source", "")
        if source.startswith("templates/"):
            continue
        install_hash = info.get("contrib_source_hash", "")

        installed_path = workspace / "skills" / name
        if not installed_path.is_dir():
            continue

        contrib_path = (project_root / source).resolve() if source else None
        if contrib_path is not None and not (
            str(contrib_path).startswith(str(contrib_skills) + "/")
        ):
            print(f"  !! skill {name}: source path escapes contrib/skills/")
            continue

        if not contrib_path or not contrib_path.is_dir() or not install_hash:
            print(f"  !! skill {name}: unverifiable (reinstall to enable verification)")
            continue

        deployed_hash = _dir_hash(installed_path)
        if deployed_hash != install_hash:
            print(f"  !! skill {name}: modified since install")

        current_hash = _dir_hash(contrib_path)
        if current_hash != install_hash:
            print(f"  stale: skill {name}")
            print(f"    Run: faff skill install {name}")


def _migrate_data_root(legacy: Path, root: Path) -> bool:
    """Move workspace/, state/, extensions/ and requirements.extra.txt out
    of the checkout into the data root. One-time, host-side.

    Data inside the checkout is deleted by whatever replaces the checkout
    on deploy, backups included. Operator snapshots move to root/backups on
    the way; compaction checkpoints stay in state/backups, which the
    container mounts.
    """
    print(f"Data found in {legacy}, and {root} has none.")
    print("faffmonkey now keeps its data outside the checkout.")
    print("Stop the container first (docker compose down): moving mounted")
    print("directories under a running container breaks them.")
    try:
        answer = input(f"Move the data to {root} now? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted. Nothing was moved.")
        return False
    if answer.strip().lower() not in ("y", "yes"):
        print("Aborted. Nothing was moved.")
        return False

    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for name in ("workspace", "state", "extensions"):
        src = legacy / name
        if src.is_symlink():
            print(f"  skipped (symlink): {name}")
            continue
        if src.is_dir():
            shutil.move(str(src), str(root / name))
            print(f"  moved: {name}/")
    req = legacy / "requirements.extra.txt"
    if req.is_file() and not req.is_symlink():
        shutil.move(str(req), str(root / "requirements.extra.txt"))
        # The Docker build still reads it from the checkout, so leave a
        # gitignored mirror behind.
        shutil.copy2(root / "requirements.extra.txt", req)
        print("  moved: requirements.extra.txt (build mirror left in checkout)")

    old_backups = root / "state" / "backups"
    new_backups = root / "backups"
    if old_backups.is_dir():
        new_backups.mkdir(parents=True, exist_ok=True)
        os.chmod(new_backups, 0o700)
        for snap in sorted(old_backups.glob("*.tar.gz")):
            shutil.move(str(snap), str(new_backups / snap.name))
            print(f"  moved snapshot: {snap.name}")

    print(f"  data root: {root}")
    print("  restart with the new mounts: docker compose up -d")
    return True


def _sync_build_inputs(root: Path, project_root: Path) -> None:
    """Mirror requirements.extra.txt into the build context.

    The source of truth lives in the data root; docker compose build can
    only COPY from the checkout, so the checkout carries a disposable,
    gitignored mirror. This refreshes it, e.g. after a re-clone.
    """
    if root.resolve() == project_root.resolve():
        return
    src = root / "requirements.extra.txt"
    if not src.is_file():
        return
    dst = project_root / "requirements.extra.txt"
    try:
        if not dst.is_file() or dst.read_text() != src.read_text():
            shutil.copy2(src, dst)
            print(f"  refreshed: {dst}")
        else:
            print("  build inputs: up to date")
    except OSError as e:
        print(f"  cannot refresh {dst}: {e} (run ./bin/faff update on the host)")


def run_update(base: Path) -> int:
    state_dir = base / "state"
    workspace_dir = base / "workspace"
    backups_dir = base / "backups"

    try:
        legacy = _find_project_root()
    except RuntimeError:
        legacy = base
    if (not state_dir.is_dir() and legacy.resolve() != base.resolve()
            and (legacy / "state").is_dir()):
        if not _migrate_data_root(legacy, base):
            return 1
        print()

    if not state_dir.is_dir():
        print('No state/ directory. Run "faff init" first.')
        return 1

    print("faff update\n")

    print("Snapshot:")
    _snapshot(base, backups_dir)

    print("\nDatabase:")
    _run_migrations(state_dir)

    print("\nTemplates:")
    if workspace_dir.is_dir():
        try:
            _sync_templates(workspace_dir)
        except RuntimeError as e:
            # A symlinked workspace/ is a deliberate layout, not a crash.
            # Report the skipped step and carry on with the rest.
            print(f"  skipped: {e}")
    else:
        print("  no workspace/ directory")

    if workspace_dir.is_dir():
        print("\nHeartbeat:")
        _migrate_heartbeat_job(workspace_dir)
        _migrate_heartbeat_file(workspace_dir)

    print("\nContrib:")
    _check_contrib_staleness(base)
    if workspace_dir.is_dir():
        _check_skill_staleness(base, workspace_dir)

    print("\nBuild inputs:")
    _sync_build_inputs(base, legacy)

    print("\nDone.")
    return 0


_MAX_EXTENSION_BACKUPS = 20


def _next_backup_path(ext_path: Path) -> Path | None:
    """First free `<name>.bak`, `<name>.bak2`, ... or None if all taken."""
    first = ext_path.with_suffix(ext_path.suffix + ".bak")
    if not first.exists() and not first.is_symlink():
        return first
    for n in range(2, _MAX_EXTENSION_BACKUPS + 1):
        candidate = ext_path.with_suffix(f"{ext_path.suffix}.bak{n}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    return None


def run_update_extension(base: Path, name: str) -> int:
    extensions_dir = base / "extensions"
    origin_path = extensions_dir / ".origin.json"

    if not extensions_dir.is_dir():
        print("No extensions/ directory.")
        return 1
    # Inside the container this died halfway with a raw OSError on the .bak.
    from faffmonkey.cli.init import ensure_extensions_writable
    ensure_extensions_writable(
        extensions_dir, f"update-extension {name}", "restart: docker compose restart",
    )

    # map short name to filename in origin.json
    try:
        origin = json.loads(origin_path.read_text()) if origin_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        origin = {}

    # find the matching entry
    matches = sorted(
        filename for filename in origin
        if filename == name or _ext_short_name(filename) == name
    )
    if len(matches) > 1:
        print(f"Ambiguous name {name!r}: matches {', '.join(matches)}.")
        print("Use the full filename.")
        return 1
    target_file = matches[0] if matches else None

    if target_file is None:
        print("This extension didn't come from contrib. Nothing to update.")
        return 1

    info = origin[target_file]
    source = info.get("source", "")

    if not source:
        print(
            f"No contrib source recorded for {name}; "
            f"reinstall it with: faff setup {name}"
        )
        return 1

    try:
        project_root = _find_project_root()
    except RuntimeError:
        project_root = base
    contrib_path = (project_root / source).resolve()
    contrib_base = (project_root / "contrib").resolve()
    if not str(contrib_path).startswith(str(contrib_base) + "/") and contrib_path != contrib_base:
        print(f"Rejected: source path escapes contrib/ directory: {source}")
        return 1

    if not contrib_path.exists():
        print(f"Contrib source not found: {source}")
        return 1

    ext_path = extensions_dir / target_file
    if ext_path.is_symlink():
        print(f"  skipped (symlink): {target_file}")
        return 1
    if ext_path.exists() and contrib_path.read_bytes() == ext_path.read_bytes():
        print("Already up to date.")
        return 0

    # backup current
    if ext_path.exists():
        # Versioned. A single fixed .bak name meant the first update
        # succeeded and every later one refused, permanently, without
        # naming the remedy, while doctor kept advising the command.
        bak_path = _next_backup_path(ext_path)
        if bak_path is None:
            print(
                f"  too many backups of {target_file}; "
                f"remove some {ext_path.name}.bak* files and retry"
            )
            return 1
        if bak_path.is_symlink():
            print(f"  skipped (symlink): {bak_path.name}")
            return 1
        shutil.copy2(ext_path, bak_path)
        print(f"  backed up: {target_file} -> {bak_path.name}")

    # Copy new version alongside and move it into place, so a kill mid-copy
    # leaves either the old extension or a stray .new file, never a truncated
    # module that faff run fails to import while reporting only a warning.
    staging = ext_path.with_name(ext_path.name + ".new")
    try:
        shutil.copy2(contrib_path, staging)
        os.replace(staging, ext_path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    print(f"  updated: {target_file}")

    source_hash = hashlib.sha256(contrib_path.read_bytes()).hexdigest()[:12]
    origin[target_file] = {
        "source": source,
        "copied_at": datetime.now(timezone.utc).isoformat(),
        "contrib_source_hash": source_hash,
    }
    if origin_path.is_symlink():
        raise RuntimeError(f"refusing to write: {origin_path} is a symlink")
    origin_path.write_text(json.dumps(origin, indent=2) + "\n")
    print(f"  updated: .origin.json")

    return 0
