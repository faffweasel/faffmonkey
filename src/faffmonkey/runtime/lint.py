import json
import py_compile
import tempfile
import tomllib
from pathlib import Path


def lint_file(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _lint_python(path)
    if suffix == ".json":
        return _lint_json(path)
    if suffix == ".toml":
        return _lint_toml(path)
    if suffix in (".yaml", ".yml"):
        return _lint_yaml(path)
    return None


def _lint_python(path: Path) -> str | None:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as tmp:
            py_compile.compile(str(path), cfile=tmp.name, doraise=True)
        return None
    except py_compile.PyCompileError as e:
        return f"syntax error: {e}"


def _lint_json(path: Path) -> str | None:
    try:
        json.loads(path.read_text())
        return None
    except json.JSONDecodeError as e:
        return f"json error: {e}"


def _lint_toml(path: Path) -> str | None:
    try:
        tomllib.loads(path.read_text())
        return None
    except tomllib.TOMLDecodeError as e:
        return f"toml error: {e}"


def _lint_yaml(path: Path) -> str | None:
    text = path.read_text()
    if not text.strip():
        return None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end == -1 and text.count("---") == 1:
            return "yaml error: unclosed frontmatter (missing closing ---)"
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            indent = len(line) - len(stripped)
            if "\t" in line[:indent]:
                return f"yaml error: tab indentation on line {i}"
    return None
