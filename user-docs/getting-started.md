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

## The panel at a glance

The panel lists your sessions, grouped by project, with what each one is doing (working, needs you, done). The row under the cursor is the one the keys act on. Press `?` in the panel to see every key.

## Start your first session

1. In the panel press `n`.
2. Type a message for Claude, or leave it empty to start an empty session, then press `↵`.
3. The session opens on the other side. Type to Claude as usual.

More about starting sessions is in `emx docs sessions`.

## Leave and come back

Press `q` in the panel to detach. Your sessions keep running. Run `emx` again to come back to them.
