# Routines

If your server runs routines as systemd user timers, `R` in the panel opens a view of what they did in the past 24 hours. `R` or `Esc` goes back to the session list. Through `emx remote` you see the server's routines (`emx docs remote`).

The view only reads. It never starts, stops or changes a routine, and it does not open a run or read what a run printed. Pass or fail comes from the exit code alone.

## List your routines

emx shows the routines you list, in a file called `routines` beside the prefs file (`emx docs reference`), by default `~/.config/emx/routines`. One timer name per line; blank lines and lines starting with `#` are ignored, and `.timer` is optional. To find the names on the server:

```
systemctl --user list-timers --all
```

The file is read each time the view opens and again every minute while it is open.

## What each routine shows

- A line with the routine's name and, on the right, the newest thing that happened: `passed`, `failed · exit 2`, `running`, `missed`, `not due`, `timer off` or `not found`.
- Under it, every run in the past 24 hours, newest first, in the server's local time: `09:10 ✓` ran and succeeded, `07:10 ✗2` failed with exit status 2, `11:00 … running` still running, `03:10 ⚠ missed` was scheduled and no run started before the next scheduled time.
- Last, `next` and when it is next due, shown as a day and time when that is more than a day away.

`not due` means nothing was scheduled in the past 24 hours and nothing ran. A timer that is stopped, or one with no calendar schedule, shows its runs and no missed times.

## On a machine without systemd

On a Mac, for example, the view says: "Routines are read from systemd user timers, and this machine has none."

## Investigating a failure

emx does not go into a run. Ask your agent to look at the routine's unit and its journal.
