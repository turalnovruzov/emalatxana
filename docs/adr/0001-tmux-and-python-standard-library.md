# 0001. tmux and the Python Standard Library

- **Status**: Accepted
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

*Amended 2026-10-08 (#9, Tural's word: "Yes, let it type", [recorded on #9](https://github.com/turalnovruzov/emalatxana/issues/9#issuecomment-6060068575)):* to rename a session, emx types exactly `/rename <name>` and Enter into that session's open viewer. That is Claude Code's own command; there is no CLI path. It does so only on his confirmed Ctrl+R edit, only when a fresh listing shows the session running and not working, only when the viewer shows that session and no other with an empty prompt, and only while he isn't in it: the viewer is not scrolled back (tmux copy mode) and his focus is on the panel. tmux re-checks the viewer and his focus in the same command that types, so no key of his lands in between. It never touches a prompt holding his draft. This is the only input emx types into a session.

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
