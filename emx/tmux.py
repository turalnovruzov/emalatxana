"""Calls into emx's private tmux server. Every command carries `-L <socket>`, so his default server is never addressed."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass

from emx import core

SESSION = "emx"
ROLE = "@emx_role"
SESSION_OPT = "@emx_session"
AGENT_OPT = "@emx_agent"
VERSION_OPT = "@emx_version"
LEFT_OPT = "@emx_left"
BACK_OPT = "@emx_back"
FOCUS_OPT = "@emx_focus"
HUSH_OPT = "@emx_hush"
OPEN_OPT = "@emx_open"
TERMINAL_OPT = "@emx_terminal"
RELAY_OPT = "@emx_relay"
RELAY_HOST_OPT = "@emx_relay_host"
PASTE_OPT = "@emx_paste"
RELAY_PASTE_OPT = "@emx_relay_paste"
# The session key a view's slot shows. Never SESSION_OPT: a pane with a key and no agent tag is a pre-2.0 viewer, which panels kill.
VIEW_OPT = "@emx_view"
REOPEN_OPT = "@emx_reopen"
AGENT_SESSION_PREFIX = "emx-a-"
VIEW_PREFIX = "emx-view-"
AGENT_KEY_ENV = "EMX_AGENT_KEY"


_PANE_ID = re.compile(r"%\d+")
_BUFFER_NAME = re.compile(r"[A-Za-z0-9_-]+")
KEYS_SENT = ("Escape",)


class TmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Pane:
    id: str
    window: str
    dead: bool
    role: str
    session: str
    width: int
    window_width: int
    active: bool = False
    back: bool = False
    zoomed: bool = False
    agent: str = ""
    pid: int = 0
    view: str = ""


@dataclass(frozen=True)
class View:
    """One view session (`emx` or an `emx-view-*`) as `Tmux.views` reads it: its clients, and what its slot shows."""

    name: str
    attached: int
    focus: str
    activity: float | None
    width: int
    shows: str
    slot_active: bool
    slot: str = ""


_PANE_FORMAT = "\t".join(
    [
        "#{pane_id}",
        "#{window_id}",
        "#{pane_dead}",
        "#{" + ROLE + "}",
        "#{" + SESSION_OPT + "}",
        "#{pane_width}",
        "#{window_width}",
        "#{pane_active}",
        "#{" + BACK_OPT + "}",
        "#{window_zoomed_flag}",
        "#{" + AGENT_OPT + "}",
        "#{pane_pid}",
        "#{" + VIEW_OPT + "}",
    ]
)

# True in a view's window, a panel and its slot: a loop over the window's panes, since tmux formats have no test for whether one pane exists.
VIEW_WINDOW = "#{&&:#{==:#{window_panes},2},#{P:#{?#{==:#{" + ROLE + "},sidebar},1,}}}"


def _parse_panes(out: str) -> list:
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 13:
            continue
        pid, window, dead, role, session, width, window_width, active, back, zoomed, agent, process, view = parts
        panes.append(
            Pane(
                pid, window, dead == "1", role, session, int(width or 0), int(window_width or 0), active == "1",
                back == "1", zoomed == "1", agent, int(process or 0), view,
            )
        )
    return panes


def agent_session(key: str) -> str:
    """The name of the tmux session that holds session `key`'s window for viewers; `key` must be a session id, as it goes into tmux targets."""
    if not core.is_session_id(key):
        raise TmuxError(f"not a session key: {key!r}")
    return AGENT_SESSION_PREFIX + key


def is_view_session(name: str) -> bool:
    return name == SESSION or (name.startswith(VIEW_PREFIX) and name[len(VIEW_PREFIX):].isdigit())


class Tmux:
    """`view` is the view session this caller draws for: the bar, the client state and the view options go to it; session options stay on `emx`."""

    def __init__(self, socket: str, config: str | None = None, view: str = SESSION):
        self.socket = socket
        self.config = config
        self.view = view

    def argv(self, *args: str) -> list:
        base = ["tmux", "-L", self.socket]
        if self.config:
            base += ["-f", self.config]
        return base + list(args)

    def run(self, *args: str) -> str:
        try:
            done = subprocess.run(
                self.argv(*args), capture_output=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxError(f"tmux failed: {exc}") from None
        if done.returncode != 0:
            raise TmuxError(done.stderr.strip() or f"tmux {args[0]} exited {done.returncode}")
        return done.stdout

    def has_session(self) -> bool:
        try:
            self.run("has-session", "-t", f"={SESSION}:")
            return True
        except TmuxError:
            return False

    def panes(self, session: str = SESSION) -> list:
        return _parse_panes(self.run("list-panes", "-s", "-t", f"={session}:", "-F", _PANE_FORMAT))

    def window_panes(self, pane: str) -> list:
        """The panes of the window `pane` is in."""
        return _parse_panes(self.run("list-panes", "-t", pane, "-F", _PANE_FORMAT))

    def view_names(self) -> list:
        """`emx` and every `emx-view-*` session on the server."""
        out = self.run("list-sessions", "-F", "#{session_name}")
        return [name for name in (line.strip() for line in out.splitlines()) if is_view_session(name)]

    def views(self) -> list:
        """Every view session as a `View`: its client count, last focus report, its most recent client's activity and width, and its slot."""
        sessions = self.run("list-sessions", "-F", "#{session_name}\t#{session_attached}\t#{" + FOCUS_OPT + "}")
        clients = self.run("list-clients", "-F", "#{client_session}\t#{client_activity}\t#{client_width}")
        panes = self.run(
            "list-panes", "-a", "-F",
            "#{session_name}\t#{window_id}\t#{pane_id}\t#{" + ROLE + "}\t#{" + VIEW_OPT + "}\t#{pane_active}",
        )
        latest = {}
        for line in clients.splitlines():
            name, activity, width = (line.split("\t") + ["", "", ""])[:3]
            try:
                at, cols = float(activity), int(width or 0)
            except ValueError:
                continue
            if name not in latest or at > latest[name][0]:
                latest[name] = (at, cols)
        rows = [(line.split("\t") + [""] * 6)[:6] for line in panes.splitlines()]
        views = []
        for line in sessions.splitlines():
            name, attached, focus = (line.split("\t") + ["", "", ""])[:3]
            if not is_view_session(name):
                continue
            mine = [r for r in rows if r[0] == name]
            panel_windows = {r[1] for r in mine if r[3] == "sidebar"}
            slot = next((r for r in mine if r[1] in panel_windows and r[3] != "sidebar"), None)
            at, cols = latest.get(name, (None, 0))
            views.append(View(
                name, int(attached) if attached.isdigit() else 0, focus, at, cols,
                slot[4] if slot else "", bool(slot) and slot[5] == "1", slot[2] if slot else "",
            ))
        return views

    def view_option(self, name: str) -> str:
        return self.run("show-options", "-qv", "-t", f"={self.view}:", name).strip()

    def set_view_option(self, name: str, value: str) -> None:
        self.run("set-option", "-t", f"={self.view}:", name, value)

    def global_option(self, name: str) -> str:
        return self.run("show-options", "-gqv", name).strip()

    def set_global_option(self, name: str, value: str) -> None:
        self.run("set-option", "-g", name, value)

    def has_agent_session(self, key: str) -> bool:
        try:
            self.run("has-session", "-t", f"={agent_session(key)}:")
            return True
        except TmuxError:
            return False

    def link_agent(self, window: str, key: str, inert: str) -> None:
        """Link the agent's window into its own session `emx-a-<key>`, which viewers attach to; the pane stays where it is in `emx`, with its id and process.

        Does nothing when the window is already linked there. The session is
        made with the `inert` command as window 0, which `link-window -k`
        replaces in one step. It shows no status line, and a viewer of it
        exits when it goes (detach-on-destroy) rather than being switched onto
        another session, a view among them. AGENT_KEY_ENV in its environment
        tells `emx copied` which session a copy made in a viewer came from.
        """
        name = agent_session(key)
        target = f"={name}:"
        try:
            windows = self.run("list-windows", "-t", target, "-F", "#{window_id}").split()
        except TmuxError:
            windows = None
        if windows is not None and window in windows:
            return
        if windows is None:
            try:
                self.run("new-session", "-d", "-s", name, inert)
            except TmuxError:
                # Another panel made it first.
                if not self.has_agent_session(key):
                    raise
        self.run("link-window", "-k", "-s", window, "-t", f"={name}:0")
        self.run("set-option", "-t", target, "status", "off")
        self.run("set-option", "-t", target, "detach-on-destroy", "on")
        self.run("set-environment", "-t", target, AGENT_KEY_ENV, key)

    def hand_on_copy(self, key: str, data: bytes) -> int:
        """Send a copy made in a viewer of session `key` to the clipboard of each terminal showing that viewer; the number of terminals it went to.

        The viewer that made it is the one with the latest input among those
        attached to `emx-a-<key>`: the copy itself is input.
        """
        target = f"={agent_session(key)}:"
        inner = []
        for line in self.run("list-clients", "-t", target, "-F", "#{client_activity}\t#{client_tty}").splitlines():
            at, _, tty = line.partition("\t")
            try:
                inner.append((float(at), tty))
            except ValueError:
                continue
        if not inner:
            return 0
        tty = max(inner)[1]
        views = set()
        for line in self.run("list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}").splitlines():
            pane_tty, _, name = line.partition("\t")
            if pane_tty == tty and is_view_session(name):
                views.add(name)
        clients = [c for name in sorted(views) for c in self.run("list-clients", "-t", f"={name}:", "-F", "#{client_name}").split()]
        if not clients:
            return 0
        names = self.run("list-buffers", "-F", "#{buffer_name}").split()
        copy = names[0] if names and self._buffer(names[0]) == data else None
        for client in clients:
            argv = self.argv("load-buffer", "-w", "-t", client, "-")
            try:
                done = subprocess.run(argv, input=data, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TmuxError(f"tmux failed: {exc}") from None
            if done.returncode != 0:
                raise TmuxError(done.stderr.decode("utf-8", "replace").strip() or "tmux load-buffer failed")
        # Sending a buffer that has a name would make it a named one, which a plain paste skips; so each send made a new
        # automatic buffer, and all but the newest of them, and the copy's own, are dropped: one buffer holds the copy, as before.
        added = self.run("list-buffers", "-F", "#{buffer_name}").split()[1:len(clients)]
        for name in added + ([copy] if copy else []):
            self.run("delete-buffer", "-b", name)
        return len(clients)

    def _buffer(self, name: str) -> bytes | None:
        try:
            done = subprocess.run(self.argv("show-buffer", "-b", name), capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout if done.returncode == 0 else None

    def pane_view(self, pane: str) -> str:
        """The session key the slot `pane` shows, "" when none."""
        if not _PANE_ID.fullmatch(pane):
            raise TmuxError(f"not a pane id: {pane!r}")
        return self.run("display-message", "-p", "-t", pane, "#{" + VIEW_OPT + "}").strip()

    def viewer_command(self, key: str) -> str:
        """The command a slot runs to show session `key`: a client of this same server attached to `emx-a-<key>`, so the agent's pane never moves."""
        target = f"={agent_session(key)}:"
        return f"exec env -u TMUX tmux -L {shlex.quote(self.socket)} attach-session -t {shlex.quote(target)}"

    def session_option(self, name: str) -> str:
        return self.run("show-options", "-qv", "-t", f"={SESSION}:", name).strip()

    def set_session_option(self, name: str, value: str) -> None:
        self.run("set-option", "-t", f"={SESSION}:", name, value)

    def clear_session_option_if(self, name: str, value: str, session: str = SESSION) -> None:
        """Empty the option only while it still holds `value`, decided on the tmux server so a write in between survives. `value` must not hold quotes or format characters (a session id does not)."""
        self.run("if-shell", "-F", "-t", f"={session}:", "#{==:#{" + name + "}," + value + "}", f'set-option -t ={session}: {name} ""')

    def source_config(self) -> None:
        self.run("source-file", self.config)

    def set_bar(self, text: str) -> None:
        self.run("set-option", "-t", f"={self.view}:", "status-format[0]", text)

    def set_pane_option(self, pane: str, name: str, value: str) -> None:
        self.run("set-option", "-p", "-t", pane, name, value)

    def unset_pane_option(self, pane: str, name: str) -> None:
        self.run("set-option", "-p", "-u", "-t", pane, name)

    def client_state(self, now: float | None = None) -> tuple:
        """(clients attached, last focus report "1" in, "0" out or "" unknown, the active pane's id, seconds since the client's last input or None when unknown)."""
        out = self.run(
            "display-message", "-p", "-t", f"={self.view}:",
            "#{session_attached}\t#{" + FOCUS_OPT + "}\t#{pane_id}\t#{client_activity}",
        )
        attached, focus, active, activity = (out.rstrip("\n").split("\t") + ["", "", "", ""])[:4]
        try:
            count = int(attached or 0)
        except ValueError:
            raise TmuxError(f"tmux gave an unreadable client state: {out!r}") from None
        try:
            idle = max(0.0, (time.time() if now is None else now) - float(activity))
        except ValueError:
            idle = None
        return count, focus, active, idle

    def client_ttys(self) -> list:
        """The tty of each client attached to this view."""
        out = self.run("list-clients", "-t", f"={self.view}:", "-F", "#{client_tty}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def new_hidden_window(self, command: str, cwd: str | None = None) -> str:
        where = ["-c", cwd] if cwd else []
        return self.run("new-window", "-d", "-t", f"={SESSION}:", *where, "-P", "-F", "#{pane_id}", command).strip()

    def respawn(self, pane: str, command: str, cwd: str | None = None) -> None:
        where = ["-c", cwd] if cwd else []
        self.run("respawn-pane", "-k", "-t", pane, *where, command)

    def swap(self, source: str, target: str) -> None:
        self.run("swap-pane", "-d", "-s", source, "-t", target)

    def select(self, pane: str) -> None:
        self.run("select-pane", "-t", pane)

    def kill_pane(self, pane: str) -> None:
        self.run("kill-pane", "-t", pane)

    def resize_width(self, pane: str, width: int) -> None:
        self.run("resize-pane", "-t", pane, "-x", str(width))

    def toggle_zoom(self, pane: str) -> None:
        self.run("resize-pane", "-Z", "-t", pane)

    def bind_collapse(self, key: str) -> None:
        """Bind `key` (no prefix) to zoom the slot of the view the key is pressed in, which hides its panel; while zoomed the same command unzooms.

        Relative to the pressing client's own window (`:.+` is the other pane
        of a view's two), so each view hides its own panel. Zooming also makes
        the slot the active pane, so from the panel the key lands in the
        session. Outside a view window the key goes through to the pane: in a
        viewer the outer level has already taken it.
        """
        here = f'if-shell -F "#{{==:#{{{ROLE}}},sidebar}}" "resize-pane -Z -t :.+" "resize-pane -Z"'
        self.run("bind-key", "-n", key, "if-shell", "-F", VIEW_WINDOW, here, f"send-keys {key}")

    def bind_toggle(self, key: str) -> None:
        """Bind `key` (no prefix) to jump between the panel and the slot of the view the key is pressed in.

        Only ← may move the panel's cursor, so on the way to the panel this
        drops any BACK_OPT mark a ← left behind before the panel could read
        it; the mark goes first, or the panel could see itself active and
        marked. Unsetting an option that is not set succeeds, so the drop never
        ends the command list before the select.
        """
        here = (
            f'if-shell -F "#{{==:#{{{ROLE}}},sidebar}}" "select-pane -t :.+" '
            f'"set-option -p -u -t :.+ {BACK_OPT} ; select-pane -t :.+"'
        )
        self.run("bind-key", "-n", key, "if-shell", "-F", VIEW_WINDOW, here, f"send-keys {key}")

    def bind_left(self, check: str) -> None:
        """Bind ← (no prefix) so that in a view's slot showing one of emx's sessions, on an empty prompt, it moves focus to that view's panel.

        Claude Code turns a ← it gets on an empty prompt into a fork of the
        conversation into its background service, so this binding fails
        closed: `check` is a shell command that exits 0 only when ← should
        reach the session, and any other outcome, a crash or a check that
        can't run included, selects the panel. tmux appends the slot, its
        cursor and width; the check finds the session's own pane from the
        slot. It runs only in a slot with VIEW_OPT, the cursor shown and the
        panel still in the window: anywhere else ← goes straight to the pane,
        so inside a viewer, whose window holds no panel, it reaches Claude.
        The command is kept in an option, not in the binding, so its paths
        need no tmux quoting. Before selecting the panel the binding marks it
        with BACK_OPT, which tells the panel focus came back by ←.
        """
        self.run("set-option", "-g", LEFT_OPT, check)
        decide = (
            f'if-shell "#{{{LEFT_OPT}}} #{{pane_id}} #{{cursor_x}} #{{cursor_y}} #{{cursor_flag}} #{{pane_width}}" '
            f'"send-keys Left" "set-option -p -t :.+ {BACK_OPT} 1 ; select-pane -t :.+"'
        )
        held = f"#{{&&:#{{{VIEW_OPT}}},#{{&&:#{{cursor_flag}},{VIEW_WINDOW}}}}}"
        self.run("bind-key", "-n", "Left", "if-shell", "-F", held, decide, "send-keys Left")

    def bind_paste(self, key: str, check: str) -> None:
        """Bind `key` (no prefix) so that in a pane holding one of emx's sessions, while a laptop on `emx remote` is listening, it asks the laptop for its clipboard image instead of reaching the pane.

        `check` is a shell command that exits 0 only after it asked the laptop;
        the key then goes nowhere, since the image's path arrives on its own.
        This binding fails open, the opposite of bind_left's: any other
        outcome, a crash or a check that can't run included, sends the key to
        the pane as it would go without emx. The listening stamp is read in the
        format gate, so on a machine with no laptop no process starts per press.
        Pressed in a view, the slot has no session tag, so the key passes to the
        viewer and the gate runs again there, on the session's own pane; the
        stamp is a global option, so it is seen from the viewer's session too.
        """
        self.run("set-option", "-g", PASTE_OPT, check)
        decide = f'if-shell "#{{{PASTE_OPT}}} #{{pane_id}} #{{{SESSION_OPT}}}" "" "send-keys {key}"'
        held = f"#{{&&:#{{{SESSION_OPT}}},#{{&&:#{{{AGENT_OPT}}},#{{{RELAY_PASTE_OPT}}}}}}}"
        self.run("bind-key", "-n", key, "if-shell", "-F", held, decide, f"send-keys {key}")

    def bind_copy(self, command: str) -> None:
        """Have every copy made in copy mode also run `command` with the copied text on its stdin.

        A copy made inside a viewer is sent as the clipboard escape to the
        viewer, not to his terminal, and the server ignores a pane's clipboard
        escapes (`set-clipboard external`), so `command` hands it on to the
        terminal showing that viewer. tmux runs it for every copy, so it must
        do nothing for a copy made outside a viewer.
        """
        self.run("set-option", "-s", "copy-command", command)

    def holds_session(self, pane: str, sid: str) -> bool:
        """True when the pane exists and holds session `sid` with an agent in it; `sid` is a checked session id."""
        if not _PANE_ID.fullmatch(pane):
            return False
        try:
            out = self.run("display-message", "-p", "-t", pane, f"#{{&&:#{{==:#{{{SESSION_OPT}}},{sid}}},#{{{AGENT_OPT}}}}}")
        except TmuxError:
            return False
        return out.strip() == "1"

    def paste_if(self, pane: str, sid: str, name: str, text: str) -> bool:
        """Paste `text`, bracketed when the program asked for it, into the pane only while it still holds session `sid` and is not in a mode; True when it pasted.

        The text goes into a named buffer as an argument, so tmux never parses
        it, and the check and the paste are one tmux command, so a session
        switch between them cannot land the text in another session. `sid` is
        a checked session id: no quote or `#` reaches the format.
        """
        if not _PANE_ID.fullmatch(pane):
            raise TmuxError(f"not a pane id: {pane!r}")
        if not _BUFFER_NAME.fullmatch(name):
            raise TmuxError(f"not a buffer name: {name!r}")
        self.run("set-buffer", "-b", name, "--", text)
        gate = f"#{{&&:#{{==:#{{{SESSION_OPT}}},{sid}}},#{{&&:#{{{AGENT_OPT}}},#{{?pane_in_mode,,1}}}}}}"
        out = self.run(
            "if-shell", "-F", "-t", pane, gate,
            f"paste-buffer -p -d -b {name} -t {pane} ; display-message -p pasted",
            f"delete-buffer -b {name} ; display-message -p skipped",
        )
        return out.strip() == "pasted"

    def show_message(self, pane: str, text: str, ms: int = 3000) -> None:
        self.run("display-message", "-d", str(ms), "-t", pane, text)

    def clear_back(self, pane: str) -> None:
        self.run("set-option", "-p", "-u", "-t", pane, BACK_OPT)

    def cursor_lines(self, pane: str, y: int) -> list:
        """The styled text of line `y` of the pane's screen and the line under it."""
        return self.run("capture-pane", "-p", "-e", "-t", pane, "-S", str(y), "-E", str(y + 1)).split("\n")[:2]

    def capture(self, pane: str) -> str:
        return self.run("capture-pane", "-p", "-t", pane)

    def prompt_probe(self, pane: str) -> tuple:
        """(cursor x, cursor y, cursor shown, pane width, in a mode) for the pane."""
        out = self.run(
            "display-message", "-p", "-t", pane, "#{cursor_x}\t#{cursor_y}\t#{cursor_flag}\t#{pane_width}\t#{pane_in_mode}",
        )
        x, y, shown, width, in_mode = out.strip().split("\t")
        return int(x), int(y), shown == "1", int(width), in_mode not in ("", "0")

    def type_if(self, pane: str, gate: str, text: str, typed: str, report: str, enter: bool = True) -> str:
        """Type `text` into the pane, and Enter after it unless `enter` is False, only while the format `gate` is true there, all in one tmux command, so no other key lands between the check and the typing.

        Prints back `typed` when it typed, else `report` expanded on the
        pane. The text goes as hex bytes (`-H`): tmux reads an argument
        ending in `;` as a command separator and drops the `;`, even with `-l`.
        """
        if not _PANE_ID.fullmatch(pane):
            raise TmuxError(f"not a pane id: {pane!r}")
        keys = " ".join(format(b, "02x") for b in text.encode("utf-8"))
        send = f"send-keys -t {pane} -H {keys} ; "
        if enter:
            send += f"send-keys -t {pane} Enter ; "
        send += f"display-message -p {typed}"
        return self.run("if-shell", "-F", "-t", pane, gate, send, f'display-message -p -t {pane} "{report}"').strip()

    def submit_if(self, pane: str, gate: str, typed: str, report: str) -> str:
        """Press Enter in the pane only while the format `gate` is true there, in one tmux command as `type_if` does. For the line `type_if` typed without it."""
        return self._key_if(pane, gate, "Enter", typed, report)

    def clear_if(self, pane: str, gate: str, typed: str, report: str) -> str:
        """Press Ctrl+U, which empties Claude Code's input box, only while the format `gate` is true there. For taking back a line `type_if` typed."""
        return self._key_if(pane, gate, "C-u", typed, report)

    def key_if(self, pane: str, gate: str, key: str, typed: str, report: str) -> str:
        """Send the one named `key`, with no Enter, into the pane only while the format `gate` is true there, in one tmux command as `type_if` does.

        Only keys in KEYS_SENT may go: a key name is read by tmux, so any other
        could name a binding or a sequence nobody checked.
        """
        if key not in KEYS_SENT:
            raise TmuxError(f"not a key emx sends: {key!r}")
        return self._key_if(pane, gate, key, typed, report)

    def _key_if(self, pane: str, gate: str, key: str, typed: str, report: str) -> str:
        if not _PANE_ID.fullmatch(pane):
            raise TmuxError(f"not a pane id: {pane!r}")
        send = f"send-keys -t {pane} {key} ; display-message -p {typed}"
        return self.run("if-shell", "-F", "-t", pane, gate, send, f'display-message -p -t {pane} "{report}"').strip()

    def detach(self) -> None:
        """Detach the client that sent the last key. Run from a pane, tmux picks the most recently active client on its session."""
        self.run("detach-client")
