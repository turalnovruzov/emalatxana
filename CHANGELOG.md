# Changelog

What changed in emx, newest version first. The panel shows a version's lines the first time that version runs.

## 1.0.0 — 2026-10-08

- emx opens one workspace: the Claude Code session you are reading, with a panel of every background session beside it, and reattaches if it is already running.
- The panel groups sessions by project (Ctrl+S switches to by state), shows what each is doing, and Space peeks at what it asks or did.
- Open a session with Enter or →, and come back with ← from an empty prompt.
- n starts a session where the cursor is, with a fuzzy folder picker on Tab.
- Ctrl+X twice stops a session, or removes a stopped one; Ctrl+F finds one, Ctrl+T pins it, Ctrl+R renames it.
- s moves the panel to the other side, and < > make it narrower or wider.
- The version shows beside the logo at the top of the panel and stays visible, while the counts beside it are cut first; emx --version prints it; and after an update a note under the list says what is new in every version since the one you last ran.
