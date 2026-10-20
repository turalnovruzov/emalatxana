# 0010. Each device gets its own view, which shows a session by attaching to it

- **Status**: Accepted; built in [#67](https://github.com/turalnovruzov/emalatxana/issues/67) (emx 2.10.0). Changes how [0005](./0005-emx-runs-agents-in-its-own-tmux.md) shows a session: the session's pane is no longer swapped into the panel's window.
- **Date**: 2026-10-18

## Context

Issue #67: working from the phone like from the laptop. Tural: *"different front ends should be able to have different views"*, and on a separate view per device, *"Now, in the same build"*; the laptop must not change (*"no need for changes for laptop"*), and of an earlier prototype, *"I don't want to lose sessions in the middle of work."* Refinement chose Path A (Decided 13, 18): a view shows a session by attaching to it instead of moving its pane, so two views can each show their own.

Until 2.9.0 the workspace was one tmux session, `emx`, whose first window held the panel and a main pane, and showing a session swapped that session's own pane into main ([0005](./0005-emx-runs-agents-in-its-own-tmux.md)). tmux keeps a pane in one window, so a swapped pane can be shown in one place only, and every terminal attached to `emx` saw the same thing.

## Decision

- **Every session's pane stays where it is,** in a hidden window of `emx`, and that window is also linked (`link-window`) into a tmux session of its own, `emx-a-<key>`, with no status line and `detach-on-destroy on`. Linking, not moving, keeps every session in `emx`, so the listing, the paste and relay options and a panel still running old code during an upgrade see the workspace as before.
- **A view is a tmux session with one window: a panel and a slot.** `emx`'s own first window is the home view, the laptop's; each further terminal gets `emx-view-<n>`. A wide terminal gets `emx` while no client has it; any other the free view used last, else a new one.
- **The slot shows a session through a viewer:** a client of the same tmux server, `tmux -L <socket> attach-session -t =emx-a-<key>:`, run in the slot. With nothing to show, the slot runs the placeholder. A window takes the size of the client that used it last, so a session open on two devices at once has one width, the last-used device's.
- **One primary panel.** The panel of `emx` itself runs what is done once per workspace: Haiku's row lines (written to `summaries.json` for the others), the notices, the voice, the hush, the updates. Every other view's panel lists sessions and reads transcripts for itself.
- **Per device:** which session is open, whether the panel is hidden, the view's width. **Shared, in prefs:** side, favourites, pins, recent folders, list view and folded groups, notice mode. The saved panel width and hidden panel are the laptop's: only the home view writes them. Each panel merges its own changes into the prefs on file when it saves.
- **Keys act on the window they are pressed in** (`:.+` is the other pane of a view's two), so each view's Ctrl+\\, Ctrl+Q and ← act on that view only. In a viewer the keys pass the root table twice: the outer level takes emx's own keys, and what it passes on reaches the session through the viewer, whose window has no panel. Ctrl+V's paste offer is a global option so the viewer's level sees it.
- **Mouse and copy through a viewer.** The viewer's session keeps the mouse on, as every emx session has it, so the viewer asks the outer level for the mouse and gets every mouse event: a drag, a wheel or a click in a slot is handled one level down exactly as 2.9.0 handled it on the session's own pane (copy mode on the session's real history, or passed to the program when it asked for the mouse). A copy finished there is sent as the clipboard escape to the viewer, not to his terminal, and `set-clipboard` stays `external`, so tmux's `copy-command` runs `emx copied`, which sends the copied text to the terminal showing that viewer (`load-buffer -w -t <that client>`). The viewer that made the copy is the one with the latest input among those attached to the session, which the copy itself is.
- **The upgrade moves a running workspace without killing a session's pane.** `emx` and the auto update's `emx relaunch` run one migration: a session's pane in the home view goes back to a hidden window in exchange for the placeholder, kept at its size, and its key is noted (`@emx_reopen`), so the new panel shows it again; a leftover spacer beside a session's pane is dropped; every session's pane is linked. A second run finds nothing to do. A view other than `emx` with no client attached parks what it shows the same way and shows it again on the next attach. The home view keeps its viewer while the laptop is away, so the session is there at once when he comes back, as before; on his return the viewer is switched to the session it already shows, which makes it the session's latest client again, so the session takes the laptop's width back without restarting the viewer.

## Consequences

- Phone and laptop attached at once each have their own panel, open session, hidden panel and width; nothing one does changes the other's layout, except the one shared width of a session open on both.
- Two laptop terminals attached at once no longer mirror each other: each gets a view of its own.
- A device other than the laptop that goes away stops showing its session, so it no longer holds that session's width, and shows it again when it comes back. The laptop's view keeps showing it and claims the width back on his return.
- A notice is skipped while any attached, focused, recently used device shows that session; a notification click opens in the wide view used last.
- Every copy in copy mode now starts `emx copied`, which does nothing for a copy made outside a viewer. On the laptop the copy, the wheel, the click, Shift+Enter, colours and the cursor shape behave as in 2.9.0 (the PR's live check compares them).
- View sessions stay on the server once made; a device that comes back reuses the free one used last.
- Followers show the plain status line for a few seconds after they start, until the primary's row lines arrive.

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **Link each session's window into its own session; views attach to it** (this record) | Two views each show their own session; no pane moves; old code during an upgrade sees no change | One width for a session open on two devices; a nested client per view | **Chosen** (Path A) |
| Swap-pane per view, as 2.9.0 | Nothing new | A pane lives in one window: two views cannot each show their own | Rejected: the issue's whole point |
| Grouped sessions per viewer | No nested client | When the shown window goes, tmux can switch the viewer onto a view window, which then shows itself | Rejected |
| A second tmux server for the sessions | Clean separation | His running sessions cannot move servers without being killed | Rejected: "I don't want to lose sessions" |
| `move-window` instead of `link-window` | Fewer windows in `emx` | A panel still running during the upgrade sees every session vanish | Rejected |
| Viewer sessions with the mouse off, and the wheel bound to the session's own copy mode (the lead's first ruling) | The outer level keeps the mouse | Probed on 2026-10-18: a drag copied nothing and the wheel did not scroll the session's history, and `q` reached the session | Replaced by the mouse staying on one level down, which matched 2.9.0 on every item probed |
| `set-clipboard on` | A viewer's copy reaches the terminal with no helper | Any program in a pane could write his laptop's clipboard | Rejected |

## Amendment, 2026-10-20 (#84): set-clipboard on

emx now sets `set-clipboard on` (2.15.1). Claude Code's own copies, which are not tmux copy mode, never reached his laptop through a nested emx. Tural, on #84: *"see what works. then implement what works. simple. dont implement untested stuff"*. Tested: a copy from a session viewed through a nested emx (a server's emx opened with `emx remote` inside the laptop's emx) reaches the terminal only when tmux has `set-clipboard on`; `allow-passthrough` added nothing and stays off. The `emx copied` and `copy-command` path stays for copy-mode copies from a viewer. With `on`, such a copy is now delivered to the terminal twice with the same text, and leaves two identical tmux buffers (proved live on private sockets); this is harmless and left as is.

The trade-off is accepted: any program in an emx pane, including one on a server shown through `emx remote`, can now write (not read) the terminal's clipboard. That is acceptable for a single-user tool whose panes are his own sessions.

This supersedes the `set-clipboard` stays `external` clause in "Mouse and copy through a viewer" and the `set-clipboard on` row of the alternatives table (Rejected). Everything else in this record stands. Status stays Accepted.
