# How emx works

- **Its own tmux.** emx keeps every session in its own private tmux server, named by a socket (`emx`, set with `EMX_SOCKET`). It does not touch your own tmux. The panel is one pane in it; beside it, the session you opened.
- **A view per device.** Each terminal that opens emx gets its own view: its own panel, its own open session, its own hidden or shown panel and its own width. Your laptop, a phone and a second terminal can be attached at once and nothing one does moves the others. The same session open on two devices at once has one width, that of the device you used last. A phone or another terminal that goes away stops showing its session, and shows it again when it comes back; your laptop's view keeps its session on screen while you are away, and takes the session's width back when you return.
- **Shared between devices:** the side, pins, favourite folders, recent folders, the list view and folded groups, and voice or notification mode. The saved panel width and hidden panel are your laptop's first terminal's.
- **Sessions outlive the panel.** Detaching with `q`, closing the terminal, or the panel restarting for an update leaves your Claude sessions running.
- **Hooks report state.** Each session runs with hooks that tell emx when it starts working, stops, or asks you something. That is how the panel knows what each one is doing.
- **Where things live.** Your choices (side, width, mode, pins, favourite folders) are in a prefs file, `prefs.json` in `emx` under `$XDG_CONFIG_HOME`, else `~/.config/emx`. emx's own state (session records and hook reports) is in `emx` under `$XDG_STATE_HOME`, else `~/.local/state`.
- **Installed copy.** An installed emx lives in `~/.local/share/emx`, with one previous version kept; see `emx docs update-and-uninstall`.
