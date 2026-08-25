#!/usr/bin/env python3
"""Package a skill directory into a distributable .skill file."""

import os
import sys
import zipfile
from pathlib import Path

from quick_validate import validate_skill

EXCLUDED_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules"}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def package_skill(
    skill_path: str | Path,
    output_dir: str | Path | None = None,
) -> Path | None:
    skill_path = Path(skill_path).resolve()

    if not skill_path.exists():
        print(f"error: skill folder not found: {skill_path}", file=sys.stderr)
        return None
    if not skill_path.is_dir():
        print(f"error: not a directory: {skill_path}", file=sys.stderr)
        return None

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"error: SKILL.md not found in {skill_path}", file=sys.stderr)
        return None

    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"error: validation failed: {message}", file=sys.stderr)
        return None
    print(f"Validated: {message}")

    skill_name = skill_path.name
    if output_dir:
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = Path.cwd()

    skill_filename = out / f"{skill_name}.skill"

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in skill_path.rglob("*"):
                if file_path.is_symlink():
                    print(f"Skipping symlink: {file_path}")
                    continue

                rel_parts = file_path.relative_to(skill_path).parts
                if any(part in EXCLUDED_DIRS for part in rel_parts):
                    continue

                if file_path.is_file():
                    resolved = file_path.resolve()
                    if not _is_within(resolved, skill_path):
                        print(
                            f"error: file escapes skill root: {file_path}",
                            file=sys.stderr,
                        )
                        return None
                    if resolved == skill_filename.resolve():
                        continue
                    arcname = Path(skill_name) / file_path.relative_to(skill_path)
                    zipf.write(file_path, arcname)

        print(f"Packaged to: {skill_filename}")
        return skill_filename

    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: package_skill.py <skill-name-or-path> [output-directory]")
        sys.exit(1)

    skill_arg = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    workspace = os.environ.get("WORKSPACE", "")
    skill_path = Path(skill_arg)
    if not skill_path.is_absolute() and workspace:
        candidate = Path(workspace) / "skills" / skill_arg
        if candidate.is_dir():
            skill_path = candidate

    result = package_skill(str(skill_path), output_dir)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
