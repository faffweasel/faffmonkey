"""Index memory files into SQLite FTS5 for memory-search skill.

Crawls workspace/memory/ and workspace/MEMORY.md, chunks by heading
structure, and stores in FTS5 at skills-data/memory-search/index.sqlite.
Uses SHA-256 content hashing for incremental updates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from providers import embed, load_config, vec_to_blob


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            heading TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            indexed_at REAL NOT NULL,
            doc_date TEXT
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "doc_date" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN doc_date TEXT")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, heading, source_file,
            content='chunks',
            content_rowid='chunk_id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content, heading, source_file)
            VALUES (new.chunk_id, new.content, new.heading, new.source_file);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, heading, source_file)
            VALUES ('delete', old.chunk_id, old.content, old.heading, old.source_file);
        END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_hashes (
            file_path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            indexed_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
            embedding BLOB NOT NULL
        )
    """)
    conn.commit()
    return conn


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def doc_date_for(path: Path) -> str:
    """Date used for recency weighting: a YYYY-MM-DD in the filename (daily
    logs) wins; otherwise the file's mtime date."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if m:
        return m.group(1)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def chunk_markdown(text: str, max_chars: int = 1600) -> list[dict]:
    lines = text.split("\n")
    chunks: list[dict] = []
    current_heading = "(top)"
    current_lines: list[str] = []
    current_start = 1

    def flush() -> None:
        if not current_lines:
            return
        content = "\n".join(current_lines).strip()
        if not content:
            return
        if len(content) <= max_chars:
            chunks.append({
                "heading": current_heading,
                "content": content,
                "start_line": current_start,
                "end_line": current_start + len(current_lines) - 1,
            })
        else:
            _split_oversized(content, current_heading, current_start, max_chars, chunks)

    for i, line in enumerate(lines, start=1):
        if re.match(r"^#{1,6}\s", line):
            flush()
            current_heading = line.lstrip("#").strip()
            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

    flush()
    return chunks


def _split_oversized(
    content: str, heading: str, start_line: int, max_chars: int, chunks: list[dict]
) -> None:
    paragraphs = re.split(r"\n\n+", content)
    buf: list[str] = []
    buf_len = 0
    chunk_start = start_line
    line_offset = 0

    for para in paragraphs:
        para_len = len(para)
        if buf and buf_len + para_len + 2 > max_chars:
            joined = "\n\n".join(buf)
            line_count = joined.count("\n") + 1
            chunks.append({
                "heading": heading,
                "content": joined,
                "start_line": chunk_start,
                "end_line": chunk_start + line_count - 1,
            })
            chunk_start = chunk_start + line_count
            buf = []
            buf_len = 0

        if para_len > max_chars:
            words = para.split()
            word_buf: list[str] = []
            word_len = 0
            for word in words:
                if word_buf and word_len + len(word) + 1 > max_chars:
                    text_chunk = " ".join(word_buf)
                    lc = text_chunk.count("\n") + 1
                    chunks.append({
                        "heading": heading,
                        "content": text_chunk,
                        "start_line": chunk_start,
                        "end_line": chunk_start + lc - 1,
                    })
                    chunk_start += lc
                    word_buf = []
                    word_len = 0
                word_buf.append(word)
                word_len += len(word) + (1 if word_len > 0 else 0)
            if word_buf:
                buf.append(" ".join(word_buf))
                buf_len += len(buf[-1])
        else:
            buf.append(para)
            buf_len += para_len + (2 if buf_len > 0 else 0)

    if buf:
        joined = "\n\n".join(buf)
        line_count = joined.count("\n") + 1
        chunks.append({
            "heading": heading,
            "content": joined,
            "start_line": chunk_start,
            "end_line": chunk_start + line_count - 1,
        })


def collect_files(workspace: Path, index_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    memory_md = workspace / "MEMORY.md"
    if memory_md.exists():
        files.append(memory_md)
    for rel in index_paths:
        target = workspace / rel
        if target.is_file() and target.suffix == ".md":
            if target not in files:
                files.append(target)
        elif target.is_dir():
            for md in sorted(target.rglob("*.md")):
                if md not in files:
                    files.append(md)
    return files


def index_file(
    conn: sqlite3.Connection,
    workspace: Path,
    path: Path,
    max_chars: int,
    embedding_config: dict | None,
    force: bool,
) -> int:
    rel = str(path.relative_to(workspace))
    current_hash = file_sha256(path)

    if not force:
        row = conn.execute(
            "SELECT content_hash FROM file_hashes WHERE file_path = ?", (rel,)
        ).fetchone()
        if row and row[0] == current_hash:
            return 0

    conn.execute("DELETE FROM chunks WHERE source_file = ?", (rel,))
    conn.execute("DELETE FROM file_hashes WHERE file_path = ?", (rel,))

    text = path.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_markdown(text, max_chars)

    now = time.time()
    doc_date = doc_date_for(path)
    indexed = 0
    for chunk in chunks:
        chunk_hash = hashlib.sha256(chunk["content"].encode()).hexdigest()
        cursor = conn.execute(
            """INSERT INTO chunks (source_file, start_line, end_line, heading, content, content_hash, indexed_at, doc_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (rel, chunk["start_line"], chunk["end_line"], chunk["heading"], chunk["content"], chunk_hash, now, doc_date),
        )
        chunk_id = cursor.lastrowid

        if embedding_config:
            vec = embed(chunk["content"], embedding_config)
            if vec:
                conn.execute(
                    "INSERT INTO embeddings (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, vec_to_blob(vec)),
                )
        indexed += 1

    conn.execute(
        "INSERT OR REPLACE INTO file_hashes (file_path, content_hash, indexed_at) VALUES (?, ?, ?)",
        (rel, current_hash, now),
    )
    conn.commit()
    return indexed


def show_stats(conn: sqlite3.Connection) -> None:
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    file_count = conn.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0]
    emb_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    last = conn.execute("SELECT MAX(indexed_at) FROM file_hashes").fetchone()[0]
    last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "never"
    print(f"Files indexed: {file_count}")
    print(f"Chunks: {chunk_count}")
    print(f"Embeddings: {emb_count}")
    print(f"Last indexed: {last_str}")


def clear_all(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM embeddings")
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM file_hashes")
    conn.commit()
    print("Index cleared.")


def load_skill_config(skill_data: Path) -> dict:
    config_path = skill_data / "config.json"
    if not config_path.exists():
        return {"index_paths": ["memory/", "LEARNINGS.md"], "max_chunk_chars": 1600}
    try:
        return json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"index_paths": ["memory/", "LEARNINGS.md"], "max_chunk_chars": 1600}


def ensure_config(skill_data: Path) -> None:
    config_path = skill_data / "config.json"
    if config_path.exists():
        return
    skill_data.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).parent.parent / "config.json"
    if template.is_file():
        config_path.write_text(template.read_text(encoding="utf-8"))
        return
    default = {
        "index_paths": ["memory/", "LEARNINGS.md"],
        "max_chunk_chars": 1600,
        "search_top_k": 10,
        "recency_half_life_days": 30,
        # Embeddings are off by default: memory content is personal data,
        # and with "auto" plus any configured API key it was sent to a
        # remote embedding endpoint without the operator ever opting in.
        "embedding": {
            "provider": "none",
            "providers": {
                "ollama": {
                    "endpoint": "http://localhost:11434/api/embed",
                    "model": "nomic-embed-text",
                    "apiKeyEnvVar": "",
                    "format": "ollama",
                },
            },
        },
    }
    config_path.write_text(json.dumps(default, indent=2) + "\n")


def run_index(workspace: Path, skill_data: Path, force: bool = False) -> str:
    """Build or refresh the index and return a one-line summary.

    Called by main() and by search.py before every query, so the index is
    self-maintaining: no cron job and no manual index step are required.
    Incremental: files are hashed and only changed ones are re-read.
    """
    ensure_config(skill_data)
    conn = init_db(skill_data / "index.sqlite")
    try:
        config = load_skill_config(skill_data)
        index_paths = config.get("index_paths", ["memory/", "LEARNINGS.md"])
        max_chars = config.get("max_chunk_chars", 1600)
        embedding_config = config.get("embedding")
        if embedding_config and embedding_config.get("provider") in ("none", "off", "disabled"):
            embedding_config = None

        files = collect_files(workspace, index_paths)
        if not files:
            return "No memory files found to index."

        total = 0
        skipped = 0
        for f in files:
            count = index_file(conn, workspace, f, max_chars, embedding_config, force)
            if count == 0 and not force:
                skipped += 1
            else:
                total += count

        # clean stale entries
        indexed_files = {str(f.relative_to(workspace)) for f in files}
        db_files = {row[0] for row in conn.execute("SELECT file_path FROM file_hashes").fetchall()}
        stale = db_files - indexed_files
        for st in stale:
            conn.execute("DELETE FROM chunks WHERE source_file = ?", (st,))
            conn.execute("DELETE FROM file_hashes WHERE file_path = ?", (st,))
        if stale:
            conn.commit()

        return f"Indexed {total} chunks from {len(files) - skipped} files ({skipped} unchanged)."
    finally:
        conn.close()


def main() -> None:
    workspace_env = os.environ.get("WORKSPACE", "")
    if not workspace_env:
        print("error: WORKSPACE not set", file=sys.stderr)
        sys.exit(1)
    workspace = Path(workspace_env)
    skill_data_env = os.environ.get("SKILL_DATA", "")
    if not skill_data_env:
        print("error: SKILL_DATA not set", file=sys.stderr)
        sys.exit(1)
    skill_data = Path(skill_data_env)

    if not workspace.is_dir():
        print("error: WORKSPACE not set or invalid", file=sys.stderr)
        sys.exit(1)

    ensure_config(skill_data)

    args = sys.argv[1:]
    force = "--force" in args
    stats = "--stats" in args
    clear = "--clear" in args

    if clear or stats:
        conn = init_db(skill_data / "index.sqlite")
        if clear:
            clear_all(conn)
        else:
            show_stats(conn)
        conn.close()
        return

    print(run_index(workspace, skill_data, force=force))


if __name__ == "__main__":
    main()
