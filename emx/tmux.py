"""Calls into emx's private tmux server. Every command carries `-L <socket>`, so his default server is never addressed."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

SESSION = "emx"
ROLE = "@emx_role"
SESSION_OPT = "@emx_session"
VERSION_OPT = "@emx_version"
LEFT_OPT = "@emx_left"


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
            if len(parts) != 8:
                continue
            pid, window, dead, role, session, width, window_width, active = parts
            panes.append(
                Pane(pid, window, dead == "1", role, session, int(width or 0), int(window_width or 0), active == "1")
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

    def new_hidden_window(self, command: str) -> str:
        return self.run("new-window", "-d", "-t", f"={SESSION}:", "-P", "-F", "#{pane_id}", command).strip()

    def respawn(self, pane: str, command: str) -> None:
        self.run("respawn-pane", "-k", "-t", pane, command)

    def swap(self, source: str, target: str) -> None:
        self.run("swap-pane", "-d", "-s", source, "-t", target)

    def select(self, pane: str) -> None:
        self.run("select-pane", "-t", pane)

    def kill_pane(self, pane: str) -> None:
        self.run("kill-pane", "-t", pane)

    def resize_width(self, pane: str, width: int) -> None:
        self.run("resize-pane", "-t", pane, "-x", str(width))

    def bind_toggle(self, key: str, sidebar: str, main: str) -> None:
        """Bind `key` (no prefix) to jump between the sidebar and the main pane.

        The ids are baked in rather than read from options at keypress time,
        because tmux does not format-expand every -t target; the sidebar
        rebinds after each switch, when the main pane's id changes.
        """
        self.run(
            "bind-key", "-n", key,
            "if-shell", "-F", f"#{{==:#{{pane_id}},{sidebar}}}",
            f"select-pane -t {main}", f"select-pane -t {sidebar}",
        )

    def bind_left(self, sidebar: str, check: str, prompt_x: int) -> None:
        """Bind ← (no prefix) so that in a session viewer on an empty prompt it moves focus to the sidebar.

        `check` is a shell command that exits 0 for that case; tmux appends
        the pane, cursor and width, so the check needs no tmux call to find
        them. It runs only when the cursor is shown at `prompt_x`, where an
        empty prompt puts it: anywhere else ← goes straight to the pane, so
        moving through typed text never waits on a process. Whenever the check
        fails or can't run, ← reaches the pane as usual. The command is kept in
        an option, not in the binding, so its paths need no tmux quoting.
        """
        self.run("set-option", "-g", LEFT_OPT, check)
        decide = (
            f'if-shell "#{{{LEFT_OPT}}} #{{pane_id}} #{{cursor_x}} #{{cursor_y}} #{{cursor_flag}} #{{pane_width}}" '
            f'"select-pane -t {sidebar}" "send-keys Left"'
        )
        at_prompt = f"#{{&&:#{{{SESSION_OPT}}},#{{&&:#{{cursor_flag}},#{{==:#{{cursor_x}},{prompt_x}}}}}}}"
        self.run("bind-key", "-n", "Left", "if-shell", "-F", at_prompt, decide, "send-keys Left")

    def cursor_lines(self, pane: str, y: int) -> list:
        """The styled text of line `y` of the pane's screen and the line under it."""
        return self.run("capture-pane", "-p", "-e", "-t", pane, "-S", str(y), "-E", str(y + 1)).split("\n")[:2]

    def capture(self, pane: str) -> str:
        return self.run("capture-pane", "-p", "-t", pane)

    def detach(self) -> None:
        """Detach the client that sent the last key. Run from a pane, tmux picks the most recently active client on its session."""
        self.run("detach-client")
