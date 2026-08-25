#!/usr/bin/env python3
"""Quick validation for skill directories."""

import os
import re
import sys
from pathlib import Path
from typing import Optional

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

MAX_SKILL_NAME_LENGTH = 64


def _extract_frontmatter(content: str) -> Optional[str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None


def _parse_simple_frontmatter(frontmatter_text: str) -> Optional[dict[str, str]]:
    """Minimal fallback parser when PyYAML is unavailable."""
    parsed: dict[str, str] = {}
    current_key: Optional[str] = None
    for raw_line in frontmatter_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        is_indented = raw_line[:1].isspace()
        if is_indented:
            if current_key is None:
                return None
            current_value = parsed[current_key]
            parsed[current_key] = (
                f"{current_value}\n{stripped}" if current_value else stripped
            )
            continue
        if ":" not in stripped:
            return None
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            return None
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        parsed[key] = value
        current_key = key
    return parsed


def validate_skill(skill_path: str | Path) -> tuple[bool, str]:
    skill_path = Path(skill_path)

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"Could not read SKILL.md: {e}"

    frontmatter_text = _extract_frontmatter(content)
    if frontmatter_text is None:
        return False, "Invalid frontmatter format"

    if yaml is not None:
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
            if not isinstance(frontmatter, dict):
                return False, "Frontmatter must be a YAML dictionary"
        except yaml.YAMLError as e:
            return False, f"Invalid YAML in frontmatter: {e}"
    else:
        frontmatter = _parse_simple_frontmatter(frontmatter_text)
        if frontmatter is None:
            return False, "Invalid frontmatter: unsupported syntax without PyYAML"

    # timeout is a documented frontmatter field the runtime reads, so a
    # skill that legitimately sets it was reported as having an unexpected
    # key by the project's own validator.
    allowed_properties = {"name", "description", "actions", "metadata", "timeout"}

    unexpected_keys = set(frontmatter.keys()) - allowed_properties
    if unexpected_keys:
        allowed = ", ".join(sorted(allowed_properties))
        unexpected = ", ".join(sorted(unexpected_keys))
        return (
            False,
            f"Unexpected key(s) in frontmatter: {unexpected}. "
            f"Allowed: {allowed}",
        )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if name:
        if not re.match(r"^[a-z0-9-]+$", name):
            return (
                False,
                f"Name '{name}' must be kebab-case "
                "(lowercase letters, digits, hyphens)",
            )
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return (
                False,
                f"Name '{name}' cannot start/end with hyphen "
                "or contain consecutive hyphens",
            )
        if len(name) > MAX_SKILL_NAME_LENGTH:
            return (
                False,
                f"Name too long ({len(name)} chars, max {MAX_SKILL_NAME_LENGTH})",
            )

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return (
            False,
            f"Description must be a string, got {type(description).__name__}",
        )
    description = description.strip()

    warnings: list[str] = []

    if description:
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets"
        if len(description) > 1024:
            return (
                False,
                f"Description too long ({len(description)} chars, max 1024)",
            )
        desc_lower = description.lower()
        if "use when" not in desc_lower and "use for" not in desc_lower:
            warnings.append(
                "Description should include trigger language "
                "('Use when...' or 'Use for...')"
            )

    if warnings:
        return True, f"Valid (with warnings): {'; '.join(warnings)}"

    return True, "Valid"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: quick_validate.py <skill-directory>")
        sys.exit(1)

    arg = sys.argv[1]
    # A bare name is a skill name and lives under WORKSPACE/skills/. Anything
    # containing a separator is a path and is used as written.
    #
    # Scripts run with the working directory set to workspace/, so a bare
    # name previously resolved to workspace/<name> while skills actually
    # live in workspace/skills/<name>. The flow this skill documents,
    # init_skill then quick_validate with the same bare name, therefore
    # failed with "SKILL.md not found" every single time.
    if "/" in arg or os.sep in arg:
        target = Path(arg)
    else:
        workspace = os.environ.get("WORKSPACE", "")
        if not workspace:
            print("WORKSPACE is not set; pass a path rather than a skill name")
            sys.exit(1)
        target = Path(workspace) / "skills" / arg

    valid, message = validate_skill(target)
    print(message)
    sys.exit(0 if valid else 1)
