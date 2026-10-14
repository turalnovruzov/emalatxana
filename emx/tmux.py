"""Calls into emx's private tmux server. Every command carries `-L <socket>`, so his default server is never addressed."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass

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


_PANE_ID = re.compile(r"%\d+")
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
    ]
)


class Tmux:
    def __init__(self, socket: str, config: str | None = None):
        self.socket = socket
        self.config = config

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

    def panes(self) -> list:
        out = self.run("list-panes", "-s", "-t", f"={SESSION}:", "-F", _PANE_FORMAT)
        panes = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 11:
                continue
            pid, window, dead, role, session, width, window_width, active, back, zoomed, agent = parts
            panes.append(
                Pane(
                    pid, window, dead == "1", role, session, int(width or 0), int(window_width or 0), active == "1",
                    back == "1", zoomed == "1", agent,
                )
            )
        return panes

    def session_option(self, name: str) -> str:
        return self.run("show-options", "-qv", "-t", f"={SESSION}:", name).strip()

    def set_session_option(self, name: str, value: str) -> None:
        self.run("set-option", "-t", f"={SESSION}:", name, value)

    def source_config(self) -> None:
        self.run("source-file", self.config)

    def set_pane_option(self, pane: str, name: str, value: str) -> None:
        self.run("set-option", "-p", "-t", pane, name, value)

    def client_state(self, now: float | None = None) -> tuple:
        """(clients attached, last focus report "1" in, "0" out or "" unknown, the active pane's id, seconds since the client's last input or None when unknown)."""
        out = self.run(
            "display-message", "-p", "-t", f"={SESSION}:",
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

    def bind_collapse(self, key: str, main: str) -> None:
        """Bind `key` (no prefix) to zoom the main pane, which hides the panel; while zoomed the same command unzooms.

        The id is baked in like bind_toggle's, so the panel rebinds when
        main's id changes. Zooming also makes main the active pane, so from
        the panel the key lands in the session. If main is not in the window
        the key goes through to the pane instead of being swallowed.
        """
        main_here = f"#{{P:#{{?#{{==:#{{pane_id}},{main}}},1,}}}}"
        self.run("bind-key", "-n", key, "if-shell", "-F", main_here, f"resize-pane -Z -t {main}", f"send-keys {key}")

    def bind_toggle(self, key: str, sidebar: str, main: str) -> None:
        """Bind `key` (no prefix) to jump between the sidebar and the main pane.

        The ids are baked in rather than read from options at keypress time,
        because tmux does not format-expand every -t target; the sidebar
        rebinds after each switch, when the main pane's id changes. Only ←
        may move the panel's cursor, so on the way to the sidebar this drops
        any BACK_OPT mark a ← left behind before the panel could read it; the
        mark goes first, or the panel could see itself active and marked.
        Unsetting an option that is not set succeeds, so the drop never ends
        the command list before the select.
        """
        self.run(
            "bind-key", "-n", key,
            "if-shell", "-F", f"#{{==:#{{pane_id}},{sidebar}}}",
            f"select-pane -t {main}", f"set-option -p -u -t {sidebar} {BACK_OPT} ; select-pane -t {sidebar}",
        )

    def bind_left(self, sidebar: str, check: str) -> None:
        """Bind ← (no prefix) so that in a pane holding one of emx's sessions, on an empty prompt, it moves focus to the sidebar.

        Claude Code turns a ← it gets on an empty prompt into a fork of the
        conversation into its background service, so this binding fails
        closed: `check` is a shell command that exits 0 only when ← should
        reach the pane, and any other outcome, a crash or a check that can't
        run included, selects the sidebar. tmux appends the pane, cursor and
        width, so the check needs no tmux call to find them. It runs only in a
        pane tagged with a session and an agent, with the cursor shown and the
        sidebar still in the window: anywhere else ← goes straight to the
        pane, and a sidebar that is gone never swallows the key. The check,
        not a column fixed here, finds where the prompt is. The command is kept
        in an option, not in the binding, so its paths need no tmux quoting.
        Before selecting the sidebar the binding marks it with BACK_OPT, which
        tells the panel focus came back by ←.
        """
        self.run("set-option", "-g", LEFT_OPT, check)
        decide = (
            f'if-shell "#{{{LEFT_OPT}}} #{{pane_id}} #{{cursor_x}} #{{cursor_y}} #{{cursor_flag}} #{{pane_width}}" '
            f'"send-keys Left" "set-option -p -t {sidebar} {BACK_OPT} 1 ; select-pane -t {sidebar}"'
        )
        # A loop over the window's panes: tmux formats have no test for whether one pane exists.
        sidebar_here = f"#{{P:#{{?#{{==:#{{pane_id}},{sidebar}}},1,}}}}"
        held = (
            f"#{{&&:#{{{SESSION_OPT}}},#{{&&:#{{{AGENT_OPT}}},"
            f"#{{&&:#{{cursor_flag}},{sidebar_here}}}}}}}"
        )
        self.run("bind-key", "-n", "Left", "if-shell", "-F", held, decide, "send-keys Left")

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
