"""Index workspace/documents/ into SQLite FTS5 for document-search.

Crawls supported files (md, txt, csv, xlsx, pdf), extracts text, chunks it,
and stores chunks in FTS5 at SKILL_DATA/index.sqlite. Files are re-indexed
only when their content hash changes; deleted files are pruned.

Usage: index.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from extract import SUPPORTED_EXTENSIONS, extract_text

_workspace_env = os.environ.get("WORKSPACE", "")
if not _workspace_env:
    print("error: WORKSPACE not set", file=sys.stderr)
    sys.exit(1)
WORKSPACE = Path(_workspace_env).resolve()
SKILL_DATA = Path(
    os.environ.get("SKILL_DATA", "") or WORKSPACE / "skills-data" / "document-search",
)
DOCS_DIR = WORKSPACE / "documents"
DB_PATH = SKILL_DATA / "index.sqlite"

_CHUNK_MAX_CHARS = 1500


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            source_file TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            indexed_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            locator TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, locator, source_file,
            content='chunks',
            content_rowid='chunk_id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content, locator, source_file)
            VALUES (new.chunk_id, new.content, new.locator, new.source_file);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, locator, source_file)
            VALUES ('delete', old.chunk_id, old.content, old.locator, old.source_file);
        END
    """)
    return conn


def chunk_segments(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Split extracted segments into chunks of at most _CHUNK_MAX_CHARS,
    breaking on blank lines where possible."""
    chunks: list[tuple[str, str]] = []
    for locator, text in segments:
        text = text.strip()
        if not text:
            continue
        if len(text) <= _CHUNK_MAX_CHARS:
            chunks.append((locator, text))
            continue
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        current = ""
        part = 1
        for block in blocks:
            while len(block) > _CHUNK_MAX_CHARS:
                chunks.append((_part_locator(locator, part), block[:_CHUNK_MAX_CHARS]))
                part += 1
                block = block[_CHUNK_MAX_CHARS:]
            if current and len(current) + len(block) + 2 > _CHUNK_MAX_CHARS:
                chunks.append((_part_locator(locator, part), current))
                part += 1
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            chunks.append((_part_locator(locator, part), current))
    return chunks


def _part_locator(locator: str, part: int) -> str:
    if part == 1 and not locator:
        return ""
    return f"{locator} part {part}".strip()


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()[:16]


def _reindex_file(conn: sqlite3.Connection, rel: str, path: Path, content_hash: str) -> int:
    conn.execute("DELETE FROM chunks WHERE source_file = ?", (rel,))
    try:
        segments = extract_text(path)
    except Exception as e:
        # Deliberately broad. A corrupt or misnamed file fails in whatever
        # way its parser chooses: a .xlsx that is not a zip raises
        # BadZipFile, a truncated PDF raises from its own library. Catching
        # only ValueError and OSError let one bad file abort the whole run,
        # and because the commit happens after the loop, every chunk
        # indexed before it was discarded too.
        print(f"  skipped {rel}: {type(e).__name__}: {e}", file=sys.stderr)
        conn.execute("DELETE FROM files WHERE source_file = ?", (rel,))
        return 0
    chunks = chunk_segments(segments)
    for locator, content in chunks:
        conn.execute(
            "INSERT INTO chunks (source_file, locator, content) VALUES (?, ?, ?)",
            (rel, locator, content),
        )
    conn.execute(
        "INSERT OR REPLACE INTO files (source_file, content_hash, indexed_at)"
        " VALUES (?, ?, ?)",
        (rel, content_hash, time.time()),
    )
    return len(chunks)


def main() -> int:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db(DB_PATH)

    known = {
        row[0]: row[1]
        for row in conn.execute("SELECT source_file, content_hash FROM files")
    }

    seen: set[str] = set()
    indexed = unchanged = 0
    chunk_count = 0
    for path in sorted(DOCS_DIR.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        rel = path.relative_to(DOCS_DIR).as_posix()
        seen.add(rel)
        content_hash = _file_hash(path)
        if known.get(rel) == content_hash:
            unchanged += 1
            continue
        n = _reindex_file(conn, rel, path, content_hash)
        if n:
            indexed += 1
            chunk_count += n
        # Commit per file so an unexpected failure later in the crawl keeps
        # everything indexed so far instead of throwing the run away.
        conn.commit()

    removed = 0
    for rel in set(known) - seen:
        conn.execute("DELETE FROM chunks WHERE source_file = ?", (rel,))
        conn.execute("DELETE FROM files WHERE source_file = ?", (rel,))
        removed += 1

    conn.commit()
    conn.close()
    print(
        f"indexed {indexed} file(s) ({chunk_count} chunks), "
        f"{unchanged} unchanged, {removed} removed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
