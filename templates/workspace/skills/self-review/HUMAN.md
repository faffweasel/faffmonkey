# self-review: setup and notes

Keeps the agent's self-maintained knowledge base (`workspace/LEARNINGS.md`)
structured and pruned. No setup required.

## How it works

- All entries live in one file, `workspace/LEARNINGS.md`, tagged `LRN`
  (learning), `ERR` (error), or `FEAT` (feature gap), with sequential IDs
  in the form TAG-YYYYMMDD-NNN. The entry format is defined in
  `references/TEMPLATES.md`; the `add` script enforces it so the agent
  cannot drift from the format.
- `review` cross-references LEARNINGS.md against AGENTS.md and MEMORY.md
  and reports cleanup candidates. Duplicate detection is keyword overlap
  (50%+ threshold); stale detection is a fixed 30-day age on pending
  entries. It never modifies files.
- `promote` appends an entry's summary to AGENTS.md or MEMORY.md and
  marks the source entry as promoted. Promotion is semi-manual by design:
  the agent decides based on review output, and should tidy the promoted
  line if needed.

## Notes

- The heartbeat watchdog flags LEARNINGS.md when it exceeds its entry
  threshold (default 30), which prompts the agent to run a review.
- You can read and edit LEARNINGS.md yourself; it is plain markdown. If
  you remove entries by hand, keep the ID lines intact on what remains
  so the sequence generator stays consistent.
