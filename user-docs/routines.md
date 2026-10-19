# Routines

If your server runs routines as systemd user timers, `R` in the panel opens a view of what they did in the past 24 hours. `R` or `Esc` goes back to the session list. Through `emx remote` you see the server's routines (`emx docs remote`).

The view only reads. It never starts, stops or changes a routine, and it does not open a run or read what a run printed. Pass or fail comes from the exit code alone.

## Which routines show

Every systemd user timer on the machine that has a unit file, loaded or not, in name order. A timer that is switched off and not loaded shows too, tagged `timer off`. Templates (`name@.timer`) are not shown. There is nothing to list: emx reads them from systemd when the view opens and again every minute while it is open, so a new timer appears and a removed one disappears on the next read. A `routines` file from an earlier version is ignored; you can delete it. To see the same set yourself:

```
systemctl --user list-timers --all
systemctl --user list-unit-files --type=timer
```

## What each routine shows

- A line with the routine's name and, on the right, the newest thing that happened: `passed`, `failed · exit 2`, `running`, `missed`, `unknown`, `not due`, `timer off` or `not found`.
- Under it, every run in the past 24 hours, newest first, in the server's local time: `09:10 ✓` ran and succeeded, `07:10 ✗2` failed with exit status 2, `11:00 … running` still running, `03:10 ⚠ missed` was scheduled and no run started before the next scheduled time.
- Last, `next` and when it is next due, shown as a day and time when that is more than a day away.

`not due` means nothing was scheduled in the past 24 hours and nothing ran. A timer that is stopped or switched off, or one with no calendar schedule, shows its runs and no missed times.

Missed times come from the journal, checked against systemd's own record of the routine's last start. A routine that was scheduled and has no run in the past 24 hours shows each of those times as `⚠ missed`. When the journal holds nothing for a routine but systemd says it started in the past 24 hours (the journal may have been rotated away), the routine shows that run and no missed times.

When the switched-off timers can't be read, a red line says so above the other routines, which still show.

When the journal can't be read, each routine shows the journal's error in red under its name, its latest run from systemd's own record, and no missed times; `unknown` if it was due and emx can see no run at all.

## When there is nothing to show

With no user timers, the view says "No systemd user timers on this machine."

## On a machine without systemd

On a Mac, for example, the view says: "Routines are read from systemd user timers, and this machine has none."

## Investigating a failure

emx does not go into a run. Ask your agent to look at the routine's unit and its journal.
