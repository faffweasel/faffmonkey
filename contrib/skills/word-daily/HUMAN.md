# word-daily: setup and configuration

One word a day in a language you're learning, with spaced repetition driven
by your 0-5 replies. Works for any language pair.

## Setup

1. Install the skill. On first run the example wordlist (Vietnamese with
   Chinese translations, 310 words) is copied to
   `skills-data/word-daily/words.json`.

2. **Set your language pair** by replacing that wordlist. The easiest way is
   to ask the agent: "Generate a words.json for word-daily: 300 practical
   [Spanish] words with [English] translations, following the existing file's
   schema." Schema:

   ```json
   {
     "_meta": {
       "learning_language": "Spanish",
       "bridge_language": "English",
       "levels": ["beginner", "elementary", "intermediate"]
     },
     "words": [
       {"id": "g01", "word": "hola", "translation": "hello",
        "pronunciation": "OH-la", "category": "greetings",
        "level": "beginner", "notes": "optional usage note"}
     ]
   }
   ```

   The `_meta` languages drive how the agent composes the daily message.

3. Add the daily cron job to `workspace/config/jobs.json`:

   ```json
   {
     "id": "word-daily",
     "schedule": "0 8 * * *",
     "session": "agent",
     "prompt": "Run the word-daily skill's pick_word action and compose today's word message per skills/word-daily/SKILL.md.",
     "deliver": { "mode": "announce", "channel": "telegram" },
     "enabled": true
   }
   ```

   Note: `session: "agent"` (a tool-capable cron session) is required
   because composing the message needs the LLM to invoke the skill and
   write the example sentence. Until that session mode is available, ask
   the agent for the daily word interactively.

## Notes

- Scheduling is SM-2. Each word has its own ease and interval. 1 (no idea)
  brings it back the day after it was sent and restarts its intervals.
  2-4 are passes: the interval goes 1 day, 6 days, then grows by the ease
  each time, and the ease drops on a 2 and rises on a 4. 5 (already know)
  jumps to at least a month. 0 skips the word permanently.
- Each day the most overdue due word comes first; a new word is sent only
  when nothing is due. Asking again the same day returns the same word.
- Progress lives in `skills-data/word-daily/word-state.json`; reset with
  `pick_word --reset --confirm`.
- Replacing the wordlist does not reset progress for word ids that match.
