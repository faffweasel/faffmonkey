# github-deps: setup and configuration

Release tracking for repos you depend on, via GitHub's public Atom feeds.
No API key, no rate-limit trouble at personal scale.

## Setup

Tell the agent which repos to watch and it writes
`workspace/skills-data/github-deps/repos.json` itself, or create the
file by hand (a legacy `config/repos.json` is still honoured):

```json
{
  "repos": [
    {"owner": "facebook", "repo": "react", "label": "React"},
    {"owner": "tailwindlabs", "repo": "tailwindcss", "label": "Tailwind CSS"}
  ],
  "rss_pattern": "https://github.com/{owner}/{repo}/releases.atom",
  "watch_for": ["releases", "security"]
}
```

`label` is what you and the agent call the repo in conversation.

## Cron (optional)

Add a weekly job to `workspace/config/jobs.json` with `"session": "agent"`
(skill invocation needs a tool-capable session) and a prompt like: "Check
dependency releases: run the github-deps skill with --days 7 and report
anything notable, majors first."

## Notes

- Classification is semver-based from the tag name; projects with
  unconventional tags may classify oddly.
- Purely public feeds; private repos are not supported.
