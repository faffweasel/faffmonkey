---
name: preconscious
description: Track what should be top-of-mind for the next few days. Use when you notice something worth remembering that isn't a fact (use MEMORY.md) or a message to the user (use carry-over). Items decay daily and fade naturally. Max 5 items, scored by currency and importance.
metadata: '{"faffmonkey":{"requires":{"bins":["python3"]}}}'
actions: add, read, run, drop_lowest
---

## When to use

Add items during conversation when something should stay top-of-mind across sessions but is neither a permanent fact nor a message to deliver:

- Something unresolved should influence the next session's tone ("user seemed frustrated about deploy pipeline")
- A bug or issue is worth monitoring
- The user shared context that should colour upcoming interactions ("travel plans next week")
- A pattern is emerging ("third time user has mentioned poor sleep")

Do not add routine task completions, trivial observations, durable facts (memory files), or messages for the user (carry-over).

| | Preconscious | Carry-Over | Memory Files |
|---|---|---|---|
| Direction | Agent to agent (internal state) | Agent to user (things to tell them) | Agent reference (persistent facts) |
| Lifespan | Days/weeks, decays daily | Consumed at next session start | Permanent until updated |
| Example | "User seemed burned out on Friday" | "Ask user about the Japan trip" | "User's timezone is Asia/Ho_Chi_Minh" |

The buffer holds at most **5 items** and is loaded into your system prompt at session start; you do not need to read it explicitly.

## Scoring

Each item has two scores you choose when adding:

- **C (Currency)**, how fresh, 1-5. Decays by 1 daily.
- **I (Importance)**, how much it matters regardless of freshness, 1-5. Never changes.

Effective score is C + I, used for ranking. Items are dropped when expired AND unimportant (C at 0, I at 2 or below), or displaced by a higher-scoring item when the buffer is full. High-I items (3+) survive many decay cycles; use I 4-5 only for genuine ongoing concerns, since they persist until displaced.

## Actions

**add**, positional args: description, then optional C (default 5) and I (default 3). Adding a duplicate description updates that item's scores instead:

```
add "user seemed frustrated about deploy pipeline" 5 4
add "mentioned wanting to try Rust" 5 2
```

**read**, output the buffer sorted by effective score. Normally unnecessary; bootstrap does this for you:

```
read
```

**run**, the daily decay pass: decrement every C by 1 and drop expired low-importance items. Designed for a daily cron job (`session: "none"`), not conversation:

```
run
```

**drop_lowest**, remove the single lowest-scoring item to free a slot manually:

```
drop_lowest
```

## Limitations

- Max 5 items; adding a 6th silently drops the lowest-scoring one.
- An I:5 item at C:0 persists indefinitely until manually dropped or displaced. Intentional, but choose I honestly.
- Not for durable facts (MEMORY.md), user reminders (carry-over), or scheduled checks (cron).
