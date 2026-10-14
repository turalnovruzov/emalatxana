# 0006. emx shows its notifications through a small helper app of its own, emx-notify.app

- **Status**: Accepted
- **Date**: 2026-10-14

## Context

Issue #43. Since 1.5.0 (#35, ADR 0004's amendment) a notice was only the spoken sentence, so with the voice muted he got nothing. Tural: *"When I mute the experimental Java speaking feature that we have for the notifications, I should get normal notifications in EMX. Because when I'm working in public and I don't have headphones, I mute that and then I don't get any notifications."* He settled two modes, never both at once: *"2 modes: voice mode where i dont get notifications, i get spoken voice. notification mode where i get notifications and no speaking. notifications should be normal mac os or the whatever the os is notifications from EMX"*. The sender must be emx, not the terminal: *"ohhh then lets go with emx notifications. but before deciding the techincal approach i want you to get a subagent to do a spike on it"*. The banners emx showed before #35 came from iTerm2 (with its sound) and from `osascript` (with Script Editor's name on them), which is why they were removed.

The spike's report is not in the repo; its findings are recorded here as the issue's item 2a states them: a small helper app of emx's own, one Swift file built with Apple's `swiftc`, ad-hoc signed, at a stable path, registered with LaunchServices, rebuilt when its source changes, posting through `UNUserNotificationCenter` with no sound; the permission is the normal one-time macOS prompt, not provisional. He approved it: *"ask once. item 2 sure"*. A click opens that session in emx (*"item 6 sure"*); with no emx window open, *"Open emx in a new window"*.

## Decision

We show notifications through `emx-notify.app`, built by emx itself from `emx/emx-notify.swift`.

- **Where and how it is built** (`emx/notify.py`). On a panel's first start, into `~/Library/Application Support/emx/`: `swiftc` compiles the one file, the app draws its own icon (⚒ in copper on a dark rounded square, from the system font's glyph) and `iconutil` makes the `.icns`, `codesign --sign -` signs it ad hoc, the new bundle is swapped in by rename, and `lsregister -f` registers it. A hash of the source, the Info.plist and the build recipe is written last; a matching hash means nothing is rebuilt. Without Apple's command line tools (`xcode-select -p` fails) nothing runs, so Apple's install dialog never pops up, and the top line says "notifications unavailable".
- **Its bundle.** Id `com.emalatxana.emx-notify`, name "emx", `LSUIElement` (no Dock icon). A helper built anywhere but that folder (`EMX_NOTIFY_HOME`, live checks) gets the id `com.emalatxana.emx-notify-test` and the name "emx test", and the real id is never built outside its own folder, so a test can never take over his registration.
- **How emx talks to it.** The panel's `Notifier` thread runs the app's executable with one verb: `status`, `authorize` (asked when the panel starts in notification mode or `m` switches into it, never at the build), `post`. Each prints one JSON line (`auth`, `alerts`, `posted`, `shown`, `delivered`); `shown` is the request found among the delivered ones, the evidence a banner showed. The banner's words reach it as JSON on stdin, never in its command line. No sound.
- **The modes.** Notification mode posts and never speaks; when banners cannot show (permission denied or alerts off, or the helper cannot be built) the top line says so and the history keeps every notice, and it never falls back to the voice. Voice mode speaks and posts nothing, except that a `say` that fails posts the plain line as a banner.
- **A click** relaunches the app through LaunchServices; its delegate runs `emx open <session id>` with an absolute Python and `bin/emx` and an allowlisted environment. `emx open` writes `@emx_open` on emx's own tmux server and brings the terminal emx was started from (`@emx_terminal`, recorded at launch) forward with `open -b`; the panel opens the session as the list would.
- **No new library.** `swiftc`, `codesign`, `iconutil`, `lsregister` and `open` are Apple's tools; the Python side stays standard library.

## Consequences

- Live checks, 2026-10-14, with a test helper built into a `mktemp -d` folder: the build takes about 3 s; `lsregister -dump` lists the test id at that path; `codesign -dv` reads `Signature=adhoc`. `status`, run as a child process, answered `notDetermined` without the "bundleProxyForCurrentProcess is nil" abort a bare executable gets, so the directly run executable is attributed to its bundle. A click proxied through LaunchServices (`open -a <app> --args click <file>`) brought the session into the main pane of a test emx. The permission prompt, a real banner and a real click need his eyes and hand; they are listed for the lead in the plan's Status.
- Building is the expensive part and happens once per source change, on the notifier's thread, under a file lock, so two panels never build at once. Rebuilds change the ad-hoc signature; whether the permission survives one is to be confirmed on his Mac.
- An "emx test" entry stays in System Settings, Notifications, after live checks.
- Bring-to-front activates the terminal app, not a tab: with emx in a background tab, the app comes forward on another tab.
- Linux and other systems get no banners ("notifications unavailable").

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **emx-notify.app, built by emx** (this record) | emx's own name and icon on the banner, no sound, a click it can handle | Apple's command line tools on his Mac; a build on first start | **Chosen** (Tural, 2026-10-14: "ask once. item 2 sure") |
| `osascript` `display notification` | Nothing to build | Script Editor named as the sender; clicks can't be handled; removed in #35 | Lost |
| Terminal OSC 9 (iTerm2) | Nothing to build | iTerm2 as the sender, with its sound; only while attached; removed in #35 | Lost |
| `terminal-notifier` | A ready tool | A new third-party tool to install and trust | Lost |
| Provisional authorization (no prompt, quiet delivery) | No prompt at all | Banners land quietly in Notification Center, not on screen | Lost: "ask once" |
