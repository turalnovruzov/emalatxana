<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Conventions

The house style for writing code in this repo: how *this* codebase does things, so the next session does them the same way instead of inventing a second way.

## Entries

### Every tmux call goes through `emx.tmux.Tmux`

1. **Rule**: we reach tmux only through the `Tmux` wrapper in `emx/tmux.py`, which puts emx's private `-L` socket on every command. New tmux behaviour becomes a method on it, never a bare `tmux` in a subprocess call or a shell string.
2. **Why**: Tural runs his own tmux server all day. A command without the private socket lands on his server and can rebind keys or kill panes in his real work.
3. **Minimal example**:
   ```python
   tmux.some_action(pane)        # a Tmux method; it adds -L <socket>
   # never: subprocess.run(["tmux", ...])
   ```
4. **Gotcha**: `-f` (emx's config file) only takes effect when the server starts, so changing the config does nothing to a running workspace until its server restarts. And the socket name must never be `default` or empty, because that is his everyday server; the launcher refuses those values.

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

1. **Rule**: a function that runs an outside command (`claude`, and anything like it) returns `(value, None)` on success or `(None, one-line error)` on failure, and never raises. The caller shows the error line on screen.
2. **Why**: the sidebar runs for hours in a pane. One escaped exception kills the poller thread or the curses loop, and he is left with a frozen or dead panel.
3. **Minimal example**:
   ```python
   value, error = run_some_command(...)
   if error:
       show(error)                  # one line on screen; nothing was raised
   ```
4. **Gotcha**: "never" includes decoding. Output with invalid UTF-8 raises inside `subprocess.run` before your own `try` around parsing sees it, so decode with `errors="replace"`. Threads and the main loop still catch everything as a backstop.

## The contract for this file

**Every entry has four parts, in this order:**

1. **Rule**: stated as a positive directive ("we do X"), precise enough to apply in a situation the entry never mentions.
2. **Why**: the reason it exists. An entry without a why gets cargo-culted and then broken by the first person who has a reason.
3. **Minimal example**: the smallest snippet that shows the shape. Minimal, not representative.
4. **Gotcha**: the thing that bites when you follow the rule naively. If there genuinely isn't one, say so; don't invent one.

**Entries arrive from the reviewer.** The independent reviewer reads every diff with fresh eyes, which is exactly the position from which a new convention is visible. Its verdict says whether the change **established** a convention, which becomes an entry here, or **violated** one, which is a finding to fix in the review loop. Nobody writes speculative conventions in advance: an entry describes what a merged change actually did.

**This is house style, not tutorial.** It says what this codebase does, not how the language works. And it obeys the [house rules](./README.md#house-rules-for-editing-these-docs); in particular, no code-knowable facts: an entry that lists exact signatures or versions is stale the moment someone edits the code.

**Decisions belong in [`adr/`](./adr/README.md), not here.** This file carries the *how we write it*; the ADR carries the *why we chose it at all*, with the alternatives that lost. A new library, a new third-party service, or a major version bump earns an ADR, not a convention entry.
