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
| `emx config` | Opens the settings file in your editor (`$VISUAL`, `$EDITOR`, else `editor`, `nano` or `vi`), or prints its path when none is found. |
| `emx update` | Installs the newest version now, a new major version included. |
| `emx uninstall` | Removes the installed emx and its link; settings and state are kept. |

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
| `wheel` | Scrolls the list, Notifications, Routines or the key help; the selection stays and `↑↓` brings it back. A thin bar at the right edge shows where you are while there is more than fits. |
| `Enter` `click` | Fold a group; a click opens a session, and a click on a line of the Unread box opens that session. |
| `space` | Peek at what the session asks or did; `↑↓` next, `space` or `Esc` closes. |
| `n` | New session where the cursor is; `Tab` there picks another folder. |
| `^x` | Interrupt a working session; twice closes it. On a project header, twice closes the group. |
| `^f` `/` | Find a session by name or folder. |
| `^t` | Pin to the top. |
| `^r` | Rename, via `/rename` in the session; `Tab` there suggests a name. |
| `c` | Settings: side, width, view, mode, colours, updates and more, in a list that edits the config file; `c` or `Esc` closes it. |
| `N` | Notifications: the last 20; `Enter` opens one. |
| `R` | Routines of every place, today and yesterday; `R` or `Esc` goes back. Keys below. |
| `h` | Stop the voice now (voice mode). |
| `^\` | Jump to the session and back. |
| `^q` | Hide the panel. |
| `q` | Detach; sessions keep running. |
| `?` | Show or close the key help. |

### On a phone

On a screen narrower than 70 columns (`emx docs phone`) the panel and the session each fill the screen, and these keys work besides the panel's own.

| Key | What it does |
| --- | --- |
| `↵` `Enter` | Open in the Claude app when the session has a Remote Control link: the link goes on screen and into the phone's clipboard. With no link, open it here. On a header, fold. |
| `t` `→` | Open the session here, in the terminal. |
| `a` | The session's actions: `↵` open in the Claude app, `t` open here, `␣` peek, `r` rename, `p` pin or unpin, `i` interrupt, `x` close (`x` again to confirm). `Esc` closes them. |
| `click` | A tap selects a row; a tap on the selected row opens its actions. |
| `‹` | `‹ sessions` in the bar, over a session: a tap goes back to the panel, as do `←` on an empty prompt, `^\` and `^q`. |
| `?` | The phone's key help; `↑↓` scroll it. |

### In the settings menu

| Key | What it does |
| --- | --- |
| `↑↓` `j` `k` | Move over the settings. |
| `←→` | Change the selected setting: the other side, a narrower or wider panel, the next or previous choice. |
| `Enter` | Type a value (a number or a command; `Enter` saves, `Esc` cancels), or open Favourites or Places. |
| `e` | Open the config file in your editor (`$VISUAL`, `$EDITOR`, else `editor`, `nano` or `vi`), in a popup. With none found, the notice gives the file's path. |
| `x` | In Favourites, remove the selected folder; `+ add a folder` takes a typed folder. In Places, `x` twice removes the selected place. |
| `a` | In Places, add a place: name, server, then user, one question each. `Enter` on a place changes it. |
| `Esc` `c` | Close the menu (`Esc` in Favourites or Places goes back to the list). |

Every change is saved into the config file.

## In the Routines view

Opened with `R`. Details in `emx docs routines`.

| Key | What it does |
| --- | --- |
| `Tab` `Shift+Tab` | Next place, the one before. |
| `↑↓` `j` `k` | Select a run. |
| `Enter` | Read the selected run's Claude session in the big pane. |
| `click` | A place's tab switches to it, a run selects it, a click on the selected run reads it. |
| `wheel` | Scrolls the view. |
| `R` `Esc` | Back to the session list. |

## In the reader

Opened with `Enter` on a run; it only reads.

| Key | What it does |
| --- | --- |
| `↑↓` `j` `k` | Scroll a line. |
| `PgUp` `PgDn` | Scroll a page. |
| `g` `G` | Top, end. |
| `Tab` | The run's other session. |
| `wheel` | Scrolls three lines at a time. |
| `click` | On `session 2 of 2` in the header, the run's next session. |
| `Esc` | Back to the list. |

## Settings

Environment variables, set before running `emx`. The settings that change how emx works are in the config file instead (see below).

| Setting | What it does |
| --- | --- |
| `EMX_SOCKET` | The tmux socket name (default `emx`). Not empty and not `default`. |
| `EMX_SAY` | Replaces the `say` command that speaks. |
| `EMX_NOTIFY` | Replaces the notification helper's command. |
| `EMX_NOTIFY_HOME` | The folder the notification helper is built in. |
| `EMX_OPEN` | Replaces the `open` command. |
| `EMX_CLIPBOARD_IMAGE` | The command `emx remote` reads the laptop clipboard's image with (default `osascript`); it is given a file path to write a PNG to. |
| `EMX_SSH` | Replaces `ssh` for `emx remote`. |
| `EMX_HOME` | The folder emx installs into (default `$XDG_DATA_HOME/emx`, else `~/.local/share/emx`). |
| `EMX_BIN_HOME` | The folder of the `emx` link (default `~/.local/bin`). |

## Colour schemes

Set with Colours under `c` (Look); a running emx switches within seconds, borders and bottom bar included. emx never switches scheme by itself.

- Terminal's own (the default): your terminal's own 16 colours, so it follows its theme. The selected row is a shade of your terminal's background (a little lighter on a dark theme, a little darker on a light one) and keeps its colours; emx asks the terminal for the background, again each time you come back to the panel, so a theme switch shows then; where the terminal doesn't say, the row is inverted. A thin grey line sits just above the bottom bar.
- Dark: emx's own colours, for dark terminals.
- Light: emx's own colours, for white or cream terminals.

## The config file

Your settings are in one commented file, `config` under `emx` in `$XDG_CONFIG_HOME`, else `~/.config/emx`. `emx config` opens it, and `c` in the panel is the same settings as a menu (`e` there opens the file). emx writes it the first time it runs, carrying over what an older `prefs.json` held.

A line is `key = value`; a line starting with `#` is a comment, and `## Name` lines group the settings. There are no comments after a value: a `#` there is part of the value. Favourite folders are one `favourite_folders` line per folder. A running emx follows an edit within seconds. A line emx can't read is skipped, the default is used instead, and the panel's notice line names the line and why; emx never rewrites a line you wrote unless you change that setting from the menu.

| Key | Values | Default | What it does |
| --- | --- | --- | --- |
| `side` | `left`, `right` | `right` | Which side of the window the panel sits on. |
| `panel_width` | 16 to 500 | `40` | The panel's width in columns. |
| `view` | `project`, `state` | `project` | How sessions are grouped. |
| `mode` | `voice`, `notification` | `notification` | Spoken notices, or macOS notifications. |
| `scheme` | `terminal`, `dark`, `light` | `terminal` | The colour scheme. |
| `updates` | `on`, `off` | `on` | Whether emx installs new versions by itself. |
| `update_check_seconds` | 5 or more | `600` | How often to check for a new version. |
| `phone_port` | 1024 to 65535 | `8787` | The port of the phone page. Applies at the next start. |
| `favourite_folders` | a folder, once per line | none | Starred folders for new sessions. |
| `place` | a name, then `server <host>` and/or `user <login>`, once per line | none | Other places whose routines `R` shows. Also set in Settings, under Places. |
| `place_user_command` | a command | `sudo -n -u` | The command that runs emx's reads as another user; the login follows it. |
| `claude_command` | a command | `claude` | The command sessions start claude with. Applies at the next start. |
| `remote_command` | a command | `emx` | emx's command on the server, for `emx remote`. |

A path or command with a line break or other control character is refused.

Pinned sessions, recent folders, the last folder, folded groups, the hidden panel and the last version seen are not settings; emx keeps them in its own state, `remembered.json` in `emx` under `$XDG_STATE_HOME`, else `~/.local/state`. `prefs.json` is read only when the config file doesn't exist (the first run, or after you delete the config), carrying its values over again, and is never changed or removed.
