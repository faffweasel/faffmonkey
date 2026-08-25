"""Trust store: tracks sha256 hashes of user-trusted workspace files."""

import hashlib
import json
import logging
import os
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _norm(filepath: str) -> str:
    return posixpath.normpath(filepath)


def _exact_name_exists(directory: Path, name: str) -> bool:
    """Check that a file with exactly this name (case-sensitive) exists."""
    try:
        return name in os.listdir(directory)
    except OSError:
        return False

ALWAYS_TRUSTED = frozenset({
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "AGENTS.md",
    "HEARTBEAT.md",
})


@dataclass
class TrustEntry:
    hash: str
    trusted_at: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _quarantine(path: Path) -> None:
    """Move an unreadable state file aside so it is not silently replaced."""
    corrupt = path.with_suffix(path.suffix + ".corrupt")
    try:
        path.replace(corrupt)
    except OSError as e:
        logger.error("could not quarantine unreadable %s: %s", path.name, e)
        return
    logger.error("%s was unreadable; kept as %s, continuing empty", path.name, corrupt.name)


def load_trust_store(state_dir: Path) -> dict[str, TrustEntry]:
    path = state_dir / "trusted.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Keep the evidence: save_trust_store would otherwise overwrite a
        # truncated file on the next write and lose every trust decision.
        _quarantine(path)
        return {}
    if not isinstance(raw, dict):
        _quarantine(path)
        return {}
    result: dict[str, TrustEntry] = {}
    for filepath, entry in raw.items():
        if isinstance(entry, dict) and "hash" in entry and "trusted_at" in entry:
            normed = _norm(filepath)
            if ".." in normed.split("/"):
                logger.warning("rejecting trust key with path traversal: %s", filepath)
                continue
            result[normed] = TrustEntry(
                hash=entry["hash"],
                trusted_at=entry["trusted_at"],
            )
    return result


def save_trust_store(state_dir: Path, store: dict[str, TrustEntry]) -> None:
    path = state_dir / "trusted.json"
    data = {
        _norm(filepath): {"hash": entry.hash, "trusted_at": entry.trusted_at}
        for filepath, entry in sorted(store.items())
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def is_trusted(filepath: str, workspace: Path, store: dict[str, TrustEntry]) -> bool:
    """Advisory only. Use read_and_check_trust if you need the content."""
    filepath = _norm(filepath)
    if ".." in filepath.split("/"):
        logger.warning("rejecting path with traversal in is_trusted: %s", filepath)
        return False
    if filepath in ALWAYS_TRUSTED:
        p = workspace / filepath
        if not p.is_file() or p.is_symlink():
            return False
        if not _exact_name_exists(workspace, filepath):
            return False
        return True
    if filepath not in store:
        return False
    full = workspace / filepath
    if not full.exists():
        return False
    return _sha256(full) == store[filepath].hash


@dataclass
class TrustedRead:
    content: str
    trusted: bool


def read_and_check_trust(
    filepath: str,
    workspace: Path,
    store: dict[str, TrustEntry],
) -> TrustedRead | None:
    filepath = _norm(filepath)
    full = (workspace / filepath).resolve()
    if not str(full).startswith(str(workspace.resolve()) + "/") and full != workspace.resolve():
        logger.warning("path escapes workspace: %s", filepath)
        return None
    if not full.exists():
        return None
    try:
        data = full.read_bytes()
    except OSError:
        return None
    content = data.decode("utf-8", errors="replace")
    if filepath in ALWAYS_TRUSTED:
        if (workspace / filepath).is_symlink():
            return TrustedRead(content=content, trusted=False)
        if not _exact_name_exists(workspace, filepath):
            logger.warning("case mismatch for always-trusted file: %s", filepath)
            return TrustedRead(content=content, trusted=False)
        return TrustedRead(content=content, trusted=True)
    entry = store.get(filepath)
    if entry is None:
        return TrustedRead(content=content, trusted=False)
    file_hash = hashlib.sha256(data).hexdigest()
    return TrustedRead(content=content, trusted=file_hash == entry.hash)


def trust_file(
    rel_path: str,
    workspace: Path,
    store: dict[str, TrustEntry],
    resolved: Path | None = None,
) -> bool:
    rel_path = _norm(rel_path)
    if rel_path.startswith("..") or "/../" in rel_path:
        logger.warning("path traversal rejected in trust_file: %s", rel_path)
        return False
    full = resolved if resolved is not None else (workspace / rel_path).resolve()
    resolved_workspace = workspace.resolve()
    if not str(full).startswith(str(resolved_workspace) + "/") and full != resolved_workspace:
        logger.warning("path escapes workspace in trust_file: %s", rel_path)
        return False
    if not full.is_file():
        return False
    store[rel_path] = TrustEntry(
        hash=_sha256(full),
        trusted_at=datetime.now(timezone.utc).isoformat(),
    )
    return True


def untrust_file(rel_path: str, store: dict[str, TrustEntry]) -> bool:
    rel_path = _norm(rel_path)
    if rel_path in store:
        del store[rel_path]
        return True
    return False
