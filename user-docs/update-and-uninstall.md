# Update and uninstall

## Automatic updates

An installed emx looks for a newer released version every 10 minutes and fetches it in the background. At a quiet moment (nobody attached, or no key pressed for 3 minutes, and nothing open in the panel) the panel switches to it by itself. Your sessions keep running untouched. After a switch the panel says "emx updated to X." with what is new.

A new major version (say 2.x to 3.0) is fetched but waits for you: the panel says it is available, and `emx update` installs it.

## Update now

`emx update` installs the newest version now, a new major version included. The running panel switches to it at a quiet moment.

## Turn automatic updates off or on

Automatic updates under `c` (Updates), or `updates = off` in the config file, stops the automatic updates; `emx update` still updates when you run it. `updates = on` starts them again.

## Uninstall

`emx uninstall` removes the installed emx and its link. Your settings and emx's state are kept.

## A checkout never updates itself

An emx run from a project folder (a git checkout) never updates itself, and `emx update` there says so. Only an installed emx (see `emx docs getting-started`) updates.

## Older version

One previous version stays on disk next to the current one; older ones are deleted.
