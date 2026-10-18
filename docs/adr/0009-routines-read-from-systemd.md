# 0009. The routines view reads systemd user timers with systemd's own tools, read-only

- **Status**: Accepted
- **Date**: 2026-10-18

## Context

Issue #51. Tural wanted a separate view of the routines that run on his server, showing which ran, which failed and which did not run when they should have, and said emx "should just be showing it". He then moved the work to the server: *"build. we have switched to the server."* The routines there are systemd user timers.

## Decision

emx calls the OS's own tools as subprocesses, off the curses thread, and only ever to read:

- `systemctl --user show` for each listed timer and the service it names (`Unit=`): state, schedule, next time, last run.
- `journalctl --user -o json` for the past 24 hours of those services. A run's outcome is read from systemd's own lifecycle messages, picked by message id and grouped by invocation id; what a script printed is never read.
- `systemd-analyze calendar` for the scheduled times in the window, so a time with no run can be named.

emx never runs a verb that changes a unit (`start`, `stop`, `reset-failed`) and keeps no history of its own. Which routines show comes from a hand-edited file beside `prefs.json`. A machine without `systemctl` (the Mac) gets one line saying there is nothing to read.

No library or service is added: these are system tools, like tmux, ssh and git.

## Consequences

- Only systemd user timers are read; launchd and cron are not.
- Runs older than the user journal holds, or lost when the journal is not persistent, fall back to the service's latest run.
- A routine due more than about once a minute gets no missed-time warnings (the calendar is capped).
- `emx remote` shows the server's view with no extra work, because it runs the server's own panel ([0007](./0007-emx-reaches-a-server-over-ssh.md)).

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **systemd's own tools, read-only** (this record) | The OS already holds the state and the log; nothing to keep or sync | Linux with systemd only | **Chosen** |
| emx keeps its own run history | Works on any OS | A store to maintain, and a second truth beside the OS's | Rejected in #51 Decided 3 |
| Reading each routine's script output | Richer detail | Pass or fail is by exit code alone | Rejected in #51 Decided 2 |
