# Changelog

What changed in emx, newest version first. The panel shows a version's lines the first time that version runs.

## 2.1.0 — 2026-10-14

- emx now notifies you in one of two modes, and m switches between them. Notification mode shows a normal macOS notification from emx and never speaks. Voice mode speaks the notice, as before, and shows no notification.
- Notification mode is the default. If you had muted the voice before, you start in notification mode; if the voice was on, you stay in voice mode. Your choice is saved.
- A notification's title is the session's name and its project, for example "tea poem · emalatxana". Its text is Haiku's sentence written to be read, without the butler's "sir"; if Haiku has nothing, the plain line ("… is done." or "… needs you.").
- The first time emx starts in notification mode, or you switch into it, macOS asks once whether emx may show notifications.
- To show them emx builds a small app of its own, emx-notify, into `~/Library/Application Support/emx/` the first time it starts, and again whenever that part of emx changes. It needs Apple's command line tools (`xcode-select --install`).
- The top line no longer says "muted". In notification mode it shows nothing while notifications work, "notifications blocked" when macOS is not allowing them, and "notifications unavailable" when emx could not build its app. In voice mode it shows "voice".
- Notification mode never falls back to speaking. In voice mode, if the voice fails, the notice comes as a notification instead.
- Clicking a notification brings your terminal forward with that session open in emx.
- If no emx window is open (after q, or after a restart), clicking a notification opens emx in a new window of the terminal you started emx from, or of Terminal if emx can't tell, with that session open. That Terminal window stays open, reading "[Process completed]", after you press q in it. The first time this happens in iTerm2, it asks "OK to run …emx.command?"; ticking "Suppress this message permanently" stops it asking again.
- h and `emx hush` still stop the voice in voice mode; in notification mode they do nothing.
- The Notices list at the bottom of the panel is now Notifications: a history of the last 20, several per session, newest first. The count in its title is how many you have not read; opening a session dims its entries instead of removing them. N opens the whole history in place of the session list: ↑↓ to move, Enter or a click opens that session, Esc goes back. The history is cleared when emx restarts.

## 2.0.0 — 2026-10-14

- emx now starts and keeps every session itself, each as the ordinary `claude` in its own window of emx's tmux. What you see in the main pane is that session, not a viewer of it.
- Your current background sessions (the ones `claude --bg` started) no longer show in emx. They keep running in Claude Code and stay in `claude agents`. The first time this version starts, any of their windows emx had open closes, and the main pane shows the placeholder until you pick a session.
- A new session reads Needs input until Claude reports anything, for example while it asks whether you trust the folder. emx never answers that question for you.
- A session you have not named shows Claude's own title once Claude gives it one, and a short id before that. A /rename, whether through Ctrl+R or typed in the session, shows on its row.
- Ctrl+X twice ends the session and removes it from emx. The conversation stays in Claude Code's history. Ctrl+X once still interrupts a working session.
- If a session's `claude` exits, crashes, or your Mac restarts, its row reads Stopped. It continues only when you open it (Enter, → or a click), and then reads Idle until you send it something.
- The r key (restart the main viewer) is gone: there is no viewer left to restart.
- A message you queue while Claude is working no longer gets a "done" for the moment between Claude finishing one turn and starting on your message.
- Known gaps: Esc pressed before Claude has written anything leaves the row reading Working until the session reports again. Sessions emx starts also appear in `claude agents` as interactive sessions; emx no longer reads that list.

## 1.7.1 — 2026-10-13

- Nothing changes in how emx works yet. This version adds the parts emx will use to start and keep every session in its own tmux (issue #41): where it records each session, how it learns a session's state from Claude's hooks, and how it would continue a stopped one. Nothing uses them yet, and the panel, your sessions and your Claude settings are untouched.

## 1.7.0 — 2026-10-13

- Ctrl+Q hides the panel so the session fills the whole window, from the panel or from inside a session. Press it again, or Ctrl+\, to bring the panel back.
- emx remembers it: if you leave the panel hidden, emx starts hidden next time.
- While the panel is hidden it keeps running, so spoken notices and the missed-notices list carry on. A resize of the terminal keeps it hidden and never changes the panel width you chose.
- While hidden, ← at an empty prompt brings the panel back too.
- The panel also comes back by itself when the session in the main window closes, when a new session finishes opening, or when emx brings a session back.

## 1.6.1 — 2026-10-11

- Nothing changes in how emx works yet. This version records your decision that emx will start and keep every session itself, in its own tmux, so other agents can join later (issue #41).

## 1.6.0 — 2026-10-09

- When you press n and Tab, the folder list opens with your favourites (★) and then your last 8 folders, before you type. Typing still searches everything.
- Ctrl+T in that list stars or unstars the highlighted folder. Favourites stay in emx's own settings on this Mac (`~/.config/emx/prefs.json`), never in the repo.
- A folder counts as recent when you start a session there or open a session in it. A folder that no longer exists is left out of the list until it is back.

## 1.5.0 — 2026-10-09

- emx no longer shows banners or plays a notification sound; a notice is only the spoken sentence. iTerm2's own notifications are left as they are.
- Notices you may have missed show at the bottom of the panel under "Notices", newest first, with how long ago and the spoken sentence cut to one row each. They show even while muted, opening that session (Enter, → or a click) clears its line, and they are gone when emx restarts.
- Press h in the panel, or run `emx hush`, to stop the voice mid-sentence and drop what was waiting to be said. Later notices still speak; m still mutes.
- To stop it from any app: in the Shortcuts app make a shortcut with one "Run Shell Script" action containing `PATH="<the folder holding tmux, which is what command -v tmux prints minus /tmux, e.g. /opt/homebrew/bin>:$PATH" <full path to emx>/bin/emx hush`, and give it a keyboard shortcut in the shortcut's details. Shortcuts runs scripts with a short PATH where tmux is usually missing, hence the first part. If your shell sets `TMUX_TMPDIR`, put it in the command too. Shortcuts may first need Settings → Advanced → Allow Running Scripts.

## 1.4.2 — 2026-10-08

- Ctrl+R now renames a session while it works, without interrupting it. emx types the `/rename`, looks once more that the prompt holds only that line, and only then presses Enter; if a question or permission box opened in between, it presses nothing and tells you; the line may still be in that prompt.
- Tab in the rename box, and the line under a row, now find what you asked in a session that continues an earlier one.
- In the split second while emx types into a working session, a question that pops up at that instant could take the typed characters; a digit in the name could answer it. This is rare, and emx never types while a question is already showing.

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
