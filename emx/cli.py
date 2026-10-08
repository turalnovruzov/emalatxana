"""Entry points behind bin/emx: the launcher, the sidebar pane, and the inert blank panes."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import sys

from emx import core
from emx.tmux import ROLE, SESSION, VERSION_OPT, Tmux, TmuxError

HERE = os.path.dirname(os.path.realpath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin", "emx")
CONFIG = os.path.join(HERE, "emx.tmux.conf")

PLACEHOLDER_TEXT = "Pick a session on the panel, or press n there to start one."

USAGE = """usage: emx

Opens the emx workspace in a private tmux server, or reattaches if it is running,
first moving a running workspace onto this version of emx.
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
    side = core.load_prefs(core.prefs_path())["side"]
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
    tmux.set_session_option(VERSION_OPT, code_version())


def code_version() -> str:
    root = os.path.dirname(HERE)
    files = []
    for path in [BIN] + [os.path.join(d, f) for d, _, names in os.walk(HERE) for f in names]:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if core.counts_for_version(rel):
            with open(path, "rb") as fh:
                files.append((rel, fh.read()))
    return core.code_version(files)


def refresh_workspace(tmux: Tmux) -> None:
    """Bring a running workspace onto this code: a sidebar process keeps running the code it started with until respawned."""
    version = code_version()
    upgrade = core.needs_upgrade(tmux.session_option(VERSION_OPT), version)
    respawn, drop = core.relaunch_plan(tmux.panes(), upgrade)
    # Spacers go first: a sidebar takes any other pane in its window as the main pane.
    for pane in drop:
        tmux.kill_pane(pane)
    for pane in respawn:
        # Respawning kills a session start still in flight in the old sidebar; accepted, as that
        # window is a few seconds and the session itself is unaffected.
        tmux.respawn(pane, self_command("sidebar", tmux.socket))
    if upgrade:
        tmux.source_config()
        # Stamped last, so an upgrade that fails partway is retried on the next launch.
        tmux.set_session_option(VERSION_OPT, version)


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
        running = tmux.has_session()
        if not running:
            create_workspace(tmux)
    except (TmuxError, OSError) as exc:
        print(f"emx: {exc}", file=sys.stderr)
        return 1
    if running:
        # Best effort: a failed upgrade must not lock him out of a workspace that still runs.
        try:
            refresh_workspace(tmux)
        except (TmuxError, OSError) as exc:
            print(f"emx: couldn't update the running workspace: {' '.join(str(exc).split())}", file=sys.stderr)
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
