import os
import subprocess
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "templates"
    / "workspace"
    / "skills"
    / "skill-writer"
    / "scripts"
)


def _run_init(workspace, args, extra_env=None):
    script = SCRIPTS_DIR / "init_skill.py"
    env = {
        "WORKSPACE": str(workspace),
        "SKILL_DATA": str(workspace / "skills-data" / "skill-writer"),
        "TZ": "UTC",
    }
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, str(script)] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _run_validate(skill_dir):
    script = SCRIPTS_DIR / "quick_validate.py"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, str(script), str(skill_dir)],
        capture_output=True,
        text=True,
        env=env,
    )


def _import_package_skill():
    fake_qv = types.ModuleType("quick_validate")
    fake_qv.validate_skill = lambda _path: (True, "Valid")
    original = sys.modules.get("quick_validate")
    sys.modules["quick_validate"] = fake_qv

    saved_path = sys.path[:]
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    sys.modules.pop("package_skill", None)

    import package_skill as pkg

    sys.path[:] = saved_path
    if original is not None:
        sys.modules["quick_validate"] = original
    else:
        sys.modules.pop("quick_validate", None)

    return pkg


# ---- init_skill.py ----


class TestInitSkillDirectoryStructure:
    def test_creates_skill_and_data_dirs(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        result = _run_init(workspace, ["test-skill"])
        assert result.returncode == 0
        assert "Created skill" in result.stdout
        assert (workspace / "skills" / "test-skill").is_dir()
        assert (workspace / "skills" / "test-skill" / "SKILL.md").exists()
        assert (workspace / "skills-data" / "test-skill").is_dir()

    def test_five_section_template(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        _run_init(workspace, ["my-skill"])
        content = (workspace / "skills" / "my-skill" / "SKILL.md").read_text()
        assert "name: my-skill" in content
        assert "## When to use" in content
        assert "## What it does" in content
        assert "## Arguments and flags" in content
        assert "## Examples" in content
        assert "## Limitations" in content

    def test_template_includes_structural_patterns(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        _run_init(workspace, ["pattern-skill"])
        content = (workspace / "skills" / "pattern-skill" / "SKILL.md").read_text()
        assert "Workflow-based" in content
        assert "Task-based" in content
        assert "DELETE" in content

    def test_template_includes_description_checklist(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        _run_init(workspace, ["checklist-skill"])
        content = (workspace / "skills" / "checklist-skill" / "SKILL.md").read_text()
        assert "description checklist" in content
        assert "Use when" in content

    def test_creates_human_md(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        _run_init(workspace, ["human-skill"])
        content = (workspace / "skills" / "human-skill" / "HUMAN.md").read_text()
        assert "human-skill: setup and notes" in content
        assert "never loaded into the agent's context" in content
        assert "skills-data/human-skill/" in content

    def test_tells_user_to_review(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        result = _run_init(workspace, ["review-skill"])
        assert "review" in result.stdout.lower()


class TestInitSkillNameNormalization:
    def test_normalizes_to_kebab_case(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        result = _run_init(workspace, ["My Cool Skill"])
        assert result.returncode == 0
        assert (workspace / "skills" / "my-cool-skill" / "SKILL.md").exists()
        assert "Normalized" in result.stdout

    def test_rejects_empty_name(self, tmp_path):
        result = _run_init(tmp_path, ["---"])
        assert result.returncode != 0

    def test_rejects_name_too_long(self, tmp_path):
        result = _run_init(tmp_path, ["a" * 65])
        assert result.returncode != 0
        assert "too long" in result.stderr


class TestInitSkillErrors:
    def test_refuses_duplicate(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills" / "existing").mkdir(parents=True)
        result = _run_init(workspace, ["existing"])
        assert result.returncode != 0
        assert "already exists" in result.stderr

    def test_requires_name_arg(self, tmp_path):
        result = _run_init(tmp_path, [])
        assert result.returncode != 0

    def test_examples_without_resources_fails(self, tmp_path):
        result = _run_init(tmp_path, ["bad-skill", "--examples"])
        assert result.returncode != 0
        assert "requires" in result.stderr


class TestInitSkillResources:
    def test_creates_resource_dirs(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        _run_init(workspace, ["res-skill", "--resources", "scripts,references,assets"])
        skill_dir = workspace / "skills" / "res-skill"
        assert (skill_dir / "scripts").is_dir()
        assert (skill_dir / "references").is_dir()
        assert (skill_dir / "assets").is_dir()

    def test_examples_creates_files(self, tmp_path):
        workspace = tmp_path
        (workspace / "skills").mkdir()
        (workspace / "skills-data").mkdir()
        _run_init(
            workspace,
            ["ex-skill", "--resources", "scripts,references,assets", "--examples"],
        )
        skill_dir = workspace / "skills" / "ex-skill"
        assert (skill_dir / "scripts" / "example.py").exists()
        assert (skill_dir / "references" / "reference.md").exists()
        assert (skill_dir / "assets" / "placeholder.txt").exists()

    def test_path_override(self, tmp_path):
        workspace = tmp_path
        custom = tmp_path / "custom"
        custom.mkdir()
        result = _run_init(workspace, ["alt-skill", "--path", str(custom)])
        assert result.returncode == 0
        assert (custom / "alt-skill" / "SKILL.md").exists()


# ---- quick_validate.py ----


class TestValidateAccepts:
    def test_valid_skill(self, tmp_path):
        skill_dir = tmp_path / "good-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: good-skill\n"
            "description: Use when testing. Does stuff.\n---\nbody"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0
        assert "Valid" in result.stdout

    def test_accepts_crlf_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "crlf-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\r\nname: crlf-skill\r\n"
            "description: Use when testing\r\n---\r\nbody\r\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0

    def test_accepts_faffmonkey_metadata(self, tmp_path):
        skill_dir = tmp_path / "meta-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: meta-skill\n"
            "description: Use when testing metadata\n"
            "actions: run, check\n"
            'metadata: {"faffmonkey": {"requires": {"env": ["API_KEY"]}}}\n'
            "---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0

    def test_accepts_actions_field(self, tmp_path):
        skill_dir = tmp_path / "actions-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: actions-skill\n"
            "description: Use when listing things\n"
            "actions: list, add\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0

    def test_accepts_multiline_frontmatter_without_pyyaml(self, tmp_path):
        skill_dir = tmp_path / "multiline-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: multiline-skill\n"
            "description: Use for testing multiline\n"
            "metadata:\n"
            '  {"faffmonkey": {}}\n'
            "---\n# Skill\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0


class TestValidateRejects:
    def test_missing_skill_md(self, tmp_path):
        result = _run_validate(tmp_path)
        assert result.returncode != 0
        assert "not found" in result.stdout

    def test_missing_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "no-fm"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("just content\nno frontmatter")
        result = _run_validate(skill_dir)
        assert result.returncode != 0

    def test_missing_closing_fence(self, tmp_path):
        skill_dir = tmp_path / "no-close"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: no-close\ndescription: test\n# oops no close\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "Invalid frontmatter" in result.stdout

    def test_rejects_unknown_properties(self, tmp_path):
        skill_dir = tmp_path / "bad-props"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: bad-props\ndescription: Use when testing\n"
            "license: MIT\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "license" in result.stdout

    def test_rejects_allowed_tools_property(self, tmp_path):
        skill_dir = tmp_path / "old-field"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: old-field\ndescription: Use when testing\n"
            "allowed-tools: gh\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "allowed-tools" in result.stdout

    def test_rejects_name_with_uppercase(self, tmp_path):
        skill_dir = tmp_path / "case"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: Bad-Name\ndescription: Use when testing\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "kebab-case" in result.stdout

    def test_rejects_consecutive_hyphens(self, tmp_path):
        skill_dir = tmp_path / "hyphens"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: bad--name\ndescription: Use when testing\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "consecutive" in result.stdout

    def test_rejects_description_too_long(self, tmp_path):
        skill_dir = tmp_path / "long"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: long\ndescription: {'a' * 1025}\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "too long" in result.stdout.lower()

    def test_rejects_angle_brackets_in_description(self, tmp_path):
        skill_dir = tmp_path / "brackets"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: brackets\ndescription: Use when <testing>\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode != 0
        assert "angle" in result.stdout.lower()


class TestValidateTriggerLanguageWarning:
    def test_warns_missing_use_when(self, tmp_path):
        skill_dir = tmp_path / "no-trigger"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: no-trigger\ndescription: Does something cool\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0
        assert "warning" in result.stdout.lower()
        assert "trigger" in result.stdout.lower() or "Use when" in result.stdout

    def test_no_warning_with_use_when(self, tmp_path):
        skill_dir = tmp_path / "ok"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: ok\ndescription: Use when you need weather data\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0
        assert "warning" not in result.stdout.lower()

    def test_no_warning_with_use_for(self, tmp_path):
        skill_dir = tmp_path / "ok2"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: ok2\ndescription: Use for checking weather\n---\n"
        )
        result = _run_validate(skill_dir)
        assert result.returncode == 0
        assert "warning" not in result.stdout.lower()


# ---- package_skill.py ----


class TestPackageSkill:
    @pytest.fixture(autouse=True)
    def _load_module(self):
        self.pkg = _import_package_skill()

    def _create_skill(self, base_dir, name="test-skill"):
        skill_dir = base_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when testing\n---\n"
        )
        (skill_dir / "script.py").write_text("print('ok')\n")
        return skill_dir

    def test_packages_normal_files(self, tmp_path):
        skill_dir = self._create_skill(tmp_path, "normal-skill")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = self.pkg.package_skill(str(skill_dir), str(out_dir))
        assert result is not None
        with zipfile.ZipFile(result, "r") as z:
            names = set(z.namelist())
        assert "normal-skill/SKILL.md" in names
        assert "normal-skill/script.py" in names

    def test_skips_symlink_to_external_file(self, tmp_path):
        skill_dir = self._create_skill(tmp_path, "symlink-skill")
        outside = tmp_path / "secret.txt"
        outside.write_text("secret\n")
        try:
            (skill_dir / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = self.pkg.package_skill(str(skill_dir), str(out_dir))
        assert result is not None
        with zipfile.ZipFile(result, "r") as z:
            names = set(z.namelist())
        assert "symlink-skill/SKILL.md" in names
        assert "symlink-skill/link.txt" not in names

    def test_skips_symlink_directory(self, tmp_path):
        skill_dir = self._create_skill(tmp_path, "dirlink-skill")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n")
        try:
            (skill_dir / "docs").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = self.pkg.package_skill(str(skill_dir), str(out_dir))
        assert result is not None
        with zipfile.ZipFile(result, "r") as z:
            names = set(z.namelist())
        assert "dirlink-skill/docs/secret.txt" not in names

    def test_rejects_path_escape(self, tmp_path):
        skill_dir = self._create_skill(tmp_path, "escape-skill")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        original = self.pkg._is_within

        def fake_within(p, r):
            if p.name == "script.py":
                return False
            return original(p, r)

        with patch.object(self.pkg, "_is_within", fake_within):
            result = self.pkg.package_skill(str(skill_dir), str(out_dir))
        assert result is None

    def test_nested_files_included(self, tmp_path):
        skill_dir = self._create_skill(tmp_path, "nested-skill")
        nested = skill_dir / "lib" / "helpers"
        nested.mkdir(parents=True)
        (nested / "util.py").write_text("pass\n")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = self.pkg.package_skill(str(skill_dir), str(out_dir))
        assert result is not None
        with zipfile.ZipFile(result, "r") as z:
            assert "nested-skill/lib/helpers/util.py" in set(z.namelist())

    def test_skips_output_archive_when_in_skill_dir(self, tmp_path):
        skill_dir = self._create_skill(tmp_path, "self-out")
        result = self.pkg.package_skill(str(skill_dir), str(skill_dir))
        assert result is not None
        with zipfile.ZipFile(result, "r") as z:
            names = set(z.namelist())
        assert "self-out/SKILL.md" in names
        assert "self-out/self-out.skill" not in names
