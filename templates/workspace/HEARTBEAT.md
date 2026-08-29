# Heartbeat

Checks evaluated on every heartbeat tick. A line that says to report
something fires every time; the rest are conditions to watch for. The
heartbeat sees only this file and the current time and has no tools, so
only add checks answerable from those. Anything that needs a tool or
the web (air quality, weather, a price, an inbox) is a cron job, not a
line here; timed tasks belong in cron too, and staleness checks
(missed morning, old carry-over items) are already covered by the
watchdog. Keep this file short: every line costs tokens on every tick.

No checks are configured yet: respond with exactly NO_REPLY.
(Delete the line above when you add your first check.)
