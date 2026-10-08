# 0002. Haiku writes the row summaries, through `claude -p`

- **Status**: Accepted
- **Date**: 2026-10-08

## Context

Issue #20: the line under each session's name should say in plain words what the session is doing, written by Haiku and kept current. Tural, in-session, 2026-10-08: *"for summaries and names, session names, and etc., you can use Haiku. Haiku is extremely cheap [...] You can use the latest model of the latest, latest version of Haiku for those stuff freely. Don't even worry. It costs almost nothing."* That is his word through the technology gate. On the cost: *"make sure you measure the haiku call to see how efficient it is"*, and then *"be 100% sure about the cost that this is going to give off make sure you have actually done tests and calculated the cost"*. Asked which rows get a Haiku line, he answered "Every row", and how often, "at most every 20 s, plus immediately when its state changes".

Before this, a row showed the last tool call or the first line of the reply (`core.row_detail`), which stays as the fallback.

## Decision

We call Haiku through **`claude -p` on his existing login**: no new library, no API key.

- **The call**: `claude -p --model haiku --output-format json --no-session-persistence --tools "" --strict-mcp-config --disable-slash-commands --setting-sources "" --system-prompt <instruction>`. Nothing in that set is dropped. `--bare` is never used (it fails on OAuth). `haiku` is the CLI's alias for the newest Haiku, which today is `claude-haiku-5-5`; every measured call reported that model.
- **The excerpt** (the session's state, its latest request, reply and tool call, each capped; for a waiting row, what it is asking) goes on **stdin**, so text starting with `-` is never read as a flag. The call runs in an empty private temp folder, never a project folder that might hold a CLAUDE.md.
- **Which rows**: every row. The state is part of the excerpt. The transcript line shows until Haiku's line for the row's current state arrives, and again if the call fails.
- **When**: only when the excerpt changed; at most every 20 s per session, at once when the state changes; a failed call backs off (5 s, doubling, capped at 5 min) and keeps the earlier line.
- **How**: one worker thread makes the calls one at a time, never on the curses thread. The wrapper never raises.

## Consequences

Measured on Claude Code 2.1.295, 2026-10-08, through emx's own `summarise` wrapper with the production args:

- **Per call** (12 calls on made-up excerpts of 64 to 1066 characters, across all four states): wall 1.9 to 2.4 s (median 2.07 s), CLI `duration_ms` 604 to 1069 (median 650), 2 input tokens, 737 to 1046 cache-creation tokens, 18 to 143 output tokens, $0.000158 to $0.000222 (median $0.000195). A call with every excerpt cap hit (1910 characters) cost $0.000404 cold and $0.000108 to $0.000143 when its prefix was cached.
- **Rate** (the real panel, 13 minutes, three throwaway sessions working the whole time): 84 calls, $0.01416 in total, $0.000169 a call, about 388 calls and $0.065 an hour for the three sessions, about $0.022 an hour for one working session.
- **Ceiling**: calls are serial, so about 3600 / (1.9 s call + 1 s tick) = 1230 calls an hour at most, however many sessions there are. At the dearest call measured ($0.000404) that is $0.50 an hour. A session that is not working costs one call per state change, not one per 20 s.
- On his OAuth login `total_cost_usd` is a notional figure that counts against his plan's usage; it is not a bill. Nothing here claims more than the CLI reports.
- The panel now sends excerpts of listed sessions to Anthropic, the same provider and login that already run them. Nothing is stored locally: `--no-session-persistence` leaves no transcript (checked: no folder written under `~/.claude/projects`).
- Summaries live in memory only; a panel restart summarises each covered session once more.
- A session shows no marker for whether its line came from Haiku or the fallback.
- If a future CLI changes the alias, the model changes with it; `SummaryCall.model` records which model each call reached.

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **`claude -p --model haiku`**, full flag set | The newest Haiku, on his login, ~2 s | ~$0.0002 a call (~740 to 1050 cache tokens); a subprocess per call | **Chosen** (Tural, 2026-10-08) |
| The same call pinned to `claude-haiku-5-5` | A model that never changes under us | Misses a newer Haiku, against his "latest, latest version" | Lost |
| `claude -p` with default flags | Nothing to write | 8 to 12 s, ~37k tokens, thinking, ~$0.006 a call | Lost |
| `--tools "" --system-prompt` without `--strict-mcp-config --setting-sources ""` | Fewer flags | ~387k cache tokens, $0.39 for one call | Lost: the incident behind the flag set |
| `--bare` | Skips hooks and settings | "Not logged in" on an OAuth login | Lost: does not work |
| Anthropic API over `urllib` | No subprocess | Needs an API key: a new secret | Lost |
| The `anthropic` SDK | Convenient | A new library, and an API key | Lost |
| Keep only the transcript line | Free, instant | Says what the last tool was, not what the session is doing | Lost: the point of #20 |
