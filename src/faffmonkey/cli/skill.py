import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from faffmonkey.cli.init import _check_no_symlink_components, _find_project_root
from faffmonkey.runtime.skills import parse_frontmatter, scan_skills, unmet_requirements

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _dir_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = f.relative_to(path)
        if "__pycache__" in rel.parts or any(part.startswith(".") for part in rel.parts):
            continue
        h.update(rel.as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _has_symlinks(path: Path) -> bool:
    return path.is_symlink() or any(p.is_symlink() for p in path.rglob("*"))


def _load_origin(origin_path: Path) -> dict:
    if not origin_path.exists():
        return {}
    try:
        origin = json.loads(origin_path.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  Warning: {origin_path} is unreadable, treating as empty.")
        return {}
    return origin if isinstance(origin, dict) else {}


def _contrib_skills_dir() -> Path:
    return _find_project_root() / "contrib" / "skills"


def _replace_tree(src: Path, dest: Path) -> None:
    """Install src at dest atomically enough to be resumable.

    The new tree is built alongside and moved into place, so an
    interrupted copy leaves either the old tree or a staging directory,
    never a partial install wearing the old provenance hash.
    """
    staging = dest.with_name(dest.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging, ignore=shutil.ignore_patterns("__pycache__", ".*"))
    if dest.exists():
        previous = dest.with_name(dest.name + ".old")
        if previous.exists():
            shutil.rmtree(previous)
        os.replace(dest, previous)
        os.replace(staging, dest)
        shutil.rmtree(previous)
    else:
        os.replace(staging, dest)


def run_skill_install(workspace_dir: Path, name: str, force: bool = False) -> int:
    if not _SKILL_NAME_RE.match(name):
        print(f"Invalid skill name: {name!r}")
        return 1

    src = _contrib_skills_dir() / name
    if not src.is_dir() or not (src / "SKILL.md").is_file():
        print(f"Skill not found in contrib/skills/: {name}")
        contrib_dir = _contrib_skills_dir()
        available = sorted(
            p.parent.name for p in contrib_dir.glob("*/SKILL.md")
        ) if contrib_dir.is_dir() else []
        if available:
            print(f"Available: {', '.join(available)}")
        return 1

    if _has_symlinks(src):
        print(f"Refusing to install: {src} contains symlinks.")
        return 1

    skills_dir = workspace_dir / "skills"
    origin_path = skills_dir / ".origin.json"
    dest = skills_dir / name
    origin = _load_origin(origin_path)

    # Before anything destructive: dest.exists() follows a symlinked
    # skills/, and the rmtree below must never reach outside the workspace.
    _check_no_symlink_components(dest, workspace_dir)

    if dest.exists():
        entry = origin.get(name)
        if entry is None:
            print(
                f"{dest} already exists and did not come from contrib. "
                f"Rename or remove it to resolve the collision."
            )
            return 1
        # Built-in installs record source_hash, contrib installs record
        # contrib_source_hash. Reading only one meant a skill that had
        # moved into contrib/ skipped the modified-after-install check and
        # went straight to the rmtree.
        recorded_hash = entry.get("contrib_source_hash") or entry.get("source_hash", "")
        deployed_hash = _dir_hash(dest)
        if recorded_hash and deployed_hash != recorded_hash and not force:
            print(
                f"{name} was modified after install (local changes would be lost). "
                f"Re-run with --force to overwrite."
            )
            return 1
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Copy beside the destination and swap. rmtree-then-copytree left a
    # half-copied tree on any interruption, and the next attempt reported
    # it as a local modification that never happened.
    _replace_tree(src, dest)
    print(f"  Installed contrib/skills/{name} -> {dest}")

    origin[name] = {
        "source": f"contrib/skills/{name}",
        "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contrib_source_hash": _dir_hash(src),
    }
    origin_path.write_text(json.dumps(origin, indent=2) + "\n")

    _print_setup_checklist(dest, workspace_dir)
    print(f"\nSkill {name} is active (skills are live on creation).")
    return 0


def _env_satisfied(var: str, state_env: Path) -> bool:
    """Present in the process environment, or declared in state/.env.

    Installs run on the host too, where state/.env is not loaded into the
    environment; reporting a key as missing when the operator has already
    written it would send them chasing a problem that does not exist.
    """
    if os.environ.get(var):
        return True
    try:
        for line in state_env.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{var}=") and line.split("=", 1)[1].strip():
                return True
    except OSError:
        pass
    return False


def _print_setup_checklist(dest: Path, workspace_dir: Path) -> None:
    """What this install still needs before the skill works: missing API
    keys and command wiring.
    """
    skill_md = dest / "SKILL.md"
    missing: list[str] = []
    if skill_md.is_file():
        try:
            frontmatter = parse_frontmatter(skill_md.read_text())
        except OSError:
            frontmatter = {}
        state_env = workspace_dir.parent / "state" / ".env"
        for item in unmet_requirements(frontmatter):
            if item.startswith("env "):
                var = item[4:]
                if not _env_satisfied(var, state_env):
                    missing.append(f"{var} in state/.env")
            else:
                missing.append(item)

    if missing:
        print("\n  Before this skill works, this install still needs:")
        for entry in missing:
            print(f"    - {entry}")
    else:
        print("\n  All declared requirements are satisfied.")

    human_md = dest / "HUMAN.md"
    if human_md.is_file():
        try:
            human_text = human_md.read_text()
        except OSError:
            human_text = ""
        if "commands.json" in human_text:
            print(
                "  Optional: can back command seams (IMAGE_GEN_CMD and "
                "friends); see HUMAN.md for state/commands.json wiring."
            )
        print(f"  Setup notes: {human_md}")


def run_skill_list(workspace_dir: Path) -> int:
    installed = scan_skills(workspace_dir)
    installed_names = {name for name, _desc in installed}

    print("Installed (workspace/skills/):")
    if installed:
        for name, desc in installed:
            print(f"  {name:20s} {desc}")
    else:
        print("  none")

    contrib_dir = _contrib_skills_dir()
    print("\nAvailable (contrib/skills/):")
    entries = sorted(contrib_dir.glob("*/SKILL.md")) if contrib_dir.is_dir() else []
    if not entries:
        print("  none")
        return 0
    for skill_md in entries:
        name = skill_md.parent.name
        fm = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        desc = fm.get("description", "")
        marker = " (installed)" if name in installed_names else ""
        print(f"  {name:20s}{marker} {desc}")
    print('\nInstall with: faff skill install <name>')
    return 0
