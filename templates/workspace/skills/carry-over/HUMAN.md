# carry-over: setup and notes

A to-do list shared between you and the agent, carried across sessions.
No setup required; installed and working by default.

## How it works

- The queue lives at `workspace/skills-data/carry-over/queue.json`.
- `add` appends an item with status `pending`, a UTC timestamp, and a
  priority (`urgent`, `normal`, `curious`, `simmering`).
- At session start, bootstrap reads pending items into the system prompt,
  sorted by priority then age. `simmering` items auto-promote to `normal`
  after 3 days.
- Items stay pending until somebody marks them done. Nothing about the
  agent replying, or the session ending, removes an item.
- Resolved items stay in queue.json with status `done` as a log; future
  reads filter them out.

## Marking things done

Say so in conversation and the agent should do it: "that's handled",
"I sorted the API key", "done with that one". It runs the skill's `done`
action against the numbers its `list` prints.

If it does not, ask it directly to run the carry-over `done` action, or
edit `queue.json` and change the item's `"status"` to `"done"`.

To wipe the list in one go, ask for `clear` (marks everything done) or
`clear --all` (empties the file, including the resolved log).

## What changed, and why

Items used to be marked delivered as soon as the agent replied for the
first time in a session. That made the queue an outbox rather than a list:
anything you did not act on immediately vanished from the next session's
prompt, so the one case the feature exists for, a follow-up nobody has got
to yet, was the case it dropped. Nothing marks an item done now except you
or the agent saying so.

The cost is that the list needs tending. An item nobody resolves is in
every prompt from now on, which is the point, but a list of thirty stale
items is a bill you pay on every turn. `list` and `clear` are how you keep
it honest.

## Notes

- Carry-over content is always loaded as untrusted content. There is no
  trust graduation.
- A repeatedly failing bootstrap shows the same items on every attempt.
  That is intentional: nothing is lost to a crash.
- `done` takes positions from the current `list` output, not stable ids, so
  the two belong in the same turn. Adding an item renumbers the rest.

## Maintenance

The resolved log accumulates in queue.json. If the file grows large, ask
the agent to run `clear --all`, or edit the file directly. The queue is
disposable; deleting it resets the skill.
