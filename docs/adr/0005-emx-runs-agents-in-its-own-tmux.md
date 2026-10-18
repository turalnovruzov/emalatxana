# 0005. emx runs every agent session itself, in its own private tmux

- **Status**: Accepted; built in [#41](https://github.com/turalnovruzov/emalatxana/issues/41) (emx 2.0.0), for Claude only. Hooks through `--settings` loaded and reported state, so no screen-reading fallback was built. Tural's answers on the open points (2026-10-13): after a reboot or crash every session shows Stopped and comes back only when he opens it (*"sure lets go with b"*); his existing `claude --bg` sessions are not listed and are moved by hand, once, on his word (*"maybe this should manual one time since i am the only user"*); remote use is not built. The major version is his word too: 2.0.0, "2.0.0 (Recommended)" chosen in-session (2026-10-13). How a session is shown changed in 2.10.0: its pane stays in its hidden window and each device's view attaches to it ([0010](./0010-a-view-per-device.md)).
- **Date**: 2026-10-11

## Context

Until now emx shows Claude Code's own background sessions: Claude's background service holds them, and emx reads `claude agents --json` and shows each one with `claude attach` ([0001](./0001-tmux-and-python-standard-library.md)). That ties emx to Claude Code and to one vendor's newer session commands.

On 2026-10-11 Tural asked what would replace it. He wants other agents to be possible later (*"for now claude is only one i use but that might change"*), his work reachable remotely (*"i like that herdr can run ona remote server and i can access the same thing from everywhere"*), less code to maintain, and his own customisation. He tried herdr and turned it down because its view is built around spaces and tabs, not agents (*"the view is very different than a agent sentric view"*), and asked for every possible architecture to be looked at (*"i need you to explore broad options. every possilbe architecture"*).

The research in that session covered nine engines, six ways of driving agents without a terminal, seven kinds of front end and about 25 existing tools. It found:

- **No engine keeps a process alive through a reboot.** Each one comes back by resuming the conversation (`claude --resume <id>`), so recovery after a reboot is the same whichever engine is picked.
- **Of the terminal agents looked at, only Claude Code has its own background service.** Codex and Gemini end when their terminal closes, so something else has to hold them.
- **Driving agents without a terminal** (ACP, the Claude Agent SDK, `claude -p` with stream-json) loses the Claude Code interface itself, and Anthropic points that kind of tool at API keys rather than the subscription.
- **tmux** is mature, already in the stack, and its control mode (`tmux -C`) gives a stream of output and change events.

The lead first recommended holding Claude on its own service and other agents in emx's tmux. Tural answered: *"but what happens when claude's service crashed? isnt the risk the same with the services crashing? if we do 2 we handle one service crashing if we do number 3 then we handle 2+ services."* That is right: a crash of whatever holds a session ends it, and two holders mean two ways to fail and two recovery paths. He decided: *"lets document the change to numerb 2. but not start in this session"*.

## Decision

**emx starts and holds every agent session itself, Claude Code included, each in emx's own private tmux server** (its own socket, as today). One engine for every agent; emx no longer depends on `claude --bg`, `claude agents` or `claude attach` to hold or show sessions.

- **What runs in the pane is the agent's own interactive program** (`claude`, and later `codex`, `gemini` and others), never a rebuilt chat screen.
- **State comes from the agent itself where it can:** for Claude, hooks given to that one session through `claude --settings <file>`, so `~/.claude/settings.json` is never edited. Other agents use their own hooks or notify settings, and reading the screen is the fallback.
- **After a crash or a reboot,** a session comes back through the agent's own resume (`claude --resume <id>` for Claude). emx keeps each session's id so it can do that.
- **The panel, the narrow column and the features built on top** (Haiku lines, notices, folder picker) stay as they are.

The first step, before any building, is to check that per-session hooks through `--settings` load and report state. If they do not, Claude's state falls back to reading the screen; that weakens this decision, and the result goes back to Tural before anything is built.

## Consequences

- **Easier:** one engine and one recovery path for every agent; adding an agent becomes adding its launch, resume and state rules; nothing new to install.
- **Harder:** emx now owns the job Claude's background service did: starting, keeping and resuming sessions, and working out each agent's state. If emx's tmux server dies, every session in it ends and comes back by resume.
- **Lost:** sessions he starts outside emx with `claude --bg` do not show in emx unless emx also keeps reading `claude agents`. Whether it should is still his call (#41).
- **Changes [0001](./0001-tmux-and-python-standard-library.md) once built:** its rule that session data comes only from the `claude` CLI is replaced. tmux and the Python standard library stay.
- **Open, for Tural:** what happens to the Claude background sessions he already has; whether sessions come back by themselves after a reboot or wait for him (coming back by themselves would also change 0001's rule that emx never resumes a session on its own); remote use (a server, and how notices reach him there).

## Alternatives considered

What Tural saw in-session on 2026-10-11, one line each:

- **1. Today's emx with a slot for other agents later.** Claude's service holds sessions and gives exact state. Lost because it stays Claude-only until a second engine is added, which is the work of 3 anyway.
- **2. emx holds every agent in its own private tmux.** Chosen.
- **3. Claude on its own service, other agents in emx's tmux.** Two engines to fail and recover. Its real gain, Claude's state straight from Claude, is what the `--settings` hooks check is for.
- **4. herdr running invisibly as the engine, with emx's panel in front.** It detects about 22 agents, and its source showed that a separate client can attach to an agent at the pane's width. Lost on a new third-party tool still at version 0.9 with frequent breaking changes, and process ownership is no better than tmux.
- **5. Zellij as the engine, with the panel as a Zellij plugin.** Has a built-in web client. Lost on a rewrite of the panel in Rust, and Zellij is pre-1.0 with plugin API breaks.
- **6. A web front end (xterm.js) over any engine.** Reaches a phone. Not an engine choice; can come later on top of 2, and would need its own gate.
- **Ruled out:**
  - **No terminal** (ACP, Agent SDK, stream-json): loses the Claude Code interface, and has a billing risk.
  - **Our own PTY daemon:** needs a terminal emulator, weeks of work.
  - **WezTerm, kitty, GNU screen, dtach, abduco, shpool:** dormant, lack an API or events, or do not hold processes on their own.
  - **A native Mac app, Textual, or terminal-app integrations:** no phone path, tied to one app, or heavy.
- **Existing products looked at:** herdr (he tried it and turned it down, see Context). Agent Deck (tmux with Claude hooks, close to this decision's shape) lost on one maintainer, about 345,000 lines, and updating and restarting itself by default.
