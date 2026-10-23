# Getting started

## What emx needs

- tmux and Python 3.
- `claude` (Claude Code), installed and logged in.
- A Mac or Linux. Notifications that appear on screen are for a Mac.

## Install

Clone the emx repo to a temporary folder and run its `install.sh`:

```
git clone <the emx repo url> /tmp/emx-src
sh /tmp/emx-src/install.sh
```

It puts emx in `~/.local/share/emx` and the `emx` command in `~/.local/bin`. If that folder is not on your PATH, the installer prints the one line to add; it changes none of your shell files. It uses git and your own GitHub login. You can delete the temporary clone afterwards.

## First run

Run `emx`. It opens the workspace: the panel on one side, the session you are in on the other.

If your terminal has a light background, run `emx colors light`; emx doesn't detect it.

## The panel at a glance

The panel lists your sessions, grouped by project. Each shows its name and age, and under the name what it is doing, in full: a long status wraps onto as many lines as it needs. The row under the cursor is the one the keys act on. Press `?` in the panel to see every key.

The colours say the state at a glance (shown here in the default Dark scheme): Needs input is yellow, Working is grey, Done is green, Idle (interrupted, waiting for you) is lavender, and Stopped is dim.

One bar runs across the bottom of the whole window. It shows emx and its version, the latest notification, a count for each state that has sessions, in that state's colour (Stopped sessions are not counted), and the key hints. The hints follow the cursor: they list the keys that act on the session or header it is on, and they change with the session's state. `N all` next to the notification opens all of them, and `?` shows every key. The selected row also repeats its likeliest key, dimmed, at the right end of its last line. The panel itself is just your sessions, on your terminal's own background.

## Start your first session

1. In the panel press `n`.
2. With no sessions yet, emx asks for a folder first: pick one from the list with `↑` and `↓` (type to filter) and press `Enter`.
3. Type a message for Claude, or leave it empty to start an empty session, then press `Enter`.
4. The session opens on the other side. Type to Claude as usual.

More about starting sessions is in `emx docs sessions`.

## Leave and come back

Press `q` in the panel to detach. Your sessions keep running. Run `emx` again to come back to them.
