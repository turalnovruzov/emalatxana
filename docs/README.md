<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Emalatxana docs

Three homes, and this index. It says **when to open which doc**: don't read everything, find the one that maps to what you're doing. All three start empty and fill as the work reaches them.

The project's two contracts sit outside this folder and outrank everything in it: [`README.md`](../README.md) is the idea, and [`CLAUDE.md`](../CLAUDE.md) is how the project runs (stages, gates, the squad, verification).

---

## If you are…

**…about to do something a later session should keep doing the same way**, read [conventions.md](./conventions.md). House style for this codebase: each entry is rule → why → minimal example → gotcha. Entries arrive from the reviewer noticing that a change established a convention.

**…questioning *why* the codebase does X this way**, read [adr/](./adr/README.md). Numbered decision records for load-bearing choices, with the alternatives that lost. A new library, a new third-party service, or a major version bump always earns one.

**…wondering what's actually in use and why it's there**, read [stack.md](./stack.md). The stack record, and the file the `SessionStart` dependency check diffs the manifest against.

---

## What's not in here

- **The idea**, what this project is for: [`README.md`](../README.md).
- **How work runs**, stages, gates, the squad, verification: [`CLAUDE.md`](../CLAUDE.md).
- **What to build**: nowhere, deliberately. There is no PRD; Tural decides in the session.
- **Open work**: GitHub Issues on this repo, written at outcome altitude.
- **Anything the code already says**: see house rule 1.

---

## House rules for editing these docs

1. **No code-knowable facts.** Don't copy exact signatures, version numbers, parameter lists or file inventories into prose; they rot the moment someone touches the code.
2. **Fix or delete on the same commit that made it wrong.** A stale doc taints every doc next to it.
3. **Decisions go in [adr/](./adr/README.md), not in feature docs.** A feature gets rewritten and its prose goes with it; the *why* survives in the ADR.
4. **Split when a topic is its own thing, not when a file feels long.** A 300-line doc on one topic beats ten 30-line shards.
5. **Filenames carry meaning.** `conventions.md` says what's inside; `notes.md` doesn't.
