# 0008. emx installs and updates itself from the repo's version tags, with git

- **Status**: Accepted
- **Date**: 2026-10-16

## Context

Issue #60. Tural: *"I want to be able to use this as an actual application I don't want to have to go into the project folder and then pull and etc I want this to auto update as much as possible and then I want to actually run it as EMX"*, and of his server: *"I do not want to go into the server and restart the MX That should just be running there 24/7 without me having to go in and maintain it"*. He settled (Decided 1-14) that it stays private for now and works through his own GitHub login; that the channel is a one-line installer plus git, with no Homebrew, PyPI or GitHub Release; that an update is only a pushed `vX.Y.Z` tag; that updates land live on the laptop and the server alike without touching a Claude session; that a major version asks first; that a checkout (his project folder, the agents' worktrees) never updates itself; and that `emx remote` catches the server up when it connects.

The install line:

```sh
git clone <this repo's URL> /tmp/emx-install && sh /tmp/emx-install/install.sh && rm -rf /tmp/emx-install
```

## Decision

**Layout.** emx installs into `EMX_HOME`, else `$XDG_DATA_HOME/emx`, else `~/.local/share/emx`:

```
<root>/source.git/          bare clone; its origin is the URL install.sh was given
<root>/versions/X.Y.Z/      `git archive vX.Y.Z` unpacked (no .git), made under a temporary name and renamed into place
<root>/current -> versions/X.Y.Z   flipped last, by a new link renamed over the old
<root>/lock                 one unpack-and-flip at a time
<root>/updates-off          present while automatic updates are off
<bin>/emx -> <root>/current/bin/emx   EMX_BIN_HOME, else ~/.local/bin
```

A tree is installed when its folder is `<root>/versions/X.Y.Z` beside a `current` link and a `source.git` folder; that is read from the layout, not the environment, so a hook Claude runs and a shell on the server agree with the panel. Everything else is a checkout, which never updates itself and never writes an install folder (the one exception is the internal `emx install`, which only installs a pushed tag). Every path emx bakes into tmux, a session's hook settings, `emx.command` and a notification goes through `<root>/current`, so a version folder can be deleted later without breaking anything that runs.

**Tags only.** Install and update choose among `vX.Y.Z` tags of 2.3.0 or newer; nothing follows `main`.

**Live switch.** The installed panel checks every 10 minutes (`EMX_UPDATE_SECONDS`) on a worker thread, with no terminal for git to prompt on. It unpacks and flips to the newest tag within its major, but only while it runs the version `current` names, so it never gets a third version under it. Once `current` has moved, at a quiet moment (nobody attached, or no key for 3 minutes, and nothing open in the panel) it starts the new code's `emx relaunch <socket>` detached. That is the launcher's own workspace refresh run from outside: it respawns the panel on the new code, re-reads the tmux config and stamps the version. A panel can't respawn its own pane, since the respawn would kill the process asking for it. Session panes are never respawned. The new panel opens with "emx updated to X." and the changelog lines.

**Majors asked.** A newer major is fetched but not unpacked; the panel says "emx X available: run emx update", and `emx update` takes it. `emx update off` / `on` set the marker file (not a pref, since the panel rewrites its prefs from memory). `emx update` and the panel's worker each delete old version folders, keeping `current`, the newest one below it and the one the process runs.

**Remote catch-up.** `emx remote <host>` runs the server's internal `emx update --report`, which updates within its major (unless updates are off there) and prints one line the laptop parses as untrusted output. A newer major on either side, or majors that differ, is asked once in the laptop's terminal for both machines.

git and tar are system tools, like tmux and ssh: no library, no new service.

## Consequences

- Each machine needs git and a GitHub credential that works without a typed passphrase; one that needs typing never updates in the background, and `emx update` says why. Setting that up on the server is server setup (#60 Decided 12).
- Every installed panel runs a `git fetch` every 10 minutes.
- A switch drops what only the panel held in memory (its Notifications list, a notice being spoken), as an `emx` relaunch always has.
- Any `emx` run while a workspace is up (including the attach `emx remote` makes on the server) moves the panel onto `current` at once, quiet or not.
- A second workspace on another socket, or a long-running `emx follow` or `emx remote`, can lag two versions behind and have its folder deleted; its loaded code keeps running, and a restart fixes it.
- Sessions started before the install keep hooks that point at the checkout until they are next resumed.
- Anything that would stop an older and a newer emx working together is a major version ([conventions](../conventions.md)).

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **One-line git installer, per-tag folders, a flipped `current`** (this record) | Nothing new to install; Mac and Linux alike; follows the tags already pushed; works on a private repo with his login | git and a credential on each machine; emx carries its own updater | **Chosen** (#60 Decided 2) |
| Homebrew tap | A familiar `brew upgrade` | A tap to keep, Homebrew to install on the server too, and no live switch | Rejected in #60 |
| PyPI and pipx | A standard Python install | Publishing a package, which is outward-facing and makes emx public | Rejected in #60 |
| GitHub Release tarball | No git needed | A release per version, which the versioning rule doesn't make | Rejected in #60 |
| `git pull` in a checkout | No new code | Runs whatever `main` holds and mixes with the folder agents work in | Rejected in #60 Decided 5 and 9 |
