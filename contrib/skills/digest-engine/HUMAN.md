# digest-engine: setup and configuration

Multi-topic news/feed digests: reliable RSS/Atom parsing, dedup across runs,
agent-side filtering, delivery on your schedule.

## Setup

1. Tell the agent what to watch and it writes
   `workspace/skills-data/digest-engine/digests.json` itself, or create
   the file by hand (legacy `config/digests.json` still honoured):

   ```json
   {
     "digests": [
       {
         "name": "Example Weekly",
         "schedule": "0 9 * * 1",
         "sources": {
           "rss": ["https://www.reddit.com/r/programming/.rss"],
           "web_search": ["your topic news"]
         },
         "filter": "Describe what to include and what to skip. Be specific, the agent uses this to decide what makes the cut.",
         "days": 8,
         "max_items": 5
       }
     ]
   }
   ```

2. Add a cron job per digest to `workspace/config/jobs.json` (or ask the
   agent to do it via cron-manager), with `"session": "agent"` (digest
   processing invokes the skill and runs web searches, which needs a
   tool-capable session) and a prompt like: "Process the 'Example Weekly'
   digest: follow skills/digest-engine/SKILL.md."

3. Verify with `feed_fetch --list`.

## Notes

- The `filter` text matters more than the sources. Write it like you'd brief
  an editor: what you care about, what bores you.
- `days` is the freshness window per digest (2 for daily, 8 for weekly;
  7 if omitted). It applies to RSS items in the script and is what the
  agent is told to hold web search results to. Keep it in `digests.json`,
  not in the cron job's prompt: the prompt should only say
  "follow skills/digest-engine/SKILL.md", so that changing the skill
  changes every digest.
- Dedup state lives in `skills-data/digest-engine/seen/`; delete a digest's
  file (or use `--reset`) to let items resurface after config changes.
- Composed digests land in `workspace/shared/digests/`.
