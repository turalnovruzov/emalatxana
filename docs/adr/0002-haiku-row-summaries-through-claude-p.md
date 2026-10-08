# 0002. Haiku writes the row summaries, through `claude -p`

- **Status**: Accepted
- **Date**: 2026-10-08

## Context

Issue #20: the line under each session's name should say in plain words what the session is doing, written by Haiku and kept current. Tural, in-session, 2026-10-08: *"for summaries and names, session names, and etc., you can use Haiku. Haiku is extremely cheap [...] You can use the latest model of the latest, latest version of Haiku for those stuff freely. Don't even worry. It costs almost nothing."* That is his word through the technology gate. On the cost: *"make sure you measure the haiku call to see how efficient it is"*, and then *"be 100% sure about the cost that this is going to give off make sure you have actually done tests and calculated the cost"*. Asked which rows get a Haiku line, he answered "Every row", and how often, "at most every 20 s, plus immediately when its state changes".

Before this, a row showed the last tool call or the first line of the reply (`core.row_detail`), which stays as the fallback.

## Decision

We call Haiku through **`claude -p` on his existing login**: no new library, no API key.

- **The call**: `claude -p --model haiku --output-format json --no-session-persistence --tools "" --strict-mcp-config --disable-slash-commands --setting-sources "" --system-prompt <instruction> --effort low --max-budget-usd 0.002`. Nothing in that set is dropped. `--bare` is never used (it fails on OAuth). `haiku` is the CLI's alias for the newest Haiku, which today is `claude-haiku-5-5`; every measured call reported that model.
- **The excerpt** (the session's state, its latest request, reply and tool call, each capped; for a waiting row, what it is asking) goes on **stdin**, so text starting with `-` is never read as a flag. The call runs in an empty private temp folder, never a project folder that might hold a CLAUDE.md.
- **Which rows**: every row. The state is part of the excerpt. The transcript line shows until Haiku's line for the row's current state arrives, and again if the call fails.
- **When**: only when the excerpt changed; at most every 20 s per session, at once when the state changes; a failed call, including one the budget flag stopped, backs off (5 s, doubling, capped at 5 min) and keeps the earlier line.
- **How**: one worker thread makes the calls one at a time, never on the curses thread. The wrapper never raises.

## Consequences

Measured on Claude Code 2.1.295, 2026-10-08, through emx's own `summarise` wrapper with the production args:

- **Per call** (12 calls on made-up excerpts of 64 to 1066 characters, across all four states): wall 1.9 to 2.4 s (median 2.07 s), CLI `duration_ms` 604 to 1069 (median 650), 2 input tokens, 737 to 1046 cache-creation tokens, 18 to 143 output tokens, $0.000158 to $0.000222 (median $0.000195). A call with every excerpt cap hit (1910 characters) cost $0.000404 cold and $0.000108 to $0.000143 when its prefix was cached.
- **Rate** (the real panel, 13 minutes, three throwaway sessions working the whole time): 84 calls, $0.01416 in total, $0.000169 a call, about 388 calls and $0.065 an hour for the three sessions, about $0.022 an hour for one working session.
- **What the budget flag does and does not do.** `--max-budget-usd 0.002` is passed on every call (Tural, 2026-10-08, asked whether to cap each call: *"Yes, cap each call"*). We measured it on his login: the flag exists and a call over budget ends with exit code 1 and JSON `is_error: true`, `subtype: "error_max_budget_usd"`, `terminal_reason: "budget_exhausted"`, `errors: ["Reached maximum budget ($0.00001)"]`, no `result`. But the CLI checks the budget **after an answer has come back**: a budget of $0.00001 stopped a call that had already cost $0.000164, and a budget of $0.0001 one that had cost $0.000256. So the flag stops a second turn; it does **not cap one call**, and emx makes one-turn calls (`--tools ""`). Tural chose to ship it anyway as a backstop ("Ship with honest limits"). A budget-stopped call is handled as any failure: the one-line error, the backoff above, the transcript line.
- **What bounds a call, then**: the excerpt is capped (about 2000 characters), the instruction asks for one line of at most 12 words, and the call times out at 30 s. `--effort low` is passed to keep Haiku from thinking; one call of about 50 on made-up text showed 90 thinking tokens (143 output tokens) before the flag, and none of 38 calls reproduced it, with or without the flag, so its effect is not proven.
- **Ceiling, per running panel, measured and not guaranteed**: calls are serial, one at a time, so a panel makes at most 3600 / (call time + 1 s tick) calls an hour whatever the number of sessions (the 20 s limit per session only lowers it). With the fastest call in the per-call measurement above (wall 1.9 to 2.4 s), 1.9 s, that is 3600 / (1.9 + 1) = about 1241 calls an hour. At the dearest production-shaped call measured ($0.000404, every excerpt cap hit, cold cache) that is about $0.50 an hour per panel. This is the product of measured figures, not a limit the CLI enforces: the budget flag does not turn it into one. Each running panel has its own ceiling; two panels double it.
  Since [0003](./0003-haiku-suggests-a-name-on-tab.md), a name call Tab asks for in the rename box can run beside the summary worker, so "serial" holds for summaries only.
- **What it actually costs** is far lower: $0.000169 a call on average, about $0.022 an hour for one busy session (above). A session that is not working costs one call per state change, not one per 20 s.
- On his OAuth login `total_cost_usd` is a notional figure that counts against his plan's usage; it is not a bill. Nothing here claims more than the CLI reports.
- The panel now sends excerpts of listed sessions to Anthropic, the same provider and login that already run them. Nothing is stored locally: `--no-session-persistence` leaves no transcript (checked before and after the final measurement: `ls ~/.claude/projects | grep -c emx-summary` was 0). The calls run in one fixed private folder per user under the system temp folder, so a panel killed mid-run leaves nothing new behind. The measurement run listed only throwaway session ids: its `EMX_CLAUDE` shim filtered `claude agents` output to the recorded ids and logged only the length of stdin.
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
