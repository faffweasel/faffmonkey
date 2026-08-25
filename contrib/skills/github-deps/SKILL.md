---
name: github-deps
description: Monitor GitHub repos for new releases via their public Atom feeds, with semver change classification. Use when the user asks about dependency updates, new releases, or whether anything they depend on shipped recently.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: github_releases
---

## When to use

- "Any dependency updates?" / "anything new released?" → `github_releases`
- "Did [project] release anything recently?" → `github_releases --repo <label>` if configured, else `github_releases --owner <owner> --repo <repo>`
- "What repos am I watching?" → `github_releases --list`
- A dependency-check cron job fires → `github_releases --days 7`, report only what's notable

Always use this instead of web_fetch on `.atom` URLs; web_fetch mangles feed XML.

## Commands

```
github_releases                       check all configured repos (last 7 days)
github_releases --days 14             custom lookback
github_releases --repo React          one configured repo by label
github_releases --owner X --repo Y    ad-hoc repo not in config
github_releases --list                configured repos
github_releases --json                structured output
github_releases --quiet               compact, no links
```

## Interpreting output

Each release shows version, date, link, and a semver classification: major (breaking, flag it clearly), minor (features), patch (fixes, usually not worth interrupting anyone for). When reporting: lead with majors, group patches into a single line, and skip repos with nothing new. Pre-releases and RCs are worth mentioning only if the user tracks that project closely.

`skills-data/github-deps/repos.json` is yours to create and edit with file_write when the user names repos to watch; write it, confirm what you added, and never claim the file needs the user's hand (only `config/jobs.json` is restricted, via cron-manager). A legacy `config/repos.json` is still read, with a note to move it. If a repo is not in the config and the user asks about it regularly, offer to add it.
