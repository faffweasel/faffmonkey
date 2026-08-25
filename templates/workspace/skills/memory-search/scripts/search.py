"""Search the memory-search FTS5 index.

Supports FTS5 keyword search, vector cosine similarity search (when
embeddings exist), and hybrid mode via Reciprocal Rank Fusion. Scores
are recency-weighted so fresh notes outrank stale ones; configure via
recency_half_life_days (0 disables) or the --no-recency flag.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from providers import blob_to_vec, cosine_similarity, embed, load_config

DEFAULT_HALF_LIFE_DAYS = 30.0


def recency_weight(doc_date: str | None, today: date, half_life_days: float) -> float:
    if half_life_days <= 0 or not doc_date:
        return 1.0
    try:
        d = date.fromisoformat(doc_date)
    except ValueError:
        return 1.0
    age_days = max(0, (today - d).days)
    return 0.5 ** (age_days / half_life_days)


def load_half_life(skill_data: Path) -> float:
    config_path = skill_data / "config.json"
    if not config_path.exists():
        return DEFAULT_HALF_LIFE_DAYS
    try:
        cfg = json.loads(config_path.read_text())
        return float(cfg.get("recency_half_life_days", DEFAULT_HALF_LIFE_DAYS))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return DEFAULT_HALF_LIFE_DAYS


def _apply_recency(
    results: list[dict], today: date | None, half_life_days: float
) -> list[dict]:
    if today is None or half_life_days <= 0:
        return results
    for r in results:
        r["score"] *= recency_weight(r.get("doc_date"), today, half_life_days)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    top_k: int,
    today: date | None = None,
    half_life_days: float = 0.0,
) -> list[dict]:
    fts_query = _sanitise_fts_query(query)
    if not fts_query:
        return []
    fetch = max(50, top_k * 5) if today is not None and half_life_days > 0 else top_k
    rows = conn.execute(
        """SELECT c.chunk_id, c.source_file, c.start_line, c.end_line,
                  c.heading, c.content, c.doc_date,
                  rank
           FROM chunks_fts f
           JOIN chunks c ON c.chunk_id = f.rowid
           WHERE chunks_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (fts_query, fetch),
    ).fetchall()
    results = [
        {
            "chunk_id": r[0],
            "source_file": r[1],
            "start_line": r[2],
            "end_line": r[3],
            "heading": r[4],
            "content": r[5],
            "doc_date": r[6],
            "score": -r[7],
        }
        for r in rows
    ]
    return _apply_recency(results, today, half_life_days)[:top_k]


def _sanitise_fts_query(query: str) -> str:
    tokens = query.split()
    safe: list[str] = []
    for t in tokens:
        cleaned = "".join(c for c in t if c.isalnum() or c in "-_")
        if cleaned:
            safe.append(cleaned)
    if not safe:
        return ""
    return " OR ".join(safe)


def vector_search(
    conn: sqlite3.Connection,
    query_vec: list[float],
    top_k: int,
    today: date | None = None,
    half_life_days: float = 0.0,
) -> list[dict]:
    rows = conn.execute(
        """SELECT e.chunk_id, e.embedding, c.source_file, c.start_line,
                  c.end_line, c.heading, c.content, c.doc_date
           FROM embeddings e
           JOIN chunks c ON c.chunk_id = e.chunk_id"""
    ).fetchall()

    results: list[dict] = []
    for r in rows:
        vec = blob_to_vec(r[1])
        sim = cosine_similarity(query_vec, vec)
        results.append({
            "chunk_id": r[0],
            "source_file": r[2],
            "start_line": r[3],
            "end_line": r[4],
            "heading": r[5],
            "content": r[6],
            "doc_date": r[7],
            "score": sim,
        })

    results = _apply_recency(results, today, half_life_days)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def rrf_merge(
    fts_results: list[dict],
    vec_results: list[dict],
    top_k: int,
    k: int = 60,
) -> list[dict]:
    scores: dict[int, float] = {}
    all_results: dict[int, dict] = {}

    for rank, r in enumerate(fts_results, start=1):
        cid = r["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
        all_results[cid] = r

    for rank, r in enumerate(vec_results, start=1):
        cid = r["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
        if cid not in all_results:
            all_results[cid] = r

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    results = []
    for cid, score in ranked:
        result = all_results[cid].copy()
        result["score"] = score
        results.append(result)
    return results


def snippet(content: str, max_len: int = 700) -> str:
    if len(content) <= max_len:
        return content
    return content[:max_len - 3] + "..."


def format_results(results: list[dict], as_json: bool) -> str:
    if not results:
        return "No results found."
    if as_json:
        for r in results:
            r["snippet"] = snippet(r.pop("content", ""))
        return json.dumps(results, indent=2)
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        lines.append(f"--- Result {i} (score: {r['score']:.4f}) ---")
        lines.append(f"File: {r['source_file']} (lines {r['start_line']}-{r['end_line']})")
        lines.append(f"Heading: {r['heading']}")
        lines.append(snippet(r.get("content", "")))
        lines.append("")
    return "\n".join(lines)


def has_embeddings(conn: sqlite3.Connection) -> bool:
    count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    return count > 0


def detect_mode(conn: sqlite3.Connection, requested: str) -> str:
    if requested in ("fts", "vector"):
        return requested
    if requested == "hybrid" or not requested:
        return "hybrid" if has_embeddings(conn) else "fts"
    return "fts"


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

    db_path = skill_data / "index.sqlite"

    args = sys.argv[1:]

    from index import run_index

    if "--check" in args:
        run_index(workspace, skill_data)
        conn = sqlite3.connect(str(db_path))
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        emb_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        mode = "hybrid" if emb_count > 0 else "fts-only"
        print(f"Index ready: {chunk_count} chunks, {emb_count} embeddings ({mode})")
        conn.close()
        return

    top_k = 10
    mode = ""
    as_json = "--json" in args
    no_recency = "--no-recency" in args
    positional: list[str] = []

    i = 0
    while i < len(args):
        if args[i] == "--top-k" and i + 1 < len(args):
            top_k = int(args[i + 1])
            i += 2
        elif args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        elif args[i] in ("--json", "--check", "--no-recency"):
            i += 1
        else:
            positional.append(args[i])
            i += 1

    query = " ".join(positional)
    if not query:
        print("error: no search query provided", file=sys.stderr)
        sys.exit(1)

    # Self-maintaining: build or refresh the index before every query, so
    # no manual or scheduled index step is required.
    run_index(workspace, skill_data)

    conn = sqlite3.connect(str(db_path))
    effective_mode = detect_mode(conn, mode)

    half_life = 0.0 if no_recency else load_half_life(skill_data)
    try:
        tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    except (KeyError, ValueError):
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()

    fts_results: list[dict] = []
    vec_results: list[dict] = []

    if effective_mode in ("fts", "hybrid"):
        fts_results = fts_search(conn, query, top_k, today=today, half_life_days=half_life)

    if effective_mode in ("vector", "hybrid"):
        embedding_config = load_config(skill_data)
        if embedding_config:
            query_vec = embed(query, embedding_config)
            if query_vec:
                vec_results = vector_search(conn, query_vec, top_k, today=today, half_life_days=half_life)

    if effective_mode == "hybrid" and fts_results and vec_results:
        results = rrf_merge(fts_results, vec_results, top_k)
    elif effective_mode == "vector" and vec_results:
        results = vec_results
    else:
        results = fts_results

    print(format_results(results[:top_k], as_json))
    conn.close()


if __name__ == "__main__":
    main()
