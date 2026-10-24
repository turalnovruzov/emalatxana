# Phone

Two ways to use emx from a phone. The **phone page** is a web page with your sessions, to check on them and to start, rename and close them; the Claude app does the work in a session. The **terminal layout** is emx itself over SSH, one screen at a time.

## The phone page

emx serves a page in your browser that lists your sessions as the panel does, in the same colours, grouped by repository with Pinned on top, one row each: the state and a short line about what it is doing, and how long ago it started.

- Each row looks as the panel's does: the same mark, colours, state word and line about what it is doing. The Working mark moves through the panel's frames (it stays still when your phone asks for reduced motion). A stopped session is one dim line.
- Tap a row to open that session in the Claude app (the session needs Claude Code's Remote Control on). A stopped row resumes on tap (below), and a running row with no link yet opens the menu instead.
- Tap a stopped row to resume it, as Enter does in the panel: it reads `resuming…`, then shows its state. `Resume` is also in the menu of any session that is not running.
- A session in a git worktree shows `⎇ <branch>` after its name, and sits under its repository, as in the panel.
- Tap a folder's name to fold it or unfold it. A folded folder shows how many sessions it holds. Folds are kept in this browser, per device, and do not change the panel.
- Pull down at the top of the list to refresh it. The page also updates by itself every few seconds.
- Tap `⋯` on a row for the menu: open in the Claude app, resume (when it is not running), peek, rename, pin to top or unpin, close. Tap the shaded space outside the menu to close it. Close asks once more: `Tap again to close`, within two seconds.
- `Peek` shows the last part of the session's conversation, as Space does in the panel, and refreshes while it is open.
- `Pin to top` and `Unpin` are the panel's own pins: the Pinned group at the top is the same on the page and in the panel.
- `Rename` has a `Suggest` button that asks Haiku for a name and puts it in the field; only `Save` renames. Tap it again for another.
- `⋯` on a folder's header has `New session here` and `Close N sessions`. Close asks once more, within two seconds. Pinned has no `⋯`, and is never closed as a folder.
- The field at the top finds sessions by name or folder, as `^f` does in the panel; every group opens while you find, and the `✕` clears it.
- `+ New` starts a session. Type any part of a folder's name and the matching folders appear, those your sessions use first; tap one, then `Start`. Favourite folders, set under `c` in the panel, are listed first. The name and the first message are optional. The new session is shown selected in the list, and its folder becomes a recent one.
- The `Notifications` row at the bottom lists the panel's past needs-input and done notices, newest first, unread ones in colour with their count on the row. Tap one to open that session.
- The `Routines` row at the bottom lists your server's scheduled routines and what they did, read-only. Tap a routine to see each of its runs, its error and when it is next due.
- After emx updates, the page says `emx updated to X.` with what changed since the version this device last saw, and `emx X available: run emx update` when a new major version is waiting. The page reloads itself once to run the new version.
- A session waiting at Claude's own question about trusting a new folder shows as needs input, "waiting to trust this folder: answer it in a terminal". The page cannot answer that question.

The page acts as an app: no pinch or double-tap zoom, no zoom when you tap a field, and the page does not shift or bounce; only the list and the menus scroll. To use it full screen, open it in Safari, tap Share, then Add to Home Screen, and open it from the new `emx` icon (leave "Open as Web App" on). The icon is Safari's picture of the page; emx has no icon of its own yet.

### Set it up once

The page is reachable over Tailscale only, so it needs no password and opens nothing to the internet.

1. On the machine that runs your emx, install Tailscale and connect it yourself: `sudo tailscale up`. emx never installs or changes Tailscale.
2. On your iPhone, install the Tailscale app and sign in as the same user.
3. On the machine, run `emx phone` for the address (for example `http://machine.your-tailnet.ts.net:8787/`) and open it in Safari. Safari says "Not Secure" because the address is plain http; the Tailscale tunnel already encrypts it.
4. If a firewall such as `ufw` is on, let traffic in on the Tailscale interface: `sudo ufw allow in on tailscale0`.

The page is served by the emx workspace itself, so it is up whenever your emx is, and again after emx updates itself. If Tailscale is missing or not connected, the panel says so once, in one line, and `emx phone` says it again. Set `phone_port` in the config file (`c` in the panel, Port) to use another port than 8787, at the next start.

### Who is let in

Only your own devices. emx listens on the machine's Tailscale address and nowhere else, and asks Tailscale who each request comes from: the device must belong to the same Tailscale user as this machine, or it gets a refusal and sees nothing. Another person you share this machine with on a tailnet is refused. A machine that is a tagged Tailscale node is refused as a whole, since emx cannot tell whose it is. Requests from the machine itself must come from your own user.

## The terminal layout: connect

On a phone, emx shows one thing at a time: the panel, or the session you opened, each across the whole screen.

Open your SSH app on the phone (Blink or Termius, for example), connect to the server that runs your emx, and run `emx` there. The phone gets a view of its own, so your laptop keeps its own panel, open session and width while the phone is on (`emx docs how-emx-works`). Nothing else needs to be set up: emx uses the SSH connection you already have and opens nothing else to the outside.

A screen narrower than 70 columns counts as a phone. Turn the phone sideways and, once the screen is 70 columns or wider, emx lays out the panel and the session side by side, as on the laptop.

## The panel

The panel fills the screen. The bar at the bottom shows the counts by state and the keys for what the cursor is on, for example `↵ open  n new  a actions  ?`.

- A tap on a row selects it. A tap on the row already selected opens its actions.
- `↑` and `↓` (or `j` and `k`) move, as on the laptop.
- `?` shows the keys that work on a phone; `↑` and `↓` scroll it, any other key closes it.

Every panel key from the laptop works, Ctrl keys included. `c` there hides Side and Width, which mean nothing when the panel fills the screen.

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
