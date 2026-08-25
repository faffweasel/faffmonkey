# memory-search: setup and notes

Keyword and (optionally) semantic search over the agent's memory files.
Works out of the box with FTS5 keyword search; add an embedding provider
for semantic search.

## How it works

- `index` crawls the configured paths, splits markdown into chunks by
  heading structure (oversized chunks split at paragraph boundaries), and
  stores them in SQLite FTS5 at `skills-data/memory-search/index.sqlite`.
  Files are SHA-256 hashed and only re-indexed on content change; entries
  for deleted files are pruned.
- `search` runs one of three modes: FTS (BM25 keyword, always available),
  vector (cosine similarity over embeddings), or hybrid (both, merged via
  Reciprocal Rank Fusion). Hybrid is the default when embeddings exist.
- Scores are recency-weighted: each chunk carries a date (a YYYY-MM-DD in
  the filename for daily logs, otherwise the file's mtime) and its score
  is halved for every `recency_half_life_days` of age. This keeps stale
  notes from outranking the current state of things. Undated legacy
  chunks are unweighted until re-indexed.

## Configuration

`skills-data/memory-search/config.json`, created automatically from the
template in the skill directory the first time the index is built (the
first search builds it):

```json
{
  "index_paths": ["memory/", "LEARNINGS.md"],
  "max_chunk_chars": 1600,
  "search_top_k": 10,
  "recency_half_life_days": 30,
  "embedding": {
    "provider": "none",
    "providers": {
      "ollama": {
        "endpoint": "http://localhost:11434/api/embed",
        "model": "nomic-embed-text",
        "apiKeyEnvVar": "",
        "format": "ollama"
      }
    }
  }
}
```

- `index_paths`: directories (crawled recursively for .md files) and
  individual files. `MEMORY.md` at workspace root is always included.
- `recency_half_life_days`: age in days at which a result's score is
  halved. 0 disables recency weighting entirely.
- `embedding.provider`: `"none"` (the default) disables embeddings;
  keyword FTS still works fully. Set it to a provider name from
  `providers` to enable semantic search.

**Embeddings send your memory content to the configured endpoint.**
That is the whole mechanism: every chunk of every indexed memory file
is posted to it. Keep it local (the Ollama example) unless you have
deliberately decided otherwise; do not point it at a shared-key cloud
endpoint just because a key happens to be configured for something
else. There is no "auto" worth having here.

Without an embedding provider the skill is FTS-only, which is still
useful; semantic search is an upgrade, not a requirement.

## Maintenance

- The index is self-maintaining: every search refreshes it first
  (incremental and hash-based, so unchanged files cost one hash each).
  No cron job and no manual index step are needed.
- The index is disposable: `index --clear` (or delete the sqlite file)
  and the next search rebuilds it.
