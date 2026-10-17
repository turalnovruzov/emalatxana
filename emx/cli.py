"""Entry points behind bin/emx: the launcher, the sidebar pane, and the inert blank panes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

import emx
from emx import BIN, __version__, core, install, remote
from emx.notify import OPEN_ENV
from emx.tmux import HUSH_OPT, OPEN_OPT, ROLE, SESSION, TERMINAL_OPT, VERSION_OPT, Tmux, TmuxError

HERE = os.path.dirname(os.path.realpath(__file__))
CONFIG = os.path.join(HERE, "emx.tmux.conf")
CHANGELOG = os.path.join(os.path.dirname(HERE), "CHANGELOG.md")
DOCS = os.path.join(os.path.dirname(HERE), "user-docs")

PLACEHOLDER_TEXT = "Pick a session on the panel, or press n there to start one."
FALLBACK_TERMINAL = "com.apple.Terminal"
LAUNCH_FILE = "emx.command"
PENDING_FILE = "pending"
PENDING_MAX_AGE = 60

USAGE = """usage: emx [--version | docs [<page>] | hush | remote <host> | update [off | on] | uninstall]
(emx hook <state folder> <session key> is internal: the command a session's hooks run.
emx install <url> is internal too: what install.sh runs to install emx from that repo's newest version tag.
emx update --report is internal too: what `emx remote` runs on a server to update it within its major and read one result line.
emx relaunch <socket> is internal too: what a running panel starts to move its workspace onto the version just installed.
emx follow is internal too: it streams the notices of this emx to stdout, for a laptop's `emx remote` to read.
emx open <session id> is internal too: what a click on one of emx's notifications runs; it prints the bundle id
of the terminal emx runs in, for the click to bring forward, and with no emx window open it opens emx in a new
terminal window with that session.)

Opens the emx workspace in a private tmux server, or reattaches if it is running,
first moving a running workspace onto this version of emx. --version prints the version.
docs prints the emx docs index (start there); docs <page> prints one page.
hush stops the voice now (the sentence being spoken and any waiting behind it) without muting later notices.
remote <host> opens the emx running on that server over ssh (host from your SSH config), in this terminal; closing it leaves the server's sessions running.
update installs the newest version of an installed emx now, a new major version included; the running panel switches to it at a quiet moment.
update off stops the automatic updates (emx update still updates when you run it); update on starts them again.
uninstall removes the installed emx and its link; your prefs and emx's state are kept.
Environment: EMX_SOCKET names the tmux socket (default "emx"); EMX_CLAUDE is the claude command (default "claude").
EMX_SHOW_TEST_SESSIONS=1 shows the emx-test- throwaway sessions that are hidden otherwise, and keeps this emx silent; EMX_SAY replaces the `say` command.
EMX_CONFIG_HOME, for live checks, holds emx's prefs in place of XDG_CONFIG_HOME, which the sessions' claude reads too.
EMX_REMOTE_COMMAND is emx's command on the server (default emx, run through its login shell), EMX_SSH replaces ssh.
EMX_NOTIFY replaces the notification helper's command, EMX_NOTIFY_HOME the folder it is built in, EMX_OPEN the `open` command.
EMX_HOME is the folder emx installs into (default $XDG_DATA_HOME/emx, else ~/.local/share/emx), EMX_BIN_HOME the folder of the emx link
(default ~/.local/bin); EMX_UPDATE_SECONDS is how often a running panel checks for updates (default 600)."""


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


def docs_pages() -> list:
    try:
        return sorted(name[:-3] for name in os.listdir(DOCS) if name.endswith(".md"))
    except OSError:
        return []


def docs_command(args: list) -> int:
    if len(args) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    pages = docs_pages()
    page = "index"
    if args:
        page = args[0][:-3] if args[0].endswith(".md") else args[0]
    # Matched against the folder's own listing, never joined into a path, so no argument can leave DOCS.
    if page not in pages:
        print(f'emx: no docs page "{args[0] if args else page}"; pages: {", ".join(pages)}', file=sys.stderr)
        return 2
    try:
        with open(os.path.join(DOCS, page + ".md"), encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError):
        print(f'emx: cannot read the docs page "{page}"', file=sys.stderr)
        return 1
    try:
        # A stream that cannot encode the page's characters gets "?" for them rather than a traceback.
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(enc, "replace").decode(enc))
        sys.stdout.flush()
    except BrokenPipeError:
        # Point stdout at devnull so the interpreter's exit flush does not raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


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
    wanted = take_pending_open()
    if wanted:
        tmux.set_session_option(OPEN_OPT, wanted)


def code_version() -> str:
    root = os.path.dirname(HERE)
    files = []
    for path in [os.path.join(root, "bin", "emx")] + [os.path.join(d, f) for d, _, names in os.walk(HERE) for f in names]:
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
    record_terminal(tmux)
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


def record_terminal(tmux: Tmux) -> None:
    """Remember which terminal app this emx was started from, so a click on a notification can bring it forward. Best effort."""
    bundle = core.terminal_bundle(os.environ)
    if not bundle:
        return
    try:
        tmux.set_session_option(TERMINAL_OPT, bundle)
    except (TmuxError, OSError):
        pass


def open_folder():
    return core.state_path() / "open"


def take_pending_open() -> str | None:
    """The session id a notification click left for an emx it had to start, read once: the file is removed whatever it holds.

    One older than `PENDING_MAX_AGE` seconds is ignored: its launch never ran
    (he cancelled iTerm2's "OK to run"), and must not open that session days later.
    """
    path = open_folder() / PENDING_FILE
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(256)
            fresh = time.time() - os.fstat(fh.fileno()).st_mtime < PENDING_MAX_AGE
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return core.open_request(text) if fresh else None


def follow() -> int:
    """The server half of `emx remote`: streams the panel's notices to stdout until the pipe closes."""
    socket = socket_name()
    if socket is None:
        print(SOCKET_REFUSAL, file=sys.stderr)
        return 2
    return remote.follow(core.relay_path(core.state_path()), sys.stdout, Tmux(socket))


def open_command() -> str:
    return os.environ.get("EMX_OPEN") or "/usr/bin/open"


def open_session(sid: str, window: bool = True) -> int:
    """What a click on a notification runs: asks the running panel to open `sid` through an option on emx's own tmux server, then prints the bundle id of the terminal emx runs in. Starts nothing, and never touches another server.

    It never brings that terminal forward itself: macOS does not let a
    background process activate another app, so the helper, which the click
    made active, reads the printed id and hands activation on.
    """
    if os.environ.get("EMX_REMOTE_HOST"):
        return remote.open_remote(sid, os.environ)
    if not core.is_session_id(sid):
        print("emx: open needs a session id.", file=sys.stderr)
        return 2
    socket = socket_name()
    if socket is None:
        print(SOCKET_REFUSAL, file=sys.stderr)
        return 2
    if shutil.which("tmux") is None:
        print("emx needs tmux, and it is not on PATH.", file=sys.stderr)
        return 1
    tmux = Tmux(socket)
    if not tmux.has_session():
        if not window:
            print("emx isn't running.", file=sys.stderr)
            return 1
        return open_window("", sid)
    if not window:
        try:
            tmux.set_session_option(OPEN_OPT, sid)
        except TmuxError as exc:
            print(f"emx: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        tmux.set_session_option(OPEN_OPT, sid)
        attached, *_ = tmux.client_state()
        bundle = tmux.session_option(TERMINAL_OPT)
    except TmuxError as exc:
        print(f"emx: {exc}", file=sys.stderr)
        return 1
    if not attached:
        return open_window(bundle)
    if bundle:
        print(bundle)
    return 0


def _run_open(argv: list) -> bool:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_window(bundle: str, sid: str | None = None) -> int:
    """Open emx in a new window of the terminal `bundle` names (Terminal.app when none is known), through `<state>/open/emx.command`.

    Only Terminal and iTerm2 are known to run a `.command` file, so any other
    terminal's window is opened in Terminal.app.

    The file is one fixed path rewritten in place, so iTerm2's "OK to run"
    warning, once suppressed, stays suppressed. It holds only emx's command and
    the environment `OPEN_ENV` allows, never a session id: when emx is not
    running, `sid` goes in the `pending` file beside it, which
    `create_workspace` takes within `PENDING_MAX_AGE` seconds. Launch files of
    earlier builds are removed.
    """
    folder = open_folder()
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        for old in folder.glob("emx-*.command"):
            try:
                old.unlink()
            except FileNotFoundError:
                pass
        env = {k: os.environ[k] for k in OPEN_ENV if os.environ.get(k)}
        lines = ["#!/bin/sh"] + [f"export {k}={shlex.quote(v)}" for k, v in env.items()]
        lines.append(f"exec {shlex.quote(sys.executable)} {shlex.quote(BIN)}")
        path = folder / LAUNCH_FILE
        _replace_file(path, "\n".join(lines) + "\n", 0o700)
        if sid:
            _replace_file(folder / PENDING_FILE, sid + "\n", 0o600)
    except OSError as exc:
        print(f"emx: couldn't open a new window: {exc}", file=sys.stderr)
        return 1
    runner = bundle if bundle in core.TERM_PROGRAM_BUNDLES.values() else FALLBACK_TERMINAL
    if not _run_open([open_command(), "-b", runner, str(path)]):
        if sid:
            try:
                (folder / PENDING_FILE).unlink()
            except OSError:
                pass
        print("emx: couldn't open a new window.", file=sys.stderr)
        return 1
    return 0


def _replace_file(path, text: str, mode: int) -> None:
    """Write `text` to `path` atomically with `mode`, so a terminal never runs a half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
    """Exit 0 when ← in a session's pane should reach Claude Code, 1 when it should move focus to the panel. Prints nothing.

    `args` are the socket, then the pane, cursor x and y, cursor shown flag
    and pane width as tmux formats them. Any failure answers 1: Claude Code
    forks the conversation on a ← it gets at an empty prompt, so a broken
    check costs him ← in typed text, never a fork.
    """
    try:
        socket, pane, x, y, shown, width = args
        if private_socket(socket) is None:
            return 1
        line, below = (Tmux(socket).cursor_lines(pane, int(y)) + ["", ""])[:2]
        return 1 if core.empty_prompt(line, below, int(x), shown == "1", int(width)) else 0
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
        while sys.stdin.buffer.read(1 << 20):
            pass
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


CHECKOUT_UPDATE = "emx: this emx runs from a checkout ({}), which never updates itself; install it with install.sh to get updates."


def install_command(url: str) -> int:
    _, error = install.install(url, os.environ, sys.stdout)
    if error:
        print(f"emx: couldn't install: {error}", file=sys.stderr)
        return 1
    return 0


def update_command(args: list) -> int:
    root = emx.ROOT
    if args == ["--report"]:
        print(update_report(root))
        return 0
    if args not in ([], ["off"], ["on"]):
        print(USAGE, file=sys.stderr)
        return 2
    if root is None:
        print(CHECKOUT_UPDATE.format(emx.CODE), file=sys.stderr)
        return 1
    if args:
        on, error = install.set_updates(root, args == ["on"])
        if error:
            print(f"emx: {error}", file=sys.stderr)
            return 1
        print("Automatic updates are on." if on else "Automatic updates are off; emx update still updates when you run it.")
        return 0
    result, error = install.update(root, emx.RUNNING, allow_major=True)
    if error:
        print(f"emx: couldn't update: {error}", file=sys.stderr)
        return 1
    before, after, _ = result
    if after == before:
        print(f"emx is up to date ({after}).")
    else:
        # The panel's updater prunes too, but a machine may run no panel; one previous version stays either way, and
        # a panel still on the version before this process's own keeps its folder.
        install.prune(root, emx.RUNNING, keep_lagging=True)
        print(f"emx updated to {after}. The running panel switches to it at a quiet moment, or when you next run emx.")
    return 0


def update_report(root) -> str:
    """The one line a laptop's `emx remote` reads: an update within this major, unless updates are off here."""
    if root is None:
        return core.update_report("failed", __version__, "runs from a checkout", None)
    now = install.current(root)
    if now is None:
        return core.update_report("failed", __version__, "its install names no current version", None)
    if not install.updates_on(root):
        return core.update_report("off", now, now, None)
    result, error = install.update(root, emx.RUNNING, allow_major=False)
    if error:
        return core.update_report("failed", now, error, None)
    before, after, major = result
    return core.update_report("updated" if after != before else "current", before, after, major)


def uninstall_command() -> int:
    if emx.ROOT is None:
        print(f"emx: this emx runs from a checkout ({emx.CODE}); there is no installed emx to remove.", file=sys.stderr)
        return 1
    removed, error = install.uninstall(os.environ, emx.ROOT)
    if error:
        print(f"emx: {error}", file=sys.stderr)
        return 1
    for path in removed:
        print(f"Removed {path}")
    print("Your prefs and emx's state are kept.")
    return 0


def relaunch(socket: str) -> int:
    """Moves a running workspace onto this code from outside it: a panel can't respawn its own pane, the respawn would kill the process asking for it."""
    if private_socket(socket) is None:
        print(SOCKET_REFUSAL, file=sys.stderr)
        return 2
    tmux = Tmux(socket, CONFIG)
    try:
        if not tmux.has_session():
            return 1
        refresh_workspace(tmux)
    except (TmuxError, OSError):
        return 1
    return 0


def main(argv: list) -> int:
    if not argv:
        return launch()
    if argv[0] == "hook":
        return hook(argv[1:])
    if argv[0] == "docs":
        return docs_command(argv[1:])
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
    if argv[0] == "open" and len(argv) == 2:
        return open_session(argv[1])
    if argv[0] == "open" and len(argv) == 3 and argv[1] == "--no-window":
        return open_session(argv[2], window=False)
    if argv[0] == "remote" and len(argv) == 2:
        return remote.connect(argv[1])
    if argv[0] == "follow" and len(argv) == 1:
        return follow()
    if argv[0] == "install" and len(argv) == 2:
        return install_command(argv[1])
    if argv[0] == "update":
        return update_command(argv[1:])
    if argv[0] == "uninstall" and len(argv) == 1:
        return uninstall_command()
    if argv[0] == "relaunch" and len(argv) == 2:
        return relaunch(argv[1])
    if argv[0] == "left":
        return left_check(argv[1:])
    if argv[0] == "blank":
        return blank(argv[1] if len(argv) > 1 else "")
    print(USAGE, file=sys.stderr)
    return 2
