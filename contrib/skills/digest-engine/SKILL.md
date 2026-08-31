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
- "Set up a daily/weekly digest on [topic]", "keep me posted on [topic]" → follow "Create a new digest" below. Do not answer with a one-off summary; the user asked for something that recurs.

Always use `feed_fetch` for RSS/Atom feeds instead of web_fetch; it parses feed XML properly and tracks what has already been surfaced. Run it with skill_invoke (name `digest-engine`, input the command line below), never through shell_exec.

## Create a new digest

A digest is a topic, its sources and a schedule. Sources are RSS/Atom feeds and web searches; either list may be empty, so a digest built purely on web research is valid.

1. Add an entry to `skills-data/digest-engine/digests.json` (create the file if absent):

   ```json
   {
     "digests": [
       {
         "name": "agents",
         "schedule": "0 8 * * *",
         "sources": {
           "rss": ["https://github.com/example/openclaw/releases.atom"],
           "web_search": ["openclaw agent release", "hermes agent framework news"]
         },
         "filter": "Releases, architecture changes, benchmarks and post-mortems. Skip hype, tutorials and reposts.",
         "days": 2,
         "max_items": 8
       }
     ]
   }
   ```

   The `filter` is what you will judge items against when the digest runs; write it the way the user briefed you. `days` is the freshness window: 2 for a daily digest, 8 for a weekly one, 7 if left out. `schedule` records the intent; the cron job below is what actually runs it.
2. `feed_fetch --list` to confirm the entry parses and the feeds respond.
3. Add the cron job with the cron-manager skill, for example:

   ```
   add '{"id": "digest-agents", "schedule": "0 8 * * *", "prompt": "Process the '\''agents'\'' digest: follow skills/digest-engine/SKILL.md.", "session": "agent", "deliver": {"mode": "announce", "channel": "last"}}'
   ```

   `session` must be `agent` (the run invokes this skill and searches the web) and `deliver.mode` must be `announce`, or the digest is written and never sent.
4. Tell the user the name, the schedule and the sources you chose, and offer `feed_fetch --digest NAME --include-seen` as an immediate first run.

## Source recipes

Most places worth following expose a feed; build a digest from the projects, discussions and searches behind a topic, not from the two or three sites that come to mind first. A topic like "autonomous agent frameworks" should have a repo feed per project named, a discussion feed per community, and a search query per project.

| Where | Feed URL |
|---|---|
| GitHub releases | `https://github.com/<org>/<repo>/releases.atom` |
| GitHub commits | `https://github.com/<org>/<repo>/commits/<branch>.atom` |
| Hacker News, keyword | `https://hnrss.org/newest?q=<term>` (add `&points=20` to skip noise) |
| Reddit subreddit | `https://www.reddit.com/r/<sub>/.rss` |
| Reddit search | `https://www.reddit.com/search.rss?q=<term>&sort=new` |
| lobste.rs tag | `https://lobste.rs/t/<tag>.rss` |
| YouTube channel | `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` |
| Any blog | try `/feed`, `/rss.xml`, `/atom.xml`; otherwise web_fetch the page and look for `<link rel="alternate" type="application/rss+xml">` |

Put anything a feed cannot cover into `web_search` instead: product names, "<project> release", "<project> vs <project>". Reddit may be unreachable from where you run (it blocks some countries and VPN exits); if `feed_fetch` reports an error for a reddit URL, drop it and cover the subreddit with a web search instead of reporting a broken digest.

## Commands

```
feed_fetch                          fetch all digests (dedup on)
feed_fetch --digest NAME            one digest
feed_fetch --digest NAME --json     structured output with seen_filtered count
feed_fetch --url <feed_url>         single feed, no dedup
feed_fetch --days 7                 override the digest's freshness window for this run
feed_fetch --include-seen           bypass dedup
feed_fetch --list                   configured digests with seen counts
feed_fetch --seen-stats             dedup stats per digest
feed_fetch --reset NAME             clear seen history (after config changes)
```

## How to process a digest

When a digest cron job fires:

1. `feed_fetch --digest NAME --json`, returns only NEW items within the digest's `days` window (dedup is automatic; previously seen items are filtered and counted in `seen_filtered`). The output's `days` field is the window in force.
2. Run each query in the digest's `web_search` list with the web_search tool; `feed_fetch` only handles RSS. Search results are not deduplicated or dated for you: apply the same `days` window yourself and drop anything older, or a week-old post resurfaces every run.
3. Combine, then filter against the digest's `filter` text. Be ruthless: the filter describes what makes the cut, everything else is dropped.
4. Write the digest to `shared/digests/NAME-YYYY-MM-DD.md`: a heading, then one short entry per item that made the cut, with link and a one-line reason it matters.
5. Send the digest as the message: the heading and the entries, nothing else. No item counts, no mention of filtering, nothing about what was dropped or why, no note that the file was written. The reader wants the news, not the process.

If nothing made the cut, the whole message is one line naming this digest's topic: "Nothing in <topic> today.", where <topic> is the subject of the digest being processed (its name, or what its filter is about), not any example text. Do not list what was checked, do not write a digest file, and do not answer NO_REPLY: a sentence says the run happened and found nothing, silence looks like it never ran.

## Dedup behaviour

Items are identified by GUID (or title+link hash). Seen entries live in `skills-data/digest-engine/seen/` and auto-prune after 90 days. First appearance surfaces; later appearances are filtered. If the user changes a digest's sources or wants a rerun, `--reset NAME` clears its history.
