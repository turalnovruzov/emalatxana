# Changelog

What changed in emx, newest version first. The panel shows a version's lines the first time that version runs.

## 2.13.0 — 2026-10-19

- The Routines view also shows user timers that are switched off and not loaded, tagged `timer off`, with their runs in the past 24 hours and no missed times.
- If emx can't read the switched-off timers, it says so in red and still shows the others.
- Template timers (`name@.timer`) are not shown.

## 2.12.0 — 2026-10-19

- On a phone (a screen narrower than 70 columns) emx shows one thing at a time: the panel across the whole screen, or the session you opened. `‹ sessions` at the left of the bar, or `←` on an empty prompt, goes back to the panel. With the keyboard up the bar is one short line.
- For a Claude session with Remote Control on, the selected row on the phone has a line "↗ open in the Claude app". `↵` shows the link for one tap and copies it to the phone's clipboard; `t` opens the session in the terminal instead. emx reads that link from the file Claude Code keeps for the session and changes nothing of Claude's.
- `a` on the phone, or a tap on the selected row, opens the session's actions: open in the Claude app, open here, peek, rename, pin, interrupt, close. All plain keys or taps, no Ctrl; close asks once more with `x again to close`.
- On the phone a tap on a row selects it, a session you start stays on the panel with the new row selected ("Started <name>.  ↵ open in app"), and `?` lists the phone's keys and scrolls.
- `emx docs phone` explains it all.
- Nothing changes on your laptop.

## 2.11.0 — 2026-10-18

- The Routines view shows every systemd user timer on the machine; there is no list to keep. A new routine appears and a removed one disappears the next time the view reads, within a minute.
- The `routines` file beside `prefs.json` is no longer read; you can delete it.
- With no user timers, the view says so.

## 2.10.0 — 2026-10-18

- Each device that opens emx gets its own view: its own panel, its own open session, its own hidden or shown panel and its own width. A phone and your laptop can be on at the same time without moving each other's layout.
- The same session open on two devices at once has one width, that of the device you used last.
- Two terminals on the same machine each get their own view too, instead of mirroring each other.
- A phone or second terminal that goes away stops showing its session, and shows it again when it comes back. A phone that starts emx before your laptop does gets its own view too, so your laptop still finds its own layout.
- Your laptop works as before: the session you had open stays open through the update and is on screen the moment you come back, at your laptop's width; closing a session shows the empty pane straight away; a right-click in a session goes to Claude as before when Claude uses the mouse (its full-screen view does), and otherwise opens the right-click menu where it did; and copying with the mouse, scrolling, clicking, Shift+Enter and colours are unchanged.
- One difference on the laptop: when the right-click menu does open (in a session that doesn't use the mouse), its Kill and Respawn act on emx's view of the session and no longer end or restart the session itself.

## 2.9.0 — 2026-10-18

- The bar shows the keys for whatever the cursor is on: the rest keys when nothing is selected or the panel is not in focus, a session's own keys by its state (answer when it needs input, interrupt while it works, pin when it is done), a project header's keys (fold, new here, close group), the peek's keys, and `n new session` when there are no sessions. A key shows only when it really does that now.
- The selected session and header repeat their likeliest key, dimmed, at the right end of their last line (`→ answer`, `^x interrupt`, `→ open`, `Enter fold`) when it fits.
- The peek's bottom edge reads `→ open · ␣ close`.
- `N all` sits next to the latest notification in the bar.
- With no sessions, the panel reads "No sessions yet." and "Press n to start one in a folder."

## 2.8.0 — 2026-10-18

- `R` in the panel opens a separate Routines view, for the routines your server runs as systemd user timers. `R` or `Esc` goes back.
- For each routine you listed it shows every run in the past 24 hours with a check or a cross by its exit code, a warning for each scheduled time that had no run, and "not due" when nothing was scheduled. It only reads; it never starts, stops or changes a routine.
- You list the routines once, one timer name per line, in a file called `routines` beside `prefs.json`. `emx docs routines` says how.
- A routine that was scheduled and didn't run at all in the past 24 hours shows each of those times as missed.
- When the journal can't be read, the routines show its error and no missed times, rather than warning about runs that may well have happened, and `unknown` when emx can see no run at all. A run systemd recorded but the journal no longer holds shows, with no missed times.
- On a machine without systemd, such as a Mac, the view says there is nothing to read here.
- Through `emx remote`, the view shows the server's routines.

## 2.7.1 — 2026-10-18

- The panel uses your terminal's own background again, as before 2.6.0.
- A session's whole status shows under its name: a long one wraps onto as many lines as it needs instead of being cut off, and a status taller than the panel stops at the panel's height with `…`.
- The bar over the selected row is blue again, as before 2.6.0.

## 2.7.0 — 2026-10-18

- On `emx remote`, Ctrl+V in a session's pane pastes the image on your laptop's clipboard into that session as a file path, so Claude Code can read it. Your laptop sends the image to the server, which keeps it in a private folder for 7 days.
- With no image on the clipboard, emx says "no image in clipboard" on the server's screen and types nothing.
- Your emx on the laptop itself is unchanged: Ctrl+V still reaches Claude Code's own image paste. Both machines need 2.7.0; with either older, Ctrl+V works as before.
- `EMX_CLIPBOARD_IMAGE` replaces the command that reads the laptop's clipboard image (default `osascript`).
- While you are connected through `emx remote`, anything running on the server as you can ask for the image on your laptop's clipboard; emx only types its path into the pane that asked.

## 2.6.0 — 2026-10-18

- A new look. The panel sits on its own, slightly lighter ground, and the bar over the selected row is calmer.
- One bar now runs across the bottom of the whole window. It shows emx and its version, the latest notification, a count per state in that state's colour, and the key hints. The panel is just your sessions: no title line, no rule, no notifications strip and no key footer.
- Each session shows on two lines: its name and age, and under the name what it is doing.
- Idle sessions have their own lavender colour, and their own count in the bar.
- Keys are written the same way everywhere: `^x`, `Esc` and `Enter`.
- The `⇄ myserver` mark and the mode (`voice`, notifications blocked or unavailable) now show in the bar, after the version.

## 2.5.1 — 2026-10-18

- A session that published an artifact no longer stays on Working after it finishes: Claude Code's watch on the artifact isn't counted as background work any more.

## 2.5.0 — 2026-10-18

- While you're in a server through `emx remote myserver`, the panel's top line shows `⇄ myserver` in violet after the version, and your terminal's title reads `emx · myserver`.
- Both go when you leave. Someone looking at the server's own screen sees the mark too.
- An emx older than this one, on either machine, shows no mark.

## 2.4.0 — 2026-10-17

- emx has docs: getting started, guides for sessions, voice and notifications, remote and updating, a page on how emx works, and a reference of every command, key and setting.
- `emx docs` prints the docs index and `emx docs <page>` prints one page, on your laptop or on a server (`emx remote` shows the server's version of them), so you or any agent can read them. `emx --help` and the README point to them.

## 2.3.0 — 2026-10-17

- emx installs as an app of its own with one line: clone this repo to a temporary folder and run its `install.sh`. It puts emx in `~/.local/share/emx`, apart from any project folder, and `emx` in `~/.local/bin`; if that folder isn't on your PATH, it prints the one line to add and changes none of your shell files. It works on a Mac and on Linux, using git and your own GitHub login.
- An installed emx updates itself. Every 10 minutes it looks for a newer released version and gets it in the background; at a quiet moment (nobody attached, or no key pressed for 3 minutes, and nothing open in the panel) the panel switches over to it by itself. Your Claude sessions keep running untouched. After a switch the panel says "emx updated to X." with what's new.
- A new major version (say 2.x to 3.0) is fetched but waits for you: the panel says "emx 3.0.0 available: run emx update", and `emx update` installs it.
- `emx update` installs the newest version now. `emx update off` stops the automatic updates (`emx update` still works), `emx update on` starts them again. `emx uninstall` removes the installed emx and its link and keeps your prefs and emx's state.
- `emx remote myserver` now asks the server's emx to update itself when it connects, and says so when it does. If it can't (for example, the server can't reach GitHub), you see the version note and why. A new major version on either machine is asked once, in your terminal, for both.
- An emx run from a project folder (a checkout) never updates itself, and `emx update` there says so.
- One previous version stays on disk next to the current one; older ones are deleted.

## 2.2.2 — 2026-10-16

- Nothing changes in how emx works. The note on what a server needs was corrected: on a server set to end a user's programs at logout (`KillUserProcesses=yes`), `loginctl enable-linger` alone does not keep your sessions running; turning that setting off, or excluding your user from it, does. Ubuntu and Debian leave it off.

## 2.2.1 — 2026-10-15

- When a session stops, Haiku now reads what you last asked that session for next to the end of Claude's last message, to tell "finished, and offering something extra" (Done) from "stopped short, waiting on you" (Needs input). An offer of a step you didn't ask for, such as "Should I also delete the old config.ini?" after a config migration, is meant to read Done, and the same question Needs input when deleting it was part of what you asked; how often each happened in emx's checks is below.
- In emx's own checks, finished work that ends by offering more (or a greeting) read Done 43 of 45 times, up from 17 of 45 with 2.2.0 (3 runs each of 15 messages, held-out set). Sessions that stopped to have you do something, choose, approve or give permission read Done 0 of 36 times, the same as 2.2.0.
- In the same checks, one final message judged under two different requests, once asking for the offered step and once not, got both right 7 of 12 times, up from 0 of 12 with 2.2.0 (4 such pairs, 3 runs each).
- When emx can't find what you asked, it judges from Claude's last message alone, as before. A turn started by a slash command is judged next to the request you typed before it.

## 2.2.0 — 2026-10-15

- `emx remote myserver` opens the emx running on a server in this terminal, over ssh. myserver is a name from your own SSH config, and the server needs emx, tmux, Python 3 and claude installed.
- Closing the window, closing the laptop or losing the connection leaves the server's emx and its sessions running; `emx remote myserver` again brings you back to them. q there leaves them running too.
- While connected, the server's notifications reach the laptop, spoken or as emx's notification, in the mode set with m on the server's panel; clicking one opens that session on the server's panel. While not connected nothing arrives; the server's Notifications list keeps them.
- h on the server's panel also stops the laptop speaking.
- If emx on the server is a different version, emx remote says so before it connects and again when you leave.
- If the server's login shell can't find emx, set EMX_REMOTE_COMMAND to its path there.
- On a server that can't show notifications itself, the top line no longer says "notifications unavailable" while a laptop is connected.
- Plain `emx` still shows only this laptop's sessions.

## 2.1.2 — 2026-10-14

- Nothing changes in how emx works. The project's working rules were updated again for how its changes get checked, including how a live check of emx is kept apart from your own sessions and settings; this version only records that.

## 2.1.1 — 2026-10-14

- A session whose background subagents or commands are still running reads Working until they finish, instead of Done.
- A session that stopped to ask you something, or to have you do something, reads Needs input instead of Done. Haiku reads Claude's last message to tell the two apart, so the row can read Done for a few seconds first, and its notification waits for that answer: a session that needs you is never announced as done.
- A session showing a question box (Claude's multiple-choice questions) reads Needs input while the box is open.

## 2.1.0 — 2026-10-14

- emx now notifies you in one of two modes, and m switches between them. Notification mode shows a normal macOS notification from emx and never speaks. Voice mode speaks the notice, as before, and shows no notification.
- Notification mode is the default. If you had muted the voice before, you start in notification mode; if the voice was on, you stay in voice mode. Your choice is saved.
- A notification's title is the session's name and its project, for example "tea poem · emalatxana". Its text is Haiku's sentence written to be read, without the butler's "sir"; if Haiku has nothing, the plain line ("… is done." or "… needs you.").
- The first time emx starts in notification mode, or you switch into it, macOS asks once whether emx may show notifications.
- To show them emx builds a small app of its own, emx-notify, into `~/Library/Application Support/emx/` the first time it starts, and again whenever that part of emx changes. It needs Apple's command line tools (`xcode-select --install`).
- The top line no longer says "muted". In notification mode it shows nothing while notifications work, "notifications blocked" when macOS is not allowing them, and "notifications unavailable" when emx could not build its app. In voice mode it shows "voice".
- Notification mode never falls back to speaking. In voice mode, if the voice fails, the notice comes as a notification instead.
- Clicking a notification brings your terminal forward with that session open in emx; in iTerm2, it brings forward the tab emx runs in. The first time, iTerm2 asks "A control sequence attempted to activate a session. Allow it?"; tick "Remember my choice" and click Allow.
- If no emx window is open (after q, or after a restart), clicking a notification opens emx in a new window of the terminal you started emx from, or of Terminal if emx can't tell, with that session open. That Terminal window stays open, reading "[Process completed]", after you press q in it. The first time this happens in iTerm2, it asks "OK to run …emx.command?"; ticking "Suppress this message permanently" stops it asking again.
- h and `emx hush` still stop the voice in voice mode; in notification mode they do nothing.
- The Notices list at the bottom of the panel is now Notifications: a history of the last 20, several per session, newest first. The count in its title is how many you have not read; opening a session dims its entries instead of removing them. N opens the whole history in place of the session list: ↑↓ to move, Enter or a click opens that session, Esc goes back. The history is cleared when emx restarts.

## 2.0.1 — 2026-10-14

- Nothing changes in how emx works. The project's working rules were updated for how its changes get checked; this version only records that.

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
