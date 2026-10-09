<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Stack

What is in use in this repo, and why each piece is here. This is the record the `SessionStart` dependency check diffs the manifest and lockfile against: a dependency named in neither this file nor an [ADR](./adr/README.md) is reported as undocumented.

**Nothing about this stack is pre-decided, on purpose.** Deciding a stack at setup, before any code exists, is mostly wrong. Entries arrive from decisions taken **in-project as the work reaches it**, through the technology gate in [`CLAUDE.md`](../CLAUDE.md#the-technology-gate): a new library, a new third-party service, or a major version bump is Tural's call, reaches him as a one-screen comparison, and lands as an ADR.

**Do not pre-populate this file with libraries nobody has chosen.** An entry here is a record of a decision that was made, not a plan.

---

## Decided in-project

| What | Why it's here | Decision record |
|---|---|---|
| tmux (≥ 3.7) | Panes, the session pane beside the panel, and instant switching via `swap-pane` between live `claude attach` viewers | [0001](./adr/0001-tmux-and-python-standard-library.md) |
| Python 3, standard library only | The `emx` launcher and the curses session sidebar; nothing pip-installed | [0001](./adr/0001-tmux-and-python-standard-library.md) |
| Claude Haiku via `claude -p` on his login | The one-line summary under each session's name, the name Tab suggests in the rename box, and the words of each notice; no library, no API key | [0002](./adr/0002-haiku-row-summaries-through-claude-p.md), [0003](./adr/0003-haiku-suggests-a-name-on-tab.md), [0004](./adr/0004-spoken-notices.md) |
| macOS `say` (built in) | Spoken notices | [0004](./adr/0004-spoken-notices.md) |
