# Reference

Every command, key and setting. Each is written as a code span so a test can check this page against emx itself.

## Commands

| Command | What it does |
| --- | --- |
| `emx` | Opens the workspace, or reattaches if it is running, first moving a running workspace onto this version. |
| `emx --help` | Prints the usage text. |
| `emx --version` | Prints the version. |
| `emx docs` | Prints the docs index. |
| `emx docs <page>` | Prints one docs page, for example `emx docs reference`. |
| `emx hush` | Stops the voice now, without muting later notices. |
| `emx remote` | `emx remote <host>` opens the emx running on a server over ssh (host from your SSH config); closing it leaves the server's sessions running. |
| `emx phone` | Prints the address of the phone page, or why it isn't up. |
| `emx update` | Installs the newest version now, a new major version included. |
| `emx update off` | Stops the automatic updates. |
| `emx update on` | Starts the automatic updates again. |
| `emx uninstall` | Removes the installed emx and its link; prefs and state are kept. |

## Keys

### Anywhere in emx

| Key | What it does |
| --- | --- |
| `^\` | Jump to the session and back to the panel. |
| `^q` | Hide the panel; press again to bring it back. |
| `←` | From an empty prompt in a session, back to the panel. |
| `^v` | On `emx remote`, pastes the image on your laptop's clipboard into the session as a file path; elsewhere it reaches the session as usual. |

### In the panel

| Key | What it does |
| --- | --- |
| `→` `Enter` | Open the session and go to it. |
| `←` | Back here, from an empty prompt. |
| `↑↓` `j` `k` | Move. |
| `Enter` `click` | Fold a group; a click opens a session. |
| `space` | Peek at what the session asks or did; `↑↓` next, `space` or `Esc` closes. |
| `n` | New session where the cursor is; `Tab` there picks another folder. |
| `^x` | Interrupt a working session; twice closes it. On a project header, twice closes the group. |
| `^f` `/` | Find a session by name or folder. |
| `^t` | Pin to the top. In the folder list it stars a favourite instead. |
| `^r` | Rename, via `/rename` in the session; `Tab` there suggests a name. |
| `^s` | Switch between by project and by state. |
| `N` | Notifications: the last 20; `Enter` opens one. |
| `R` | Routines: what ran in the past 24 hours; `R` or `Esc` goes back. |
| `m` | Switch between voice mode and notification mode. |
| `h` | Stop the voice now (voice mode). |
| `s` | Move the panel to the other side. |
| `<` `>` | Narrower / wider panel (`,` and `.` do the same). |
| `^\` | Jump to the session and back. |
| `^q` | Hide the panel. |
| `q` | Detach; sessions keep running. |
| `?` | Show or close the key help. |

### On a phone

On a screen narrower than 70 columns (`emx docs phone`) the panel and the session each fill the screen, and these keys work besides the panel's own (all but `s`, `<` and `>`).

| Key | What it does |
| --- | --- |
| `↵` `Enter` | Open in the Claude app when the session has a Remote Control link: the link goes on screen and into the phone's clipboard. With no link, open it here. On a header, fold. |
| `t` `→` | Open the session here, in the terminal. |
| `a` | The session's actions: `↵` open in the Claude app, `t` open here, `␣` peek, `r` rename, `p` pin or unpin, `i` interrupt, `x` close (`x` again to confirm). `Esc` closes them. |
| `click` | A tap selects a row; a tap on the selected row opens its actions. |
| `‹` | `‹ sessions` in the bar, over a session: a tap goes back to the panel, as do `←` on an empty prompt, `^\` and `^q`. |
| `?` | The phone's key help; `↑↓` scroll it. |

## Settings

Environment variables, set before running `emx`.

| Setting | What it does |
| --- | --- |
| `EMX_SOCKET` | The tmux socket name (default `emx`). Not empty and not `default`. |
| `EMX_CLAUDE` | The claude command (default `claude`). |
| `EMX_SAY` | Replaces the `say` command that speaks. |
| `EMX_NOTIFY` | Replaces the notification helper's command. |
| `EMX_NOTIFY_HOME` | The folder the notification helper is built in. |
| `EMX_OPEN` | Replaces the `open` command. |
| `EMX_PHONE_PORT` | The port of the phone page (default 8787; a number from 1024 to 65535). |
| `EMX_REMOTE_COMMAND` | emx's command on the server for `emx remote` (default `emx`, run through its login shell). |
| `EMX_CLIPBOARD_IMAGE` | The command `emx remote` reads the laptop clipboard's image with (default `osascript`); it is given a file path to write a PNG to. |
| `EMX_SSH` | Replaces `ssh` for `emx remote`. |
| `EMX_HOME` | The folder emx installs into (default `$XDG_DATA_HOME/emx`, else `~/.local/share/emx`). |
| `EMX_BIN_HOME` | The folder of the `emx` link (default `~/.local/bin`). |
| `EMX_UPDATE_SECONDS` | How often a running panel checks for updates (default 600). |

## The prefs file

Your choices are saved in `prefs.json` under `emx` in `$XDG_CONFIG_HOME`, else `~/.config/emx`: panel side and width, voice or notification mode, view, pinned sessions, and favourite and recent folders. Whether automatic updates are on is kept in the install folder instead (`emx update off` and `emx update on`). emx's own state is in `emx` under `$XDG_STATE_HOME`, else `~/.local/state`.
