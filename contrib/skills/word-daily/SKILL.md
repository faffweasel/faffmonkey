---
name: word-daily
description: Daily vocabulary with spaced repetition for any language pair. Picks one word per day; the user scores it 0-5 and hard words resurface sooner. Use for the daily word cron, when the user asks for today's word, replies with a score, or asks about learning progress.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: pick_word
---

## Daily word

1. `pick_word` returns JSON: the word, translation, pronunciation, notes, a `languages` object naming the learning and bridge languages, `total_sent`, and the selection `reason` (new / review / review_hard).

2. Compose the message in the bridge language. Format:

   **[word]**
   Pronunciation: [pronunciation]
   Meaning: [translation]

   Example: [one natural sentence in the learning language using the word]
   = [translation of the example into the bridge language]

   [Usage note or cultural context from `notes`, 1-2 sentences, only if the word has one]

   Reply 1-5 (1 = no idea, 5 = already know) or 0 to skip

3. The example sentence must be natural and conversational, something the user would actually encounter, matched to the word's level. Not a textbook drill.

4. One word per day. Never send a replacement, even after a 0 or 5 score.

**First message only** (`total_sent` is 1): append an explainer of the scoring scale, written in the bridge language: 1 = no idea (back tomorrow), 2 = hard (3 days), 3 = ok (1 week), 4 = easy (2 weeks), 5 = already know (1 month), 0 = skip permanently.

## Handling a score reply

When the user replies with a bare number 0-5 shortly after a word was sent:

1. Run `pick_word --feedback last <score>`. The state file tracks the last word sent, so no id lookup is needed; the daily word was sent from a different session and `--history` only lists words already scored.
2. Acknowledge in one short phrase in the bridge language, mentioning when it will return ("back in 3 days"). Nothing more.

## Progress questions

- "How's my vocab going?" → `pick_word --stats`, summarise counts by status and due-for-review.
- "What were the last few words?" → `pick_word --history 5`.
- "Skip all the food words" → no bulk action exists; explain scores of 0 skip individual words.
