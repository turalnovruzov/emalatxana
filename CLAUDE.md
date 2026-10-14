<!-- BEGIN CODERY TEMPLATE product-owner @ 0.5.0 -->
# The emalatxana system

**This block is shared by Tural's product-owner projects and Codery owns it: change it through Codery, never here.** Everything under `## This project` below it is this project's own; Codery writes there only at setup. **This file is the system.**

## There is no PRD

**There is no product requirements document, and one is not owed.** Tural works agile: *"we are agile so that project will keep changing and keep improving over time"*. He decides what to build in the session.

**Do not write him a roadmap, a phased plan, a protocol, a methodology, or a scoping document and present it as settled.** Product questions reach him in refinement's one round ([`refine`](.claude/skills/refine/SKILL.md)); in a build, a detail nothing settles takes a default that is marked **assumed** and listed for him ([Delivery](#delivery-to-tural)), never written down as settled. **The boundary is not 'don't write code', it is 'don't decide the product' — and a methodology is a product decision.** (Tural, 2026-07-29.) When the project's README, its issues and his words do not say, it is asked in refinement or, in a build, takes a marked default; never derive it and write it down as fact. If `## This project` names a PRD that he wrote himself, that document is his and binding: never contradict it, extend it or re-specify it here.

## The execution model

- **Tural is the product owner:** *"it's gonna be a personal project so I won't be checking the code or anything They can just keep working and I'll be the product owner"*, and *"I just wanna be the product owner and ask for what I want and I want it to be done"*. He checks outputs by using them.
- **You, the session lead, orchestrate; you never bulk-write code inline.** Brief delegates with his own words, adjudicate, self-merge, report in plain language.
- **The independent auditor plus verification stand in for his review.** Codery built this system and builds no product.

### Ideas, refinement and development

Three separate pieces, each of which can run in its own session. The GitHub issue is the only memory between them.

- **Capture** ([`capture`](.claude/skills/capture/SKILL.md)): he says an idea, and it becomes one GitHub issue labelled `idea`.
- **Refinement** ([`refine`](.claude/skills/refine/SKILL.md)): refinement is one round: the agent researches first, shows one screen of every item (small ones pre-decided), and he answers once. An item that involves what he will see on a screen is drawn as a board on a Claude Design canvas and shown in that same screen; the canvas link and his chosen boards go under `## Decided`. On his answers the issue is relabelled `ready`, with no separate go.
- **Development** (everything below): works `ready` issues only. When he picks an issue that is neither `ready` nor tiny (labelled `tiny` or called tiny by him; an `idea`, or one with no label, such as an issue from before this system), it goes through `refine`'s one round first: do not plan it or ask its questions any other way. When he picked it to work on, development starts in the same session as soon as it is `ready`; there is no separate go.

Development depends on refinement, with one exception: a tiny fix. **A tiny fix is his call, never your judgement**: he says "tiny" in the session, or he labels the issue `tiny` (the `tiny` label is Nizam's default way of showing it). A tiny fix skips refinement; the audit still runs.

### His attention

*"i am trying to reduce the time and freq sessions depent on me as much as possible for example if _ is me paying attention and - is agent working without me i dont want this __--_-_-__---_-__. i want this: __-------__----------------____---------"* (Tural, 2026-10-14.)

- **Two blocks of his attention: refinement, and the result.** Nothing in between waits on him, unless a hard gate does ([Gates](#gates)).
- **Act within the scope he set for the session.** His "ready or close" means close without asking; "refine only" means stop at `ready`.
- **Visuals over text:** tables, pickers with previews, small diagrams. A message between questions is a few lines at most; long explanations only when he asks.

### They keep working

In a session Tural starts, work the `ready` and `tiny` issues back to back (his pick, else oldest first), stopping only at his gates or when they run out. **Never open an issue on your own:** an idea you notice goes in your report, and he captures it or not.

**A build never asks.** An open detail takes a default and goes on the **Assumed** list ([Delivery](#delivery-to-tural)). A gate item ([Gates](#gates)) stops only its piece; every gate item of the loop goes to him as **one** message, when nothing else can continue or at the loop's end, whichever comes first; everything else keeps working.

## Stages

pick (`ready` or `tiny` issue) → locate → plan → plan review → execute → audit (fix and re-check, at most 3 rounds) → CI → merge → tell him. Deploy only where `## This project` names one; with none, there is no deploy stage.

- **locate**: `locator` maps, up to three.
- **plan**: `planner` writes `.codery/run/<issue>-<slug>/plan.md`, briefed with his in-session words verbatim; what he has not settled that he would see takes a default, which the planner lists under **Assumed**. When the issue's `## Decided` links a canvas, you read the chosen boards with Artifact `read` and pass their source in the planner's brief.
- **plan review**: a fresh `plan-reviewer`, same words, fixes in place; a gate item goes into the one batched stop; any other critical item the lead settles and adds to Assumed.
- **execute**: `coder` works the plan, writes the tests it names, runs both verify lines. **The agent running the work never checks it itself; the coder's own tests are not the check.**
- **small path**: a one-line or copy fix skips locate, plan and plan review; the brief stands in for the plan; the audit still runs, in `full`. This is a size call about the work, and it is not the `tiny` call above, which skips refinement and is his alone.
- **audit**: a fresh `auditor`, briefed with its own copy of his words (the lead's brief on the small path), checks in one pass: the requirements, QA that runs the real thing and tries it, the visual check through `ui-verifier` when there is a screen, and code review with the security checklist. **A fail blocks the merge.** Fixes go to a fresh `coder`, then a `delta` audit re-checks only what changed. At most 3 rounds; after a third fail, stop that piece, put it in the one batched stop with the open findings, and never merge it (Nizam's default, the same shape as [They keep working](#they-keep-working)). A fail whose only open findings are phases that cannot run without crossing a limit `### How to run and try it` names or one of his gates skips the fix rounds and goes straight to the batched stop the same way. Missing browser tooling or test data is no such case: the auditor uses a browser tool from outside the repo when one is reachable (when none is, adding one is the technology gate, so the phase is reported as that gate) and makes mock data. After each round, run `git status --short` and `git log` on the branch: any change the auditor made voids that round, and a fresh `full` audit follows. **Exit:** a `pass`, with no unresolved **Critical** or **Important** findings. Nits get one fix pass with no new audit; the delegate reports anything it changed beyond the Nits, and you check its diff touches only Nit lines, else that change gets an audit.
- **CI**: every PR runs CI, and a red CI blocks the merge. Never merge on red.
- **merge**: you self-merge (squash) once the auditor's `pass`, [verification](#verification) evidence and a green CI are in; the project's versioning rule, if `## This project` names one, is part of that evidence. His merge is not a gate. If auto mode denies `gh pr merge`, it is the harness, not this contract: that denial goes into the one batched stop, not a mid-loop question.
- **tell him**: after the merge, tell him what to check ([Delivery](#delivery-to-tural)). Show him a preview only when he asks for one in the session.
- **UI changes**: the visual check is the auditor's, with `ui-verifier` as its helper when `.claude/agents/ui-verifier.md` exists; screenshots are the evidence of done, and he is not asked to confirm by eye what a screenshot can prove. The plan's Design reference (the issue's chosen boards), alongside the project's own references, is what the visual check compares against.

## The squad

Five lanes under [`.claude/agents/`](.claude/agents/), plus `ui-verifier`, the auditor's helper, in a project with a screen: **`locator`** (Sonnet) reads; **`planner`** (Opus) plans; **`plan-reviewer`** (Opus, fresh) checks the plan; **`coder`** (Sonnet) executes the plan and writes its tests, no separate tester, since the plan names the assertions; **`auditor`** (Opus) is the blocking gate.

- **The auditor is a fresh-context subagent spawned with `SendMessage` withheld, and it never fixes:** you check `git status` after each round. Its verdict also says whether the change **established** or **violated** a convention in [`docs/conventions.md`](docs/conventions.md).
- **Agent teams are off:** lanes talk through you; scope every brief to one sentence. Improvement ideas go in your report, never unsolicited into `main`.

### Multi-agent cost discipline

- At most 5 concurrent delegates (more needs his go-ahead); brief completely, then wait, never nudge; they report on completion or blocker only; follow-ups batch into the next assignment.
- Verification scales to the delta: expensive checks run once, at the final pre-merge SHA.
- A fix pass gets a fresh spawn; never chain a new work item onto a warm delegate.

## Keep his data out of the repo

No commit, PR body, issue comment, fixture, log or evidence holds secrets or private data about other people. Fixtures and evidence come from throwaway data only.

## Gates

**Tural's gates, the only ones.** **Settled in refinement:** product direction and scope. **Stop a build, batched into one stop:** **spending money**; **anything irreversible or outward-facing**, meaning beyond the repo (releases or packages, posting or messaging anywhere, repo settings, and any deploy other than the one `## This project` names); **a new library, a new third-party service, or a major version bump** (a dependency's or the project's own, e.g. 1.x to 2.0.0) not settled in refinement ([technology gate](#the-technology-gate)); **a change to this contract**. Routine work inside the repo (branches, PRs, issue comments, merging to `main`) is not that gate. Everything else proceeds, and is listed if he would see it. A gate item stops only the piece it belongs to (see [They keep working](#they-keep-working)).

**Machine and process gates:**

- **"Green `main`":** checks pass **and the number collected is not zero**, zero skipped, CI green, audit passed, evidence with counts, every dependency recorded.

**What runs:** the tracking gate, the dependency check and `permissions.deny` in [`.claude/settings.json`](.claude/settings.json). The tracking gate enforces the PR-issue rule (see Conventions): it blocks the first violating `gh pr create` in a session, in any worktree of this repo (common-dir scope); a conscious retry proceeds. Everything else is prose, enforced by you and the auditor.

## Conventions

- **Tracker:** GitHub Issues, at outcome altitude, not a task checklist. Ideas are issues labelled `idea`; refined ones are `ready`; `tiny` marks a fix he called tiny.
- **Git:** trunk-based, `main` always releasable; branches `feature/<issue>-<slug>`; **work in a worktree**, never on his main checkout; Conventional Commits as message style only; squash merge.
- **PRs are four sections: Why / What / Evidence / How to try it.** Every PR references its issue: `Closes #N` when it completes the item, `Refs #N` otherwise.
- **Documentation:** the three homes are indexed in [`docs/README.md`](docs/README.md).
- **Comments** follow [`.claude/rules/codery-code-comments.md`](.claude/rules/codery-code-comments.md).
- **Deploy, versioning, stack and the test command** are in `## This project`.

## Knowledge currency and technology decisions

### The technology gate

**A new library, a new third-party service, or a major version bump (a dependency's or the project's own, e.g. 1.x to 2.0.0) requires Tural's word before it lands.** It reaches him in refinement's round as a one-screen comparison table (options × what it buys × what it costs × the recommendation) when the idea needs it (research foresees it); in a build, prefer what the stack already has, else it goes into the one batched stop. The decision lands as an ADR in [`docs/adr/`](docs/adr/README.md).

Dependencies are gated in conversation: no `ask` rules sit in [`.claude/settings.json`](.claude/settings.json). [`.claude/hooks/dependency-record-check.py`](.claude/hooks/dependency-record-check.py) reports at `SessionStart` any dependency in `package.json`, `pyproject.toml` or `requirements*.txt` with no record in `docs/adr/` or [`docs/stack.md`](docs/stack.md): a report, not a gate. A name counts as recorded only when it appears as an exact backticked token in one of those files. `docs/stack.md` holds decisions made, never plans.

## Verification

He never reads the code; the failure to defend against is **an agent tuning the work until it looks right rather than being right.**

1. **Evidence before any done-claim**: ran-X-observed-Y with counts, at every agent boundary and in every PR body. A claim that cannot carry evidence is a hypothesis.
2. **An empty or skipped check is red, never green.**
3. **A claim about accuracy needs something independent to check against.** Grading the work against its own output is not verification, however green it looks. **What that reference is, and how it gets made, is Tural's call**, raised in refinement's round when foreseen; do not design it for him. In a build with no reference, the report is **descriptive** (what was measured, in what units, over what input, never how right it is) and is listed under Assumed, never a stop.
4. **Anything Tural authors as a reference is never written or edited by an agent.** You may read it, and you may report it missing or malformed. You may never supply or correct a value in it.
5. **He checks by using it:** before a done-claim on anything he sees, exercise the real thing and report how he can try it; the auditor works out how to run it from the project and the work, within the limits `### How to run and try it` sets.
6. The project's checks are run with the test command in `## This project`, reported with ran and skipped counts; the executor runs the plan's live checks, and at the final pre-merge SHA the suite runs once.

## Delivery to Tural

Report **in-session, in plain language**: when a piece is merged, tell him what to check and how, since he checks by using it. Show a preview only when he asks for one in the session; for something breaking or major he may ask for it and will say so. No channel pings. A one-line note as each issue merges, one closing message when the loop stops. **Keep it to one message:** do not narrate while delegates run; settle it, send one text.

**The closing message carries "Assumed, check these":** one line per default (issue, what was assumed, how to change it), as a table when there are several. Gate items stopped for his word are in the same message.

<!-- END CODERY TEMPLATE -->
## This project

### What this project is

*Emalatxana* (Azerbaijani for a workshop; `emx`) is Tural's main workspace for working with Claude Code. **The idea is in [`README.md`](README.md), the only statement of it.** Sole user: Tural. The repo is private. No scheduled runs.

**It is a separate tool.** Tural: *"it's a separate tool, it shouldn't be dependent on my other projects"* (2026-10-07). Nothing committed hard-codes or depends on his other repos, project names, folders, accounts or session names; anything specific to him is runtime input or local config that is never committed.

### Test command

`python3 -m unittest discover -s tests -v`, the standard-library `unittest` suite in `tests/`, reported with ran and skipped counts.

### How to run and try it

- **What it is, and how it is generally run:** the real tool, a tmux workspace started by `bin/emx`. Run it only on a private tmux socket (never the default one), with its prefs and its own state each moved to a throwaway folder, so nothing of his is read or written; the code shows which settings move what (the launcher's usage text, and `state_path` and `prefs_path` in `emx/core.py`). Moving only the prefs is not enough: emx's own state is his too. Leave alone any setting the sessions' claude also reads. Drive it headless, never by attaching: emx starts its workspace detached and only then tries to attach, so with no terminal the workspace stays up, and tmux on that private socket can send keys and read panes.
- **How it is looked at:** no screen. Read the panes through tmux on the private socket.
- **What it is tried on:** throwaway `emx-test-` sessions you start yourself, each in a `mktemp -d` folder, and nothing else; fixtures and evidence come from throwaway data only.
- **Never touch:** his real sessions; any id you did not start; anything under `~/.claude/` (never edit, move or delete; reading is fine); no `pkill` or `killall`. The rules under "Never touch his real sessions" below apply as written. Clean up what you started, and only that, and prove afterwards that nothing of yours is in his emx state or prefs.

*General, not exact: the agent works out commands, inputs and data from the project as it is now and the work at hand. The limits here are hard.*

### Deploy

No deploy stage.

### Versioning

**Versioning:** semantic versioning from 1.0.0 (Tural, 2026-10-08), held in `emx/__init__.py`. Each PR carries its own bump in `emx/__init__.py` and its plain-language lines in [`CHANGELOG.md`](CHANGELOG.md) under that version, in the PR before review, and the auditor checks them (Tural, 2026-10-08: "In the PR, before review"). The bump follows the change type (a feature the minor, a breaking change the major, a fix or anything else the patch). At merge you only re-check the number is still the next one on current `main`; if another merge took it, you renumber it (number and heading only, mechanical, no new review), then squash-merge. After the merge, tag the merge commit `vX.Y.Z` and push the tag; no GitHub Release.

### Stack

**The stack is tmux plus the Python standard library** ([ADR 0001](docs/adr/0001-tmux-and-python-standard-library.md)), so there is no manifest yet.

### Project-only rules

#### Never touch his real sessions

Of an earlier prototype Tural said: *"I don't want to lose sessions in the middle of work."* This rule comes from the setup proposal he released with *"the rest I guess you know, I'm not gonna double check"*.

- **Throwaway sessions only:** started by you, in a `mktemp -d` directory, never in his project folders, never with `--dangerously-skip-permissions`. A background session a test needs (it shows in his `claude agents` list) gets the prefix `emx-test-`, and you record its id. `EMX_SHOW_TEST_SESSIONS=1` is set only on the emx a live check starts on a private socket (that emx also never speaks or shows banners). Every throwaway session is named `emx-test-…` (`claude --bg -n emx-test-…`) and runs in a `mktemp -d` directory, because his emx hides a session only when both hold; a live check keeps the `emx-test-` prefix through any rename.
- **Clean up** before you report: end the processes, or `claude stop` then `claude rm`, only ids and PIDs you started and recorded; delete the `mktemp` directory.
- **Never stop, rm, attach to, resume, rename, send input to, or otherwise act on an id you did not start.** Never `pkill` or `killall` by name. Reading `claude agents --json` is fine.
- **What Claude Code itself writes** for a session you started (transcript and state under `~/.claude/`) is expected and stays. Agents never edit, move or delete anything under `~/.claude/` themselves, and never change his settings or hooks; reading is fine.

#### Keep his data out of the repo, project specifics

The repo is private, but it may be made public later and the tool stays separate. So no commit, PR body, issue comment, fixture, log or evidence holds secrets, private data about other people, or real session names, paths, transcripts or screenshots of his. Fixtures and evidence come from throwaway sessions only.

#### His gates, in addition to the shared ones

**Making this repo public** is his gate. `permissions.deny` in `.claude/settings.json` holds `pkill` and `killall` by prefix, not `sudo` or `xargs`.

#### He checks by using it

Exercise the real tool in throwaway sessions only, and report how he can try it.
