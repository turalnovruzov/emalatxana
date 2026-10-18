# Voice and notifications

emx tells you when a session finishes (done) or stops for you (needs input). It does it in one of two modes.

## The two modes

- **Notification mode** (the default) shows a normal macOS notification and never speaks.
- **Voice mode** speaks the notice aloud instead of showing a notification. If speaking fails, or there is no `say`, emx shows the notification instead.

Press `m` in the panel to switch. Your choice is saved.

The first time you use notification mode, macOS asks whether emx may show notifications. emx builds a small helper app for this, which needs Apple's command line tools (`xcode-select --install`).

## Stop the voice

In voice mode, `h` in the panel stops the voice now: the sentence being spoken and any waiting behind it. It does not mute later notices. From any terminal, `emx hush` does the same. In notification mode they do nothing.

## The history

The newest notification shows in the bar at the bottom of the window. Press `N` in the panel to see the last 20 notifications, newest first. `↑` and `↓` move, `Enter` or a click opens that session, `Esc` goes back. Opening a session dims its entries.

## Click a notification

Clicking a notification brings your terminal forward with that session open in emx. If no emx window is open, it opens one.

## Change what speaks or notifies

`EMX_SAY` replaces the command used to speak (default `say`). `EMX_NOTIFY` replaces the notification helper's command. See `emx docs reference` for the full list of settings.
