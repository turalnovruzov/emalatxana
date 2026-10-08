# Changelog

What changed in emx, newest version first. The panel shows a version's lines the first time that version runs.

## 1.3.0 — 2026-10-08

- In the rename box (Ctrl+R), Tab asks Claude Haiku for a name drawn from the requests you made in that session (for a very long one, the first and the latest); Tab again for another. Edit it if you like, Enter renames, Esc cancels.

## 1.2.0 — 2026-10-08

- The line under each session's name is now written by Claude Haiku in plain words, for every session whatever its state, and kept current as the session moves on. The old line shows until Haiku's arrives, and whenever it can't be had.

## 1.1.0 — 2026-10-08

- Ctrl+X on a working session interrupts it, as Esc does in Claude Code: it stays in the panel and its row reads Idle. If it isn't open in emx, emx opens it out of sight to do this.
- Ctrl+X twice closes a session: a running one is stopped, then removed. The row itself reads closing… until it is gone.
- When a row goes, the cursor moves to the line below it, or above it when it was the last, instead of jumping to the top.
- Ctrl+X twice on a project's header closes every session in it. The first press unfolds a folded group and says how many it will close.

## 1.0.0 — 2026-10-08

- emx opens one workspace: the Claude Code session you are reading, with a panel of every background session beside it, and reattaches if it is already running.
- The panel groups sessions by project (Ctrl+S switches to by state), shows what each is doing, and Space peeks at what it asks or did.
- Open a session with Enter or →, and come back with ← from an empty prompt.
- n starts a session where the cursor is, with a fuzzy folder picker on Tab.
- Ctrl+X twice stops a session, or removes a stopped one; Ctrl+F finds one, Ctrl+T pins it, Ctrl+R renames it.
- s moves the panel to the other side, and < > make it narrower or wider.
- The version shows beside the logo at the top of the panel and stays beside the logo (when room is short the name goes first, then the counts are cut); emx --version prints it; and after an update a note under the list says what is new in every version since the one you last ran.
