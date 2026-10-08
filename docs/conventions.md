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

### Every `claude attach` asks `core.attach_allowed` first

1. **Rule**: before emx runs `claude attach` (a new viewer window, a respawn, a restart), it passes the session's row and a named trigger (`OPEN`, `RESTART`, `NEW`, `INTERRUPT`) to `core.attach_allowed`. Only `OPEN`, his explicit Enter or click, may attach a session with no live process, and any trigger other than `OPEN` is judged on a listing a worker thread took for that attach (`Checker`'s, through `core.checked_row`, for `RESTART`; `Starter`'s, from `wait_until_running`, for `NEW`; the `Interrupter`'s own, for `INTERRUPT`), never on one taken on the curses thread. `INTERRUPT` attaches a running session only, and opens its viewer in a hidden window with no swap and no select, so his focus stays on the panel.
2. **Why**: attaching to a stopped session resumes it, and he must be the only one who reopens a session he stopped.
3. **Minimal example**:
   ```python
   # worker thread (Checker): one listing, taken now
   self.result = fetch_sessions(self.claude)

   # curses thread: judges, never lists
   sessions, _ = checker.result or (None, "listing sessions failed")
   checked = core.checked_row(sessions, sid)          # (session, fresh)
   if trigger == core.OPEN:
       session, fresh = self.session(sid), False      # the row he clicked
   else:
       session, fresh = checked or (None, False)
       fresh = fresh and not self.list_error
   if core.attach_allowed(session, trigger, fresh):
       self.tmux.respawn(pane, core.viewer_command(self.claude, sid))
   ```
4. **Gotcha**: the panel's rows can be a poll old, or older while listing fails. "Running" means an integer pid and a state that isn't stopped, never `state` alone. The answer arrives later, so before acting check that it is still about what is on screen (`core.check_still_applies`): he may have opened another session in main meanwhile. A worker that may attach must stand down when a close of that session starts (the `Interrupter`'s `cancel`) and kill any hidden viewer it opened before it ends, because `new_hidden_window` returns before `claude attach` has connected. The close waits for it to end, and closes nothing if the cancelled worker's kill failed and its pane is still listed; otherwise a starting attach could land after the stop and resume the session. A hidden viewer left by an interrupt that had already ended (timed out or failed) before the close began is not covered. For the same reason a `RESTART`, by `r` or the automatic restore, of a session in `App.closing` is refused, and the automatic restore does not start for one.

### `claude` calls, git calls and filesystem scans never run on the curses thread

1. **Rule**: any call to the `claude` CLI or git runs on a worker thread. One-shot workers (`Starter`, `Checker`) are started by the curses thread, which checks `is_alive()` from `refresh()` and then reads `.result`. Long-running ones (`Poller`, and the `Resolver` it starts) loop on their own, and `refresh()` collects their latest answer with `.take()`, which hands it over under a lock and returns None when nothing is new. The curses thread never calls `list_rows`, `fetch_sessions` or `run_git` itself. Listing or stat-ing any folder the picker reads follows the same rule (startup reads of home, the prefs file and emx's own `CHANGELOG.md` excepted): the folder picker asks `Listings.get` or `Listings.readable`, which return whatever is cached (None until the first read lands) and starts a short-lived thread per folder, never two for one folder and at most `MAX_LISTINGS` at once; the curses thread judges a typed path from those cached reads (`core.folder_exists`) and dedupes paths with `normpath` only.
2. **Why**: `claude agents` can take up to its 15 s timeout, and a folder on a hung mount can block a listing or a stat for good. On the curses thread either freezes the panel, and every key he presses meanwhile is lost or queued.
3. **Minimal example**:
   ```python
   self.checker = Checker(self.claude, (main.session, main.id))
   self.checker.start()
   # later, in refresh():
   checker = self.checker
   if checker is not None and not checker.is_alive():
       sessions, _ = checker.result or (None, "listing sessions failed")
   ```
4. **Gotcha**: the answer can be stale on arrival. Record what was asked (here the session and main-pane id), and drop the answer when that no longer holds. Keep one worker per kind at a time, so repeated keys don't stack calls; interrupts are one per session, so a second working row is interrupted while the first still is; folder listings are the other exception, one per folder under a cap, because a hung folder must not stop other folders being listed, and it keeps its slot until it returns. A listing or a poll that lands late moves the picker's rows, so a highlight is kept by path, not by row.

### A tmux key binding that intercepts a key fails open

1. **Rule**: a root binding first checks a cheap `if-shell -F` format condition. Only when that passes does it run `bin/emx <check>`, whose exit 0 means intercept. Every other outcome (a non-zero exit, an exception, an unknown socket, a target pane gone) sends the original key through with `send-keys`. The check's command lives in a user option, never inlined in the binding.
2. **Why**: a broken check must cost him the shortcut, never a keypress. Keeping the command in an option avoids tmux quoting of paths.
3. **Minimal example** (the ← binding as `list-keys` shows it, abridged):
   ```
   bind-key -n Left if-shell -F "#{&&:<at an empty prompt>,#{P:#{?#{==:#{pane_id},%1},1,}}}" \
       "if-shell \"#{@emx_left} #{pane_id} …\" \"set-option -p -t %1 @emx_back 1 ; select-pane -t %1\" \"send-keys Left\"" \
       "send-keys Left"
   ```
4. **Gotcha**: `if-shell` without `-b` holds that client's key queue until the check returns. That keeps keys in order but makes him wait, so keep the format condition tight and the check fast (about 50 ms). And a failed `select-pane` has no "else": it ends the command list and the key is gone, so a pane the binding targets is tested in the format condition first.

### Style-aware detectors are tested on styled captures

1. **Rule**: a detector that depends on styling is tested on `capture-pane -e` captures, with the cursor tmux reported recorded in `prompt-cursors.json` together with the Claude Code version.
2. **Why**: the dim example text is the only thing that tells an empty prompt apart from typed text.
3. **Minimal example**:
   ```python
   core.empty_prompt(*at_cursor("prompt-empty"))   # line, line below, cursor x, shown, width
   ```
4. **Gotcha**: removing SGR codes changes what the test proves, so redact only inside the same styled span. SGR colour parameters such as `38;5;2` are not dim.

### emx closes a session only on a confirmed double Ctrl+X on its row or its project's header

1. **Rule**: `claude stop` and `claude rm` run only when `core.press_close` hands back what the first press armed: a second Ctrl+X on the same session row, or the same project header, within `CONFIRM_SECONDS` of the first, with what the first press took still listed in the same running state (`core.confirm_holds`). A row arms a `Confirm`; a project header arms a `GroupConfirm` holding a `Confirm` for every session in the group, folded ones too; Pinned and the state groups never arm. The ids, names and commands come from what the first press took, never from the cursor at the second. A worker (`Closer`) then closes each target in turn: it takes a fresh listing, and only if `core.close_still_applies` runs `core.close_args`, which stops a running session and then removes it, and only removes any other. No other key, trigger or automatic path stops or removes a session, and no call passes `--discard-unpushed` or `--force-remove-worktree`.
2. **Why**: closing ends his work in progress, and `claude rm` deletes the session and its worktree. A stray key, a cursor the poll just moved, or a row a poll out of date must never cost him a session.
3. **Minimal example**:
   ```python
   self.confirm, act = core.press_close(self.confirm, self.cursor, row, time.monotonic(), self.view())
   if act is not None:
       self.closer = Closer(self.claude, core.close_targets(act))   # lists, re-checks, then stop and rm
       self.closer.start()
   ```
4. **Gotcha**: the cursor and the rows can change without a key (a poll regrouping, ← from main), so `refresh()` re-checks `core.confirm_holds` every tick, not only on keys; a group's confirm drops when a session joins or leaves it. The command follows `running`, not the shown state: a Done row whose process has exited is only removed. And while a close runs its row must not open: an Enter on a session being stopped would resume it.

### Claude Code's own files are read read-only, tail-bounded, off the curses thread, for listed sessions only

1. **Rule**: emx reads files Claude Code writes (under `CLAUDE_CONFIG_DIR`, else `~/.claude`) only from a worker thread, which the curses thread collects with `.take()`. It opens them `rb`, reads a bounded tail, and reads only files named after a `sessionId` in the current listing that passes the id check (`core.is_session_id`). `sidebar.py` gathers the bytes; `core.parse_transcript` turns them into a value and never raises.
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
4. **Gotcha**: a tail's first line is cut and its last line may be half-written, so neither is read as an entry: the first is dropped (`partial=True`) and a half-written last one fails to parse as JSON and is skipped. The project folder name is Claude Code's own mangling of the cwd and can be shortened, so a miss is looked up by `<sessionId>.jsonl` across the project folders, with backoff. The listing, not the transcript, decides a session's state.

### A session counts as gone only after `STALE_AFTER_MISSES` listings in a row without it

1. **Rule**: emx acts on a session's absence from a listing (dropping its pin, discarding an open edit about it) only after `core.STALE_AFTER_MISSES` successful listings in a row lack it, and a listing with no sessions at all counts as no listing. The count and the decision live in core (`stale_viewers`, or a helper built on it such as `prune_pins` or `edit_session_gone`). Viewer closing (`App.close_stale`) counts misses but does not yet skip empty listings.
2. **Why**: `claude agents` can return one glitchy or empty result. Acting on it closes or discards something he is using.
3. **Minimal example**:
   ```python
   misses, gone = core.edit_session_gone(sid, live_ids, misses)
   ```
4. **Gotcha**: each use keeps its own misses dict, or one use's count leaks into another's. And check for the empty listing before counting: `stale_viewers` alone counts an empty listing as a miss for everything.

### emx types into a session only through `Tmux.type_if` or `Tmux.key_if`, after the listing and screen checks

1. **Rule**: before emx types anything into a session's viewer, three things hold, in order. A fresh listing taken on a worker thread passes the listing refusal check for what is typed: running and not working for a line (`rename_listing_refusal`), running and working and not yet idle for Esc (`interrupt_listing_refusal`). The core screen checks pass on the viewer as it is now: not in a mode, showing this session and no other, and for a line an empty prompt; for Esc the cursor is shown and a draft does not refuse. Then the keys go only through `Tmux.type_if` (a line and Enter) or `Tmux.key_if` (one allowlisted key, no Enter), with a gate format built by `core.type_gate` pinned at the cursor the checks saw, so tmux re-checks the viewer and his focus and types in the same command.
2. **Why**: a check in Python and a separate `send-keys` leave a gap of 15–50 ms. A key of his that lands in it is submitted along with the line, and a viewer he scrolled back in the meantime (copy mode) takes the keys as tmux commands instead of text. Esc needs no empty prompt because, tried on throwaway sessions, Esc during a turn interrupted it and left a draft in the prompt untouched.
3. **Minimal example**:
   ```python
   # worker thread: listing refusal, then screen refusal, then
   gate = core.type_gate(viewer.id, panel, sid, cursor_y, SESSION_OPT, cursor_x=x)
   if gate is not None:
       report = tmux.key_if(viewer.id, gate, "Escape", core.TYPED, core.gate_report(SESSION_OPT))
       refusal = core.gate_refusal(interrupt, report, cursor_y, cursor_x=x)
   ```
4. **Gotcha**: tmux reads a `;` at the end of an argument as a command separator even with `-l`, so the text goes as hex (`-H`). `display-message` reads `%` as a time format, so the report carries none. `-t` on a pane that is gone falls back to another pane, so the gate tests the pane id itself. An id enters a format only after it matches the plain-id pattern, or it could break out of the format. One Esc worker per session at a time (`App.interrupters`, keyed by id): two Escs in quick succession into one session open Claude Code's rewind, while Escs into different sessions are independent.

### A cancelled worker ends what it started before it returns

1. **Rule**: a worker thread that is cancelled ends any long-lived process or pane it started (the `Interrupter`'s hidden viewer) before it returns, and records whether it could (`ended`).
2. **Why**: a waiter's join must mean nothing the worker launched can still act. A `claude attach` that is still starting reopens a session the close has just stopped.
3. **Minimal example**:
   ```python
   if self.cancel.is_set():
       self.tmux.kill_pane(opened)
       return
   ```
4. **Gotcha**: `new_hidden_window` returns before the command inside it has done anything, so the pane must be killed even when the worker never saw the viewer come up. A kill that fails on a pane already gone is not a failure; check `panes()` before calling it one.

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

### The version lives in `emx/__init__.py`; `CHANGELOG.md` follows it

1. **Rule**: the version is held only in `emx/__init__.py` (`__version__`). `CHANGELOG.md` lists versions newest first as `## X.Y.Z — YYYY-MM-DD` headings, each followed by flat `- ` plain-language bullets, and the newest heading equals `__version__`.
2. **Why**: the panel parses the file for the what's-new note (`core.changelog_entry`, `core.whats_new_since`), and `test_the_shipped_changelog_leads_with_the_code_version` ties the two together.
3. **Minimal example**: the 1.0.0 entry in `CHANGELOG.md`.
4. **Gotcha**: no `###` subheadings (Keep-a-Changelog's "### Fixed"). Any `#` line that is not a version heading ends the entry, so lines after it silently never reach the panel.

## The contract for this file

**Every entry has four parts, in this order:**

1. **Rule**: stated as a positive directive ("we do X"), precise enough to apply in a situation the entry never mentions.
2. **Why**: the reason it exists. An entry without a why gets cargo-culted and then broken by the first person who has a reason.
3. **Minimal example**: the smallest snippet that shows the shape. Minimal, not representative.
4. **Gotcha**: the thing that bites when you follow the rule naively. If there genuinely isn't one, say so; don't invent one.

**Entries arrive from the reviewer.** The independent reviewer reads every diff with fresh eyes, which is exactly the position from which a new convention is visible. Its verdict says whether the change **established** a convention, which becomes an entry here, or **violated** one, which is a finding to fix in the review loop. Nobody writes speculative conventions in advance: an entry describes what a merged change actually did.

**This is house style, not tutorial.** It says what this codebase does, not how the language works. And it obeys the [house rules](./README.md#house-rules-for-editing-these-docs); in particular, no code-knowable facts: an entry that lists exact signatures or versions is stale the moment someone edits the code.

**Decisions belong in [`adr/`](./adr/README.md), not here.** This file carries the *how we write it*; the ADR carries the *why we chose it at all*, with the alternatives that lost. A new library, a new third-party service, or a major version bump earns an ADR, not a convention entry.
