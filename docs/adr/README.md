<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# Architecture Decision Records

ADRs preserve the **why** behind load-bearing technical decisions: the kind that survive rewrites and need to make sense to a reader years later. Feature docs and conventions describe the *what*; ADRs describe the *why we chose this what*.

When to write a new one:

- A non-obvious technical choice with viable alternatives that someone might later question or try to reverse.
- A decision that constrains future code.
- A pattern adoption that reaches across multiple modules.
- **A new library, a new third-party service, or a major version bump, always.** These are Tural's gate: the one-screen comparison table (options × what it buys × what it costs × the recommendation) goes to him in-session, and **that table is what lands in the record's *Alternatives considered* section**. Writing it down is what stops the next session re-litigating the same debate. A major bump counts because a major version is a new set of behaviours under an old name.

When **not** to write one:

- Conventions that are obvious from the code (naming, folder layout): those go in [conventions.md](../conventions.md) if anywhere.
- One-off implementation details with no architectural reach.
- Version bumps with no semantic impact.

---

## Index

| # | Title | Status |
|---|---|---|
| [0001](./0001-tmux-and-python-standard-library.md) | tmux and the Python Standard Library | Accepted |

---

## Template

```markdown
# NNNN. <Short title in title case>

- **Status**: Proposed | Accepted | Superseded by [NNNN](./NNNN-...) | Deprecated
- **Date**: YYYY-MM-DD (or "captured retroactively YYYY-MM-DD" for past decisions)

## Context

What forces are at play? What problem are we solving? What constraints exist (technical, physical, budgetary)? Cite specific incidents or pain points if they drove the decision.

## Decision

The rule we adopted, stated as a positive directive ("We do X"). Be precise enough that a reader can apply the rule in a new situation.

## Consequences

What changes because of this decision? What's now easier? What's now harder? What follow-up work or future risks does this create?

## Alternatives considered

Other options on the table and why they lost. This is what stops the next person from re-litigating the same debate, and for a library, service, or major bump, it is where the comparison table Tural saw in-session lands.
```

Numbering is **gap-free**, starting at `0001`. If a record is superseded, **leave it in place** and mark its status: never renumber, and never reuse a number.

Filenames are `NNNN-kebab-case-title.md`, and every new record is added to the index table above in the same commit.
