"""Entry points behind bin/emx: the launcher, the sidebar pane, and the inert blank panes."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import sys

from emx.core import load_prefs, prefs_path
from emx.tmux import ROLE, SESSION, Tmux, TmuxError

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin", "emx")
CONFIG = os.path.join(HERE, "emx.tmux.conf")

PLACEHOLDER_TEXT = "Pick a session on the panel, or press n there to start one."

USAGE = """usage: emx

Opens the emx workspace in a private tmux server, or reattaches if it is running.
Environment: EMX_SOCKET names the tmux socket (default "emx"); EMX_CLAUDE is the claude command (default "claude")."""


SOCKET_REFUSAL = "emx: the tmux socket must be a private name, not empty or \"default\"."


def private_socket(name: str) -> str | None:
    """None for an empty or "default" name: "default" is the socket of the user's default tmux server."""
    return None if name in ("", "default") else name


def socket_name() -> str | None:
    return private_socket(os.environ.get("EMX_SOCKET", "emx"))


def self_command(*args: str) -> str:
    return "exec " + " ".join(shlex.quote(a) for a in [sys.executable, BIN, *args])


def create_workspace(tmux: Tmux) -> None:
    cols, lines = shutil.get_terminal_size((200, 50))
    side = load_prefs(prefs_path())["side"]
    main = tmux.run(
        "new-session", "-d", "-s", SESSION, "-n", SESSION, "-x", str(cols), "-y", str(lines),
        "-P", "-F", "#{pane_id}", self_command("blank", PLACEHOLDER_TEXT),
    ).strip()
    tmux.set_pane_option(main, ROLE, "placeholder")
    toward_sidebar = [] if side == "right" else ["-b"]
    sidebar = tmux.run(
        "split-window", "-h", *toward_sidebar, "-t", main, "-P", "-F", "#{pane_id}",
        self_command("sidebar", tmux.socket),
    ).strip()
    tmux.set_pane_option(sidebar, ROLE, "sidebar")


def revive_sidebar(tmux: Tmux) -> None:
    for pane in tmux.panes():
        if pane.role == "sidebar" and pane.dead:
            tmux.respawn(pane.id, self_command("sidebar", tmux.socket))


def launch() -> int:
    if shutil.which("tmux") is None:
        print("emx needs tmux, and it is not on PATH.", file=sys.stderr)
        return 1
    socket = socket_name()
    if socket is None:
        print(SOCKET_REFUSAL, file=sys.stderr)
        return 2
    tmux = Tmux(socket, CONFIG)
    try:
        if tmux.has_session():
            revive_sidebar(tmux)
        else:
            create_workspace(tmux)
    except TmuxError as exc:
        print(f"emx: {exc}", file=sys.stderr)
        return 1
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    os.execvpe("tmux", tmux.argv("attach-session", "-t", f"={SESSION}"), env)
    return 1


def blank(message: str) -> int:
    """An inert pane: shows `message`, swallows any keys typed into it, never exits on its own."""
    import termios

    fd = sys.stdin.fileno()
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass

    def draw(*_):
        sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H")
        if message:
            sys.stdout.write("\r\n  " + message)
        sys.stdout.flush()

    signal.signal(signal.SIGWINCH, draw)
    draw()
    while True:
        try:
            if not os.read(fd, 1024):
                signal.pause()
        except InterruptedError:
            continue


def main(argv: list) -> int:
    if not argv:
        return launch()
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "sidebar" and len(argv) == 2:
        if private_socket(argv[1]) is None:
            print(SOCKET_REFUSAL, file=sys.stderr)
            return 2
        from emx.sidebar import run

        return run(argv[1])
    if argv[0] == "blank":
        return blank(argv[1] if len(argv) > 1 else "")
    print(USAGE, file=sys.stderr)
    return 2
