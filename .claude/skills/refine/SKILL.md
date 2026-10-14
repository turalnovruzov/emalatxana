---
name: refine
description: Refine an `idea` issue with Tural, items one at a time, until he says go and it becomes `ready`. Use when he asks to refine an idea, or picks an issue to work on that is neither `ready` nor tiny (labelled `tiny` or called tiny by him; an `idea`, or one with no label). A collaboration in the conversation: no background agents, and every decision is his unless he hands it over.
---
<!-- template: product-owner @ 0.2.0 — shared; change it through Codery -->

# refine

**Goal.** An `idea` is made into what he actually has in mind, with its broad impact and edge cases considered, before development builds it. This skill is self-contained: it runs here, where only this repo's `CLAUDE.md` loads. Its abstract shape is the shape of Codery's `system` skill (talk, decide, then build), not a copy of it.

His words, which this skill serves:

> "i wanna introduce this idea refineemnt because many times i asy the idea, agent handles and buiolds it but it is not exactly what i had in mind. another equally imprtant reason is that agent doesnt consider the broad impact or edge cases or other things of the idea."

> "you see how this system skill is? how we decide on what the system should be beofre you build? the abstract workflow should be like that. but dont copy it one to one because for exxample the visualization wont match idea refinement, among other things."

> "i dont want background agents in side projects. agent shoudl do research and etc but the idea refinement needs to be a collaboration"

> "all ideas and decisions need to be approved by me whether its your or my suggestion, unless i explicitly say that the agent can go ahead and decide on its own. my role is to make decisions. Agents role is to read and know as much as possible and let me know what i should know to make decisions. and it should be like a conversation of itesm one by one just like we are talking right now."

> "yes it's job is also to tell me what needs to be considered, one by one."

> "so, for ideas or issues that have UI involved, cloud design should be used. I thought the agent would automatically use it, but it doesn't. So, cloud design should be used to decide on a design before implementation starts. Something like that."

> "I think it belongs in refinement probably because a lot of questions raised during refinement can actually be solved easier with design if UI is related."

## The loop

1. **Pick one `idea` issue.** He names it, or you list them (`gh issue list --label idea`) and he picks. Read the issue (his words and what is already under Decided), its comments, and the code and docs it touches.
   An issue he picks that has no label (one from before this system) is treated as an idea: refine it the same way.
2. **Research and think, inside the conversation.** Read as much as you need. Work out what is missing, what has to be discussed, the broad impact and the edge cases. No background agents: research runs here, in the foreground, and nothing is left running while he is away or after the session ends (Nizam's reading of "background agents").
3. **Raise the items one at a time.** Say what he should know to decide it, and what you recommend where the choice is his. Do not raise the next item before he has decided this one. An item that involves what he will see on a screen is drawn, not talked through (see "A UI item is drawn" below).
4. **He decides.** Every idea and decision is his, whether the suggestion was yours or his. Write his decision, with his line, under `## Decided` in the issue straight away (this recording shape is Nizam's default), so a later session, or another agent, can resume from the issue alone. For a drawn item the entry also carries the canvas link and the name of the board or boards he chose beside his line. Those boards, and only those, are the issue's design reference for development; a rejected alternative on the same canvas is never built.
5. **Back to step 2.** A decision can open new items; research again, then raise the next one.

## A UI item is drawn

When an item involves what he will see on a screen, answer it by drawing it on a Claude Design canvas, in this conversation, in the foreground, so a question that talk would leave open is settled by looking. Make the canvas with the Artifact tool, `action: "quickstart"` with `intent: "design"`, and follow the instructions it returns. Give him the link; he reacts, and decides. In doubt whether an item involves UI, draw it: he can wave it off (Nizam's default).

- **One canvas per issue.** A later UI item on the same issue becomes a new board on the same canvas, so the issue carries one link (Nizam's default).
- **Where the choice is his, draw the alternatives** as separate boards rather than picking one for him (Nizam's default).
- **The project's own references.** When `## This project` names design references (a design system, tokens, a design project), the canvas uses them (Nizam's default).
- **This skill names no style, layout or design method.** A drawing is a proposal; nothing drawn counts as decided until his line is under `## Decided`.

## A hand-over

Unless he explicitly says the agent can go ahead and decide on its own, he decides. A hand-over covers one idea only:

> "only for one idea unless i say you decide everything"

If he says `you decide everything`, it covers all ideas. When you decide under a hand-over, say so and record it under `## Decided` as a decision made on his hand-over, with his line, so he can see what was his and what was handed over.

## Ending

When you have nothing left to raise, say so. When he then says go, relabel the issue from `idea` to `ready`:

```
gh issue edit <N> --remove-label idea --add-label ready
```

For an issue with no `idea` label, the command is:

```
gh issue edit <N> --add-label ready
```

The loop ends this way (Nizam's proposal; Tural: "nice"), and an idea lives as a GitHub issue labelled `idea`, switched to `ready` after his go (Nizam's proposal; Tural: "sure i agree"). Never relabel without his go.

## Separate sessions

> "reinfement and development dont have to strictly connected and one session. they are seperate systems with development being dependent on refinement with the expection of tiny fixes."

Refinement can run in its own session, apart from capture and development. Development works `ready` issues only; a tiny fix is the exception, and a tiny fix is his call, not yours.

## Never

- Never decide for him what he has not handed over; never write an invented acceptance criterion, ticket type or priority, since he has not said any.
- Never treat anything drawn as decided until his line is under `## Decided`; a board is a proposal.
- Never carry refinement past `ready`; development is its own piece, in this session or another.
