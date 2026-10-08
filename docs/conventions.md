<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Conventions

The house style for writing code in this repo: how *this* codebase does things, so the next session does them the same way instead of inventing a second way.

## Entries

No entries yet.

## The contract for this file

**Every entry has four parts, in this order:**

1. **Rule**: stated as a positive directive ("we do X"), precise enough to apply in a situation the entry never mentions.
2. **Why**: the reason it exists. An entry without a why gets cargo-culted and then broken by the first person who has a reason.
3. **Minimal example**: the smallest snippet that shows the shape. Minimal, not representative.
4. **Gotcha**: the thing that bites when you follow the rule naively. If there genuinely isn't one, say so; don't invent one.

**Entries arrive from the reviewer.** The independent reviewer reads every diff with fresh eyes, which is exactly the position from which a new convention is visible. Its verdict says whether the change **established** a convention, which becomes an entry here, or **violated** one, which is a finding to fix in the review loop. Nobody writes speculative conventions in advance: an entry describes what a merged change actually did.

**This is house style, not tutorial.** It says what this codebase does, not how the language works. And it obeys the [house rules](./README.md#house-rules-for-editing-these-docs); in particular, no code-knowable facts: an entry that lists exact signatures or versions is stale the moment someone edits the code.

**Decisions belong in [`adr/`](./adr/README.md), not here.** This file carries the *how we write it*; the ADR carries the *why we chose it at all*, with the alternatives that lost. A new library, a new third-party service, or a major version bump earns an ADR, not a convention entry.
