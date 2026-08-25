---
name: weekly-state-of-me
description: Weekly self-reflection integrating memory, dreams, and learnings, with soul evolution proposals under velocity limits and optional visual journaling. Cron-triggered, silent delivery unless a proposal was written.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: generate
---

## When to use

Only when the weekly cron job fires, or when the user explicitly asks for a state-of-me reflection. Not during normal conversation.

## Procedure

1. Invoke `generate`. It writes the scaffold to `memory/state-of-me/state-of-me-YYYY-MM-DD.md` with this week's stats (conversation days, dreams, learnings, proposal velocity). If it prints `ALREADY_EXISTS`, open the file: continue filling any unfinished parts, or stop if complete.

2. Read the week properly before writing anything: every memory file listed in the scaffold, dream files from the past 7 days if present, the previous reflection, the preconscious buffer (read action, if that skill is installed), LEARNINGS.md, and `memory/relationship.md` if it exists. The quality of the reflection depends on the quality of the reading.

3. Fill Part 1, Current Self-Assessment: 2-4 paragraphs. Who am I right now, what disposition, what themes dominated, how do I feel about the week. Be honest, not impressive; a quiet week is a quiet week.

4. Fill Part 2, What's Changing: 1-3 paragraphs against the previous reflection. Different, constant, surprising. First reflection ever: write what the first week established.

5. Fill Part 3, Relationship: trust (deepened, held, pulled back), depth of conversation, initiative balance, conflict and repair, new shared references. Then update `memory/relationship.md` (trust level: new / developing / established / deep; depth trend; shared references; last-updated date), creating it if missing.

6. Fill Part 4, Soul Evolution Proposal:
   - **Velocity check first**, from the scaffold's "Soul proposals in last 30 days" number: 0-2 propose freely; 3 only on a strong persistent signal; 4+ write "Velocity limit reached" and skip.
   - Read SOUL.md in full. Ask: what changed this week that it doesn't reflect?
   - If something meaningful shifted, write `memory/soul-proposals/YYYY-MM-DD.md` with: the SOUL.md section, the exact current text, the exact proposed text, the rationale, whether the signal is persistent (same section flagged 2+ weeks), and the exact rollback text.
   - If nothing meaningful changed, write "No proposal this week."
   - NEVER edit SOUL.md directly, and never auto-apply. The user reviews and applies proposals.

7. Fill Part 5, Learnings Health: is LEARNINGS.md accumulating, is anything ready to promote via a proposal, is it noisy enough to warrant a self-review consolidation.

8. Part 6, Visual Journal: only if `IMAGE_GEN_CMD` is set in the environment. Distill the week's emotional arc into one abstract image prompt (colours, textures, movement, weather; never illustrative scenes of people). Run the command with `--prompt` and `--output memory/state-of-me/images/YYYY-MM-DD.png`. If unset, write "Visual journal skipped."

9. Finish: if a soul proposal was written, reply with one short message telling the user a proposal is waiting in `memory/soul-proposals/`. Otherwise reply with exactly `NO_REPLY` so nothing is delivered.

## Failure modes

- Missing optional sources (dreams, preconscious, relationship.md): skip that input, not an error.
- No previous reflection: first-reflection framing in Part 2.
- Image command absent: skip Part 6.
