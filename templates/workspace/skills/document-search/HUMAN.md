# document-search: setup and notes

Keyword search over documents you drop into `workspace/documents/`. FTS5 only,
no embeddings, no network, no dependencies.

## Use

1. Put files in `workspace/documents/` (subdirectories are fine).
2. Ask the agent about them, or tell it "index my documents".

Supported formats: `.md`, `.txt`, `.csv`, `.xlsx`, `.pdf`.

## How it works

- `index` walks `documents/`, extracts text, chunks it (~1500 chars), and
  stores it in SQLite FTS5 at `skills-data/document-search/index.sqlite`.
  Files are only re-read when their content changes; deleted files are pruned.
- `search` runs a keyword query (BM25 ranked). Query terms are quoted, so
  FTS5 syntax in queries has no effect.

## Limits

- Files over 20MB are skipped; extracted text is capped at 2MB per file.
- PDF extraction is best-effort stdlib parsing: plain and FlateDecode text
  streams work; scanned images, encrypted PDFs, and CID-encoded fonts yield
  little or nothing. If a PDF matters and doesn't index, convert it to text
  or markdown.
- XLSX cell values come from the shared-strings table and inline strings;
  formulas index as their last computed value.

## Maintenance

The index is disposable. Delete `skills-data/document-search/index.sqlite`
and re-run `index` to rebuild from scratch.
