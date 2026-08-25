---
name: digest-engine
description: Configurable multi-topic digest system with a dedicated RSS/Atom parser and duplicate tracking across runs. Cron fires per digest; the agent fetches new items, filters ruthlessly, composes a digest file, and delivers it via the configured channel.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: feed_fetch
---

## When to use

- A digest cron job fires → follow "How to process a digest" below
- "What's new in [digest topic]?" → `feed_fetch --digest NAME`, summarise the new items
- "Add a feed to my [name] digest" → edit `skills-data/digest-engine/digests.json` (yours to write; only `config/jobs.json` is restricted), then confirm with `feed_fetch --list`

Always use this script for RSS/Atom feeds instead of web_fetch; it parses feed XML properly and tracks what has already been surfaced.

## Commands

```
feed_fetch                          fetch all digests (dedup on)
feed_fetch --digest NAME            one digest
feed_fetch --digest NAME --json     structured output with seen_filtered count
feed_fetch --url <feed_url>         single feed, no dedup
feed_fetch --days 7                 limit to last N days
feed_fetch --include-seen           bypass dedup
feed_fetch --list                   configured digests with seen counts
feed_fetch --seen-stats             dedup stats per digest
feed_fetch --reset NAME             clear seen history (after config changes)
```

## How to process a digest

When a digest cron job fires:

1. `feed_fetch --digest NAME --json --days 7`, returns only NEW items since the last run (dedup is automatic; previously seen items are filtered and counted in `seen_filtered`).
2. Run each query in the digest's `web_search` list with the web_search tool; the script only handles RSS.
3. Combine, then filter against the digest's `filter` text. Be ruthless: the filter describes what makes the cut, everything else is dropped.
4. Write the digest to `shared/digests/NAME-YYYY-MM-DD.md`: a heading, then one short entry per surviving item with link and a one-line reason it matters.
5. Deliver a brief summary via the configured channel; the file has the detail.

If nothing survives filtering, say nothing was worth surfacing rather than padding the digest.

## Dedup behaviour

Items are identified by GUID (or title+link hash). Seen entries live in `skills-data/digest-engine/seen/` and auto-prune after 90 days. First appearance surfaces; later appearances are filtered. If the user changes a digest's sources or wants a rerun, `--reset NAME` clears its history.
