# Emalatxana

His words: "lets do emalatxana with emx as the shorthand". [Nizam: emalatxana is Azerbaijani for a workshop, a place where things are made; `emx` is the shorthand.]

## The idea

> The vision is a workspace, my main workspace, where I work with Claude Code This is gonna hopefully evolve to have a lot of features that will be good for me And its purpose will be when I'm working, I open this and I start working with Claude Code It's my workspace.

> I don't know the CLI seems more comfortable to me I tried that a few times it wasn't as comfortable as me working in the CLI Maybe I could try working in cloud desktop but also that consumed so much more RAM and resources than this Which is the CLI

## What he wants first

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

## Docs

How to use emx is in [`user-docs/`](user-docs/index.md), also printed by `emx docs`.

## How this project runs

See [`CLAUDE.md`](CLAUDE.md).
