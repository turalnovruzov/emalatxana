<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Stack

What is in use in this repo, and why each piece is here. This is the record the `SessionStart` dependency check diffs the manifest and lockfile against: a dependency named in neither this file nor an [ADR](./adr/README.md) is reported as undocumented.

**Nothing about this stack is pre-decided, on purpose.** Deciding a stack at setup, before any code exists, is mostly wrong. Entries arrive from decisions taken **in-project as the work reaches it**, through the technology gate in [`CLAUDE.md`](../CLAUDE.md#the-technology-gate): a new library, a new third-party service, or a major version bump is Tural's call, reaches him as a one-screen comparison, and lands as an ADR.

**Do not pre-populate this file with libraries nobody has chosen.** An entry here is a record of a decision that was made, not a plan.

---

## Decided in-project

| What | Why it's here | Decision record |
|---|---|---|
| tmux (≥ 3.7) | Panes, and emx's own private server that holds every session: each runs as the interactive `claude` in its own pane, and switching swaps that pane beside the panel | [0001](./adr/0001-tmux-and-python-standard-library.md), [0005](./adr/0005-emx-runs-agents-in-its-own-tmux.md) |
| Python 3, standard library only | The `emx` launcher and the curses session sidebar; nothing pip-installed | [0001](./adr/0001-tmux-and-python-standard-library.md) |
| Claude Haiku via `claude -p` on his login | The one-line summary under each session's name, the name Tab suggests in the rename box, and the words of each notice; no library, no API key | [0002](./adr/0002-haiku-row-summaries-through-claude-p.md), [0003](./adr/0003-haiku-suggests-a-name-on-tab.md), [0004](./adr/0004-spoken-notices.md) |
| macOS `say` (built in) | Spoken notices, in voice mode | [0004](./adr/0004-spoken-notices.md) |
| `swiftc` (Apple's command line tools) and the `emx-notify.app` helper it builds | Notifications from emx itself, in notification mode; with `codesign`, `iconutil`, `lsregister` and `open`, all Apple's | [0006](./adr/0006-emx-notify-helper.md) |
| OpenSSH client (`ssh`), the system's own | `emx remote` reaches the emx on a server; connection sharing (ControlMaster) carries the notifications back | [0007](./adr/0007-emx-reaches-a-server-over-ssh.md) |
| `git` and `tar`, the system's own | Installing emx and updating it from the repo's `vX.Y.Z` tags: a bare clone, `git archive` of a tag, unpacked into its own folder | [0008](./adr/0008-emx-installs-and-updates-itself-from-git.md) |
| systemd's own `systemctl`, `journalctl` and `systemd-analyze` | The routines view reads a server's user timers, their last runs and their scheduled times; read-only, never a verb that changes a unit | [0009](./adr/0009-routines-read-from-systemd.md) |
