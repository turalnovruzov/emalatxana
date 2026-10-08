"""Calls into emx's private tmux server. Every command carries `-L <socket>`, so his default server is never addressed."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

SESSION = "emx"
ROLE = "@emx_role"
SESSION_OPT = "@emx_session"
VERSION_OPT = "@emx_version"


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


_PANE_FORMAT = "\t".join(
    [
        "#{pane_id}",
        "#{window_id}",
        "#{pane_dead}",
        "#{" + ROLE + "}",
        "#{" + SESSION_OPT + "}",
        "#{pane_width}",
        "#{window_width}",
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
            if len(parts) != 7:
                continue
            pid, window, dead, role, session, width, window_width = parts
            panes.append(
                Pane(pid, window, dead == "1", role, session, int(width or 0), int(window_width or 0))
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

    def capture(self, pane: str) -> str:
        return self.run("capture-pane", "-p", "-t", pane)

    def detach(self) -> None:
        """Detach the client that sent the last key. Run from a pane, tmux picks the most recently active client on its session."""
        self.run("detach-client")
