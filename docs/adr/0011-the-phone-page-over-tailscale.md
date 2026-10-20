# 0011. The phone page is served by the primary panel, over Tailscale, to the machine's owner only

- **Status**: Accepted; built in [#67](https://github.com/turalnovruzov/emalatxana/issues/67) (emx 2.14.0).
- **Date**: 2026-10-20

## Context

Issue #67: the phone. After the terminal layout of 2.12.0 failed on his iPhone, refinement chose a web page (Decided 21-28) over Tailscale, in plain http inside the tunnel, with the sessions in emx's own colours, a `⋯` sheet, a "+ New" screen and a read-only routines list. Tural: *"it needs to be extremely secure"* and *"I don't want extra setup"*; the Claude app does the work in a session, the page starts and overviews.

The page can start, rename and close real sessions, so the question is who may reach it, not what it shows.

## Decision

- **Network: Tailscale (Decided 23).** The page binds this machine's Tailscale IPv4 and nothing else (loopback only in tests). emx never installs Tailscale or runs `tailscale up`, `serve` or `funnel`; with Tailscale missing, down or tagged, it says so once in the panel and in `emx phone`.
- **Identity: `tailscale whois` of every request's peer** (cached 60 s, a refusal 5 s). The peer is let in only when its node's user and its profile are the same Tailscale user as this node's own (`status --json` `Self.UserID`) and neither is tagged; anything else, any error and any timeout is a 403 with no data. There is no token and no login: the tunnel is the login. A peer with this machine's own Tailscale address, which whois cannot tell from another user of the machine, must also own the connection, read from `/proc/net/tcp` and `tcp6`; with no `/proc` it is refused.
- **Request checks, in order:** peer, `Host` (this node's IP, MagicDNS name or short name, with the port), method (GET and POST only), and for a POST `Origin` equal to `http://<Host>`, `Content-Type: application/json`, a `Content-Length` of at most 16 KiB and a JSON object body. Every response carries `Cache-Control: no-store`, a CSP of `'self'` only with no inline script or style, `nosniff` and no referrer; the server's logs and errors print nothing (the terminal is the panel's).
- **Folders:** the page lists and starts only in folders under `$HOME` or ones emx already knows (session, recent, favourite), by real path.
- **Served from inside the running workspace:** a thread of the primary panel (`PhoneService`), so it is up whenever his emx is and re-served after an update relaunch, with no new service or systemd unit ([0010](./0010-a-view-per-device.md) has the one primary panel). It writes `phone.json` (the address or the reason) in emx's state for `emx phone`, and the panel waits for a running request before a relaunch.
- **Actions go through emx's own paths:** the listing, `Closer` and `Renamer` with their checks; the page only asks. A stopped session's close also removes its dead pane and its `emx-a-<key>` session.
- **Standard library only:** `http.server` and `subprocess` calling the system's `tailscale` read-only (`status`, `whois`), the same kind of dependency as `ssh` in [0007](./0007-emx-reaches-a-server-over-ssh.md).

## Consequences

- The page works from his own devices on his tailnet and nothing else; another person sharing the machine on a tailnet is refused (assumed; a one-line change to allow).
- A revoked device keeps access up to 60 s.
- The server shares the panel's process, so heavy page traffic shares its GIL (a 3 s poll is light), and it dies with the panel, so an update or a crash cannot leave an orphan listening.
- Safari shows "Not Secure"; the tunnel encrypts. HTTPS needs `tailscale serve`, which is a change to the tailnet's configuration emx does not make.
- Remote Control must be on for a row to open the Claude app; a session waiting at Claude's trust question shows as needing input with a line saying so, and the page does not answer it.
- Two Tailscale tests read fixtures with invented ids; no test runs the real `tailscale`, so the refusal paths for another user are proven by fixtures only.

## Alternatives considered

| Option | What it buys | What it costs | Verdict |
|---|---|---|---|
| **Tailscale, bound to its IP, whois of the peer** (this record) | No setup beyond Tailscale; no secret to leak; only his devices | Needs Tailscale on both ends; trusts the tunnel and the local `tailscale` command | **Chosen** (Decided 23) |
| A WireGuard tunnel run by emx | No third party | emx would manage keys and the network; far more to get wrong | Rejected |
| An ssh port forward from the phone | Nothing new on the server | Needs an ssh app that forwards ports on the phone; the opposite of "no extra setup" | Rejected |
| A separate daemon or systemd unit | Survives the panel | A new service to install, update and secure; two things to keep in step | Rejected (Decided 27) |
| A token or password login | Works over any network | A secret to store, type and leak; more setup | Rejected (Decided 24) |
| `tailscale serve` for HTTPS | No "Not Secure" in Safari | Changes the tailnet's configuration; extra setup | Rejected for now |
