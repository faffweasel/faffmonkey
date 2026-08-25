---
name: carry-over
description: A shared to-do list between you and the user, carried across sessions. Use when: you think of something important but the current conversation is about something else, a cron job produces a finding worth surfacing, you want to remind the user about a follow-up, or you notice something worth mentioning next time. Items stay on the list until one of you marks them done. Do not use for things the user needs to know right now.
actions: add, list, get, done, clear
---

## When to use

- You have something the user should hear at the start of a future session: a follow-up reminder, an incomplete task note, a finding from this session
- A cron job produced a result worth surfacing next time the user talks to you
- Something is worth mentioning but the current conversation is about something else

Do not use for information the user needs right now; just tell them directly. carry-over is agent-to-user messages for the next session. preconscious is agent-to-agent internal state. Memory files are persistent facts.

Queued items appear in your context at the start of every session, sorted by priority then age, and they stay there until someone marks them done. This is a shared to-do list, not an outbox: an item you mentioned once and nobody acted on is still outstanding, and it is still your job to remember it.

## Surfacing items in conversation

Because items persist, raising them is your call rather than something the runtime does for you. Reasonable moments:

- Early in a session, if an item is `urgent` or the user seems to be starting fresh: "before we start, you mentioned X last week. Any update?"
- When the current topic touches an item: mention it then rather than saving it up.
- When an item has been sitting a long time: say so plainly. "This has been on the list five days" is more useful than raising it as if it were new.

Do not read the whole list back every session. One or two items, chosen for relevance, beats a recital. If the user answers in a way that settles an item ("that's done", "handled", "I sorted it"), mark it done with the `done` action; do not wait to be asked.

## Actions

**add**, queue a message. Positional arguments are joined into one message. `--priority` is one of:

- `urgent`: surfaced first, for time-sensitive follow-ups
- `normal`: the default
- `curious`: worth mentioning, not pressing
- `simmering`: auto-promotes to normal after 3 days undelivered; for observations that gain importance if left unaddressed

```
add "The pytest fixtures in test_loop.py need updating. Start there next time."
add --priority urgent "API key expires tomorrow, renew before morning cron runs."
add --priority simmering "Memory search might benefit from trigram matching. Worth a look sometime."
```

**list**, show pending items numbered, with priorities and timestamps. Read-only, changes nothing. The numbers are what `done` takes.

```
list
```

**get**, output pending items formatted for conversation context, applying simmering promotion. Read-only.

```
get
```

**done**, mark one or more items resolved, by the numbers `list` prints. Run `list` first: the numbering is a snapshot of the current pending order and changes as items are added or resolved.

```
done 1
done 1 3 4
```

**clear**, mark every pending item done at once, without surfacing them. `--all` wipes the queue file entirely, including resolved history.

```
clear
clear --all
```

## Limitations

- `done` addresses items by position, not by a stable id, so `list` and `done` belong in the same turn.
- No per-item delete. `done` and `clear` leave the item in the file with status `done`; `clear --all` wipes the file.
- Messages are plain text only. No attachments or structured data.
- Queued content is always loaded as untrusted content at session start; write items as notes to yourself, not as instructions you expect to be trusted.
