---
name: document-search
description: Keyword search across documents the user has placed in workspace/documents/ (md, txt, csv, xlsx, pdf). Use when the user asks about the content of their documents, reports, spreadsheets, or notes. Run index after documents change, search to find passages.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: index, search
---

## When to use

- The user asks a question their documents might answer ("what did the Q3 report say about churn", "find the invoice amount in that spreadsheet")
- The user tells you they added or updated files in `documents/` (run `index` then confirm what was indexed)
- You need a source passage before summarising or quoting a document

Do not use for memory recall (memory-search) or for files outside `workspace/documents/`.

## Actions

**index**, scans `documents/`, indexes new and changed files, prunes deleted ones. Cheap when nothing changed; run it before searching if the user mentioned adding files.

```
index
```

**search**, keyword search over indexed content, best matches first:

```
search churn rate Q3
search invoice --limit 5
```

Terms are matched as keywords (implicit AND), not as a natural-language question: search for "churn rate", not "what was our churn rate". Results show the source file, a location hint (sheet and rows, or part), and a snippet:

```
[reports/q3-review.md (part 2)]
  Churn rate fell to 2.1% after the pricing change ...
```

## Interpreting results

- Quote and cite the source file when answering from a result.
- If a snippet is cut off, read the actual file at `documents/<source_file>` for full context before answering in detail.
- No matches: say so, and suggest the user check the document is in `documents/` and indexed. PDFs with scanned images or unusual encodings may index poorly; if a PDF returns nothing, say that its text could not be extracted.
