# Routines

A routine is something on a machine that runs Claude by itself: a systemd user timer, or a long-running systemd user service you made. `R` in the panel opens a view of what the routines of every place did today and yesterday, and `Enter` on a run opens the Claude session behind it, read-only. The mouse wheel scrolls the view, and a click on a place's tab or on a run works as `Tab` and the arrow keys do. `R` or `Esc` goes back to the session list. The phone page has the same view (`emx docs phone`).

emx only reads. It never starts, stops or changes a routine, never sends anything to a session, and never resumes one.

## Places

A place is a machine and a user on it. The view shows one place at a time, as a row of tabs under the title; `Tab` goes to the next place and `Shift+Tab` to the one before. `this user`, this machine as you, is always first. Add, change and remove the others in Settings: `c`, then Places, `Enter`. `a` (or `+ add a place`) asks three things, one at a time: the **name** (letters, digits, dots, dashes and underscores, for example `worker`), the **server** (as you would type it after `ssh`, for example `srv2`; leave empty for this machine) and the **user** (a login on that machine, for example `bot`; leave empty for yourself). A wrong answer shows why and waits. At least one of server and user is needed. `Enter` on a place changes it (it keeps its tab and its line in the file), and `x` twice removes it. Each change is saved into the config file at once.

The same places are one line each in the config file (`emx config`, or `e` in Settings), if you would rather write them there:

```
place = worker user bot
place = server-2 server srv2
place = far server srv2 user bot
```

The four combinations:

- **This machine as you:** built in, named `this user`. No line.
- **This machine as another user:** `place = worker user bot`. emx reaches that user with `place_user_command`, `sudo -n -u` by default, so the login follows it. It never asks for a password: the account needs passwordless sudo for that, or the place shows an error line.
- **Another server as your login:** `place = server-2 server srv2`. emx uses `ssh` with the server's name or address as you would type it; the key must work without a prompt.
- **Another server as another user there:** `place = far server srv2 user bot`.

Moving a routine to another server is a one-line change: edit that line (or add one), and the routine shows under the new place. A running emx follows the edit within seconds. `place_user_command` is for a different way to switch user, for example `doas -u`.

A place that can't be reached shows one red line under its tab, and the other places still show. A line that can't be read is skipped and the panel's notice names it.

## Which routines show

- Every systemd user timer of the place that has a unit file, loaded or not, in name order. A timer that is switched off shows too, tagged `timer off`. Templates (`name@.timer`) are not shown.
- Every long-running user service you made (its unit file is under the user's `~/.config/systemd/user/`) that started at least one Claude session in the past two days. Each stretch of back-to-back sessions is one run.

emx reads them from systemd when the view opens and again every minute while it is open. To see the same set yourself:

```
systemctl --user list-timers --all
systemctl --user list-unit-files --type=timer
```

## The top of the view

Under the tab row, `⚠ 1 failed · worker, 19:42` counts every routine, in any place, whose latest run failed or hit the usage limit, whichever tab you are on. With one problem it names the place and the time; with several it names the places. Nothing shows there when all is fine.

## What each routine shows

- A line with the routine's name and, on the right, the newest thing that happened: `passed`, `failed · exit 2`, `running`, `missed`, `unknown`, `not due`, `timer off` or `not found` for a timer; `working`, `finished`, `failed`, `usage limit`, `between runs` or `stopped` for a service. A service waiting on the usage limit says till when.
- Under it, `today` and `yesterday`, with every run in the server's local time. A timer's runs show `09:10 ✓` ran and succeeded, `07:10 ✗2` failed with exit status 2, `11:00 … running` still running, `03:10 ⚠ missed` scheduled and not started. A service's runs show how their session ended.
- The selected run is on a row of its own, with the title of its session and `2 sessions  ↵ read`. Under a run with several sessions, each is listed with how it ended.
- Last, `next` and when it is next due, for a timer.

How a Claude session ended comes from the session itself: `finished` when Claude's last turn was a reply, `usage limit` when it stopped on the usage limit, `failed` when it ended in an error or stopped mid-step and nothing is running any more, and `working` while it is still going (its last line less than five minutes old). A timer's pass or fail comes from systemd's exit code.

A routine whose runs started no Claude session says `no Claude session · a plain script`.

`not due` means nothing was scheduled in the two days and nothing ran. A timer that is stopped or switched off, or one with no calendar schedule, shows its runs and no missed times. Missed times come from the journal, checked against systemd's own record of the routine's last start; when the journal holds nothing for a routine but systemd says it started, the routine shows that run and no missed times. When the journal can't be read, each routine shows the journal's error in red under its name.

## Choosing a run and reading it

| Key | What it does |
| --- | --- |
| `Tab` `Shift+Tab` | Next place, the one before. |
| `↑↓` `j` `k` | Select a run. |
| `Enter` | Read the selected run's Claude session. |
| `click` | A place's tab switches to it, a run selects it, and a click on the selected run reads it. |
| `wheel` | Scrolls the view. |
| `R` `Esc` | Back to the session list. |

`Enter` puts the session in the big pane, next to the panel, opened at the end: your prompt for the run, Claude's replies, one line per tool call with `✓` or `✗ exit 1`, and errors in red. The last line says where it stopped, for example `■ stopped here · 19:55 · the run ended: usage limit`. A run with no session says `No Claude session behind that run.`

| Key in the reader | What it does |
| --- | --- |
| `↑↓` `j` `k` | Scroll a line. |
| `PgUp` `PgDn` | Scroll a page. |
| `g` `G` | Top, end. |
| `Tab` | The run's other session, when it has more than one. |
| `wheel` | Scrolls three lines at a time. |
| `click` | On `session 2 of 2` in the header, goes to the run's next session, as `Tab` does. |
| `Esc` | Back to the list; the session you had open before comes back. |

Every other key does nothing: the reader can't resume, answer or stop the session. A session still going refreshes every few seconds at this machine and as another user; over `server`, it is read when you open it and when you press `Tab`. Only the last 8 MB of a long session are read.

## What emx reads

At a place, only the Claude session files of that user (`projects/<folder>/<id>.jsonl`, never the subagent files, credentials or settings) and systemd's own output: unit lists, properties, the journal and the calendar. emx runs a small read-only script there that needs `python3` on that machine; without it the place shows an error line.

## When there is nothing to show

With no routines in a place, the view says "No routines here: no systemd user timers, and no user service that started Claude."

## On a machine without systemd

On a Mac, for example, this place says: "Routines are read from systemd, and this place has none."

## Investigating a failure

Select the failed run and press `Enter`: the end of its session usually says what went wrong. For a run with no session, ask your agent to look at the routine's unit and its journal.
