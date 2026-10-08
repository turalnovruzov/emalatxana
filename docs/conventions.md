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
4. **Gotcha**: logic creeps into the wrappers. A condition written inline in `sidebar.py` "because it needs the pane" is untested; pass the pane's text or fields into a core function instead.

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
4. **Gotcha**: redact only lines the detector does not read. Editing a rule line, the `❯` line, the agent header or the footer changes what the test proves. The captures are pinned to the Claude Code version they came from, so recapture when it changes.

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

1. **Rule**: before emx runs `claude attach` (a new viewer window, a respawn, a restart), it passes the session's row and a named trigger (`OPEN`, `RESTART`, `NEW`) to `core.attach_allowed`. Only `OPEN`, his explicit Enter or click, may attach a session with no live process, and any trigger other than `OPEN` is judged on a listing a worker thread took for that attach (`Checker`'s, through `core.checked_row`, for `RESTART`; `Starter`'s, from `wait_until_running`, for `NEW`), never on one taken on the curses thread.
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
4. **Gotcha**: the panel's rows come from the last poll, which can be a poll old, or older while listing fails. "Running" means an integer pid and a state that isn't stopped, never `state` alone. The answer arrives later, so before acting check that it is still about what is on screen (`core.check_still_applies`): he may have opened another session in main meanwhile.

### `claude` and git calls never run on the curses thread

1. **Rule**: any call to the `claude` CLI or git runs on a worker thread. One-shot workers (`Starter`, `Checker`) are started by the curses thread, which checks `is_alive()` from `refresh()` and then reads `.result`. Long-running ones (`Poller`, and the `Resolver` it starts) loop on their own, and `refresh()` collects their latest answer with `.take()`, which hands it over under a lock and returns None when nothing is new. The curses thread never calls `list_rows`, `fetch_sessions` or `run_git` itself.
2. **Why**: `claude agents` can take up to its 15 s timeout. On the curses thread that freezes the panel, and every key he presses meanwhile is lost or queued.
3. **Minimal example**:
   ```python
   self.checker = Checker(self.claude, (main.session, main.id))
   self.checker.start()
   # later, in refresh():
   checker = self.checker
   if checker is not None and not checker.is_alive():
       sessions, _ = checker.result or (None, "listing sessions failed")
   ```
4. **Gotcha**: the answer can be stale on arrival. Record what was asked (here the session and main-pane id), and drop the answer when that no longer holds. Keep one worker per kind at a time, so repeated keys don't stack calls.

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

### emx stops or removes a session only on a confirmed double Ctrl+X on that row

1. **Rule**: `claude stop` and `claude rm` run only when `core.press_close` hands back a `Confirm`: a second Ctrl+X on the same session row within `CONFIRM_SECONDS` of the first, with that row still in the running state it had at the first press. The id, name and command come from the `Confirm` taken at the first press, never from the cursor at the second. A worker (`Closer`) then takes a fresh listing and runs the command only if `core.close_still_applies`. No other key, trigger or automatic path stops or removes a session, and no call passes `--discard-unpushed` or `--force-remove-worktree`.
2. **Why**: stopping ends his work in progress, and `claude rm` deletes the session and its worktree. A stray key, a cursor the poll just moved, or a row a poll out of date must never cost him a session.
3. **Minimal example**:
   ```python
   self.confirm, act = core.press_close(self.confirm, self.cursor, row, time.monotonic())
   if act is not None:
       self.closer = Closer(self.claude, act)   # lists, re-checks, then stop or rm
       self.closer.start()
   ```
4. **Gotcha**: the cursor and the row can change without a key (a poll regrouping, ← from main), so `refresh()` re-checks `core.confirm_holds` every tick, not only on keys. And the command follows `running`, not the shown state: a Done row whose process has exited is removed, not stopped.

## The contract for this file

**Every entry has four parts, in this order:**

1. **Rule**: stated as a positive directive ("we do X"), precise enough to apply in a situation the entry never mentions.
2. **Why**: the reason it exists. An entry without a why gets cargo-culted and then broken by the first person who has a reason.
3. **Minimal example**: the smallest snippet that shows the shape. Minimal, not representative.
4. **Gotcha**: the thing that bites when you follow the rule naively. If there genuinely isn't one, say so; don't invent one.

**Entries arrive from the reviewer.** The independent reviewer reads every diff with fresh eyes, which is exactly the position from which a new convention is visible. Its verdict says whether the change **established** a convention, which becomes an entry here, or **violated** one, which is a finding to fix in the review loop. Nobody writes speculative conventions in advance: an entry describes what a merged change actually did.

**This is house style, not tutorial.** It says what this codebase does, not how the language works. And it obeys the [house rules](./README.md#house-rules-for-editing-these-docs); in particular, no code-knowable facts: an entry that lists exact signatures or versions is stale the moment someone edits the code.

**Decisions belong in [`adr/`](./adr/README.md), not here.** This file carries the *how we write it*; the ADR carries the *why we chose it at all*, with the alternatives that lost. A new library, a new third-party service, or a major version bump earns an ADR, not a convention entry.
