---
name: capture
description: Capture an idea Tural states, briefly or at length, as one GitHub issue labelled `idea`, holding his words verbatim. Use when he says an idea, a wish or "I want X" and has not asked for it to be built now. Does no research and no refinement unless he asks.
---
<!-- template: product-owner @ 0.1.0 — shared; change it through Codery -->

# capture

**Goal.** An idea he says is kept as he said it, as one GitHub issue labelled `idea`, so it is not lost and can be refined later, in this session or another. This skill is self-contained: it runs here, where only this repo's `CLAUDE.md` loads.

His words, which this skill serves:

> "when i work i have an idea and i just say it. those should be tracked as ideas."

> "i say idea briifely or long -> agent puts as structured idea"

> "capture, refinement and development can be seperate session and etc. dont have to be together always"

Capture is only the first of three pieces: capture (this skill), `refine` (the idea is refined with him) and development (works `ready` issues). The issue is the only memory between them.

## Steps

1. **Take the idea as he said it.** Keep his wording and his length. Do not rephrase it, shorten it or complete it.
2. **Open one issue per idea.** One idea he says is one issue; two ideas are two issues.
   ```
   gh issue create --label idea --title "<a short title in his terms>" --body-file <file>
   ```
   If `gh label list` does not show `idea`, the project's setup is incomplete: tell him, and do not create the label yourself (Nizam's default: the label is a repo setting made at setup, after his go).
3. **Write the body in this shape** (the shape is Nizam's default; he did not specify the fields of a structured idea, so it stays minimal):
   ```
   ## His words
   <his words, verbatim>

   ## Decided
   (nothing yet)
   ```
   No ticket types, acceptance criteria or priorities: he has not said any of those.
4. **Stop there.** Run no research and no refinement in the same step unless he asks (Nizam's default). A fix he calls tiny is not captured here; it goes straight to development (his call, in the session).
5. **Tell him in one line:** the issue link, and that it is an `idea` waiting for refinement.

## Never

- Never open an issue he did not state as an idea, and never invent one from your own observation: an improvement you notice goes in your report, and he captures it or not (the project's CLAUDE.md says the same: "Never open an issue on your own").
- Never relabel an issue `ready`. Only `refine` does, on his go.
