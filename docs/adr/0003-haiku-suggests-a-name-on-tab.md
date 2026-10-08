# 0003. Haiku suggests a session name on Tab in the rename box

- **Status**: Accepted
- **Date**: 2026-10-08

## Context

Issue #21 asked for Haiku to suggest a better name when a session's work has moved on from its name. Tural, in-session, 2026-10-08: *"maybe it shouldn't be automatic, maybe it can be a feature that I can trigger that would basically suggest a rename, suggest another name for the session based on maybe we've pivoted from what we were doing"*. On what Haiku reads: *"the suggestion for the rename shouldn't be based on just the last few terms I feel like the whole conversation matters for that is it gonna be expensive?"*. He chose every request he made, start to finish, trimmed in the middle only when extremely long. On cost: *"be 100% sure about the cost that this is going to give off make sure you have actually done tests and calculated the cost"*. On the style of the name he picked "Short plain phrase": 2 to 5 lowercase words with spaces, such as "tea poem and csv parser".

The agreed shape: Ctrl+R opens the rename box holding the current name. Tab asks Haiku and puts a name in the box. He can edit it, and Tab again gives another. Enter renames through the existing path, unchanged, and Esc cancels. Nothing runs in the background, so there is no on/off switch.

## Decision

We ask Haiku for a name through the same `claude -p` call as the row summaries (ADR 0002), only when he presses Tab in the rename box.

- **The call**: `core.name_args`, which is `summary_args`'s flag set with its own `--system-prompt` (`NAME_INSTRUCTION`) and `--max-budget-usd 0.04`. The excerpt goes on stdin, the call runs in the same empty private folder, and the wrapper never raises.
- **One on-demand worker.** Tab starts a one-shot `Namer` thread, and there is at most one at a time. Names never queue behind summary calls, because he is waiting in the box. A Tab while a call runs does nothing. A Tab in a new box while an earlier box's call is still finishing says so and starts nothing.
- **What Haiku reads**: every request he typed in the session, oldest first, and none of Claude's replies. "His request" has one definition, `core._said_by_user`, which `parse_transcript` uses too. The excerpt also holds the current name and the names this box has already offered, at most the 10 most recent.
- **The bounds**:
  - **Bytes read**: a transcript of up to 16 MiB (`NAME_READ_MAX_BYTES`) is read whole. A bigger one is read as its first and last 8 MiB, and a marker shows where the unread middle was. A single line longer than its window is lost.
  - **Characters sent**: requests go whole, with runs of whitespace collapsed, while together they fit in 24,000 characters. Only past that is each one cut to 2,000 characters. If they still don't fit, the earliest are kept up to a quarter of the total and the most recent fill the rest, with a marker saying how many were left out.
  - **The header**: the current name and each offered name are capped at 50 characters.
  - **Who changes them**: only Tural lowers these caps, since lowering one narrows what Haiku sees.
- **Only Enter renames.** An answer is put in the box only when it is a name emx would type (`core.rename_request`) and was not already offered in this box. Even then it is dropped if he has typed since the Tab, closed the box, or opened a new one. It types nothing and starts no rename.

## Consequences

Measured on Claude Code 2.1.295, 2026-10-08, through `sidebar.suggest_name` with the production args, on made-up request histories only. 64 calls in all cost $0.158, and every one reached `claude-haiku-5-5`. In the table, **cold** is a first call on a history, **warm** is the same input sent again, and **Tab again** adds 2 offered names. Wall times include process start.

| History (excerpt chars) | Cold cost min / median / max | Warm cost median | Tokens, cold (cache creation) | Wall ms, cold median | `duration_ms`, cold median |
|---|---|---|---|---|---|
| Short: 3 requests (410) | $0.000189 / $0.000190 / $0.000190 | $0.000015 | ~920 | 1755 | 653 |
| Typical: 20 requests (4,109) | $0.000477 / $0.000491 / $0.000502 | $0.000101 | ~2,050 | 2757 | 1674 |
| Cap hit, English: 300 requests of ~600 chars (23,637) | $0.001525 / $0.001612 / $0.001652 | $0.000165 | ~7,600 | 2851 | 1714 |
| Cap hit, CJK and emoji phrases (23,528) | $0.005221 / $0.005227 / $0.005298 | $0.000359 | ~25,600 | 2890 | 1803 |
| Cap hit, random CJK and emoji characters (23,530) | $0.006235 / $0.006280 / **$0.012507** | $0.000378 | ~30,900, or ~61,900 for two requests | 3393 | 2253 |

- **The typical size is an assumption**, not calibrated from his transcripts.
- **Tab again costs as much as a cold call.** Each offered name changes the input, so it is not served from the cache. The first Tab again on each history cost the same as a cold call. Repeats with identical input cost the warm figure.
- **Haiku sometimes repeats a name it was told was already offered.** On the 3-request history, 2 of 3 Tab-again calls did, and the box then says "Haiku's name didn't fit; Tab for another." On longer histories, 0 of 12 did; one more English call answered with a sentence too long to be a name.
- **Output tokens are 7 to 349 for a name of a few words.** `--effort low` does not stop Haiku using some tokens before it answers.
- **Refusals are paid twice.** The random character text, which no person would type, sometimes trips Haiku's safety refusal (`stop_reason: "refusal"`). The CLI then tries once more (`num_turns: 2`) and both requests are paid.
  - The dearest call, $0.0125, was refused, then answered.
  - Three re-runs were refused twice and returned no name for $0.0125 each, so the box shows the CLI's error line.
  - Coherent CJK phrases and English were never refused.
- **The budget**: $0.04, over three times the dearest measured cold cap-hit call. As with summaries, the CLI checks it only after an answer, so it stops a second turn but does not cap one call. With the final value, 9 re-run cold cap-hit calls (3 English, 3 CJK phrases, 3 random CJK) all ran without a budget stop. The 6 English and phrase calls named the session, and the 3 random-CJK calls were the refusals above.
- **What bounds a call**: no flag caps a call's cost or output length. The input caps above, the instruction (one name of 2 to 5 words) and the 30 s timeout do.
- **Wait**: a made-up 200 MiB transcript was read with every cap hit in 29 to 30 ms, from the OS file cache, and the process peaked at about 63 MB RSS. A 10 MiB one, read whole, took 19 ms and peaked at about 60 MB. The bare interpreter peaks at 15 MB.
  - End to end on the 200 MiB file (read, excerpt, call), a `Namer` took 2104 ms, 2679 ms and 3999 ms. The slowest cost $0.0037, twice the other two, which fits a two-request call.
  - In the real panel with a throwaway five-request session, Tab to name took 1.9 to 2.2 s.
- **The ceiling**: name calls run only on his Tab, at most one at a time per panel, next to the summary worker. So ADR 0002's "calls are serial" now holds for summaries only. The dearest name measured is $0.0125 (a refused-then-answered call). A cancelled box's call still finishes and is paid.
- **What isn't read**: only the session's current `<sessionId>.jsonl`. Requests from before a `/clear`, or kept in another file after a resume, are not seen.
- **Not measured**: a history made only of emoji, which may take more tokens per character than the mixed text above.
- **No transcript is left behind**: the measurement calls ran in their own throwaway folders, and the panel's in emx's scratch folder. Afterwards `~/.claude/projects` held no folder named after either, only the one for the throwaway session the live check ran in.

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **Tab in the rename box asks once** | He asks when he wants one; nothing runs unasked | A ~2 to 3 s wait in the box | **Chosen** (Tural, 2026-10-08) |
| Automatic suggestions shown on the row | No key to press | Calls in the background, a switch to turn them off, and noise | Lost: *"maybe it shouldn't be automatic"* |
| Run name calls on the `Summaries` thread | One worker | A name waits behind every summary while he waits in the box | Lost |
| Send only the last few requests | Cheaper and faster | Misses where the session started | Lost: *"the whole conversation matters"* |
| Send Claude's replies too | More context | Far more tokens, and replies are not what he asked for | Lost: his requests are what the session is about |
