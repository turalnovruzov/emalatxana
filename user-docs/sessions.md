# Sessions

Everything here happens in the panel unless it says otherwise. Press `?` there to see the keys.

## Start a session

Press `n`. With no sessions yet, emx opens the folder list first: pick a folder and press `↵`, then type the message. After that, the session starts in the folder of the session or project the cursor is on, or in the last folder you used when the cursor is on a header with no folder. Type a message for Claude and press `↵`, or leave the message empty to start an empty session.

To start in another folder, press `Tab` and pick one. Type to filter; `↑` and `↓` move through the list. `Tab` completes the path you typed and `↵` picks the folder. Folders you used recently are listed, and starred ones come first. Press `^t` on a folder to star it as a favourite (or unstar it). `Esc` in a list opened with `Tab` goes back to the message and keeps what you typed; when the list opened on its own (the first session), `Esc` cancels.

## Open and switch

- Move the cursor with `↑` and `↓` (or `j` and `k`).
- `→` or `↵` opens the session and goes to it. A click on a session does the same.
- `←` in a session, from an empty prompt, comes back to the panel. With text in the prompt, `←` moves the cursor in the prompt as usual.
- `^\` jumps to the session and back to the panel, from anywhere.
- `↵` or a click on a project header folds or unfolds the group.

## Peek

Press `space` on a session to see what it asks or did last, without opening it. `↑` and `↓` go to the next one; `space` or `Esc` closes the peek.

## Find

Press `^f` or `/` and type part of a session's name or folder.

## Pin

Press `^t` to pin a session to the top of the panel. A pin only orders the list; it does not keep the session running.

## Rename

Press `^r`. emx renames the session through Claude's `/rename`. `Tab` there suggests a name.

## Interrupt and close

Press `^x` on a working session to interrupt it. Press `^x` twice to close it. On a project header, twice closes the whole group.

## Views

`^s` switches between listing by project and listing by state.

## Hide the panel

`^q` hides the panel and gives the whole window to the session. `^q` again, `^\`, or `←` brings it back.

## Panel side and width

`s` moves the panel to the other side. `<` makes it narrower and `>` wider. Both are remembered.

## Detach

`q` leaves the workspace. Your sessions keep running; run `emx` to return.
