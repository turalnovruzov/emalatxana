"""Entry points behind bin/emx: the launcher, the sidebar pane, and the inert blank panes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sys
import time

from emx import BIN, __version__, core
from emx.tmux import HUSH_OPT, ROLE, SESSION, VERSION_OPT, Tmux, TmuxError

HERE = os.path.dirname(os.path.realpath(__file__))
CONFIG = os.path.join(HERE, "emx.tmux.conf")
CHANGELOG = os.path.join(os.path.dirname(HERE), "CHANGELOG.md")

PLACEHOLDER_TEXT = "Pick a session on the panel, or press n there to start one."

USAGE = """usage: emx [--version | hush]
(emx hook <state folder> <session key> is internal: the command a session's hooks run.)

Opens the emx workspace in a private tmux server, or reattaches if it is running,
first moving a running workspace onto this version of emx. --version prints the version.
hush stops the voice now (the sentence being spoken and any waiting behind it) without muting later notices.
Environment: EMX_SOCKET names the tmux socket (default "emx"); EMX_CLAUDE is the claude command (default "claude").
EMX_SHOW_TEST_SESSIONS=1 shows the emx-test- throwaway sessions that are hidden otherwise, and keeps this emx silent; EMX_SAY replaces the `say` command."""


SOCKET_REFUSAL = "emx: the tmux socket must be a private name, not empty or \"default\"."


def private_socket(name: str) -> str | None:
    """None for an empty or "default" name: "default" is the socket of the user's default tmux server."""
    return None if name in ("", "default") else name


def read_changelog() -> str:
    try:
        with open(CHANGELOG, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError):
        return ""


def socket_name() -> str | None:
    return private_socket(os.environ.get("EMX_SOCKET", "emx"))


def self_command(*args: str) -> str:
    return "exec " + " ".join(shlex.quote(a) for a in [sys.executable, BIN, *args])


def create_workspace(tmux: Tmux) -> None:
    cols, lines = shutil.get_terminal_size((200, 50))
    prefs = core.load_prefs(core.prefs_path())
    side = prefs["side"]
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
    if core.panel_collapsed(prefs):
        tmux.toggle_zoom(main)
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
    os.execvpe("tmux", tmux.argv("attach-session", "-t", f"={SESSION}:"), env)
    return 1


def hush() -> int:
    """Asks the running panel to stop speaking, through an option on emx's own tmux server. Starts nothing, and never touches another server."""
    socket = socket_name()
    if socket is None:
        print(SOCKET_REFUSAL, file=sys.stderr)
        return 2
    if shutil.which("tmux") is None:
        print("emx needs tmux, and it is not on PATH.", file=sys.stderr)
        return 1
    tmux = Tmux(socket)
    if not tmux.has_session():
        print("emx isn't running.", file=sys.stderr)
        return 1
    try:
        tmux.set_session_option(HUSH_OPT, str(time.time_ns()))
    except TmuxError as exc:
        print(f"emx: {exc}", file=sys.stderr)
        return 1
    return 0


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


def left_check(args: list) -> int:
    """Exit 0 when ← in this viewer should move focus to the panel, 1 when it should reach Claude Code. Prints nothing.

    `args` are the socket, then the pane, cursor x and y, cursor shown flag
    and pane width as tmux formats them. Any failure answers 1, so a broken
    check costs him the shortcut, never a keypress.
    """
    try:
        socket, pane, x, y, shown, width = args
        if private_socket(socket) is None:
            return 1
        line, below = (Tmux(socket).cursor_lines(pane, int(y)) + ["", ""])[:2]
        return 0 if core.empty_prompt(line, below, int(x), shown == "1", int(width)) else 1
    except Exception:
        return 1


HOOK_STDIN_BYTES = 32 * 1024 * 1024


def hook(args: list) -> int:
    """Append what a session's hook was told to `<state>/sessions/<key>/events.jsonl`. Always returns 0 and prints nothing.

    Claude Code reads a hook's stdout as context or as a decision and exit 2
    as a block, so this must never print, fail or hang, whatever it is given.
    Only a key that is a session id and already has a record is written to.
    """
    try:
        root, key = args
        folder = os.path.join(root, "sessions", key)
        if not os.path.isabs(root) or not core.is_session_id(key) or not os.path.isfile(os.path.join(folder, "session.json")):
            return 0
        text = sys.stdin.buffer.read(HOOK_STDIN_BYTES).decode("utf-8", errors="replace")
        record = core.hook_record(text, time.time() * 1000)
        if record is None:
            return 0
        fd = os.open(os.path.join(folder, "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except BaseException:
        pass
    return 0


def main(argv: list) -> int:
    if not argv:
        return launch()
    if argv[0] == "hook":
        return hook(argv[1:])
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] == "--version":
        print(f"emx {__version__}")
        return 0
    if argv[0] == "sidebar" and len(argv) == 2:
        if private_socket(argv[1]) is None:
            print(SOCKET_REFUSAL, file=sys.stderr)
            return 2
        from emx.sidebar import run

        return run(argv[1])
    if argv[0] == "hush" and len(argv) == 1:
        return hush()
    if argv[0] == "left":
        return left_check(argv[1:])
    if argv[0] == "blank":
        return blank(argv[1] if len(argv) > 1 else "")
    print(USAGE, file=sys.stderr)
    return 2
