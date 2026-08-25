---
name: memory-search
description: Search across all memory files when you don't know the exact wording or which file contains it. Use for: 'what did we decide about...', 'when did I mention...', cross-file topic lookup, finding past conversations or decisions. Prefer over grep for anything except exact-string matches in known files.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: index, search
timeout: 300
---

## When to use

| Situation | Use |
|-----------|-----|
| Exact string lookup ("ERR-20260315") | `grep` |
| Known filename or path | Direct `file_read` |
| Topic search, unknown wording | **memory-search** |
| Cross-file lookup (person across months) | **memory-search** |
| "What did we decide about..." | **memory-search** |
| "When did I mention..." | **memory-search** |

Do not use when you already know the file path (file_read) or need an exact string match (shell_exec with grep). For the user's documents, use document-search; this skill covers memory files.

## Actions

**index**, crawl memory files (default: `memory/`, `LEARNINGS.md`, `MEMORY.md`) into the search index. Incremental: only changed files are re-read, deleted files are pruned. Search runs this automatically before every query, so invoke it directly only for maintenance:

```
index
index --force      re-index everything regardless of change detection
index --stats      index statistics, no changes
index --clear      drop all indexed data
```

**search**, query the index:

```
search visa appointment
search deploy pipeline --top-k 5
search "budget decision" --mode fts
search --check     report index status without searching
```

- `--top-k N`: number of results (default 10)
- `--mode hybrid|fts|vector`: hybrid (keyword + semantic) is the default when embeddings exist, fts otherwise
- `--json`: structured output
- `--no-recency`: disable recency weighting for this search

Results show source file, line range, and heading path:

```
--- Result 1 (score: 0.0323) ---
Source: memory/daily/2026-05-08.md (lines 12-18) > ## Morning
Phill mentioned the visa appointment is on May 16...
```

## Interpreting results

- Results are recency-weighted: a match from today outranks an equally good match from months ago, so the top result reflects the current state, not a stale note. When you specifically want the oldest mentions ("when did I first mention..."), pass `--no-recency`.
- For fuller context, file_read the source file at the line range shown.
- FTS mode is keyword-based (OR across tokens), good for names, IDs, and specific terms; it misses conceptual matches with different wording. Vector/hybrid finds meaning-based matches but needs a configured embedding provider (see HUMAN.md); without one, search silently falls back to FTS.
- No matches does not mean the fact is absent: try different terms, or grep if you know an exact string.

## Limitations

- The index refreshes automatically before every search (incremental, hash-based).
- Chunks are capped at ~1600 characters; very long sections split, which can break context across chunk boundaries.

Related: memory-search finds things in memory files. self-review maintains the agent's own learnings. carry-over queues messages for the user.
