"""Search the document index built by index.py.

Usage: search.py <query terms...> [--limit N]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_workspace_env = os.environ.get("WORKSPACE", "")
if not _workspace_env:
    print("error: WORKSPACE not set", file=sys.stderr)
    sys.exit(1)
WORKSPACE = Path(_workspace_env).resolve()
SKILL_DATA = Path(
    os.environ.get("SKILL_DATA", "") or WORKSPACE / "skills-data" / "document-search",
)
DB_PATH = SKILL_DATA / "index.sqlite"

_SNIPPET_WORDS = 60


def _fts_query(terms: list[str]) -> str:
    """Quote each term so user input cannot inject FTS5 syntax."""
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms if t.strip()]
    return " ".join(quoted)


def _snippet(content: str) -> str:
    words = content.split()
    if len(words) <= _SNIPPET_WORDS:
        return content.replace("\n", " ")
    return " ".join(words[:_SNIPPET_WORDS]).replace("\n", " ") + " ..."


def main() -> int:
    ap = argparse.ArgumentParser(description="Search indexed documents")
    ap.add_argument("terms", nargs="+", help="Search terms")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    if not DB_PATH.is_file():
        print("No index yet. Run the index action first.", file=sys.stderr)
        return 1

    query = _fts_query(args.terms)
    if not query:
        print("Empty query.", file=sys.stderr)
        return 1

    limit = max(1, min(args.limit, 25))
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            """
            SELECT c.source_file, c.locator, c.content
            FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts)
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Search failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if not rows:
        print("No matches.")
        return 0

    for source_file, locator, content in rows:
        where = f"{source_file} ({locator})" if locator else source_file
        print(f"[{where}]")
        print(f"  {_snippet(content)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
