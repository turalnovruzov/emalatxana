# Changelog

What changed in emx, newest version first. The panel shows a version's lines the first time that version runs.

## 1.4.1 — 2026-10-08

- Test sessions emx starts for its own checks (named emx-test- and running in a temporary folder) no longer show in your panel or speak, and an emx run for a check makes no sound or banner on your Mac. A session you name emx-test- in a real project stays visible.

## 1.4.0 — 2026-10-08

- emx now tells you when a session needs you (a permission or a question) or has finished: a banner and one or two short spoken sentences written by Claude Haiku (28 words at most), with the project and what it was doing first and then what is waiting on you. For a more natural voice, download a Premium British one under System Settings → Accessibility → Spoken Content; emx picks it up on the next start. If Haiku can't be reached it says a plain line instead, so a notice is never lost.
- It stays quiet about the session you are looking at, but if you are in the panel, have switched to another window, or have been away for five minutes, you hear about it too. Press m in the panel to mute or unmute the voice; banners keep showing and the top line says muted.
- To stop Claude Code sending its own notices as well, add `"preferredNotifChannel": "notifications_disabled"` to `~/.claude/settings.json`; emx does not change them. After that, a `claude` you run outside emx, and anything while emx is closed, gives no notice at all.

## 1.3.0 — 2026-10-08

- In the rename box (Ctrl+R), Tab asks Claude Haiku for a name drawn from the requests you made in that session (for a very long one, the first and the latest); Tab again for another. Edit it if you like, Enter renames, Esc cancels.

## 1.2.1 — 2026-10-08

- A hiccup in the session list no longer closes the sessions you have open; a view still closes once its session is really gone.

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
