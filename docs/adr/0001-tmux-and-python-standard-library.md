# 0001. tmux and the Python Standard Library

- **Status**: Accepted; replaced in part by [0005](./0005-emx-runs-agents-in-its-own-tmux.md), built in [#41](https://github.com/turalnovruzov/emalatxana/issues/41) (emx 2.0.0). Its rule that session data comes only from the `claude` CLI (`claude agents`, `claude attach`, `claude --bg`), and the amendments' `claude stop`/`claude rm` close and hidden-viewer interrupt, no longer hold: emx starts and holds each session itself. tmux, the Python standard library, and the rule that emx never resumes a session on its own (only his open does) stand.
- **Date**: 2026-10-07

## Context

Issue #1: one workspace where Tural sees every Claude Code session with its live status, switches between them as smoothly as `claude agents` does, reads the current one in a narrow column, and starts new sessions in a folder he picks. Hard constraint, in his words: *"I don't want to lose sessions in the middle of work."* An earlier prototype that switched with `/resume` moved conversations between sessions and was rejected, and so was the desktop app (too heavy).

A headless spike on Claude Code 2.1.293 and tmux 3.7c, run on throwaway sessions only (recorded on #1), found:

- `claude attach <id>` in a hidden tmux window, `swap-pane`d into a 90-column main pane, redraws cleanly at the new width, including mid-answer.
- Killing the viewer pane leaves the session running with the same id and pid. Two viewers on one session mirror each other.
- Shift+Enter reaches Claude as a newline when extended keys are on.
- ← on an empty prompt turns the pane into the full agent view.

## Decision

We build `emx` on **tmux** (a private server, its own socket and config) for the panes and switching, with the launcher and sidebar written in **Python 3 using only the standard library** (curses for the sidebar). Session data comes only from the `claude` CLI: `claude agents --json`, `claude attach`, `claude --bg`. emx never moves a conversation between sessions, and never resumes, stops or removes a session on its own.

*Amended 2026-10-08 (#4, Tural's word):* emx may stop a session (`claude stop`) or remove one that is no longer running (`claude rm`), but only on his confirmed double Ctrl+X on that session's row, matching `claude agents`. Only his explicit open reopens a stopped session. See `docs/conventions.md`.

*Amended 2026-10-08 ([#24](https://github.com/turalnovruzov/emalatxana/issues/24) and [#18](https://github.com/turalnovruzov/emalatxana/issues/18), Tural's in-session decisions):* on #24 he chose **"Ctrl+X once = interrupt"** from the lead's proposal: Ctrl+X once on a working session interrupts it, the same as pressing Esc in it, and the session stays and waits; Ctrl+X a second time closes it, stopping it if it is still running, then removing it. On #18 he answered "Yes, same as claude agents" when asked whether a double Ctrl+X on a header should delete the whole group. On the plan's gaps, [recorded on #24](https://github.com/turalnovruzov/emalatxana/issues/24#issuecomment-6067699949), he answered "Open a hidden window to interrupt" for a session with no viewer open, "Project headers only" for which headers close their group, "Only arms" for the first Ctrl+X on a header, and "Maybe it should unfold on the first Ctrl+X click Then on the next one It will close" for a folded group. This replaces the #4 double Ctrl+X above.

*emx's implementation of that amendment (an engineering choice, not Tural's word):* the interrupt sends Esc into the session's viewer, as `claude agents` does. With no viewer open, emx opens a hidden one out of sight, and only on a fresh listing showing the session running, never on a stopped one. The close runs `claude stop` if the session is running, then `claude rm`; on a header it does so for every session in the group. Before an interrupt ends, emx kills the hidden viewer it opened, except when no close has started and either the viewer came up and took the Esc or he has swapped it into main, which makes it his. A close cancels any interrupt of its sessions and waits for it. A cancelled interrupt always kills its hidden viewer; if he has swapped that viewer into main, emx first swaps the placeholder back into main, so main keeps a pane. If tmux can't say where the viewer is, there is no placeholder or the swap fails, emx leaves the viewer alone; if the kill fails and the pane is still listed, it is still there too. Either way the close, once it has waited, tries once more the same way, and closes nothing while that viewer may still be there. So a hidden viewer of emx's that is still starting can't resume a session being closed. Not covered: a window tmux made for an open that then reported an error, which leaves emx no pane to kill.

*Amended 2026-10-08 (#9, Tural's word: "Yes, let it type", [recorded on #9](https://github.com/turalnovruzov/emalatxana/issues/9#issuecomment-6060068575)):* to rename a session, emx types exactly `/rename <name>` and Enter into that session's open viewer. That is Claude Code's own command; there is no CLI path. It does so only on his confirmed Ctrl+R edit, only when a fresh listing shows the session running and not working, only when the viewer shows that session and no other with an empty prompt, and only while he isn't in it: the viewer is not scrolled back (tmux copy mode) and his focus is on the panel. tmux re-checks the viewer and his focus in the same command that types, so no key of his lands in between. It never touches a prompt holding his draft.

## Consequences

- tmux becomes a runtime requirement. Nothing gets pip-installed, so the dependency check has no manifest to read.
- Switching only swaps viewers, so the tool cannot lose a conversation except by his confirmed remove. Closing emx leaves every session running.
- The sidebar draws itself with curses, which means more hand-written UI code than a TUI library would need.
- Not verified headless: the iTerm2→tmux leg of Shift+Enter, wheel scroll and flicker. These are checked by hand.

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **tmux + Python standard library** | A clickable, live sidebar; no packages; easy to change | tmux dependency; curses drawn by hand | **Chosen** (Tural, 2026-10-07) |
| tmux + bash | Smallest; no runtime | Clicks and live refresh get hacky and glitchy | Lost: smoothness matters to him |
| tmux + Go or Rust binary | One fast binary | A toolchain he doesn't have; every change is slower | Lost: the project keeps changing |
| Claude Code mod with `/resume` switching | Lives inside Claude Code | Moves conversations between sessions; glitchy | Rejected earlier by Tural |
| Desktop app Code tab | Native GUI | Heavy on RAM; less comfortable than the CLI | Rejected by Tural |
