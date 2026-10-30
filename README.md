# emx

**A tmux workspace for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).** Every Claude Code session you run sits in a side panel next to the one you are working in, with its live state (working, needs input, done), so you see them all and switch with a key or a click. Sessions live in emx's own private tmux server, so they keep running when you detach, close the terminal or lose the connection.

*Emalatxana* is Azerbaijani for a workshop, a place where things are made; `emx` is the shorthand.

## What it does

- **A side panel of sessions**, grouped by project, each with its state and a line on what it is doing. Start, switch, find, pin, rename and close sessions from the panel, by key or mouse.
- **A narrow session beside it**, so text reads like a page rather than a wide landscape screen.
- **Tells you when a session needs you** or has finished, spoken or as a notification.
- **Remote:** `emx remote <host>` opens the emx running on a server over SSH; its sessions keep running there while your laptop sleeps.
- **Phone page:** a small web page over Tailscale, reachable only from your own devices, to check on sessions and start, rename or close them.
- **Routines:** what your server's scheduled jobs ran in the past day.

## Requirements

- macOS or Linux, tmux and Python 3 (standard library only, nothing to `pip install`).
- `claude` (Claude Code), installed and logged in.

## Install

```sh
git clone https://github.com/turalnovruzov/emalatxana.git /tmp/emx-src
sh /tmp/emx-src/install.sh
emx
```

It installs into `~/.local/share/emx` and links `emx` into `~/.local/bin`, editing none of your shell files. emx updates itself from this repo.

## Docs

How to use emx is in [`user-docs/`](user-docs/index.md), also printed by `emx docs`. Start with [getting started](user-docs/getting-started.md); every key, command and setting is in the [reference](user-docs/reference.md).

## How this repo is built

emx is a personal tool and an experiment in agent-driven development: its owner acts only as product owner and does not write or review the code. Claude Code agents plan, build, test and audit every change under the rules in [`CLAUDE.md`](CLAUDE.md) (the "Codery" template; "Nizam" in issues and docs is its systems-engineer agent). That is why issues, ADRs and docs quote the owner's words verbatim. Design decisions are in [`docs/adr/`](docs/adr/README.md), and every release is in [`CHANGELOG.md`](CHANGELOG.md).

Tests: `python3 -m unittest discover -s tests -v`. Issues are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## The idea, in the owner's words

The words that started the project, kept as they were said. His words: "lets do emalatxana with emx as the shorthand". [Nizam: emalatxana is Azerbaijani for a workshop, a place where things are made; `emx` is the shorthand.]

### The vision

> The vision is a workspace, my main workspace, where I work with Claude Code This is gonna hopefully evolve to have a lot of features that will be good for me And its purpose will be when I'm working, I open this and I start working with Claude Code It's my workspace.

> I don't know the CLI seems more comfortable to me I tried that a few times it wasn't as comfortable as me working in the CLI Maybe I could try working in cloud desktop but also that consumed so much more RAM and resources than this Which is the CLI

### What he wanted first

From his opening message, about the `claude agents` view, which he calls "cloud agents view":

> I want you to look into how the cloud agents view works I like that view, however, one I am inside one session I cannot see the status of the other sessions and I actually like my session width to be a little bit smaller to be a little bit narrower, so it's easier to read text um, reading like a book instead of... do you get what I mean? like reading a landscape ratio is harder than reading a portrait landscape for me um, and the panel on the left side or the right side can be basically the cloud agents view [...] so I always have visibility, I can click and switch in between I wanna be able to start new sessions from the right hand panel um, in different folders, in a folder of my choosing and etc We can improve it over time

> I want it to be as smooth as cloud agents.

His answers to three questions from Nizam (the Codery systems engineer; the bracketed notes are Nizam's):

> 1. i have been primarily using claude agents for a few days and i love it except for a few things that i told you about
> 2. sure  [Nizam: to "one launcher command each time, instead of `claude agents`?"]
> 3. both should be availbel.  [Nizam: to "should a new session start with a message you type, or just open empty in the folder you pick?"]

After a prototype that switched sessions via `/resume`, which loaded another conversation into the current session's slot, he said:

> this ain't good. I don't know if we can make it better I don't want to lose sessions in the middle of work.

> Also it's a little bit glitchy, which I don't like

> this doesn't have to be a mod [...] maybe this can be something else You know CLI tool, visual tool

## License

[MIT](LICENSE)
