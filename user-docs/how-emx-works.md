# How emx works

- **Its own tmux.** emx keeps every session in its own private tmux server, named by a socket (`emx`, set with `EMX_SOCKET`). It does not touch your own tmux. The panel is one pane in it, your session another.
- **Sessions outlive the panel.** Detaching with `q`, closing the terminal, or the panel restarting for an update leaves your Claude sessions running.
- **Hooks report state.** Each session runs with hooks that tell emx when it starts working, stops, or asks you something. That is how the panel knows what each one is doing.
- **Where things live.** Your choices (side, width, mode, pins, favourite folders) are in a prefs file, `prefs.json` in `emx` under `$XDG_CONFIG_HOME`, else `~/.config/emx`. emx's own state (session records and hook reports) is in `emx` under `$XDG_STATE_HOME`, else `~/.local/state`.
- **Installed copy.** An installed emx lives in `~/.local/share/emx`, with one previous version kept; see `emx docs update-and-uninstall`.
