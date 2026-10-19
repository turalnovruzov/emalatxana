# Phone

On a phone, emx shows one thing at a time: the panel, or the session you opened, each across the whole screen. For a Claude session it can send you to the Claude app, which suits a phone better than a terminal.

## Connect

Open your SSH app on the phone (Blink or Termius, for example), connect to the server that runs your emx, and run `emx` there. The phone gets a view of its own, so your laptop keeps its own panel, open session and width while the phone is on (`emx docs how-emx-works`). Nothing else needs to be set up: emx uses the SSH connection you already have and opens nothing else to the outside.

A screen narrower than 70 columns counts as a phone. Turn the phone sideways and, once the screen is 70 columns or wider, emx lays out the panel and the session side by side, as on the laptop.

## The panel

The panel fills the screen. The bar at the bottom shows the counts by state and the keys for what the cursor is on, for example `↵ open  n new  a actions  ?`.

- A tap on a row selects it. A tap on the row already selected opens its actions.
- `↑` and `↓` (or `j` and `k`) move, as on the laptop.
- `?` shows the keys that work on a phone; `↑` and `↓` scroll it, any other key closes it.

Every panel key from the laptop works, Ctrl keys included, except the panel's side and width (`s`, `<`, `>`), which mean nothing when the panel fills the screen.

## Open in the Claude app

When the selected session is a Claude session whose Remote Control link emx knows, a third line under it reads `↗ open in the Claude app`, and the bar says `↵ open in app  t here`.

- `↵` (or a tap on that line) shows the link at the bottom of the screen and copies it to the phone's clipboard. Tap the link, or paste it in a browser, to open the session in the Claude app.
- `t` opens the session here, in the terminal, instead.

The link needs Claude Code's Remote Control on for your sessions, which is a setting of yours in Claude Code, not of emx. emx finds the link in the file Claude Code itself keeps for each running session; it reads only that, for its own sessions, and changes nothing of Claude's. With no link known, there is no third line and `↵` opens the session here.

## A session on the phone

Opening a session here shows it across the whole screen, with `‹ sessions` at the left of the bar, then the session's name and state. To go back to the panel, tap `‹ sessions`, or press `←` on an empty prompt (`^\` and `^q` do the same). With the keyboard up the bar shrinks to `‹` and the name.

## Actions

`a` opens a box under the selected session, titled with its name:

| Key | What it does |
| --- | --- |
| `↵` | Open in the Claude app (only when it has a link). |
| `t` | Open here, in the terminal. |
| `␣` | Peek at what it asks or did. |
| `r` | Rename. |
| `p` | Pin to the top, or unpin. |
| `i` | Interrupt it; with nothing to interrupt, emx says so. |
| `x` | Close it. The line then says `x again to close`; press `x` again to close it. |

Press a key or tap a line. `Esc`, or any other key, closes the box. Closing a session ends it in emx; it then shows as offline in the Claude app.

## Start a session

`n` works as on the laptop. When the session starts, the phone stays on the panel with the new session selected, and the bar says `Started <name>.  ↵ open in app` (or `↵ open` until emx knows its link).

## Not tried on a real phone yet

These were checked on a terminal sized like a phone, not on an iPhone:

- whether a tap on the link opens the Claude app in your SSH app, and whether the app passes on the copy to the phone's clipboard;
- whether a tap on `‹ sessions` and on rows reaches emx in your SSH app;
- how your SSH app's key bar sends `Esc`, `↵` and the Ctrl keys.
