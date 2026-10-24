# 0007. emx reaches the emx on a server over plain ssh

- **Status**: Accepted
- **Date**: 2026-10-14

## Context

Issue #50. Tural wants his sessions to keep running while he is away: *"I need to close my laptop and go eat then they will stop but I want them to keep running why should they stop."* He settled (Decided 1-6) that emx runs on a server, that the laptop reaches it with plain `ssh` and emx needs nothing new for it, that there is no merged list of laptop and server sessions (the laptop's plain `emx` shows only the laptop's, and nothing moves sessions), that the server's notices travel back over the same connection and the laptop speaks them or shows emx's banner in whichever #43 mode he is in, and that emx tells him when the two emx versions differ. The server's only needs are tmux, Python 3 and `claude`; emx installs nothing there.

## Decision

`emx remote <host>` on the laptop (`emx/remote.py`) does five things, all through the system's own `ssh`:

1. It opens one master connection (`ControlMaster=yes`, `ControlPersist=30`, `-N -f`) at a per-process socket, `/tmp/emx-ssh-<uid>/<pid>`. `/tmp`, not the macOS temp folder, because a socket path is limited to about 100 bytes. Any password prompt happens here, in the foreground. When the folder can't be made private there is no sharing: the follower then runs in `BatchMode` and the attach authenticates on its own.
2. It asks the server for `emx --version` through that connection and compares it with its own; a difference is printed before connecting and again on leaving, and it still connects.
3. It starts a follower, `ssh -n -T ... emx follow`, whose lines a thread hands to the laptop's own `Voice` and `Notifier` (the ones the panel uses, imported lazily from `emx/sidebar.py`).
4. It attaches, `ssh -t ... emx`, in the foreground, so the follower runs beside it, and returns the attach's exit code. A lost connection (ssh's 255) prints one line saying the sessions keep running and how to reconnect; there is no automatic reconnect.
5. On leaving it stops the follower and ends the master with `ssh -O exit`.

Every remote command is `exec "$SHELL" -lc '<emx command> <args>'`, so the server's login shell finds emx and `claude` on its PATH. `EMX_REMOTE_COMMAND` replaces the command (default `emx`) and `EMX_SSH` replaces `ssh`.

On the server, the panel's `Notices.send` appends every worded notice to `<state>/relay.jsonl` (bounded at 256 KiB, one rotated `.1`), and the panel's hush and a switch into notification mode append a hush record. The internal `emx follow` reads that file from its end, writes each valid record as one JSON line, and every 5 s writes a beat line and stamps the session option `@emx_relay`; a panel that finds it fresh (15 s) knows a laptop is showing banners, and its top line drops "notifications unavailable". A dead pipe ends the follower on the next beat. The laptop treats every record as untrusted: `core.parse_relay` checks the session id and runs every text through `clean_notice`.

A click on a laptop banner for a server session runs `emx open <sid>` with `EMX_REMOTE_HOST` (and the control socket and the laptop terminal's bundle id) in its environment, so `open_session` runs `emx open --no-window <sid>` on the server over ssh and prints the laptop terminal's bundle id; it never touches a local tmux and never opens a local emx window (ADR 0006's click contract).

ssh is a system tool like tmux and `say`: no library, no new service.

2026-10-18 (#69): Ctrl+V pastes the laptop's clipboard image into a server session. `emx follow` started with `EMX_RELAY_PASTE=1` (the laptop's `follow_shell` sets it) stamps `@emx_relay_paste` on every beat and clears it when it ends; the server's Ctrl+V binding takes the key only while that stamp is fresh and the pane holds an emx session, so with no laptop, an older laptop or a local emx the key goes through untouched. The binding runs `emx paste`, which appends a `paste` record (pane, session id, image id) to the relay file; the laptop reads the clipboard with `EMX_CLIPBOARD_IMAGE` (default `osascript`) and runs `emx paste-image <pane> <sid> <id>` on the server over the same connection with the PNG on stdin. That saves `<state>/images/<id>.png` (folder 0700, file 0600, deleted after 7 days) and pastes its path, bracketed, into the pane only while it still holds that session; an empty answer shows "no image in clipboard".

## Consequences

- A Linux server gets no banners or voice of its own (ADR 0006); the laptop shows them.
- Nothing is replayed: what happened while disconnected stays in the server panel's Notifications list only.
- A hard-killed `emx remote` leaves the master alive for 30 s (`ControlPersist`) after its last client.
- A sleeping laptop ends the attach after about 45 s (`ServerAliveInterval=15`, `CountMax=3`).
- The server must keep a user's processes after the last ssh login ends. emx's tmux starts inside the ssh login's `session-N.scope`, so systemd-logind's `KillUserProcesses=yes` ends it and every session with that login; Ubuntu 24.04 and Debian 12 and 13 ship `KillUserProcesses=no`. `loginctl enable-linger` alone does not prevent it (it keeps only `user@UID.service`). What does: `KillUserProcesses=no`, the user in `KillExcludeUsers`, or linger together with `EMX_REMOTE_COMMAND='systemd-run --user --scope --quiet emx'`. That is server setup, not emx's; checked in throwaway Ubuntu 24.04 containers running systemd 255, 3 disconnects (q, a killed master, a closed window) per setting.
- A click after disconnecting only brings the terminal forward: the ssh call needs the open connection or a key without a passphrase.
- Two emx on different sockets sharing one state folder would relay each other's notices.
- On a Linux server emx leaves an empty `~/Library/Application Support/emx` folder (the macOS helper's home, which it tries and fails to build there).
- The live checks ran over a fake ssh (`tests/fake_ssh.py`) and over real OpenSSH against a throwaway Linux container on this Mac, never a real host; his own host, network and a real laptop sleep are first tried on his first use.

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **Plain `ssh` with a shared master, a follower and a relay file** (this record) | Nothing to install on the server beyond what emx already needs; one authentication; notices over the same connection | A relay file and a heartbeat on the server | **Chosen** |
| The laptop draws the server's sessions over a data link | A merged view | A protocol to design and keep in step on both sides | Rejected in #50 Decided 2 |
| Tailscale or mosh | Reaching the server; a link that survives roaming | They are server setup, not emx's | Left to the Chief of Staff |
| A separate follower connection that does not share the master | Simpler | A second authentication, and only possible with a key (`BatchMode`) | Lost |
| `tail -F` of a guessed path on the server | No `emx follow` | The laptop can't know the server's state folder | Lost |

*Amended 2026-10-24 (#97, 2.21.0):* `remote_command` in the config file replaces `EMX_REMOTE_COMMAND`, which no longer has any effect; the `systemd-run --user --scope --quiet emx` example above now goes in `remote_command`.
