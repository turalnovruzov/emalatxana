---
name: refine
description: Refine an `idea` issue with Tural in one round (research first, one screen of every item, he answers once) until it is `ready`. Use when he asks to refine an idea, or picks an issue to work on that is neither `ready` nor tiny (labelled `tiny` or called tiny by him; an `idea`, or one with no label). A collaboration in the conversation: no background agents, and every needs-you decision is his unless he hands it over.
---
<!-- template: product-owner @ 0.4.0 — shared; change it through Codery -->

# refine

**Goal.** An `idea` is made into what he actually has in mind, with its broad impact and edge cases considered, before development builds it. This skill is self-contained: it runs here, where only this repo's `CLAUDE.md` loads. Its abstract shape is the shape of Codery's `system` skill (talk, decide, then build), not a copy of it.

His words, which this skill serves:

> "i wanna introduce this idea refineemnt because many times i asy the idea, agent handles and buiolds it but it is not exactly what i had in mind. another equally imprtant reason is that agent doesnt consider the broad impact or edge cases or other things of the idea."

> "you see how this system skill is? how we decide on what the system should be beofre you build? the abstract workflow should be like that. but dont copy it one to one because for exxample the visualization wont match idea refinement, among other things."

> "i dont want background agents in side projects. agent shoudl do research and etc but the idea refinement needs to be a collaboration"

> "all ideas and decisions need to be approved by me whether its your or my suggestion, unless i explicitly say that the agent can go ahead and decide on its own. my role is to make decisions. Agents role is to read and know as much as possible and let me know what i should know to make decisions."

The next two lines were replaced on 2026-10-14 by his words below (the one-by-one steer, which was the cause of the many questions):

> "and it should be like a conversation of itesm one by one just like we are talking right now."

> "yes it's job is also to tell me what needs to be considered, one by one."

His words of 2026-10-14, which replace them:

> "I want to do refinement once if possible Then I want to leave the session and only come back when it's done I don't want to have to keep checking back So I want to give my attention to a session, refine the idea But I don't think I want as many questions You can maybe investigate with me Ask me questions on what's more important to me But those were too many questions that some of them didn't really matter Yeah, I want to be done with that, then give the go And I don't really need to give the go As soon as the idea is defined, if I already said we're going to work on that idea And we're going to implement it, then go ahead and build it And I want to check the result when it's done So that gap I want to not be concerned that Oh, is this session stuck and waiting for me? I wanted to keep working"

> "i am trying to reduce the time and freq sessions depent on me as much as possible for example if _ is me paying attention and - is agent working without me i dont want this __--_-_-__---_-__. i want this: __-------__----------------____---------"

His words of 2026-10-14 on a UI item, which stand beside the one round:

> "so, for ideas or issues that have UI involved, cloud design should be used. I thought the agent would automatically use it, but it doesn't. So, cloud design should be used to decide on a design before implementation starts. Something like that."

> "I think it belongs in refinement probably because a lot of questions raised during refinement can actually be solved easier with design if UI is related."

## The loop

One round: research first, one screen, he answers once.

1. **Pick one `idea` issue.** He names it, or you list them (`gh issue list --label idea`) and he picks. Read the issue (his words and what is already under Decided), its comments, and the code and docs it touches. If he wants to say his idea first, take his words first, before any research; he would rather state his own idea than read a menu of options (Tural, 2026-10-14: "instead of reading your message i'll give you my idea then you help me refine it").
   An issue he picks that has no label (one from before this system) is treated as an idea: refine it the same way.
2. **Research first, inside the conversation.** Read as much as you need. Work out what is missing, the broad impact and the edge cases, and what each open item should be. Read-only `locator` subagents may run in the foreground while you wait for them (Nizam's default); nothing runs in the background, and nothing is left running while he is away or after the session ends (Nizam's reading of "background agents").
3. **One screen.** A table of every item, nothing held back for later:

   | # | item | what I'll do | needs you? |
   |---|------|--------------|------------|

   Small items come pre-decided in the "what I'll do" column: "I'll do X; say so if not." Items that need him go together, as pickers in one question call (several questions in one call), each with its options and your recommendation marked, and a preview where the item is something he will see. An item that involves what he will see on a screen is drawn first, as a board on a Claude Design canvas (see "A UI item is drawn" below), and its board is shown in this same screen, so the canvas link and its boards are part of the one screen, not a second one.
   **Needs you** is product direction or scope he would notice that no word of his supports, and anything under his gates (`CLAUDE.md`, Gates). Everything else is pre-decided.
4. **He answers once** ("all fine except 3"). Write every result under `## Decided` in the issue in one edit, with his line: his answers as his, the pre-decided items marked "Nizam's default, not objected to" (this recording shape is Nizam's default), so a later session, or another agent, can resume from the issue alone. For a drawn item the entry also carries the canvas link and the name of the board or boards he chose beside his line. Those boards, and only those, are the issue's design reference for development; a rejected alternative on the same canvas is never built.
5. **A second round only if his answers open something new** or change another item's recommendation. Research that item, then show only the new items on one screen. Otherwise go to Ending.

## Visuals over text

Tables, pickers with previews, small diagrams; a message between questions is a few lines at most; long explanations only when he asks. His "visualization wont match idea refinement" above meant (Nizam's reading) that the `system` skill's architecture canvas (boxes, people, outside systems) is not copied here; a UI item still gets a Claude Design canvas, as the next section says, and everything else is shown in the session itself.

## A UI item is drawn

When an item involves what he will see on a screen, answer it by drawing it on a Claude Design canvas, in this conversation, in the foreground, so a question that talk would leave open is settled by looking. Make the canvas with the Artifact tool, `action: "quickstart"` with `intent: "design"`, and follow the instructions it returns. Draw it during the research step, before the screen is shown, so the drawn items are part of the one screen and the one round, not a second round: the screen carries the canvas link, and a UI item's row points at its board (or boards, where the alternatives are his, offered as one picker with the boards as its options). He reacts in his one answer and decides from the drawing. In doubt whether an item involves UI, draw it: he can wave it off (Nizam's default).

- **One canvas per issue.** A later UI item on the same issue becomes a new board on the same canvas, so the issue carries one link (Nizam's default).
- **Where the choice is his, draw the alternatives** as separate boards rather than picking one for him (Nizam's default).
- **The project's own references.** When `## This project` names design references (a design system, tokens, a design project), the canvas uses them (Nizam's default).
- **No extra round.** A drawn item never opens a round of its own; a second round happens only as step 5 says.
- **This skill names no style, layout or design method.** A drawing is a proposal; nothing drawn counts as decided until his line is under `## Decided`.

## A hand-over

Unless he explicitly says the agent can go ahead and decide on its own, he decides the needs-you items. A hand-over covers one idea only:

> "only for one idea unless i say you decide everything"

If he says `you decide everything`, it covers all ideas. Under a hand-over there is no round: decide, record each decision under `## Decided` as made on his hand-over, with his line, so he can see what was his and what was handed over, and list them for him.

## Ending

When his answers are in and nothing new is open, relabel the issue from `idea` to `ready`, with no separate go:

```
gh issue edit <N> --remove-label idea --add-label ready
```

For an issue with no `idea` label, the command is:

```
gh issue edit <N> --add-label ready
```

If he picked the issue to work on it, development starts now, in this session. If he set a scope for the session ("refine only", "ready or close"), act within it: relabel, or close the issue with a one-line reason, with no confirmation question.

An idea lives as a GitHub issue labelled `idea`, switched to `ready` once his round is answered (Nizam's proposal; Tural, 2026-10-14: "go", on the one-round fix). Never relabel before his round is answered.

## Separate sessions

> "reinfement and development dont have to strictly connected and one session. they are seperate systems with development being dependent on refinement with the expection of tiny fixes."

Refinement can run in its own session, apart from capture and development. Development works `ready` issues only; a tiny fix is the exception, and a tiny fix is his call, not yours.

## Never

- Never decide a needs-you item for him; never write an invented acceptance criterion, ticket type or priority, since he has not said any.
- Never carry refinement past `ready` unless he picked the issue to work on it; development is its own piece, in this session or another.
- Never treat anything drawn as decided until his line is under `## Decided`; a board is a proposal.
