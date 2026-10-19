<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Conventions

The house style for writing code in this repo: how *this* codebase does things, so the next session does them the same way instead of inventing a second way.

## Entries

### Every tmux call goes through `emx.tmux.Tmux`

1. **Rule**: we reach tmux only through the `Tmux` wrapper in `emx/tmux.py`, which puts emx's private `-L` socket on every command. New tmux behaviour becomes a method on it, never a bare `tmux` in a subprocess call or a shell string.
2. **Why**: the user's default tmux server holds his real work. A command without the private socket lands on that server and can rebind keys or kill panes there.
3. **Minimal example**:
   ```python
   tmux.some_action(pane)        # a Tmux method; it adds -L <socket>
   # never: subprocess.run(["tmux", ...])
   ```
4. **Gotcha**: `-f` (emx's config file) only takes effect when the server starts. A running workspace picks up config changes only because the launcher re-sources the file when the code stamp changes. Re-sourcing applies added and changed lines but never undoes a removed one (that still needs a server restart), `set -a` appends again on every source unless the list is reset first, and a line tmux rejects fails `source-file` even though server start tolerated it. And the socket name must never be `default` or empty, because that is the user's everyday server; the launcher refuses those values.

### `emx/core.py` stays pure

1. **Rule**: decisions and parsing live in `emx/core.py` as plain functions over text and data. It imports no curses, no tmux wrapper and no subprocess; `sidebar.py`, `cli.py` and `tmux.py` gather input, call core, and act on the answer.
2. **Why**: the pure part is what the unit tests can cover without a terminal, a tmux server or a Claude session, and nothing in it can touch his sessions.
3. **Minimal example**:
   ```python
   text = tmux.some_capture(pane)          # the wrapper gathers
   verdict = core.judge(text, expected)    # core decides, from plain values
   ```
4. **Gotcha**: logic creeps into the wrappers. A condition written inline in `sidebar.py` "because it needs the pane" is untested; pass the pane's text or fields into a core function instead. Group keys in the project view are not always folders: `core.PINNED` is a pseudo-key, so code that reads a group key or a `("group", key)` cursor as a folder must exclude it.

### CLI wrappers never raise

1. **Rule**: a function that wraps the `claude` CLI returns `(value, None)` on success or `(None, one-line error)` on failure, and never raises. The caller shows the error line on screen. The `Tmux` wrapper is the exception: it raises `TmuxError`, and every caller of it catches that and shows the line.
2. **Why**: the sidebar runs for hours in a pane. One escaped exception kills the poller thread or the curses loop, and he is left with a frozen or dead panel.
3. **Minimal example**:
   ```python
   value, error = run_some_command(...)
   if error:
       show(error)                  # one line on screen; nothing was raised
   ```
4. **Gotcha**: "never" includes decoding. Output with invalid UTF-8 raises inside `subprocess.run` before your own `try` around parsing sees it, so decode with `errors="replace"`. Threads and the main loop still catch everything as a backstop.

### Screen detection is tested on real captures

1. **Rule**: we test screen detection against real captures from throwaway sessions, stored as `tests/fixtures/screens/<what>.w<pane width>.txt`. The width in the file name is passed to the detector, and anything private is replaced with `<placeholder>` tokens.
2. **Why**: hand-written screens encode what we think Claude Code draws. Real captures caught the logo prefix, the zero counts in the agent header and footer differences that broke the detector before, and the width must be tmux's, not one inferred from the text.
3. **Minimal example**:
   ```python
   text, width = real_screen("agent-view")   # agent-view.w100.txt
   core.main_view_state(text, "emx-test-named", width)
   ```
4. **Gotcha**: redact only lines the detector does not read. Editing a rule line, the `❯` line, the agent header or the footer changes what the test proves. The captures are pinned to the Claude Code version they came from, so recapture when it changes. Transcript fixtures under `tests/fixtures/transcripts/` follow the same rule: throwaway sessions only, allowlisted entry and message fields, and the Claude Code version asserted.

### tmux targets for a session name the session exactly: `=name:`

1. **Rule**: when a tmux command takes a target and we mean the emx session, we write `-t "={SESSION}:"`. The `=` asks for an exact match, and the trailing colon makes tmux read it as a session, not a pane or window.
2. **Why**: `show-options -q` hides target errors. `-t =emx` (no colon) returns an empty string with exit 0, which reads as "option unset", so the version check would upgrade on every launch with no error anywhere.
3. **Minimal example**:
   ```python
   self.run("show-options", "-qv", "-t", f"={SESSION}:", name)
   # never: "-t", f"={SESSION}"   (silently empty under -q)
   ```
4. **Gotcha**: `-q` also hides a session that does not exist (`=emxx:` returns `''` with exit 0 too), so an empty value never proves the option is unset. Read it only after `has_session()`.

### Every start of `claude` for an existing session asks `core.attach_allowed` first

1. **Rule**: before emx starts `claude --resume` for a session of its own (in the session's dead pane, or a new hidden window), it passes the session's row and a named trigger (`OPEN`, `NEW`) to `core.attach_allowed`. Only `OPEN`, his explicit Enter, → or click, may resume a session with no live process; `NEW` is judged on the listing the `Starter` took for it (`wait_until_running`), never on one taken on the curses thread, and needs the session running in it. A running session is never started again: its own pane stays in its hidden window, and the view's slot shows it through a viewer.
2. **Why**: resuming a stopped session starts it again, and he must be the only one who reopens a session that stopped, after an exit, a crash or a reboot (ADR 0001, kept by ADR 0005; Tural, 2026-10-13, "sure lets go with b").
3. **Minimal example**:
   ```python
   # curses thread, in App.resume: judges, never lists
   if not self.confirm_attach(sid, trigger, checked):
       return None
   command, error = self.engine.resume_command(sid)
   ```
4. **Gotcha**: the panel's rows can be a poll old, or older while listing fails. "Running" means a live pane carrying both the session's key and the agent tag, never `state` alone. A new pane is tagged before a view's slot can show it, and one that can't be tagged is killed, or a second open would resume the same conversation again. The agent tag goes on before the session's key: a pane with a key and no agent tag is an old `claude attach` viewer, which the panel kills, and a listing can land between the two `set-option`s. Respawning a live session's pane ends that session, so nothing respawns a pane that is not dead, and a trigger other than `OPEN` that finds the session's pane dead or gone is refused even when its listing showed it running.

### `claude` calls, git calls and filesystem scans never run on the curses thread

1. **Rule**: any call to the `claude` CLI or git, and any listing of emx's sessions, runs on a worker thread. One-shot workers (`Starter`, `Closer`, `Interrupter`, `Renamer`) are started by the curses thread, which checks `is_alive()` from `refresh()` and then reads `.result`. Long-running ones (`Poller`, and the `Resolver` and `Transcripts` it starts) loop on their own, and `refresh()` collects their latest answer with `.take()`, which hands it over under a lock and returns None when nothing is new. The curses thread never calls `fetch_sessions` or `run_git` itself. Opening a session reads and rewrites only that session's own small files under emx's state folder on the curses thread, as the prefs are. Listing or stat-ing any folder the picker reads follows the same rule (startup reads of home, the prefs file and emx's own `CHANGELOG.md` excepted): the folder picker asks `Listings.get` or `Listings.readable`, which return whatever is cached (None until the first read lands) and starts a short-lived thread per folder, never two for one folder and at most `MAX_LISTINGS` at once; the curses thread judges a typed path from those cached reads (`core.folder_exists`) and dedupes paths with `normpath` only.
2. **Why**: a listing reads every session's hook records and transcript tail and asks tmux, a `claude -p` call takes seconds, and a folder on a hung mount can block a listing or a stat for good. On the curses thread any of them freezes the panel, and every key he presses meanwhile is lost or queued.
3. **Minimal example**:
   ```python
   self.starter = Starter(self.engine, folder, message)
   self.starter.start()
   # later, in refresh():
   starter = self.starter
   if starter is not None and not starter.is_alive():
       session, error = starter.result or (None, "starting a session failed")
   ```
4. **Gotcha**: the answer can be stale on arrival. Record what was asked (here the session and main-pane id), and drop the answer when that no longer holds. Keep one worker per kind at a time, so repeated keys don't stack calls; interrupts are one per session, so a second working row is interrupted while the first still is; folder listings are the other exception, one per folder under a cap, because a hung folder must not stop other folders being listed, and it keeps its slot until it returns. A listing or a poll that lands late moves the picker's rows, so a highlight is kept by path, not by row.

### A tmux key binding that intercepts a key fails open, except ← in a slot showing a session

1. **Rule**: a root binding first checks a cheap `if-shell -F` format condition. Only when that passes does it run `bin/emx <check>`, whose exit 0 means intercept. Every other outcome (a non-zero exit, an exception, an unknown socket, a target pane gone) sends the original key through with `send-keys`. The check's command lives in a user option, never inlined in the binding. The one exception is ← in a view's slot showing a session (`@emx_view` set): there `bin/emx left` exits 0 only to let ← reach Claude Code, and every other outcome, a crash or a check that can't run included, selects that view's panel. Its format condition (the slot's view tag, cursor shown, the panel in the window) never guesses the prompt's column; the check finds the session's own pane from the slot and reads the column from where the `❯` is drawn.
2. **Why**: a broken check must cost him the shortcut, never a keypress. Keeping the command in an option avoids tmux quoting of paths. For ← the costs run the other way: Claude Code turns a ← it gets on an empty prompt into a fork of the conversation into its background service, so a broken check costs him ← in typed text instead.
3. **Minimal example** (the ← binding as `list-keys` shows it on a real server, wrapped):
   ```
   bind-key -T root Left if-shell -F "#{&&:#{@emx_view},#{&&:#{cursor_flag},#{&&:#{==:#{window_panes},2},#{P:#{?#{==:#{@emx_role},sidebar},1,}}}}}" \
       "if-shell \"#{@emx_left} #{pane_id} #{cursor_x} #{cursor_y} #{cursor_flag} #{pane_width}\" \"send-keys Left\" \"set-option -p -t :.+ @emx_back 1 ; select-pane -t :.+\"" \
       "send-keys Left"
   ```
4. **Gotcha**: `if-shell` without `-b` holds that client's key queue until the check returns. That keeps keys in order but makes him wait, so keep the format condition tight and the check fast (about 50 ms); in a session's pane every ← with the cursor shown pays it. And a failed `select-pane` has no "else": it ends the command list and the key is gone, so a pane the binding targets is tested in the format condition first.

### Root keys act on the window they are pressed in

1. **Rule**: a root binding finds the panes it acts on relative to the pressing client's own window: `:.+` is the other pane of a view's two, and the binding is gated on the `VIEW_WINDOW` format (two panes, one of them the panel). No pane id is baked in at bind time, so every view's panel binds the same keys and binding again changes nothing. A key pressed in a slot passes the root table twice, once at the view's level and, when that level sends it on with `send-keys`, again inside the viewer on the session's own pane, whose window holds no panel. So the view's level either takes the key or passes it on, and the gate makes the inner pass send it straight to the pane.
2. **Why**: each device has its own view, with its own panel and slot (ADR 0010). A pane id baked into a binding would make a key pressed on the phone act on the laptop's view.
3. **Minimal example** (`bind_collapse`, as `list-keys` shows it):
   ```
   bind-key -T root C-q if-shell -F "#{&&:#{==:#{window_panes},2},#{P:#{?#{==:#{@emx_role},sidebar},1,}}}" \
       "if-shell -F \"#{==:#{@emx_role},sidebar}\" \"resize-pane -Z -t :.+\" \"resize-pane -Z\"" "send-keys C-q"
   ```
4. **Gotcha**: the mouse goes through the viewer too. A viewer is a tmux client, which turns mouse reporting on in the slot, so tmux's own mouse bindings pass a click inward (`send-keys -M`) and act on the session's window, not the view's. A mouse binding that must act on the view takes the event at the view's level, gated on the slot's role and on the mouse flag of the pane the slot shows, never the slot's own, which the viewer always sets; a format cannot read another pane, so that flag is read by a shell check kept in a global option (`bind_menu`, `MENU_CHECK_OPT`).

### An option lives on a view, on `emx`, or globally, by who must read it

1. **Rule**: an option that belongs to one device's view is set on that view's session through `Tmux(view=…)` (`view_option`, `set_view_option`: `OPEN_OPT`, `REOPEN_OPT`, the bar, the focus report). One that belongs to the whole workspace is set on `=emx:` (`session_option`: `VERSION_OPT`, `HUSH_OPT`, `TERMINAL_OPT`). Anything a binding's format or check must see from inside a viewer is global (`-g`: `LEFT_OPT`, `PASTE_OPT`, `RELAY_PASTE_OPT`), as is what every view's panel must find the same (`MENU_OPT`, the stash of tmux's own right-click binding, and `MENU_CHECK_OPT`).
2. **Why**: a format in a binding expands in the session the key lands in. Inside a viewer that is the session's own `emx-a-<key>`, which sees neither a view's options nor `emx`'s, only the global ones.
3. **Minimal example**:
   ```python
   tmux.set_global_option(RELAY_PASTE_OPT, str(clock()))   # remote.follow: the Ctrl+V gate reads it inside a viewer
   self.tmux.set_view_option(REOPEN_OPT, slot.view)        # this device's view only
   ```
4. **Gotcha**: an `emx follow` started before 2.10.0 keeps running on the server after an update and keeps stamping its paste offer on `emx`, where no viewer sees it, until he reconnects. The primary panel copies a live stamp to the global option (`carry_paste_stamp`). Moving an option to another scope needs the same bridge for whatever old process still writes the old one.

### Style-aware detectors are tested on styled captures

1. **Rule**: a detector that depends on styling is tested on `capture-pane -e` captures, with the cursor tmux reported recorded in `prompt-cursors.json` (interactive `claude`: `interactive-cursors.json`) together with the Claude Code version.
2. **Why**: the dim example text is the only thing that tells an empty prompt apart from typed text.
3. **Minimal example**:
   ```python
   core.empty_prompt(*at_cursor("prompt-empty"))   # line, line below, cursor x, shown, width
   ```
4. **Gotcha**: removing SGR codes changes what the test proves, so redact only inside the same styled span. SGR colour parameters such as `38;5;2` are not dim.

### emx closes a session only on a confirmed double Ctrl+X on its row or its project's header, or a double `x` in its phone actions menu

1. **Rule**: a session is ended and removed from emx only when `core.press_close` hands back what the first press armed: a second Ctrl+X on the same session row, or the same project header, or a second `x` (key or tap) in that session's phone actions menu, within `CONFIRM_SECONDS` of the first, with what the first press took still listed in the same running state (`core.confirm_holds`). A row arms a `Confirm`; a project header arms a `GroupConfirm` holding a `Confirm` for every session in the group, folded ones too; Pinned and the state groups never arm. The ids and names come from what the first press took, never from the cursor at the second. A worker (`Closer`) then closes each target in turn: it takes a fresh listing, and only if `core.close_still_applies` kills a running session's own pane (every view showing it gets the placeholder first), waits for a listing that shows it not running, and then `engine.forget`s it, which removes its folder under emx's state folder and nothing of Claude's. No other key, trigger or automatic path ends or forgets a session.
2. **Why**: closing ends his work in progress (Tural, 2026-10-13: "End it and remove it"). A stray key, a cursor the poll just moved, or a row a poll out of date must never cost him a session. The conversation stays in Claude Code's own history.
3. **Minimal example**:
   ```python
   self.confirm, act = core.press_close(self.confirm, self.cursor, row, time.monotonic(), self.view())
   if act is not None:
       self.closer = Closer(self.engine, core.close_targets(act), wait_for, self.me)   # lists, re-checks, kills, forgets
       self.closer.start()
   ```
4. **Gotcha**: the cursor and the rows can change without a key (a poll regrouping, ← from main), so `refresh()` re-checks `core.confirm_holds` every tick, not only on keys; a group's confirm drops when a session joins or leaves it. A session still listed running after the kill is not forgotten, and a forget is tried a few times, since the killed `claude`'s last hook can still be writing into the folder. And while a close runs its row must not open: an Enter on a session being ended would resume it. In the phone's actions menu the first `x` arms without interrupting the session first (a first Ctrl+X interrupts a working one), and a tap in the menu is passive to `core.confirm_after_key`, so two taps on the `x` line close; any other key or a tap outside the menu disarms.

### Claude Code's own files are read read-only, tail-bounded, off the curses thread, for listed sessions only

1. **Rule**: emx reads files Claude Code writes (under `CLAUDE_CONFIG_DIR`, else `~/.claude`) only from a worker thread, which the curses thread collects with `.take()`. A path a session's hooks reported is opened only when it lies under Claude's `projects` folder (after `realpath`) and is named `<that session's id>.jsonl`. It opens them `rb`, reads a bounded tail, or, for the name Tab asks for, a bounded head and tail of at most `NAME_READ_MAX_BYTES` (ADR 0003), and reads only files named after a `sessionId` (and, for a listed session whose own file has no turn, the bounded tails, `CONTINUED_TAIL_BYTES`, of the `<session-id>.jsonl` files in that session's own project folder, to find its continued-in predecessor; cached per file stat, backed off on a miss) in the latest listing that had any sessions (an empty listing is taken as a glitch, not as every session gone) that passes the id check (`core.is_session_id`). The one file it reads outside `projects` is `sessions/<pid>.json`, through `engine.bridge_link` only, for the pid of a live agent-tagged pane of emx's own: opened read-only with `O_NOFOLLOW|O_NONBLOCK` (a planted link or FIFO is refused, not followed or waited on), used only when `fstat` shows a regular file of this user's within the size bound, and only when its `pid` and `sessionId` match that pane's session; of it only `bridgeSessionId` is taken, and nothing else in that folder is listed or opened. `sidebar.py` gathers the bytes; `core.parse_transcript` turns them into a value and never raises.
2. **Why**: the contract allows reading `~/.claude` and forbids writing, moving or deleting anything there. Transcripts run to many megabytes and grow while being read, and a read on the curses thread would freeze the panel.
3. **Minimal example**:
   ```python
   # worker thread (Transcripts), for a sessionId from the listing
   if not core.is_session_id(uuid):
       return None
   with open(path, "rb") as f:
       while (want := core.tail_bytes(found, read, size)) is not None:
           f.seek(size - want)
           text = f.read(want).decode("utf-8", errors="replace")
           found, read = core.parse_transcript(text, partial=want < size), want

   # curses thread, in refresh()
   found = self.poller.transcripts.take()
   ```
4. **Gotcha**: a tail's first line is cut and its last line may be half-written, so neither is read as an entry: the first is dropped (`partial=True`) and a half-written last one fails to parse as JSON and is skipped. The project folder name is Claude Code's own mangling of the cwd and can be shortened, so a miss is looked up by `<sessionId>.jsonl` across the project folders, with backoff. A session's hooks, not its transcript, decide its state, with one exception: Claude Code fires no hook when Esc interrupts a turn or a permission is denied, so a working or blocked session whose transcript's latest request is an interrupt marker stamped after its latest work hook reads Idle (`core.interrupted_after`). The `/rename` name and Claude's title are read from the transcript and written into emx's own record, never back into Claude's files.

### A session counts as gone only after `STALE_AFTER_MISSES` listings in a row without it

1. **Rule**: emx acts on a session's absence from a listing (dropping its pin, discarding an open edit about it, closing its viewer) only after `core.STALE_AFTER_MISSES` successful listings in a row lack it, and a listing with no sessions at all counts as no listing. The count, the decision and the empty-listing rule live in core `stale_viewers`, or a helper built on it such as `prune_pins` or `edit_session_gone`. The one thing an empty listing still counts against is a viewer whose pane is dead (`dead=`), since a stopped session ends its viewer.
2. **Why**: a listing can come back empty or short for a moment (a record being rewritten, a read that failed). Acting on it closes or discards something he is using. A pane holding one of emx's own sessions is the session itself and is never closed on an absence; only old `claude attach` viewers are.
3. **Minimal example**:
   ```python
   misses, gone = core.edit_session_gone(sid, live_ids, misses)
   ```
4. **Gotcha**: each use keeps its own misses dict, or one use's count leaks into another's. The cleanup a panel does once when it starts (`App.clear_legacy_viewers`) does not wait for listings: it kills every old `claude attach` viewer (a key, no agent tag) at once, since emx's own registry is empty on upgrade day and an empty listing would never count against them.

### emx types into a session only through `Tmux.type_if`, `submit_if`, `clear_if` or `key_if`, after the listing and screen checks

1. **Rule**: before emx types anything into a session's pane, three things hold, in order. A fresh listing taken on a worker thread passes the listing refusal check for what is typed: running for a line, working allowed (`rename_listing_refusal`), running and working and not yet idle for Esc (`interrupt_listing_refusal`). The pane is the one tagged with the session's key and the agent (`core.live_viewer`); its tags, never its screen, prove which session it holds. The core screen checks pass on the pane as it is now: not in a mode, and for a line an empty prompt; for Esc the cursor is shown and a draft does not refuse. Then the keys go only through `Tmux.type_if` (a line; it goes in with `enter=False`), `Tmux.submit_if` (Enter), `Tmux.clear_if` (C-u) or `Tmux.key_if` (one allowlisted key), with a gate format built by `core.type_gate` pinned at the cursor the checks saw, so tmux re-checks the viewer and his focus and types in the same command. Enter follows only through `submit_if`, after `core.prompt_has_only_line` holds on a fresh read, with a gate pinned at `cursor_x = 2 + display_width(line)`. After `type_if`, while the line does not yet stand alone (Claude Code draws `/rename` with a grey `[name]` hint first), the reader reads again for up to `PROMPT_SETTLE_SECONDS`; the wait never stands in for the check. An unsent line is taken back only through `clear_if` while `core.prompt_has_only_line` holds. An empty prompt is never taken as proof that a session is idle: interactive `claude` keeps its input box open while it works.
2. **Why**: a check in Python and a separate `send-keys` leave a gap of 15–50 ms. A key of his that lands in it is submitted along with the line, and a viewer he scrolled back in the meantime (copy mode) takes the keys as tmux commands instead of text. Enter is a second step because (Tural, 2026-10-08, "Narrow it") a dialog can open in a working session between typing and Enter, and Enter on it would answer it. Esc needs no empty prompt because, tried on throwaway sessions, Esc during a turn interrupted it and left a draft in the prompt untouched.
3. **Minimal example**:
   ```python
   # worker thread: listing refusal, then screen refusal, then
   gate = core.type_gate(viewer.id, panel, sid, cursor_y, SESSION_OPT, cursor_x=x)
   if gate is not None:
       report = tmux.key_if(viewer.id, gate, "Escape", core.TYPED, core.gate_report(SESSION_OPT))
       refusal = core.gate_refusal(interrupt, report, cursor_y, cursor_x=x)
   ```
4. **Gotcha**: tmux reads a `;` at the end of an argument as a command separator even with `-l`, so the text goes as hex (`-H`). `display-message` reads `%` as a time format, so the report carries none. `-t` on a pane that is gone falls back to another pane, so the gate tests the pane id itself. An id enters a format only after it matches the plain-id pattern, or it could break out of the format. Fixed key names go through `_key_if` and bypass `KEYS_SENT`; never pass a caller's key there. One Esc worker per session at a time (`App.interrupters`, keyed by id): two Escs in quick succession into one session open Claude Code's rewind, while Escs into different sessions are independent.

### A cancelled worker acts no more after its cancel

1. **Rule**: a worker that may type into a session (the `Interrupter`) checks its `cancel` event before it sends anything, and a close of that session sets it and joins the worker (`Closer.wait_for`, up to `INTERRUPT_JOIN_SECONDS`) before it kills anything.
2. **Why**: an Esc landing in a pane being killed, or after a close has begun, acts on a session he has just decided to end.
3. **Minimal example**:
   ```python
   if self.cancel.is_set():
       return False, ""
   report = self.tmux.key_if(pane.id, gate, "Escape", core.TYPED, core.gate_report(SESSION_OPT))
   ```
4. **Gotcha**: a close held back by an interrupt that has not ended closes none of its targets and says so; it never closes half a group.

### The session list is measured in screen rows, not items

1. **Rule**: we size, scroll and hit-test the session list in screen rows. Each list line's height is the number of laid-out lines it draws (`core.row_lines` for a session, which an armed close label, a `closing…` label or a rename edit is padded to keep; 1 for a header or a spacer). `core.list_window` picks the scroll from those heights, `core.row_hits` builds `row_map` from them, and drawing walks the lines adding each one's height.
2. **Why**: a wrapped row takes several screen rows. Code that assumes one item per row scrolls the cursor off screen and sends a click to the wrong session.
3. **Minimal example**:
   ```python
   lines, rows, heights = self.list_layout(width)
   self.scroll = core.list_window(heights, at, self.scroll, height)
   self.row_map = core.row_hits(lines, heights, self.scroll, top, height)
   ```
4. **Gotcha**: anything that reads `row_map` or `scroll`, or reserves list space (the peek box), must count screen rows the same way, from heights worked out in the same frame they are drawn: a row's height changes with the width, its group's name column and its summary.

### Every `claude -p` call is built by a pure core function, isolated, budget-flagged, and run through a never-raising wrapper

1. **Rule**: every `claude -p` call emx makes has its command built by a pure function in `core` (`core.summary_args`, `core.name_args`) with the full isolation flag set plus `--max-budget-usd`; its input goes on stdin, never argv; it runs in an empty private temp folder as its cwd, on a worker thread, through a wrapper that never raises.
2. **Why**: without `--strict-mcp-config --setting-sources ""` one call cost $0.39; without `--no-session-persistence` each call writes a transcript under `~/.claude/projects`; input in argv can be read as a flag; a project folder as cwd loads its CLAUDE.md.
3. **Minimal example**: `subprocess.run(core.summary_args(claude), input=excerpt, cwd=scratch, ...)` in `sidebar.summarise`.
4. **Gotcha**: `--bare` fails on OAuth ("Not logged in"), so it is never the isolation. Pin the flag set in a test (`SummaryArgs`, `NameArgs`). Output length is not capped by the other flags, and `--max-budget-usd` is checked only after an answer, so it does not cap one call either: what bounds a call is the input caps, the instruction and the 30 s timeout (ADR 0002).

### Text emx speaks or posts goes through `core.clean_notice`, and reaches `say` or the helper only on stdin

1. **Rule**: every text emx speaks or shows in a notification (Haiku's words, the plain fallback line and the notification's title alike) goes through `core.clean_notice` first. `say` gets it on stdin, and `emx-notify` gets it as JSON on stdin, never in a command line or in generated code. Control characters are judged on code points (C0, DEL and C1), not bytes.
2. **Why**: `say` runs `[[ ]]` commands embedded in its text, and a leading `-` in a command line would be read as an option.
3. **Minimal example**: `proc.communicate(text)` in `Voice.speak_aloud`; `notify.run(command, "post", payload)` in `Notifier.step`.
4. **Gotcha**: a C1 control such as U+009C (ST) is two bytes in UTF-8 and slips past a byte check; the cleaning must run on the decoded string. A name or project taken from a session is untrusted too, so the fallback line is cleaned like Haiku's.

### The version lives in `emx/__init__.py`; `CHANGELOG.md` follows it

1. **Rule**: the version is held only in `emx/__init__.py` (`__version__`). `CHANGELOG.md` lists versions newest first as `## X.Y.Z — YYYY-MM-DD` headings, each followed by flat `- ` plain-language bullets, and the newest heading equals `__version__`.
2. **Why**: the panel parses the file for the what's-new note (`core.changelog_entry`, `core.whats_new_since`), and `test_the_shipped_changelog_leads_with_the_code_version` ties the two together.
3. **Minimal example**: the 1.0.0 entry in `CHANGELOG.md`.
4. **Gotcha**: no `###` subheadings (Keep-a-Changelog's "### Fixed"). Any `#` line that is not a version heading ends the entry, so lines after it silently never reach the panel.

### Every consumer of emx's sessions reads them through `engine.list_sessions`

1. **Rule**: whatever lists sessions (the panel, the counts, the notices, the workers' fresh listings) takes them from `sidebar.fetch_sessions`, which is `engine.list_sessions`, which builds them in `core.engine_sessions` from emx's own records, the hooks' records, the panes and the transcript rule.
2. **Why**: it is the one place a session's state, its name and whether it is running are decided, and the one place the `emx-test-` throwaway sessions checks start are dropped (by the name and folder in emx's own record), so none of them reach his panel, counts or spoken notices.
3. **Minimal example**:
   ```python
   sessions, error = fetch_sessions(self.engine)   # worker thread; None and an error when it can't be read
   ```
4. **Gotcha**: a test that feeds `emx-test-` sessions with a temporary cwd must set `EMX_SHOW_TEST_SESSIONS=1`, or they are dropped; one is dropped only when its cwd is under a temporary directory too, so a fixture cwd like `/p/alpha` is never hidden. An unreadable state folder is an error, never an empty list.

### A command from outside reaches the running panel through a session option, never a signal

1. **Rule**: a command run from outside (`emx hush`) reaches the running panel only by writing a fresh token (`time.time_ns()`) to a session user option on emx's own socket. The command checks `has_session()` first, uses no `-f`, and never starts a server. The panel reads the option on each housekeep tick and acts on a change, decided by a pure core function (`core.hush_due`); never a signal.
2. **Why**: a panel on an older version would be killed by SIGUSR1's default action, a handler could deadlock on `Voice.lock`, and an option on emx's socket cannot reach his default tmux server.
3. **Minimal example**:
   ```python
   tmux.set_session_option(HUSH_OPT, str(time.time_ns()))              # cli.hush
   self.hush_seen, due = core.hush_due(self.hush_seen, value)          # App.check_hush
   ```
4. **Gotcha**: the panel's first read is a baseline, so a write before it is ignored; a `-q` read cannot prove the option is unset, so an empty value is no change. `emx open <id>` (`@emx_open`, what a notification click runs) is the exception: it is a request, not a token, so the panel reads it once its first listing is in, clears it and acts on it; a request written before the panel started still counts.

### A pref that mirrors a state tmux holds is written from what tmux reports, never from the key that changed it

1. **Rule**: a pref that mirrors a state tmux holds is written from what tmux reports on the housekeep tick, never from the key that changed it. Hidden/shown is `#{window_zoomed_flag}`, adopted through `core.follow_zoom`; only `create_workspace` reads the pref back, at a fresh start, for the home view. Only the home view's panel (the primary) writes it; another view's hidden panel and width are its own and never saved.
2. **Why**: many paths change the state without his key (Ctrl+\, the left arrow, `swap-pane -d`, the auto-restore `select-pane`), and they all mean "shown".
3. **Minimal example**:
   ```python
   followed = core.follow_zoom(self.prefs, sidebar.zoomed)   # then save if changed
   ```
4. **Gotcha**: the pref lags by up to `HOUSEKEEP_SECONDS`; `select-pane` of the panel, `resize-pane -x` and `swap-pane -d` all unzoom, so width and layout code must check `zoomed` and hold while hidden.

### A command Claude Code runs as a hook never fails, speaks or reads past its folder

1. **Rule**: a command Claude Code runs as a hook always exits 0, writes nothing to stdout or stderr, reads its whole stdin, records only allowlisted scalar fields, and writes only into an existing emx session folder named by a key that passes `core.is_session_id`.
2. **Why**: Claude Code reads a hook's stdout as context or a decision and exit 2 as "block"; a broken hook must cost emx a state update, never cost him his session.
3. **Minimal example**:
   ```python
   return f"{words} >/dev/null 2>&1; exit 0"   # engine.hook_command: the shell forces success and silence
   except BaseException:                       # cli.hook: everything else is swallowed
       pass
   return 0
   ```
4. **Gotcha**: an interpreter that cannot open its script exits 2 before any Python runs, so success must be forced by the shell, never by the script; payloads can be megabytes, so read to the end and keep only the allowlisted fields.

### Every read-modify-write of a session's `session.json` runs under `engine._record_lock`

1. **Rule**: every read-modify-write of a session's `session.json` runs under `engine._record_lock(state, key)`, which locks `session.lock` in the same folder.
2. **Why**: `_write_atomic` replaces `session.json`'s inode, so a lock on that file would be lost with it; opening the lock file without creating the folder keeps a forgotten session gone.
3. **Minimal example**:
   ```python
   with _record_lock(state, key):
       record = _read_record(state, key)
       _write_atomic(path, {**record, field: value})
   ```
4. **Gotcha**: `start` writes without the lock because it creates the record; hold the lock only around these small reads and writes, never around a tmux or `claude` call.

### Anything emx builds or registers outside the real Application Support folder gets the test identity

1. **Rule**: the helper app emx builds anywhere but its own folder under Application Support gets the test identity ("emx test", `com.emalatxana.emx-notify-test`), and the real id is built nowhere else. The test id is never built in the real folder.
2. **Why**: macOS keeps one notification permission and one registration per bundle id; a throwaway build under the real id would take over his registration, and a test build in his folder would replace the app he uses.
3. **Minimal example**:
   ```python
   bundle = notify.bundle_id(env)
   app, error = notify.ensure_built(home, bundle)
   ```
4. **Gotcha**: `bundle_id` picks the test id for any absolute `EMX_NOTIFY_HOME`, including one that names the real folder, so `ensure_built` is the guard that refuses that pairing; a new place that builds or registers must go through both.

### Anything emx runs on another machine goes through the ssh builders, and what comes back through `parse_relay`

1. **Rule**: an ssh command is built only by `core.ssh_master_args`, `core.ssh_client_args` or `core.ssh_exit_args`, the host comes from `core.remote_host`, and the command on the server from `core.remote_shell`. Every line that comes back from the server passes `core.parse_relay` before it reaches `say`, the helper or an argv.
2. **Why**: the host and the command are surfaces for ssh options and shell code, and relay lines come from another machine, so neither is trusted as given.
3. **Minimal example**:
   ```python
   host = core.remote_host(arg)
   argv = core.ssh_client_args(ssh, host, control, core.remote_shell(base, "follow"))
   ```
4. **Gotcha**: `remote_shell` quotes the args but not `base` (`EMX_REMOTE_COMMAND` may carry `env X=Y`), so `base` must only come from the laptop's own environment, never from the server or a relay line.

### A change that would stop an older and a newer emx working together over `emx remote` is a major version

1. **Rule**: what one emx calls or reads of another emx is a contract between versions. Removing it, renaming it, or changing it so an older caller or reader breaks bumps the major version. Over `emx remote` that is every server subcommand the laptop runs and every line it reads back. On one machine it is everything an older emx left behind that the newer `current` runs: the `hook <state> <key>` argv in a session's `settings.json`, the `relaunch <socket>` an old panel starts, `emx.command`, a notification's `open` command and the `left` binding.
2. **Why**: patch and minor updates land on each machine on their own, at different times (#60 Decided 4, 5, 7), so a laptop and a server one minor apart, or an old panel and the new tree it switches to, must always keep working together; only a major waits for his yes on both.
3. **Minimal example**: the laptop runs `--version`, `follow`, `open --no-window <id>` and `update --report` on the server and reads `emx X.Y.Z`, the relay records through `parse_relay`, and one `emx-update …` line through `parse_update_report`.
4. **Gotcha**: adding a subcommand, a line or a relay field that the older side ignores is not a major; dropping a field an older `parse_relay` requires, or changing the words of the `emx-update` line, is. A session's hook argv outlives the version that wrote it until the session is resumed, so the newest `current` must still accept every older form.

### Anything new one emx hands another over `emx remote` travels in an `env VAR=value` prefix, and a value written for another machine to read is validated again on read

1. **Rule**: what a newer emx adds to its call on the server goes in an `env VAR=value` prefix built by a core builder over `core.remote_shell` (`core.follow_shell`), never as a new positional argument. A value one machine writes into a session option for another to read carries its own timestamp and is validated again in core when read (`core.relay_host`).
2. **Why**: an older server ignores an unknown variable but rejects an unknown argument, and a session option outlives its writer, so what is read back may be stale or from a version that wrote it differently.
3. **Minimal example**:
   ```python
   shell = core.follow_shell(base, host_name)
   host = core.relay_host(option_value, time.time())
   ```
4. **Gotcha**: `env` finds only real files on PATH, so an `emx` that exists only as an alias needs `EMX_REMOTE_COMMAND`. `@emx_relay` stays a bare float, because an older panel parses it as one.

### A transcript path from a hook record goes through `engine.allowed_transcript` first

1. **Rule**: a Claude transcript emx opens at a path taken from a hook record passes `engine.allowed_transcript(path)` first and is opened only at the real path it returns. (The older `Transcripts.locate` in `emx/sidebar.py` does its own equivalent check; new code uses the helper.)
2. **Why**: transcript paths arrive from hook records and could point anywhere; the guard keeps reads inside Claude's projects folder and only on `<uuid>.jsonl` files.
3. **Minimal example**:
   ```python
   real = engine.allowed_transcript(path)
   if real:
       with open(real, encoding="utf-8") as fh:
           ...
   ```
4. **Gotcha**: the guard checks name and location, not file type, so reads stay on a worker thread, never the curses thread.

### Every public command, key and setting appears in `user-docs/reference.md` as its own code span

1. **Rule**: every public command, panel key, root key and public `EMX_` setting is listed in `user-docs/reference.md` as its own backtick code span, and `tests/test_docs.py` fails when one is missing.
2. **Why**: the docs are what Tural reads, and a drift test is the only thing that notices a key added without a line.
3. **Minimal example**: ``| `^g` | Go somewhere. |``
4. **Gotcha**: the test matches the exact backticked form, so `emx update` is not satisfied by `emx update off`. Keys handled only in `handle_key` or `prompt_key` (Tab, Esc, the folder list's `^t`, `,` and `.`) are not drift-tested and are kept by hand.

### Text that reaches a tmux format goes through `sidebar.bar_format`

1. **Rule**: anything shown in a tmux format string (the bar) is built by `sidebar.bar_format`, which doubles every `#` and `%` in the text; styles are only emx's own `#[...]` tags that `bar_format` adds.
2. **Why**: the text is untrusted (session, folder and tool names, Haiku output). In a tmux format `#(...)` runs a shell command, `#[...]` restyles, and `%` goes through strftime.
3. **Minimal example**: ``sidebar.bar_format([(session_name, "muted", False)])``
4. **Gotcha**: escape after cutting with `fit()`, never before, or a cut can split a doubled `##` and leave a lone `#`. Set the option on the session (`-t =emx:`), not globally.

### Phone behaviour is gated on the window's width, never writes prefs, and the wide path is held by a golden

1. **Rule**: anything that behaves differently on a phone is gated on `core.narrow(window_width)` in Python and on `NARROW_WINDOW` (the window narrower than `core.NARROW_BELOW`) in a tmux binding, with the laptop's behaviour as the other branch. A narrow view never writes prefs: zoom, hidden panel and widths saved are the laptop's. Before a change to a path the laptop also takes, what that path gives is captured from the previous release tag's code into a committed fixture, and a test holds the wide output to it byte for byte.
2. **Why**: in #67 every attached device gets its own view, and the laptop looks and works as it did before. A width check in one place and the prefs shared by every view mean a phone can never move the laptop's layout, and a golden from the old tag catches a wide change that a test computed by the code under test would not.
3. **Minimal example**:
   ```python
   if self.narrow:            # core.narrow(self.window_width)
       self.tend_narrow(sidebar, main)
   # binding: if-shell -F '{NARROW_WINDOW}' '{OTHER_ALONE}' 'select-pane -t :.+'
   # golden: tests/fixtures/bar/wide-2.10.0.json, checked by WideBarIsUnchanged
   ```
4. **Gotcha**: `select-pane` unzooms the window, so on a phone the zoom comes after the select (`OTHER_ALONE`, `Tmux.show_alone`), never before. A golden is captured before the edit, from the tag, never regenerated from the branch.

## The contract for this file

**Every entry has four parts, in this order:**

1. **Rule**: stated as a positive directive ("we do X"), precise enough to apply in a situation the entry never mentions.
2. **Why**: the reason it exists. An entry without a why gets cargo-culted and then broken by the first person who has a reason.
3. **Minimal example**: the smallest snippet that shows the shape. Minimal, not representative.
4. **Gotcha**: the thing that bites when you follow the rule naively. If there genuinely isn't one, say so; don't invent one.

**Entries arrive from the auditor.** The independent auditor reads every diff with fresh eyes, which is exactly the position from which a new convention is visible. Its verdict says whether the change **established** a convention, which becomes an entry here, or **violated** one, which is a finding to fix in the review loop. Nobody writes speculative conventions in advance: an entry describes what a merged change actually did.

**This is house style, not tutorial.** It says what this codebase does, not how the language works. And it obeys the [house rules](./README.md#house-rules-for-editing-these-docs); in particular, no code-knowable facts: an entry that lists exact signatures or versions is stale the moment someone edits the code.

**Decisions belong in [`adr/`](./adr/README.md), not here.** This file carries the *how we write it*; the ADR carries the *why we chose it at all*, with the alternatives that lost. A new library, a new third-party service, or a major version bump earns an ADR, not a convention entry.
