# 0004. emx words its own notices with Haiku and speaks them with the system voice

- **Status**: Accepted
- **Date**: 2026-10-08

## Context

Issue #8: tell him when a background session needs him (a permission, a question) or has finished. Tural, in-session, on what a notice should say: *"it should be brief. It should give me context, just like Jarvis. [...] I work on a lot of stuff at the same time, right? Should definitely give me context for me to know, okay, this is about this. [...] so short that I recall what the fuck we were working on. And then whatever I need to know, basically."* On how: *"just make it good. lets aslo do eith the local voice."* On the open session: *"stay quiet if I am looking at a session, of course, but there is a chance that I'll leave a session open and then just go away. Maybe we can make it so that if I am focused on the panel, even if a session is open, it should still notify me."* He withdrew a "stop talking when I click a session" idea (*"No, no need for that"*), and on Claude Code's own notices: *"we gotta consolidate this with that"* and, when asked whether emx should own every notice, *"Yes, switch CC's off"*. On a better voice later: *"once its done, if i like it, we can improve and connect openai"*.

## Decision

emx is the source of notices. When a session moves **into Needs input or Done**, emx shows one banner and speaks one or two sentences that Haiku wrote.

- **Triggers** (`core.notice_transitions`). Only a change of category into Needs input or Done counts. The first non-empty listing is the baseline and gives none, and an id first seen within `STALE_AFTER_MISSES` listings, counting the first, joins the baseline silently (the first listing can be partial). An empty or failed listing changes nothing. Ids are never forgotten, so a session that drops out of a partial listing and returns as it was gives none; one first seen after the baseline counts as coming from nowhere. A session being closed, interrupted or renamed by emx is quiet, and a stopped one never triggers.
- **In front** (`core.in_front`). A notice is skipped only when the session is the one in the main pane, a client is attached and its terminal last reported focus-in, **the main pane (not the panel) is the active pane**, and the client had input within `AWAY_SECONDS` (300 s). Anything unknown means he is told. tmux hooks on `client-focus-in` and `client-focus-out` keep `@emx_focus`; `Tmux.client_state` reads it with the active pane and `#{client_activity}` in one call.
- **The words** (`core.NOTICE_INSTRUCTION`, through `claude -p --model haiku` with ADR 0002's full flag set, the excerpt on stdin, in the same private empty folder). Context first (project, then what the work was), then what came of it or what is waiting on him; one or two sentences, 25 words at most, no file names or paths (what the file is for instead); a calm British butler, "sir" at most once; exact figures when the excerpt has them; no filler. The transcript is given about 6 s (`NOTICE_SETTLE_SECONDS`) to catch up with the state. If Haiku fails, refuses or says nothing usable, a plain line (`{name}, in {project}, needs you.` or `is done.`) is used, so a notice is never lost. The answer is cleaned by `core.clean_notice`, which also cuts it at `NOTICE_MAX_WORDS` (28) words, at the last sentence end inside the cap, else at the last word, not `clean_summary`, which keeps one line cut at 120 characters.
- **Showing it. Superseded by the 2026-10-09 amendment: no banner shows any more.** With a client attached, an OSC 9 sequence in tmux's DCS passthrough is written to the terminal after the panel has drawn. `allow-passthrough` is set **on the panel's pane only**; set globally it would also let Claude Code's own notifications out of every viewer and double each notice. With no client attached (after `q`), the banner is `osascript` `display notification`, the text passed after `--` as an argument, never inside the script. Whether a client is attached is read again at delivery, so pressing `q` between the transition and the banner does not lose it.
- **Speaking it.** `say`, text on stdin, one thread, so two sentences never overlap. The voice is picked once from `say -v '?'` by an ordered allowlist (`core.VOICES`): every Premium or Enhanced British voice ranks above every compact one, and with none installed `say`'s own default is used. `m` mutes and unmutes the voice (`"voice": false` in prefs.json), stops the sentence being said and drops the queue. The top line shows a "muted" mark, kept ahead of the session counts.
- **No new dependency.** The standard library, `claude -p`, and the macOS tools `say` and `osascript` (the `osascript` clause is superseded by the 2026-10-09 amendment: no banner, so no `osascript`).

## Consequences

Measured on Claude Code 2.1.295, 2026-10-08, with the production arguments, `claude-haiku-5-5` on every call. Made-up excerpts through emx's own `announce`: 14 calls, both kinds, short, with figures, with bad news, unnamed, and two with every cap hit (a 400-word request and reply, a long tool call). Plus 8 calls from the live panel run, made under load beside the summary worker with the first wording of the instruction.

| | n | min | median | max |
|---|---|---|---|---|
| wall, made-up excerpts (s) | 14 | 2.06 | 2.25 | 4.28 |
| CLI `duration_ms`, made-up | 14 | 682 | 870 | 2611 |
| cost, made-up (USD) | 14 | 0.000221 | 0.000237 | 0.000412 |
| output tokens, made-up | 14 | 35 | 50 | 312 |
| cache-creation tokens, made-up | 14 | 1014 | 1062 | 1278 |
| wall, live (s) | 8 | 3.61 | 6.30 | 11.16 |
| cost, live (USD) | 8 | 0.000219 | 0.000225 | 0.000243 |

- **The dearest call** was $0.000412 (every cap hit, 312 output tokens). `NOTICE_MAX_BUDGET_USD` is $0.002, over three times that. As ADR 0002 found, the CLI checks the budget after an answer has come back, so it stops a second turn and does not cap one call. What bounds a call is the excerpt (about 2000 characters), the 25-word instruction and the 15 s timeout (`NOTICE_TIMEOUT`).
- **Live walls reached 11 s** beside other `claude` calls, so the 15 s timeout has little room under load. A timeout gives the plain line, and the notice is not lost.
- **What it costs.** One call per notice, about $0.00023, so a hundred notices a day cost about 2.3 cents. Calls are one at a time on their own thread; the most a panel could make is about 3600 / 2.06 = 1750 an hour, about $0.72 at the dearest figure, and it makes one per transition, not per second.
- **The plain line is not rare for a session emx has only just found.** A session that blocks within seconds of first being listed can have no transcript read yet, so after the 6 s wait it is announced with the plain line (seen once in the live run). A session emx has watched for longer is worded by Haiku.
- **The word cap.** After the first wording, Tural judged it too long. Re-measured with the final instruction (no file names or paths) and the 28-word cap, 14 made-up excerpts (both kinds, figures, bad news, a file-heavy reply, two with every cap hit), counted after `clean_notice`: min 13, median 22, max 28 words. No file name or path appeared in any of the 14.
- **The voice** in the live run was `Jamie (Premium)`, then the first installed entry of `core.VOICES` on this machine. Tural called it "also very robotic", after Daniel sounded robotic to him too. Premium and Enhanced still rank above compact ones for quality, and the order tries Daniel (Premium), Daniel (Enhanced), Serena (Premium), Serena (Enhanced) first, so a better one is picked up if he installs it (System Settings, Accessibility, Spoken Content). There is no voice picker yet.
- **Not covered, and said plainly in the CHANGELOG.** emx hears only about the background sessions `claude agents` lists, and only while its panel runs. After he sets `"preferredNotifChannel": "notifications_disabled"`, a `claude` in another terminal and any time emx is closed give no notice at all. No agent changes his settings.
- **Not provable here (the banner parts are superseded by the amendment).** Whether iTerm2 shows an OSC 9 banner while it is in front, and whether the `osascript` banner shows under his notification permissions (exit 0 is all that was observed), can only be seen by him. Whether the focus hooks fire on a real iTerm2 tab switch was proved only with a pty client sending the focus reports.
- Excerpts of listed sessions go to Anthropic, as in ADR 0002; nothing is stored (`--no-session-persistence`).

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **emx words and shows its own notices** (this record) | One source, his wording, context first, one switch | Only background sessions, only while emx runs | **Chosen** (Tural, 2026-10-08) |
| Claude Code's own notification channel | Nothing to build | Terse, no context, covers no "which project" need; he asked to consolidate | Lost |
| OpenAI (or another cloud) voice | A more natural voice | A new service, a key, money | Later: *"if i like it, we can improve and connect openai"* |
| `osascript` banners only | No terminal escape handling | Not an iTerm2 notification, and it needs the attached client to be told apart from none | Lost as the only path; kept for when no client is attached (superseded: removed in #35) |
| `allow-passthrough on` globally | One line of config | Lets Claude Code's own OSC 9 out of every viewer and doubles each notice | Lost: pane-only (superseded: no passthrough at all after #35) |
| Notify for every state change | Simple | Noise: Working and Stopped are not news | Lost |

## Amendment, 2026-10-09 (#35): voice only

emx shows no banner of any kind any more: no OSC 9 terminal notification, no `osascript` banner, and no `allow-passthrough` on the panel's pane. A notice is the spoken sentence only. Tural, in-session on #35: *"I don't want to hear the notification sound. I just want to hear the guy speaking"*, and *"the banner can be ignored. I only want voice. [...] I don't want the banner there at all."* The "Showing it" bullet, the banner parts of "Not provable here" and the `osascript` and `allow-passthrough` rows of the alternatives are superseded by this section. Everything else in this record stands. Status stays Accepted.

## Amendment, 2026-10-14 (#43): two modes

The voice-only scope of the 2026-10-09 amendment is now **voice mode**, one of two modes that `m` switches between; banners come back in **notification mode**, shown by emx's own helper app ([ADR 0006](./0006-emx-notify-helper.md)), never by iTerm2 or `osascript`. Tural: *"2 modes: voice mode where i dont get notifications, i get spoken voice. notification mode where i get notifications and no speaking."* Notification mode is the default; a saved `"voice": true` becomes voice mode and `"voice": false` notification mode (the prefs key is now `"mode"`). In notification mode Haiku writes the sentence to be read, without the butler (`core.NOTIFICATION_INSTRUCTION`); the triggers and the in-front rule are unchanged in both modes. `m` no longer mutes: there is no fully quiet mode, and the top line's "muted" mark is gone (it shows "voice" in voice mode). `h` and `emx hush` stop the voice in voice mode and do nothing in notification mode. The Notices list becomes the Notifications history (#43 item 11): every notice kept, newest first, up to 20, read ones dimmed, `N` for the full view, in memory only. Status stays Accepted.
