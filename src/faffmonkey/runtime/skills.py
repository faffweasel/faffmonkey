from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# The default is the ceiling and matches the loop's inactivity clock, so
# a skill only dies when the turn would have anyway. A tighter budget
# kills feed fetches and image edits that would have finished.
SKILL_TIMEOUT = 600
_MAX_SKILL_TIMEOUT = 600

_BANNED_CHARS = frozenset({"/", "\\", os.sep})

_COMMAND_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RESERVED_COMMAND_KEYS = frozenset({
    "WORKSPACE", "SKILL_DATA", "TZ", "PATH", "HOME",
    "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
})


def load_commands(state_dir: Path) -> dict[str, str]:
    path = state_dir / "commands.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("commands.json unreadable: %s", e)
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "commands.json must be a JSON object, got %s", type(raw).__name__,
        )
        return {}
    commands: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _COMMAND_KEY_RE.match(key):
            logger.warning("commands.json: invalid key %r, skipping", key)
            continue
        if key in _RESERVED_COMMAND_KEYS:
            logger.warning("commands.json: reserved key %r, skipping", key)
            continue
        if not isinstance(value, str) or not value.strip():
            logger.warning(
                "commands.json: value for %r must be a non-empty string, skipping",
                key,
            )
            continue
        commands[key] = value
    return commands


def _validate_name(name: str, parent: Path) -> Path | None:
    if not name or ".." in name or _BANNED_CHARS & set(name):
        return None
    try:
        resolved = (parent / name).resolve()
    except (ValueError, OSError):
        return None
    parent_resolved = parent.resolve()
    if not str(resolved).startswith(str(parent_resolved) + "/"):
        return None
    return resolved


def parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        m = re.match(r"^([\w][\w_-]*)\s*:\s*(.+)$", line)
        if m:
            result[m.group(1).strip()] = m.group(2).strip().strip("\"'")
    return result


def unmet_requirements(frontmatter: dict[str, str]) -> list[str]:
    """What a skill's `requires` block asks for and this host lacks, so a
    skill whose API key is not set stays out of the catalog.
    """
    raw = frontmatter.get("metadata", "")
    if not raw:
        return []
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("skill metadata is not valid JSON, ignoring: %r", raw[:80])
        return []
    if not isinstance(meta, dict):
        return []
    requires = (meta.get("faffmonkey") or {}).get("requires") or {}
    if not isinstance(requires, dict):
        return []

    missing: list[str] = []
    for var in requires.get("env") or []:
        if isinstance(var, str) and not os.environ.get(var):
            missing.append(f"env {var}")
    for binary in requires.get("bins") or []:
        if isinstance(binary, str) and shutil.which(binary) is None:
            missing.append(f"command {binary}")
    return missing


def scan_skills(workspace: Path) -> list[tuple[str, str]]:
    skills_dir = workspace / "skills"
    skills: list[tuple[str, str]] = []
    if not skills_dir.is_dir():
        return skills
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        # _replace_tree builds its staging and rollback trees as <name>.new
        # and <name>.old inside this directory, so an interrupted install
        # left a duplicate or half-copied skill in the agent's catalog,
        # listed under the same frontmatter name as the real one.
        if skill_dir.name.endswith((".new", ".old")):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        fm = parse_frontmatter(skill_md.read_text())
        name = fm.get("name", skill_dir.name)
        missing = unmet_requirements(fm)
        if missing:
            logger.info(
                "skill %s not offered: missing %s", name, ", ".join(missing),
            )
            continue
        desc = fm.get("description", "")
        skills.append((name, desc))
    return skills


def load_full(workspace: Path, skill_name: str) -> str | None:
    skills_dir = workspace / "skills"
    if _validate_name(skill_name, skills_dir) is None:
        return None
    skill_md = skills_dir / skill_name / "SKILL.md"
    if not skill_md.exists():
        return None
    return skill_md.read_text()


def parse_media_lines(output: str, workspace: Path) -> list[Path]:
    attachments: list[Path] = []
    workspace_resolved = workspace.resolve()
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("MEDIA:"):
            path_str = stripped[len("MEDIA:"):].strip()
            if path_str:
                path = Path(path_str)
                if not path.is_absolute():
                    path = workspace / path
                path = path.resolve()
                if not path.is_relative_to(workspace_resolved):
                    logger.warning("MEDIA path rejected (outside workspace): %s", path_str)
                    continue
                attachments.append(path)
    return attachments


def skill_timeout(skill_dir: Path) -> int:
    """The subprocess budget for a skill's scripts: its frontmatter
    `timeout` if declared, else the default, never above the ceiling."""
    skill_md = skill_dir / "SKILL.md"
    try:
        declared = parse_frontmatter(skill_md.read_text()).get("timeout", "")
    except OSError:
        declared = ""
    if not declared:
        return SKILL_TIMEOUT
    try:
        return min(int(declared), _MAX_SKILL_TIMEOUT)
    except ValueError:
        logger.warning(
            "skill %s: ignoring non-integer timeout %r", skill_dir.name, declared,
        )
        return SKILL_TIMEOUT


def invoke(
    workspace: Path,
    skill_name: str,
    action: str,
    args: list[str] | None = None,
    tz: str = "UTC",
    state_dir: Path | None = None,
) -> tuple[str, list[Path], bool]:
    skills_dir = workspace / "skills"
    if _validate_name(skill_name, skills_dir) is None:
        return f"invalid skill name: {skill_name}", [], True

    skill_dir = skills_dir / skill_name
    script_dir = skill_dir / "scripts"

    if _validate_name(action, script_dir) is None:
        return f"invalid action: {action}", [], True

    # A skill that lists its actions gets an allow-list. Without one,
    # every shared module in scripts/ was reachable: invoking one exits 0
    # with no output, which reads to the model as a successful action.
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        declared = parse_frontmatter(skill_md.read_text()).get("actions", "")
        allowed = {a.strip() for a in declared.split(",") if a.strip()}
        if allowed and action not in allowed:
            return (
                f"unknown action {action!r} for {skill_name}; "
                f"available: {', '.join(sorted(allowed))}"
            ), [], True

    script = script_dir / action
    if not script.exists():
        script = script_dir / f"{action}.py"
    if not script.exists():
        return f"script not found: {skill_name}/scripts/{action}", [], True

    timeout = skill_timeout(skill_dir)

    skill_data = workspace / "skills-data" / skill_name
    skill_data.mkdir(parents=True, exist_ok=True)

    if state_dir is None:
        state_dir = workspace.parent / "state"

    # commands.json entries override inherited env for non-reserved keys;
    # runtime-owned vars always win
    env = {
        **os.environ,
        **load_commands(state_dir),
        "WORKSPACE": str(workspace),
        "SKILL_DATA": str(skill_data),
        "TZ": tz,
    }

    cmd = ["python3", str(script)]
    if args:
        cmd.extend(args)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            return f"skill {skill_name}/{action} timed out ({timeout}s)", [], True
    except OSError as e:
        return f"skill {skill_name}/{action} error: {e}", [], True

    output = stdout
    if stderr:
        output += ("\n" if output else "") + stderr

    is_error = proc.returncode != 0
    if is_error:
        output += f"\n[exit code: {proc.returncode}]"

    attachments = parse_media_lines(stdout, workspace)

    return output or "(no output)", attachments, is_error
