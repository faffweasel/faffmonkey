#!/usr/bin/env python3
"""
Skill initializer: creates a new skill directory from template.

Usage:
    init_skill.py <skill-name> [--path <path>] [--resources scripts,references,assets] [--examples]
"""

import argparse
import os
import re
import sys
from pathlib import Path

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_RESOURCES = {"scripts", "references", "assets"}

SKILL_TEMPLATE = """\
---
name: {skill_name}
# description checklist: front-load capability keywords, include "Use when...",
# trigger-based not implementation-based, negative boundaries, under 1024 chars
description: "TODO: describe what this skill does and when to use it"
actions:
---

[DELETE this block after choosing a structure for the sections below.

Structural patterns (pick the one that fits, then fill in the five sections):

1. Workflow-based (sequential processes): e.g. init -> configure -> validate -> deploy
2. Task-based (independent operations): e.g. list, add, remove, search
3. Reference/guidelines (standards, specs): e.g. brand colours, API schema, coding rules
4. Capabilities-based (interrelated features): e.g. auth + sessions + tokens

Most faffmonkey skills are task-based (one "What it does" entry per action).
Patterns can mix. The five sections below are required.]

## When to use

[TODO: When should the agent invoke this skill? Be specific about triggers:
user phrases, file types, task patterns. Include contrast with other approaches
("Use X for ..., use Y for ..."). Add cross-skill disambiguation if triggers
overlap with another skill. This is the most important section.]

## What it does

[TODO: Concrete description of each action's behaviour. For task-based skills,
one paragraph per action. For workflow skills, describe the pipeline.]

## Arguments and flags

[TODO: For each action, document positional args and flags with types and
defaults. Use a table for complex actions.]

## Examples

[TODO: Show skill_invoke calls with realistic arguments and expected output.
At least one example per action. The agent pattern-matches on these.]

## Limitations

[TODO: What this skill cannot do. Known edge cases. What to fall back to.
Be honest: vague limitations are worse than none.]
"""

HUMAN_TEMPLATE = """\
# {skill_name}: setup and notes

[TODO: one-paragraph summary of what this skill does for the human.
This file is never loaded into the agent's context. Setup steps, API
keys, configuration, architecture notes, and maintenance all belong
here, not in SKILL.md.]

## Setup

[TODO: required env vars in state/.env, config files, external accounts.
Delete this section if no setup is needed.]

## How it works

[TODO: where data lives (skills-data/{skill_name}/), what each script
does, external services called.]

## Maintenance

[TODO: what can be safely deleted or rebuilt, known limits.]
"""

EXAMPLE_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"Example script for {skill_name}. Replace or delete.\"\"\"

import os
import sys


def main() -> None:
    workspace = os.environ.get("WORKSPACE", "")
    skill_data = os.environ.get("SKILL_DATA", "")
    print(f"workspace: {{workspace}}")
    print(f"skill_data: {{skill_data}}")
    print(f"args: {{sys.argv[1:]}}")


if __name__ == "__main__":
    main()
"""

EXAMPLE_REFERENCE = """\
# Reference: {skill_title}

[TODO: domain-specific documentation the agent loads on demand via file_read.
Keep only in references/. Do not duplicate in SKILL.md.]
"""

EXAMPLE_ASSET = """\
# Asset placeholder for {skill_title}

[TODO: replace with actual asset files (templates, data, images).
Assets are used in output, not loaded into context.]
"""


def normalize_skill_name(skill_name: str) -> str:
    normalized = skill_name.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized


def title_case_skill_name(skill_name: str) -> str:
    return " ".join(word.capitalize() for word in skill_name.split("-"))


def parse_resources(raw_resources: str) -> list[str]:
    if not raw_resources:
        return []
    resources = [item.strip() for item in raw_resources.split(",") if item.strip()]
    invalid = sorted({item for item in resources if item not in ALLOWED_RESOURCES})
    if invalid:
        allowed = ", ".join(sorted(ALLOWED_RESOURCES))
        print(f"error: unknown resource type(s): {', '.join(invalid)}", file=sys.stderr)
        print(f"  allowed: {allowed}", file=sys.stderr)
        sys.exit(1)
    deduped: list[str] = []
    seen: set[str] = set()
    for resource in resources:
        if resource not in seen:
            deduped.append(resource)
            seen.add(resource)
    return deduped


def create_resource_dirs(
    skill_dir: Path,
    skill_name: str,
    skill_title: str,
    resources: list[str],
    include_examples: bool,
) -> None:
    for resource in resources:
        resource_dir = skill_dir / resource
        resource_dir.mkdir(exist_ok=True)
        if not include_examples:
            continue
        if resource == "scripts":
            example = resource_dir / "example.py"
            example.write_text(EXAMPLE_SCRIPT.format(skill_name=skill_name))
            example.chmod(0o755)
        elif resource == "references":
            (resource_dir / "reference.md").write_text(
                EXAMPLE_REFERENCE.format(skill_title=skill_title)
            )
        elif resource == "assets":
            (resource_dir / "placeholder.txt").write_text(
                EXAMPLE_ASSET.format(skill_title=skill_title)
            )


def init_skill(
    skill_name: str,
    path: str,
    resources: list[str],
    include_examples: bool,
    workspace: str,
) -> Path | None:
    skill_dir = Path(path).resolve() / skill_name

    if skill_dir.exists():
        print(f"error: skill directory already exists: {skill_dir}", file=sys.stderr)
        return None

    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        print(f"error: cannot create directory: {e}", file=sys.stderr)
        return None

    skill_title = title_case_skill_name(skill_name)
    content = SKILL_TEMPLATE.format(skill_name=skill_name, skill_title=skill_title)
    (skill_dir / "SKILL.md").write_text(content)
    (skill_dir / "HUMAN.md").write_text(HUMAN_TEMPLATE.format(skill_name=skill_name))

    if resources:
        create_resource_dirs(
            skill_dir, skill_name, skill_title, resources, include_examples
        )

    if workspace:
        data_dir = Path(workspace) / "skills-data" / skill_name
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"Created data directory at {data_dir}")

    print(f"Created skill '{skill_name}' at {skill_dir}")
    print("Review the generated SKILL.md (all five sections) and HUMAN.md.")
    return skill_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a new skill directory with template SKILL.md.",
    )
    parser.add_argument("skill_name", help="Skill name (normalized to kebab-case)")
    parser.add_argument(
        "--path",
        default=None,
        help="Output directory (default: $WORKSPACE/skills/)",
    )
    parser.add_argument(
        "--resources",
        default="",
        help="Comma-separated list: scripts,references,assets",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="Create example files in resource directories",
    )
    args = parser.parse_args()

    workspace = os.environ.get("WORKSPACE", "")

    raw_skill_name = args.skill_name
    skill_name = normalize_skill_name(raw_skill_name)
    if not skill_name:
        print(
            "error: skill name must include at least one letter or digit",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(skill_name) > MAX_SKILL_NAME_LENGTH:
        print(
            f"error: name '{skill_name}' too long "
            f"({len(skill_name)} chars, max {MAX_SKILL_NAME_LENGTH})",
            file=sys.stderr,
        )
        sys.exit(1)
    if skill_name != raw_skill_name:
        print(f"Normalized name: '{raw_skill_name}' -> '{skill_name}'")

    resources = parse_resources(args.resources)
    if args.examples and not resources:
        print("error: --examples requires --resources", file=sys.stderr)
        sys.exit(1)

    if args.path:
        path = args.path
    elif workspace:
        path = str(Path(workspace) / "skills")
    else:
        print(
            "error: no --path specified and WORKSPACE not set", file=sys.stderr
        )
        sys.exit(1)

    result = init_skill(skill_name, path, resources, args.examples, workspace)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
