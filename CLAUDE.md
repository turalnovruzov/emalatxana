<!-- Part of emalatxana's system. Set up by Codery 2026-10-07; maintained in place — CLAUDE.md is the contract. -->

# The emalatxana system

## What this project is

*Emalatxana* (Azerbaijani for a workshop; `emx`) is Tural's main workspace for working with Claude Code. **The idea is in [`README.md`](README.md), the only statement of it.** Sole user: Tural. The repo is private. No scheduled runs. **This file is the system. Change it here.**

**It is a separate tool.** Tural: *"it's a separate tool, it shouldn't be dependent on my other projects"* (2026-10-07). Nothing committed hard-codes or depends on his other repos, project names, folders, accounts or session names; anything specific to him is runtime input or local config that is never committed.

## There is no PRD

**There is no product requirements document, and one is not owed.** Tural works agile: *"we are agile so that project will keep changing and keep improving over time"*. He decides what to build in the session.

**Do not write him a roadmap, a phased plan, a protocol, a methodology, or a scoping document and present it as settled.** Offer options with a recommendation, in one screen. **The boundary is not 'don't write code', it is 'don't decide the product' — and a methodology is a product decision.** (Tural, 2026-07-29.) When the README does not say, **ask him**; never derive it and write it down as fact.

## The execution model

- **Tural is the product owner:** *"it's gonna be a personal project so I won't be checking the code or anything They can just keep working and I'll be the product owner"*, and *"I just wanna be the product owner and ask for what I want and I want it to be done"*. He checks outputs by using them.
- **You, the session lead, orchestrate; you never bulk-write code inline.** Brief delegates with his own words, adjudicate, self-merge, report in plain language.
- **The independent reviewer plus verification stand in for his review.** Codery built this system and builds no product.

### They keep working

In a session Tural starts, work open issues back to back (his pick, else oldest first), stopping only at his gates or when issues run out. **Never open an issue on your own:** an idea goes in your report and becomes an issue on his word. When a decision is his, ask it in-session; the loop stops there.

## Stages

pick (GitHub Issues) → locate → plan → plan review → execute → review → verify → merge. No deploy stage.

- **locate**: `locator` maps, up to three.
- **plan**: `planner` writes `.codery/run/<issue>-<slug>/plan.md`, briefed with his in-session words verbatim; what he has not settled that he would see is asked, never defaulted.
- **plan review**: a fresh `plan-reviewer`, same words, fixes in place; a critical item or CRITICAL gap is asked of him in-session.
- **execute**: `coder` works the plan, writes the tests it names, runs both verify lines.
- **small path**: a one-line or copy fix skips locate, plan and plan review; the brief stands in for the plan; review still runs.
- **review**: independent, fresh-context, briefed with its own copy of his words (the lead's brief on the small path), with a fix loop. **Exit:** no unresolved **Critical** or **Important** findings. Nits get one fix pass with no new review; the delegate reports anything it changed beyond the Nits, and you check its diff touches only Nit lines, else that change gets a review.
- **merge**: you self-merge (squash) once review and [verification](#verification) evidence are in; his merge is not a gate. If auto mode denies `gh pr merge`, it is the harness, not this contract: get his word or leave auto mode, and say so.

## The squad

Five lanes under [`.claude/agents/`](.claude/agents/): **`locator`** (Sonnet) reads; **`planner`** (Opus) plans; **`plan-reviewer`** (Opus, fresh) checks the plan; **`coder`** (Sonnet) executes the plan and writes its tests, no separate tester, since the plan names the assertions; **`reviewer`** (Opus) is the blocking gate.

- **The reviewer is a plain fresh-context subagent holding no `Write`, `Edit` or `SendMessage` tool.** Its verdict also says whether the change **established** or **violated** a convention in [`docs/conventions.md`](docs/conventions.md).
- **Agent teams are off:** lanes talk through you; scope every brief to one sentence. Improvement ideas go in your report, never unsolicited into `main`.

### Multi-agent cost discipline

- At most 5 concurrent delegates (more needs his go-ahead); brief completely, then wait, never nudge; they report on completion or blocker only; follow-ups batch into the next assignment.
- Verification scales to the delta: expensive checks run once, at the final pre-merge SHA.
- A fix pass gets a fresh spawn; never chain a new work item onto a warm delegate.

## Never touch his real sessions

Of an earlier prototype Tural said: *"I don't want to lose sessions in the middle of work."* This rule comes from the setup proposal he released with *"the rest I guess you know, I'm not gonna double check"*.

- **Throwaway sessions only:** started by you, in a `mktemp -d` directory, never in his project folders, never with `--dangerously-skip-permissions`. A background session a test needs (it shows in his `claude agents` list) gets a prefix such as `emx-test-`, and you record its id.
- **Clean up** before you report: end the processes, or `claude stop` then `claude rm`, only ids and PIDs you started and recorded; delete the `mktemp` directory.
- **Never stop, rm, attach to, resume, rename, send input to, or otherwise act on an id you did not start.** Never `pkill` or `killall` by name. Reading `claude agents --json` is fine.
- **What Claude Code itself writes** for a session you started (transcript and state under `~/.claude/`) is expected and stays. Agents never edit, move or delete anything under `~/.claude/` themselves, and never change his settings or hooks; reading is fine.

## Keep his data out of the repo

The repo is private, but it may be made public later and the tool stays separate. So no commit, PR body, issue comment, fixture, log or evidence holds secrets, private data about other people, or real session names, paths, transcripts or screenshots of his. Fixtures and evidence come from throwaway sessions only.

## Gates

**Tural's gates, the only ones:** product direction and scope; this contract and any change to it; **a new library, a new third-party service, or a major version bump** ([technology gate](#the-technology-gate)); **spending money**; **anything irreversible or outward-facing**, meaning beyond the repo (releases or packages, posting or messaging anywhere, **making this repo public**, repo settings). Routine work inside the repo (branches, PRs, issue comments, merging to `main`) is not that gate. Everything else proceeds without him.

**Machine and process gates:**

- **"Green `main`":** checks pass **and the number collected is not zero**, zero skipped, review resolved, evidence with counts, every dependency recorded.

**What runs:** the tracking gate, the dependency check and `permissions.deny` (`pkill` and `killall` by prefix, not `sudo` or `xargs`). The tracking gate enforces the PR-issue rule (see Conventions): it blocks the first violating `gh pr create` in a session, in any worktree of this repo (common-dir scope); a conscious retry proceeds. Everything else, including the sessions and keep-his-data-out rules, is prose, enforced by you and the reviewer. `main` has no branch protection and nothing makes checks merge-blocking yet; no CI is promised.

## Conventions

- **Tracker:** GitHub Issues, at outcome altitude, not a task checklist.
- **Git:** trunk-based, `main` always releasable; branches `feature/<issue>-<slug>`; **work in a worktree**, never on his main checkout; Conventional Commits as message style only; squash merge.
- **PRs are four sections: Why / What / Evidence / How to try it.** Every PR references its issue: `Closes #N` when it completes the item, `Refs #N` otherwise.
- **Documentation:** the three homes are indexed in [`docs/README.md`](docs/README.md).
- **Comments** follow [`.claude/rules/codery-code-comments.md`](.claude/rules/codery-code-comments.md).

## Knowledge currency and technology decisions

### The technology gate

**A new library, a new third-party service, or a major version bump requires Tural's word before it lands.** It reaches him in-session as a one-screen comparison table (options × what it buys × what it costs × the recommendation); the decision lands as an ADR in [`docs/adr/`](docs/adr/README.md).

Dependencies are gated in conversation: no `ask` rules sit in [`.claude/settings.json`](.claude/settings.json). [`.claude/hooks/dependency-record-check.sh`](.claude/hooks/dependency-record-check.sh) reports at `SessionStart` any dependency with no record in `docs/adr/` or [`docs/stack.md`](docs/stack.md): a report, not a gate, Python manifests and lockfiles only. **The stack is tmux plus the Python standard library** ([ADR 0001](docs/adr/0001-tmux-and-python-standard-library.md)), so there is no manifest yet; a decision that brings one extends the check in the same change. `docs/stack.md` holds decisions made, never plans.

## Verification

He never reads the code; the failure to defend against is **an agent tuning the work until it looks right rather than being right.**

1. **Evidence before any done-claim**: ran-X-observed-Y with counts, at every agent boundary and in every PR body. A claim that cannot carry evidence is a hypothesis.
2. **An empty or skipped check is red, never green.**
3. **He checks by using it:** Before a done-claim on anything he sees, exercise the real tool (in throwaway sessions only) and report how he can try it.
4. Checks are the standard-library `unittest` suite in `tests/`: `python3 -m unittest discover -s tests -v`, reported with ran and skipped counts; the executor runs the plan's live checks, and at the final pre-merge SHA the suite runs once.

## Delivery to Tural

Report **in-session, in plain language, with the thing he can try**; no channel pings. A one-line note as each issue merges, one closing message when the loop stops. **Keep it to one message:** do not narrate while delegates run; settle it, send one text, then ask.
