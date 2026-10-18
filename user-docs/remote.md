# Remote

`emx remote <host>` opens the emx running on a server, in your terminal, over ssh. Your sessions there keep running on the server.

## Use it

```
emx remote myserver
```

`myserver` is a name from your own SSH config. You see the server's panel and its sessions, and everything in `emx docs sessions` works there.

## What the server needs

- emx, tmux, Python 3 and `claude` installed, and `claude` logged in.
- To keep your sessions running after you disconnect, a Linux server must not end your programs at logout. On a server with `KillUserProcesses=yes`, `loginctl enable-linger` alone is not enough: turn that setting off, or exclude your user from it. Ubuntu and Debian leave it off.

## Closing and coming back

Closing the window, closing the laptop or losing the connection leaves the server's emx and its sessions running. Run `emx remote myserver` again to come back. `q` in the panel leaves them running too. Plain `emx` shows only this machine's sessions.

## Knowing you're on remote

While you are in a server through `emx remote myserver`, the panel's top line shows `⇄ myserver` in violet after the version, and your terminal's title reads `emx · myserver`. Both go when you leave. Someone looking at the server's own screen sees the mark too, while you are connected. An emx older than this feature, on either machine, shows no mark.

## Notifications while connected

The server's notifications reach your laptop, spoken or as emx's notification, in the mode set with `m` on the server's panel. Clicking one opens that session on the server's panel. While you are not connected nothing arrives; the Notifications list (`N`) keeps them. `h` on the server's panel also stops the laptop speaking.

## Versions

If the server's emx is a different version, `emx remote` says so before it connects and again when you leave. If a newer major version is waiting on either machine, or the two majors differ, and you are at a terminal, `emx remote` asks `Update emx on <host> and this laptop to X? [y/N]`; answer `y` and it updates both. When it connects it also asks the server's emx to update itself, and tells you if it did or why it could not.

## If the server cannot find emx

Set `EMX_REMOTE_COMMAND` to emx's full path on the server. `EMX_SSH` replaces the `ssh` command.

## Docs of the server's version

`emx remote` runs the server's own emx, so the `emx docs` you read there are the server's version of them.
