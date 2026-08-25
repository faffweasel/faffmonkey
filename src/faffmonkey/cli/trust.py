"""faff trust / faff untrust CLI commands."""

import os
from pathlib import Path

from faffmonkey.runtime.trust import (
    ALWAYS_TRUSTED,
    is_trusted,
    load_trust_store,
    save_trust_store,
    trust_file,
    untrust_file,
)


def _walk_files(root: Path):
    """Yield non-symlink file paths under root, pruning symlinked dirs."""
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if not os.path.islink(os.path.join(dirpath, d))
        ]
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            if os.path.islink(full):
                continue
            yield full


def _workspace_files(workspace: Path) -> list[str]:
    ws = str(workspace)
    result: list[str] = []
    for full in _walk_files(workspace):
        rel = os.path.relpath(full, ws)
        if rel.startswith("skills-data/") or rel.startswith("tmp/"):
            continue
        result.append(rel)
    result.sort()
    return result


def run_trust_status(state_dir: Path, workspace: Path) -> None:
    store = load_trust_store(state_dir)
    files = _workspace_files(workspace)

    not_tracked: list[str] = []
    trusted: list[str] = []
    untrusted: list[str] = []

    for rel in files:
        if rel in ALWAYS_TRUSTED:
            not_tracked.append(rel)
            continue
        if is_trusted(rel, workspace, store):
            trusted.append(rel)
        else:
            untrusted.append(rel)

    if not_tracked:
        print("Not tracked (always trusted):")
        for f in not_tracked:
            print(f"  {f}")

    if trusted:
        print("Trusted (hash current):")
        for f in trusted:
            print(f"  {f}")

    if untrusted:
        print("Untrusted:")
        for f in untrusted:
            print(f"  {f}")

    if not not_tracked and not trusted and not untrusted:
        print("No workspace files found.")


def _validate_under_workspace(workspace: Path, target: str) -> Path | None:
    try:
        resolved = (workspace / target).resolve()
    except (ValueError, OSError):
        return None
    ws_resolved = workspace.resolve()
    if resolved == ws_resolved or str(resolved).startswith(str(ws_resolved) + "/"):
        return resolved
    return None


def run_trust(state_dir: Path, workspace: Path, target: str) -> int:
    resolved = _validate_under_workspace(workspace, target.rstrip("/"))
    if resolved is None:
        print(f"Path outside workspace: {target}")
        return 1

    store = load_trust_store(state_dir)

    if target.endswith("/"):
        target_dir = workspace / target.rstrip("/")
        if not target_dir.is_dir():
            print(f"Directory not found: {target}")
            return 1
        count = 0
        for full in _walk_files(target_dir):
            file_resolved = Path(full).resolve()
            rel = os.path.relpath(full, str(workspace))
            trust_file(rel, workspace, store, resolved=file_resolved)
            count += 1
        save_trust_store(state_dir, store)
        print(f"Trusted {count} file(s) in {target}")
        return 0

    target_path = workspace / target
    if not target_path.is_file():
        print(f"File not found: {target}")
        return 1
    rel = target
    trust_file(rel, workspace, store, resolved=resolved)
    save_trust_store(state_dir, store)
    print(f"Trusted: {rel}")
    return 0


def run_untrust(state_dir: Path, workspace: Path, target: str) -> int:
    if _validate_under_workspace(workspace, target) is None:
        print(f"Path outside workspace: {target}")
        return 1

    store = load_trust_store(state_dir)
    if untrust_file(target, store):
        save_trust_store(state_dir, store)
        print(f"Untrusted: {target}")
        return 0
    print(f"Not in trust store: {target}")
    return 1
