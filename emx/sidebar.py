"""The live sessions panel: a curses app running in emx's sidebar pane."""

from __future__ import annotations

import curses
import locale
import os
import subprocess
import textwrap
import threading
import time

from emx import core
from emx.cli import self_command
from emx.tmux import ROLE, SESSION_OPT, Pane, Tmux, TmuxError

POLL_SECONDS = 1.5
HOUSEKEEP_SECONDS = 0.5
FRESH_GRACE_SECONDS = 60
LISTED_WAIT_SECONDS = 5
LISTED_CHECK_SECONDS = 0.5
GIT_SECONDS = 5
LISTING_FRESH_SECONDS = 5
MAX_LISTINGS = 4
TRANSCRIPT_SECONDS = 3
BACK_KEY = "C-\\"
RESTORED_NOTICE_SECONDS = 4
FOOTERS = (
    "→ open  ← back  ␣ peek  n new  ^f find  ^t pin  ^x close  ^s view  ? keys",
    "→ open ← back ␣ peek n new ^f find ^t pin ^x close ^s view ? keys",
    "→ open ← back n new ^f find ^t pin ^x close ^s view ? keys",
    "→ open ← back ␣ peek n new ^x close ^s view ? keys",
    "→ open  ← back  n new  ^x close  ^s view  ? keys",
    "→ open ← back n new ^x close ^s view ? keys",
    "→ open ← back n new ^x close ? keys",
    "→ open ← back ? keys",
    "→ open ? keys",
    "? keys",
)
HELP = (
    ("→ ↵", "open the session and go to it"),
    ("←", "back here, from an empty prompt"),
    ("↑↓ j k", "move"),
    ("↵ click", "fold a group; a click opens a session"),
    ("space", "peek at what the session asks or did; ↑↓ next, space or Esc close"),
    ("n", "new session where the cursor is; Tab there picks another folder"),
    ("^x ^x", "stop the session; on a stopped one, remove it"),
    ("^f /", "find a session by name or folder"),
    ("^t", "pin to the top (in emx; doesn't keep it running)"),
    ("^s", "by project / by state"),
    ("r", "restart the main viewer"),
    ("s", "panel to the other side"),
    ("< >", "narrower / wider panel"),
    ("^\\", "jump to main and back"),
    ("q", "detach; sessions keep running"),
    ("?", "close this help"),
)
HELP_KEY_WIDTH = 9
ENTER_KEYS = ("\n", "\r", curses.KEY_ENTER)
FOCUS_REPORTS_ON, FOCUS_REPORTS_OFF = "\x1b[?1004h", "\x1b[?1004l"
BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)
CTRL_S = "\x13"
CTRL_X = "\x18"
CTRL_F = "\x06"
CTRL_T = "\x14"
CONFIRM_FOOTER = "ctrl+x to confirm"
FIND_HINT = "↑↓ move · ↵ open · Esc clear"
PEEK_FOOTERS = ("␣ Esc close  ↑↓ next  → open", "␣ close ↑↓ next", "␣ close")
PEEK_KEYS = (" ", "\x1b", curses.KEY_UP, curses.KEY_DOWN, "k", "j")
PEEK_TONES = {"body": "plain", "code": "accent", "dim": "muted"}
PEEK_NO_ROOM = "Too short to peek."
CLOSE_SECONDS = 30
FRAME_SECONDS = 0.15
KEY_WAIT_MS = 200
MAX_QUEUED_KEYS = 64

# (256-colour index, basic colour) for each tone; the basic one is used when the terminal has fewer than 256 colours.
TONES = {
    "muted": (245, curses.COLOR_WHITE),
    "needs": (221, curses.COLOR_YELLOW),
    "done": (114, curses.COLOR_GREEN),
    "stopped": (241, curses.COLOR_WHITE),
    "accent": (75, curses.COLOR_CYAN),
    "path": (244, curses.COLOR_WHITE),
    "error": (203, curses.COLOR_RED),
    "plain": (-1, -1),
}
BAR_BG = {"focused": 24, "unfocused": 236}
CATEGORY_TONE = {core.NEEDS_INPUT: "needs", core.WORKING: "plain", core.DONE: "done", core.STOPPED: "stopped"}


def list_rows(claude: str) -> tuple:
    """(rows of `claude agents --json --all`, None) on success, (None, one-line error) on any failure. Never raises.

    `--all` keeps stopped and completed sessions listed, as `claude agents` shows them, until he removes them.
    """
    try:
        done = subprocess.run(
            [claude, "agents", "--json", "--all"], capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=15,
        )
    except FileNotFoundError:
        return None, f"can't find the claude command ({claude})"
    except subprocess.TimeoutExpired:
        return None, "claude agents --json timed out"
    except OSError as exc:
        return None, f"can't run claude: {exc}"
    if done.returncode != 0:
        return None, core.first_message(done.stderr) or f"claude agents exited with {done.returncode}"
    try:
        return core.parse_agents(done.stdout), None
    except core.AgentsError as exc:
        return None, str(exc)


def fetch_sessions(claude: str) -> tuple:
    rows, error = list_rows(claude)
    if error:
        return None, error
    return core.background_sessions(rows), None


def run_git(cwd: str, *args: str) -> tuple:
    """(stdout, None) from `git -C cwd ...`, or (None, one-line error). Never raises.

    The folder is not checked here: a stat on a hung mount would block the
    caller with no timeout, while git meets it under one and fails on its own
    for a folder that is gone.
    """
    if not cwd:
        return None, "no folder"
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=GIT_SECONDS, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"can't run git: {exc}"
    if done.returncode != 0:
        return None, core.first_message(done.stderr) or f"git exited with {done.returncode}"
    return done.stdout, None


def git_placement(cwd: str, home: str = "") -> tuple:
    """(placement, None) for `cwd`, or (None, one-line error) when git failed, so the caller can ask again later. Never raises.

    `home` is the real path of the home folder; see `core.placement_from_git`.
    """
    rev_parse, error = run_git(
        cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel", "--show-prefix",
    )
    if error:
        return None, error
    branch, _ = run_git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    return core.placement_from_git(cwd, rev_parse, branch, home, os.path.realpath), None


def start_session(claude: str, folder: str, message: str) -> tuple:
    """Run `claude --bg` in `folder`. Returns (session_id, None) or (None, error)."""
    if not os.path.isdir(folder):
        return None, f"folder not found: {folder}"
    try:
        done = subprocess.run(
            core.new_session_args(claude, message),
            cwd=folder, capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=60,
        )
    except FileNotFoundError:
        return None, f"can't find the claude command ({claude})"
    except subprocess.TimeoutExpired:
        return None, "claude --bg timed out"
    except OSError as exc:
        return None, f"can't run claude: {exc}"
    if done.returncode != 0:
        return None, core.first_message(done.stderr) or core.first_message(done.stdout) or (
            f"claude --bg exited with {done.returncode}"
        )
    sid = core.parse_new_session_id(done.stdout)
    if not sid:
        return None, "claude --bg started, but its output had no session id"
    return sid, None


def wait_until_running(claude: str, sid: str) -> tuple:
    """(session, None) once `claude agents --json` lists `sid` with a live process, else (None, error) after a few seconds.

    A session can be listed a moment before its pid is. The listing that
    finds it running is the fresh one the new viewer attaches on.
    """
    deadline = time.monotonic() + LISTED_WAIT_SECONDS
    seen = False
    while True:
        rows, _ = list_rows(claude)
        session = core.listed_session(rows, sid) if rows is not None else None
        if session is not None and session.running:
            return session, None
        seen = seen or session is not None
        if time.monotonic() >= deadline:
            if seen:
                return None, f"claude agents lists session {sid}, but it isn't running, so no viewer was opened"
            return None, f"claude --bg reported session {sid}, but claude agents doesn't list it, so no viewer was opened"
        time.sleep(LISTED_CHECK_SECONDS)


def close_session(claude: str, confirm: core.Confirm) -> tuple:
    """Run `claude stop` or `claude rm` on the confirmed session: (its output, None), or (None, its whole message on one line). Never raises."""
    args = core.close_args(claude, confirm)
    try:
        done = subprocess.run(
            args, capture_output=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            timeout=CLOSE_SECONDS,
        )
    except FileNotFoundError:
        return None, f"can't find the claude command ({claude})"
    except subprocess.TimeoutExpired:
        return None, f"claude {args[1]} timed out"
    except OSError as exc:
        return None, f"can't run claude: {exc}"
    if done.returncode != 0:
        return None, core.one_line(done.stderr) or core.one_line(done.stdout) or (
            f"claude {args[1]} exited with {done.returncode}"
        )
    return done.stdout, None


def list_subdirs(folder: str) -> list | None:
    """Names of the folders in `folder`, at most MAX_LISTED_NAMES of them, or None when it can't be read as a folder. Never raises.

    Called only from a `Listings` thread: a folder on a hung mount can block
    here for good.
    """
    names = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
                if len(names) >= core.MAX_LISTED_NAMES:
                    break
    except OSError:
        return None
    return names


class Listings:
    """Folder listings for the picker, each read on its own short-lived thread and kept for a while.

    `get` and `readable` never wait: they hand back whatever is cached (None
    before the first read arrives) and start a read when the cache is
    missing or old. `readable` says whether the folder itself could be read,
    which confirms a folder its parent's listing can't. A folder still being
    read is never read twice, and at most MAX_LISTINGS reads run at once, so
    a hung mount holds one slot instead of piling up threads or freezing the
    panel.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}
        self.reading = set()

    def get(self, folder: str):
        return self.lookup(folder)[0]

    def readable(self, folder: str):
        return self.lookup(folder)[1]

    def lookup(self, folder: str) -> tuple:
        with self.lock:
            names, readable, at = self.cache.get(folder, (None, None, None))
            stale = at is None or time.monotonic() - at >= LISTING_FRESH_SECONDS
            if stale and folder not in self.reading and len(self.reading) < MAX_LISTINGS:
                self.reading.add(folder)
                threading.Thread(target=self.read, args=(folder,), daemon=True).start()
        return names, readable

    def read(self, folder: str) -> None:
        try:
            names = list_subdirs(folder)
        except Exception:
            names = None
        with self.lock:
            self.cache[folder] = (names or [], names is not None, time.monotonic())
            self.reading.discard(folder)


class Closer(threading.Thread):
    """Stops or removes a confirmed session off the main thread, after checking a fresh listing still shows it as he saw it."""

    def __init__(self, claude: str, confirm: core.Confirm):
        super().__init__(daemon=True)
        self.claude, self.confirm = claude, confirm
        self.result = None

    def run(self) -> None:
        try:
            sessions, _ = fetch_sessions(self.claude)
            session, fresh = core.checked_row(sessions, self.confirm.id)
            if not core.close_still_applies(self.confirm, session, fresh):
                self.result = (False, core.close_refusal(self.confirm, session, fresh))
                return
            output, error = close_session(self.claude, self.confirm)
            self.result = (error is None, error or core.closed_text(self.confirm, output))
        except Exception as exc:
            self.result = (False, f"{self.confirm.action} failed: {core.error_line(exc)}")


class Starter(threading.Thread):
    """Starts a session off the main thread, so the panel keeps drawing while `claude --bg` runs."""

    def __init__(self, claude: str, folder: str, message: str):
        super().__init__(daemon=True)
        self.claude, self.folder, self.message = claude, folder, message
        self.result = None

    def run(self) -> None:
        try:
            sid, error = start_session(self.claude, self.folder, self.message)
            session = None
            if sid:
                session, error = wait_until_running(self.claude, sid)
        except Exception as exc:
            session, error = None, f"starting a session failed: {core.error_line(exc)}"
        self.result = (session, error)


class Checker(threading.Thread):
    """Lists sessions off the main thread before a restart, so a slow `claude agents` never freezes the panel.

    `asked` is (session id, main pane id) from when he pressed `r`, or from
    when emx caught the agent view in main (`auto`).
    """

    def __init__(self, claude: str, asked: tuple, auto: bool = False):
        super().__init__(daemon=True)
        self.claude, self.asked, self.auto = claude, asked, auto
        self.result = None

    def run(self) -> None:
        try:
            self.result = fetch_sessions(self.claude)
        except Exception as exc:
            self.result = (None, f"listing sessions failed: {core.error_line(exc)}")


class Resolver(threading.Thread):
    """Works out each folder's repository on its own thread, so a slow or hung git never holds up listing or drawing.

    Until a folder resolves, its sessions group by the folder itself. A folder
    git failed on is asked again later, backing off, never remembered as
    failed.
    """

    def __init__(self, home: str):
        super().__init__(daemon=True)
        self.home = home
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.wanted = set()
        self.placements = {}
        self.failures = {}
        self.changed = False

    def want(self, cwds) -> None:
        """Ask about these folders, the listed sessions' own, and stop asking about any other."""
        cwds = set(cwds)
        with self.lock:
            new = cwds - self.wanted
            self.wanted = cwds
        if new:
            self.wake.set()

    def due(self, now: float) -> list:
        with self.lock:
            return sorted(
                c for c in self.wanted
                if c not in self.placements and self.failures.get(c, (0, 0.0))[1] <= now
            )

    def resolve(self, cwd: str) -> None:
        try:
            placement, _ = git_placement(cwd, self.home)
        except Exception:
            placement = None
        with self.lock:
            if placement is None:
                tries = self.failures.get(cwd, (0, 0.0))[0] + 1
                self.failures[cwd] = (tries, time.monotonic() + core.retry_after(tries))
            else:
                self.placements[cwd] = placement
                self.failures.pop(cwd, None)
                self.changed = True

    def run(self) -> None:
        while True:
            try:
                for cwd in self.due(time.monotonic()):
                    self.resolve(cwd)
            except Exception:
                pass
            self.wake.wait(core.RETRY_FIRST_SECONDS)
            self.wake.clear()

    def take(self):
        """A copy of the placements when any resolved since the last take, else None."""
        with self.lock:
            if not self.changed:
                return None
            self.changed = False
            return dict(self.placements)


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def read_transcript(path: str, size: int) -> tuple:
    """(what the end of the transcript at `path` says, or None; whether the read succeeded), reading only as much of its tail as `core.tail_bytes` asks for. Never raises, and never writes."""
    found, read = None, 0
    try:
        with open(path, "rb") as f:
            while (want := core.tail_bytes(found, read, size)) is not None:
                f.seek(size - want)
                text = f.read(want).decode("utf-8", errors="replace")
                found, read = core.parse_transcript(text, partial=want < size), want
    except (OSError, ValueError):
        return None, False
    return found, True


class Transcripts(threading.Thread):
    """Reads each listed session's transcript on its own thread, so a large file or a slow disk never holds up listing or drawing.

    A session is read again at most every TRANSCRIPT_SECONDS, and only when
    its file's size or time changed. The transcript is looked for where
    Claude Code keeps it for the session's folder; failing that, by its file
    name in each project folder, which is retried later, backing off, rather
    than on every pass.
    """

    def __init__(self, root: str):
        super().__init__(daemon=True)
        self.root = root
        self.lock = threading.Lock()
        self.wanted = {}
        self.found = {}
        self.stamps = {}
        self.paths = {}
        self.misses = {}
        self.changed = False

    def want(self, sessions) -> None:
        with self.lock:
            self.wanted = {s.id: (s.cwd, s.session_id) for s in sessions if s.session_id}

    def run(self) -> None:
        while True:
            try:
                self.read_all()
            except Exception:
                pass
            time.sleep(TRANSCRIPT_SECONDS)

    def read_all(self) -> None:
        with self.lock:
            wanted = dict(self.wanted)
        for sid, (cwd, uuid) in wanted.items():
            try:
                self.read_one(sid, cwd, uuid)
            except Exception:
                continue
        uuids = {uuid for _, uuid in wanted.values()}
        for uuid in set(self.paths) - uuids:
            del self.paths[uuid]
        for uuid in set(self.misses) - uuids:
            del self.misses[uuid]
        with self.lock:
            gone = (set(self.found) | set(self.stamps)) - set(wanted)
            for sid in gone:
                self.stamps.pop(sid, None)
                if self.found.pop(sid, None) is not None:
                    self.changed = True

    def read_one(self, sid: str, cwd: str, uuid: str) -> None:
        path = self.locate(cwd, uuid)
        if path is None:
            return
        info = os.stat(path)
        stamp = (path, info.st_mtime_ns, info.st_size)
        if self.stamps.get(sid) == stamp:
            return
        found, ok = read_transcript(path, info.st_size)
        if not ok:
            return
        with self.lock:
            self.stamps[sid] = stamp
            if self.found.get(sid) != found:
                self.found[sid] = found
                self.changed = True

    def locate(self, cwd: str, uuid: str) -> str | None:
        if not core.is_session_id(uuid):
            return None
        name = uuid + ".jsonl"
        direct = os.path.join(self.root, "projects", core.transcript_dir(cwd), name)
        if os.path.isfile(direct):
            return direct
        known = self.paths.get(uuid)
        if known and os.path.isfile(known):
            return known
        tries, after = self.misses.get(uuid, (0, 0.0))
        if after > time.monotonic():
            return None
        # Recorded before the scan, so a scan that raises (no projects folder yet) backs off too.
        self.misses[uuid] = (tries + 1, time.monotonic() + core.retry_after(tries + 1))
        with os.scandir(os.path.join(self.root, "projects")) as folders:
            for folder in folders:
                path = os.path.join(folder.path, name)
                if folder.is_dir() and os.path.isfile(path):
                    self.paths[uuid] = path
                    self.misses.pop(uuid, None)
                    return path
        return None

    def take(self):
        """A copy of what each session's transcript says when any of it changed since the last take, else None."""
        with self.lock:
            if not self.changed:
                return None
            self.changed = False
            return dict(self.found)


class Poller(threading.Thread):
    """Lists sessions, and hands each folder to the resolver, and each session to the transcript reader, without waiting for either."""

    def __init__(self, claude: str):
        super().__init__(daemon=True)
        self.claude = claude
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.result = None
        self.resolver = Resolver(os.path.realpath(os.path.expanduser("~")))
        self.transcripts = Transcripts(claude_home())

    def poll(self) -> tuple:
        sessions, error = fetch_sessions(self.claude)
        if error:
            return None, error
        self.resolver.want(s.cwd for s in sessions)
        self.transcripts.want(sessions)
        return sessions, None

    def run(self) -> None:
        self.resolver.start()
        self.transcripts.start()
        while True:
            try:
                result = self.poll()
            except Exception as exc:
                result = None, f"listing sessions failed: {core.error_line(exc)}"
            with self.lock:
                self.result = result
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def take(self):
        with self.lock:
            result, self.result = self.result, None
        return result


class App:
    def __init__(self, scr, tmux: Tmux, claude: str):
        self.scr = scr
        self.tmux = tmux
        self.claude = claude
        self.me = os.environ.get("TMUX_PANE", "")
        self.prefs_file = core.prefs_path()
        self.prefs = core.load_prefs(self.prefs_file)
        self.sessions = []
        self.placements = {}
        self.groups = []
        self.loaded = False
        self.list_error = ""
        self.notice = ""
        self.notice_error = False
        self.notice_until = None
        self.cursor = None
        self.wanted_cursor = None
        self.help = False
        self.palette = Palette()
        self.main_pane = None
        self.applied = None
        self.misses = {}
        self.fresh = {}
        self.bound = None
        self.prompt = None
        self.row_map = {}
        self.scroll = 0
        self.last_housekeep = 0.0
        self.poller = Poller(claude)
        self.listings = Listings()
        self.home = os.path.expanduser("~")
        self.starter = None
        self.started = None
        self.checker = None
        self.closer = None
        self.confirm = None
        self.find = None
        self.pin_misses = {}
        self.flagged_viewers = frozenset()
        self.auto_tried = frozenset()
        self.last_restore = None
        self.focused = None
        self.pending_keys = []
        self.transcripts = {}
        self.peeking = False
        self.peek_wrapped = (None, [])

    def say(self, text: str, error: bool = False, seconds: float | None = None) -> None:
        """Show a notice; with `seconds` it goes by itself after that long, unless another replaces it first."""
        self.notice, self.notice_error = text, error
        self.notice_until = time.monotonic() + seconds if seconds is not None else None

    def save_prefs(self) -> None:
        try:
            core.save_prefs(self.prefs_file, self.prefs)
        except OSError as exc:
            self.say(f"Couldn't save prefs: {exc}", error=True)

    def layout_panes(self, panes: list) -> tuple:
        """(sidebar, main) in the sidebar's window. The main slot is whichever pane is not the sidebar, since swap-pane moves pane ids around."""
        mine = next((p for p in panes if p.id == self.me), None)
        if mine is None:
            return None, None
        main = next((p for p in panes if p.window == mine.window and p is not mine), None)
        return mine, main

    def session(self, sid: str):
        """The session with this id in the latest successful poll, or None."""
        return next((s for s in self.sessions if s.id == sid), None)

    def view(self) -> str:
        return core.current_view(self.prefs)

    def collapsed(self) -> frozenset:
        return core.finding_collapsed(self.prefs, self.view(), bool(self.find))

    def grouped(self) -> list:
        shown = core.find_matches(self.sessions, self.find or "", self.home)
        return core.group_sessions(shown, self.placements, self.view(), core.pinned_ids(self.prefs))

    def lines(self) -> list:
        return core.panel_lines(self.groups, self.collapsed())

    def items(self) -> list:
        return [line for line in self.lines() if line]

    def regroup(self) -> None:
        self.groups = self.grouped()
        self.cursor = core.settle_cursor(self.items(), self.cursor, self.groups, self.wanted_cursor)
        if self.cursor == self.wanted_cursor:
            self.wanted_cursor = None

    def toggle_group(self, key: str) -> None:
        self.prefs = core.toggle_collapsed(self.prefs, self.view(), key)
        self.save_prefs()
        self.regroup()

    def toggle_view(self) -> None:
        group = core.cursor_group(self.cursor, self.groups)
        on_session = self.cursor if self.cursor and self.cursor[0] == "session" else None
        self.prefs = core.toggle_view(self.prefs)
        self.save_prefs()
        self.groups = self.grouped()
        # The session under the cursor stays under it; a header has no counterpart in the other view.
        self.cursor = core.settle_cursor(self.items(), on_session or ("group", group), self.groups)

    def activate(self, item) -> None:
        if not item:
            return
        self.cursor, self.wanted_cursor = item, None
        if item[0] == "group":
            self.toggle_group(item[1])
        else:
            self.switch_to(item[1], core.OPEN)

    def housekeep(self, fresh_list: bool) -> None:
        try:
            panes = self.tmux.panes()
            sidebar, main = self.layout_panes(panes)
            self.main_pane = main
            if sidebar and main and self.bound != (sidebar.id, main.id):
                self.tmux.bind_toggle(BACK_KEY, sidebar.id, main.id)
                self.tmux.bind_left(sidebar.id, self_command("left", self.tmux.socket), core.PROMPT_TEXT_X)
                self.bound = (sidebar.id, main.id)
            if sidebar:
                self.take_focus(sidebar)
            if sidebar and main:
                self.reconcile_widths(sidebar)
            if fresh_list:
                self.close_stale(panes, main)
            self.flagged_viewers &= {p.id for p in panes}
            self.check_main(main)
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

    def set_focus(self, focused: bool, back: bool = False) -> None:
        """Note whether the panel has focus.

        Coming back from main with ← (`back`) puts the cursor on the session
        there, as `claude agents` does; Ctrl+\\ or a click leaves it where he
        left it.
        """
        if focused and self.focused is False and back:
            main = self.main_pane
            main_sid = main.session if main is not None and not self.main_is_off() else ""
            self.cursor = core.cursor_on_focus(self.items(), self.cursor, self.groups, main_sid)
            self.wanted_cursor = None
        if not focused:
            self.confirm = None
        self.focused = focused

    def read_focus(self) -> None:
        """Take focus from tmux's active pane, never from the report itself.

        tmux also passes on his terminal window losing and regaining focus,
        which would read as leaving the panel and coming back from main.
        """
        try:
            sidebar, _ = self.layout_panes(self.tmux.panes())
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
            return
        if sidebar:
            self.take_focus(sidebar)

    def take_focus(self, sidebar: Pane) -> None:
        """Focus as tmux reports it. The ← binding marks the panel before selecting it; a mark is cleared as soon as it is seen, and moves the cursor only when the panel is active."""
        back = core.came_back(sidebar.active, sidebar.back)
        if sidebar.back:
            try:
                self.tmux.clear_back(sidebar.id)
            except TmuxError as exc:
                self.say(f"tmux: {exc}", error=True)
        self.set_focus(sidebar.active, back=back)

    def check_main(self, main: Pane | None) -> None:
        """Flag the main pane once it stops showing the session emx put there, and bring the session back if it shows Claude Code's agent view."""
        if main is None or main.dead or not main.session:
            return
        state = self.view_state(main)
        self.flagged_viewers, _ = core.latch_off_main(self.flagged_viewers, main.id, state)
        self.auto_tried = core.still_tried(self.auto_tried, self.flagged_viewers)
        self.last_restore = core.last_restore_after(self.last_restore, main.id, state)
        action = core.auto_restore(
            state, main.id, main.id in self.auto_tried, self.checker is not None, self.last_restore, time.monotonic(),
        )
        if action is not None:
            self.auto_tried |= {main.id}
        if action == core.RESTORE:
            self.start_check(main, auto=True)
        elif action == core.GIVE_UP:
            self.say("")

    def view_state(self, viewer: Pane) -> str:
        session = self.session(viewer.session)
        if session is None:
            return "unknown"
        expected = "" if session.name == session.id else session.name
        return core.main_view_state(self.tmux.capture(viewer.id), expected, viewer.width)

    def main_is_off(self) -> bool:
        return self.main_pane is not None and self.main_pane.id in self.flagged_viewers

    def restoring_main(self) -> bool:
        checker = self.checker
        return checker is not None and core.restoring_main(checker.asked, checker.auto, self.main_id())

    def main_id(self) -> str | None:
        return self.main_pane.id if self.main_pane is not None else None

    def reconcile_widths(self, sidebar: Pane) -> None:
        action = core.width_action(self.applied, (sidebar.window_width, sidebar.width))
        if action == "adopt-panel":
            self.prefs["panel_width"] = sidebar.width
            self.save_prefs()
        if action is not None:
            self.apply_layout(sidebar)

    def apply_layout(self, sidebar: Pane) -> None:
        want = core.compute_layout(sidebar.window_width, self.prefs["panel_width"])
        self.tmux.resize_width(sidebar.id, want.panel)
        sidebar, _ = self.layout_panes(self.tmux.panes())
        if sidebar:
            self.applied = (sidebar.window_width, sidebar.width)

    def close_stale(self, panes: list, main: Pane | None) -> None:
        now = time.monotonic()
        live = {s.id for s in self.sessions}
        self.fresh = {sid: t for sid, t in self.fresh.items() if sid not in live and now - t < FRESH_GRACE_SECONDS}
        viewers = {p.session for p in panes if p.session and p.session not in self.fresh}
        self.misses, close = core.stale_viewers(viewers, live, self.misses)
        self.close_viewers(panes, main, close)

    def close_viewers(self, panes: list, main: Pane | None, sids) -> None:
        """Kill the viewers of these sessions; one in main is first swapped out for the placeholder, so the layout keeps its two columns."""
        for pane in panes:
            if pane.session not in sids:
                continue
            if main is not None and pane.id == main.id:
                placeholder = next((p for p in panes if p.role == "placeholder"), None)
                if placeholder is None:
                    continue
                self.tmux.swap(placeholder.id, pane.id)
            self.tmux.kill_pane(pane.id)

    def switch_to(self, sid: str, trigger: str, checked: tuple | None = None) -> None:
        try:
            panes = self.tmux.panes()
            _, main = self.layout_panes(panes)
            if main is None:
                self.say("The main pane is missing; restart emx.", error=True)
                return
            viewer = next((p for p in panes if p.session == sid), None)
            command = core.viewer_command(self.claude, sid)
            if viewer is None:
                if not self.confirm_attach(sid, trigger, checked):
                    return
                pane_id = self.tmux.new_hidden_window(command)
                self.tmux.set_pane_option(pane_id, SESSION_OPT, sid)
            else:
                pane_id = viewer.id
                self.flagged_viewers, respawn = core.latch_off_main(self.flagged_viewers, pane_id, self.view_state(viewer))
                if viewer.dead or respawn:
                    if not self.confirm_attach(sid, trigger, checked):
                        return
                    self.tmux.respawn(pane_id, command)
                    self.flagged_viewers -= {pane_id}
            if pane_id != main.id:
                self.tmux.swap(pane_id, main.id)
            self.tmux.select(pane_id)
            self.say("")
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
        self.housekeep(fresh_list=False)

    def confirm_attach(self, sid: str, trigger: str, checked: tuple | None = None) -> bool:
        """Ask core whether `claude attach` may run, and say why not when it may not.

        `checked` is (session, fresh) from a listing a worker thread took just
        now (`core.checked_row`); this thread never lists, so a slow `claude
        agents` can't freeze the panel. A trigger other than OPEN without one
        is refused.
        """
        if trigger == core.OPEN:
            session, fresh = self.session(sid), False
        else:
            session, fresh = checked or (None, False)
            fresh = fresh and not self.list_error
        if core.attach_allowed(session, trigger, fresh):
            return True
        shown = session or self.session(sid)
        self.say(core.refusal(shown.name if shown else sid, trigger, fresh, session), error=True)
        return False

    def restart_viewer(self) -> None:
        main = self.main_pane
        if main is None or not main.session:
            self.say("No session is open in the main pane.")
            return
        if self.checker is None:
            self.start_check(main)
        elif not core.check_still_applies(self.checker.asked, main.id, main.session):
            self.say("The previous restart check is still finishing; press r again in a moment.")
        else:
            self.checker.auto = False

    def start_check(self, main: Pane, auto: bool = False) -> None:
        if self.list_error:
            self.confirm_attach(main.session, core.RESTART)
            return
        self.checker = Checker(self.claude, (main.session, main.id), auto)
        self.checker.start()
        shown = self.session(main.session)
        name = shown.name if shown else main.session
        if auto:
            self.say(f"Main showed Claude Code's agent view; bringing back {name}…")
        else:
            self.say(f"Checking {name} is still running…")

    def finish_check(self) -> None:
        checker = self.checker
        if checker is None or checker.is_alive():
            return
        self.checker = None
        sid, pane_id = checker.asked
        sessions, _ = checker.result or (None, "listing sessions failed")
        try:
            _, main = self.layout_panes(self.tmux.panes())
            if main is None or not core.check_still_applies(checker.asked, main.id, main.session):
                self.say("The main pane changed while emx was checking, so it wasn't restarted.")
                return
            state = self.view_state(main) if checker.auto else None
            if not core.restore_still_wanted(checker.auto, state):
                self.say("")
                return
            if not self.confirm_attach(sid, core.RESTART, core.checked_row(sessions, sid)):
                return
            self.tmux.respawn(pane_id, core.viewer_command(self.claude, sid))
            self.flagged_viewers -= {pane_id}
            if checker.auto:
                self.last_restore = core.record_restore(self.last_restore, pane_id, time.monotonic())
                self.tmux.select(self.me)
                self.set_focus(True, back=True)
                self.say("Main showed Claude Code's agent view, so emx brought the session back.", seconds=RESTORED_NOTICE_SECONDS)
            else:
                self.tmux.select(pane_id)
                self.say("Viewer restarted.")
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

    def press_close(self) -> None:
        session = self.session(self.cursor[1]) if self.cursor and self.cursor[0] == "session" else None
        if session is None:
            return
        blocked = core.close_blocked(bool(self.list_error), self.closer.confirm if self.closer else None)
        if blocked:
            self.confirm = None
            self.say(blocked)
            return
        self.confirm, act = core.press_close(self.confirm, self.cursor, session, time.monotonic())
        if act is None:
            return
        self.closer = Closer(self.claude, act)
        self.closer.start()
        self.say(core.closing_text(act))

    def finish_close(self) -> None:
        closer = self.closer
        if closer is None or closer.is_alive():
            return
        self.closer = None
        done, message = closer.result or (False, f"{closer.confirm.action} failed")
        self.say(message, error=not done)
        self.poller.wake.set()
        if done and closer.confirm.action == core.REMOVE:
            try:
                panes = self.tmux.panes()
                _, main = self.layout_panes(panes)
                self.close_viewers(panes, main, {closer.confirm.id})
            except TmuxError as exc:
                self.say(f"tmux: {exc}", error=True)

    def toggle_pin(self) -> None:
        session = self.session(self.cursor[1]) if self.cursor and self.cursor[0] == "session" else None
        if session is None:
            return
        self.prefs = core.toggle_pin(self.prefs, session.id)
        self.save_prefs()
        self.regroup()
        self.say(core.pin_notice(self.prefs, session), seconds=RESTORED_NOTICE_SECONDS)

    def open_find(self) -> None:
        self.find = ""
        self.say("")

    def close_find(self) -> None:
        self.find = None
        self.regroup()

    def find_key(self, key) -> None:
        if key == "\x1b":
            self.close_find()
            return
        if key in ENTER_KEYS:
            self.find_submit()
            return
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            self.cursor = core.find_move(self.items(), self.cursor, -1 if key == curses.KEY_UP else 1)
            self.wanted_cursor = None
            return
        if key in BACKSPACE_KEYS:
            text = self.find[:-1]
        elif key == "\x15":
            text = ""
        elif isinstance(key, str) and key.isprintable():
            text = self.find + key
        else:
            return
        self.find, self.wanted_cursor = text, None
        self.regroup()
        self.cursor = core.top_match(self.items()) or self.cursor

    def find_submit(self) -> None:
        """Enter opens the session under the cursor, which typing keeps on the top match."""
        target = core.find_target(self.items(), self.cursor)
        if target is None:
            self.say("No session matches.")
            return
        blocked = core.open_blocked(self.starter is not None or self.awaiting_started_row(), target)
        if blocked:
            self.say(blocked)
            return
        self.close_find()
        self.activate(target)

    def flip_side(self) -> None:
        try:
            sidebar, main = self.layout_panes(self.tmux.panes())
            if sidebar is None or main is None:
                return
            self.tmux.swap(sidebar.id, main.id)
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
            return
        self.prefs = core.flip_side(self.prefs)
        self.save_prefs()
        self.applied = None
        self.housekeep(fresh_list=False)

    def change_panel_width(self, delta: int) -> None:
        self.prefs = core.adjust_panel_width(self.prefs, delta)
        self.save_prefs()
        self.applied = None
        self.housekeep(fresh_list=False)

    def detach(self) -> None:
        try:
            self.tmux.detach()
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

    def open_prompt(self) -> None:
        """`n`: straight to the first message in the cursor's folder, or to the folder picker when there is none to go on."""
        if self.starter is not None:
            self.say("Still starting the last session…")
            return
        folder = core.default_folder(
            self.cursor, self.groups, self.sessions, self.view(), self.prefs.get("last_folder", ""),
        )
        self.prompt = {"stage": "message", "text": "", "folder": folder or "", "hint": ""}
        if folder is None:
            self.open_picker(back=False)
        self.say("")

    def open_picker(self, back: bool = True) -> None:
        """`back` says Esc returns to the message, keeping what he typed, rather than cancelling."""
        p = self.prompt
        p.update(stage="folder", message=p["text"], text="", picked=None, back=back, hint="")

    def prompt_key(self, key) -> None:
        p = self.prompt
        if key == "\x1b":
            if p["stage"] == "folder" and p.get("back"):
                p.update(stage="message", text=p["message"], hint="")
            else:
                self.prompt = None
            return
        if key in ENTER_KEYS:
            self.prompt_submit()
        elif key in BACKSPACE_KEYS:
            p["text"] = p["text"][:-1]
            p.update(picked=None, hint="")
        elif key == "\x15":
            p.update(text="", picked=None, hint="")
        elif key == "\t":
            if p["stage"] == "message":
                self.open_picker()
            else:
                self.complete_suggestion()
        elif p["stage"] == "folder" and key in (curses.KEY_UP, curses.KEY_DOWN):
            self.move_pick(-1 if key == curses.KEY_UP else 1)
        elif isinstance(key, str) and key.isprintable():
            p["text"] += key
            p.update(picked=None, hint="")

    def suggestions(self) -> list:
        p = self.prompt
        folders = core.session_folders(self.sessions, self.groups, self.view())
        recent = self.prefs.get("recent_folders", [])
        roots = core.repo_roots([s.cwd for s in self.sessions] + recent, self.placements)
        listed = {}
        for folder in core.folders_to_list(p["text"], folders, recent, self.home, roots):
            names = self.listings.get(folder)
            if names is not None:
                listed[folder] = names
        return core.folder_suggestions(p["text"], folders, recent, listed, self.home, roots=roots)

    def move_pick(self, delta: int) -> None:
        """The highlight is kept by path: a listing arriving late can move rows, and the folder he chose must not change under him."""
        paths = [r.path for r in self.suggestions()]
        if not paths:
            return
        current = self.prompt.get("picked")
        at = paths.index(current) + delta if current in paths else (0 if delta > 0 else len(paths) - 1)
        self.prompt["picked"] = paths[max(0, min(len(paths) - 1, at))]

    def complete_suggestion(self) -> None:
        """Tab: the highlighted folder, else the top suggestion. With neither, the rows already say "no match"."""
        rows = self.suggestions()
        path = self.prompt.get("picked") or (rows[0].path if rows else None)
        if path is None:
            return
        chosen = core.Suggestion(path, core.tilde_path(path, self.home), "")
        self.prompt.update(text=core.completed_text(chosen), picked=None, hint="")

    def prompt_submit(self) -> None:
        p = self.prompt
        if p["stage"] == "folder":
            self.pick_folder()
            return
        self.prompt = None
        self.starter = Starter(self.claude, p["folder"], p["text"])
        self.starter.start()
        self.say("Starting a session…")

    def pick_folder(self) -> None:
        """Enter in the picker (`core.picker_enter`). The typed path is judged from cached reads; the filesystem itself is never touched here."""
        p = self.prompt
        typed = core.resolve_folder(p["text"], self.home)
        known = core.session_folders(self.sessions, self.groups, self.view()) + self.prefs.get("recent_folders", [])
        parent = os.path.dirname(typed)
        names = self.listings.get(parent)
        listed = (parent, names) if names is not None else None
        exists = core.folder_exists(typed, known, listed, self.listings.readable(typed))
        folder, hint = core.picker_enter(p.get("picked"), typed, exists, self.suggestions(), not p["text"].strip())
        if folder is None:
            p["hint"] = hint
            return
        p.update(stage="message", folder=folder, text=p["message"], hint="")

    def finish_start(self) -> None:
        starter = self.starter
        if starter is None or starter.is_alive():
            return
        self.starter = None
        session, error = starter.result or (None, "starting a session failed")
        if error:
            self.say(error, error=True)
            return
        sid = session.id
        self.started = (sid, time.monotonic())
        self.prefs = core.remember_folder(self.prefs, starter.folder)
        self.save_prefs()
        self.fresh[sid] = time.monotonic()
        self.wanted_cursor = ("session", sid)
        self.poller.wake.set()
        self.switch_to(sid, core.NEW, (session, True))
        if not self.notice_error:
            self.say("Session started.")

    def awaiting_started_row(self) -> bool:
        """Whether the cursor still waits to land on the session just started: until it does, he moves it, or FRESH_GRACE_SECONDS pass, as its viewer's wait does."""
        if self.started is None:
            return False
        sid, at = self.started
        if self.wanted_cursor != ("session", sid) or time.monotonic() - at >= FRESH_GRACE_SECONDS:
            self.started = None
            return False
        return True

    def handle_mouse(self) -> None:
        try:
            _, _, y, _, state = curses.getmouse()
        except curses.error:
            return
        if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            return
        item = self.row_map.get(y)
        if self.find is not None:
            # A header click would fold a group he cannot see folded until find closes.
            if not item or item[0] == "group":
                return
            self.close_find()
        self.activate(item)

    def handle_key(self, key) -> None:
        self.confirm = core.confirm_after_key(self.confirm, key == CTRL_X, key == curses.KEY_RESIZE)
        if self.peeking and key not in PEEK_KEYS and key != curses.KEY_RESIZE:
            self.peeking = False
        if key == curses.KEY_MOUSE:
            self.handle_mouse()
        elif key == curses.KEY_RESIZE:
            self.housekeep(fresh_list=False)
        elif self.prompt is not None:
            self.prompt_key(key)
        elif self.find is not None:
            self.find_key(key)
        elif self.help or key == "?":
            # Any key closes the help, so Enter can't open a session hidden behind it.
            self.help = not self.help
        elif self.peeking and key in PEEK_KEYS:
            self.peek_key(key)
        elif key == " ":
            self.open_peek()
        elif key in (curses.KEY_UP, "k"):
            self.cursor, self.wanted_cursor = core.move_cursor(self.items(), self.cursor, -1), None
        elif key in (curses.KEY_DOWN, "j"):
            self.cursor, self.wanted_cursor = core.move_cursor(self.items(), self.cursor, 1), None
        elif key in ENTER_KEYS or key == curses.KEY_RIGHT:
            blocked = core.open_blocked(self.starter is not None or self.awaiting_started_row(), self.cursor)
            if blocked:
                self.say(blocked)
            elif key in ENTER_KEYS or (self.cursor and self.cursor[0] == "session"):
                self.activate(self.cursor)
        elif key == CTRL_S:
            self.toggle_view()
        elif key == CTRL_X:
            self.press_close()
        elif key in (CTRL_F, "/"):
            self.open_find()
        elif key == CTRL_T:
            self.toggle_pin()
        elif key == "n":
            self.open_prompt()
        elif key == "r":
            self.restart_viewer()
        elif key == "s":
            self.flip_side()
        elif key in ("<", ","):
            self.change_panel_width(-core.WIDTH_STEP)
        elif key in (">", "."):
            self.change_panel_width(core.WIDTH_STEP)
        elif key == "q":
            self.detach()

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addstr(y, x, core.fit(text, w - x), attr)
            except curses.error:
                pass

    def bottom_lines(self, width: int, height: int | None = None) -> list:
        """(text, attr) lines for the area under the list: notice, prompt, footer. With the pane's `height`, the picker's rows fit under the panel's top two lines."""
        lines = []
        if core.off_main_warning(self.flagged_viewers, self.main_id(), self.restoring_main()):
            session = self.session(self.main_pane.session)
            name = session.name if session else self.main_pane.session
            # An unnamed session's screen can't confirm it is back, so the flag may outlive the cross-wire.
            verb = "isn't" if session and session.name != session.id else "may not be"
            warning = f"Main {verb} showing {name} — press r to go back"
            lines += [(t, self.color("error")) for t in core.wrap_words(warning, width)[:3]]
        if self.list_error:
            lines += [(t, self.color("error")) for t in core.wrap_words(f"! {self.list_error}", width)[:3]]
        if self.notice:
            attr = self.color("error") if self.notice_error else curses.A_DIM
            lines += [(t, attr) for t in core.wrap_words(self.notice, width)[:6]]
        if self.prompt is not None:
            # The prompt's own hint line stands in for the footer, whose keys don't apply while typing.
            room = None if height is None else height - 2 - len(lines)
            return lines + self.prompt_lines(width, room)
        if self.find is not None:
            lines += [("Find a session:", curses.A_BOLD)]
            lines += [(t, 0) for t in core.wrap("> " + self.find + "▏", width)[-3:]]
            return lines + [(t, curses.A_DIM) for t in core.wrap_words(FIND_HINT, width)[:2]]
        footers = (CONFIRM_FOOTER,) if self.confirm is not None else PEEK_FOOTERS if self.peeking else FOOTERS
        footer = next((f for f in footers if core.display_width(f) <= width - 1), footers[-1])
        lines.append((footer, curses.A_DIM))
        return lines

    def prompt_lines(self, width: int, room: int | None = None) -> list:
        """The prompt's lines; the picker's suggestions get only what `room` leaves after the title, input and hints."""
        p = self.prompt
        if p["stage"] == "message":
            title = "New session in "
            head = [(title + core.short_path(p["folder"], width - core.display_width(title), self.home), curses.A_BOLD)]
            hint = "Enter start · Tab folder · Esc cancel"
        else:
            head = [("New session · pick a folder:", curses.A_BOLD)]
            hint = "↑↓ pick · Tab fill · ↵ use · Esc " + ("back" if p.get("back") else "cancel")
        head += [(t, 0) for t in core.wrap("> " + p["text"] + "▏", width)[-3:]]
        tail = [(t, curses.A_DIM) for t in core.wrap_words(p["hint"], width)[:3]] if p["hint"] else []
        tail += [(t, curses.A_DIM) for t in core.wrap_words(hint, width)[:2]]
        if p["stage"] == "message":
            return head + tail
        fits = core.PICKER_ROWS if room is None else max(0, room - len(head) - len(tail))
        return head + self.suggestion_lines(width, fits) + tail

    def suggestion_lines(self, width: int, fits: int) -> list:
        """At most `fits` lines. Rows that don't fit become one "more" line, highlighted when the highlighted folder is among them."""
        try:
            rows = self.suggestions()
        except Exception as exc:
            return [(t, self.color("error")) for t in core.wrap_words(f"! {core.error_line(exc)}", width)][:fits]
        if not rows:
            return [("no match", curses.A_DIM)][:fits]
        picked = self.prompt.get("picked")
        shown = rows if len(rows) <= fits else rows[:max(0, fits - 1)]
        lines = []
        for row in shown:
            text = core.fit((" " + core.short_path(row.path, width - 1, self.home)).ljust(width), width)
            lines.append((text, self.palette.attr("plain", "focused") if row.path == picked else self.palette.attr("path")))
        hidden = rows[len(shown):]
        if hidden and fits > 0:
            attr = self.palette.attr("plain", "focused") if picked in {r.path for r in hidden} else curses.A_DIM
            lines.append((core.fit(f" … {len(hidden)} more".ljust(width), width), attr))
        return lines

    def color(self, name: str) -> int:
        return self.palette.attr(name)

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.row_map = {}
        self.draw_summary(w)
        self.put(1, 0, "─" * w, self.palette.attr("muted"))
        bottom = self.bottom_lines(max(1, w - 1), h)
        list_top, list_bottom = 2, h - len(bottom)
        if self.help:
            self.draw_help(list_top, list_bottom, w)
        elif not self.loaded:
            if not self.list_error:
                self.put(list_top, 1, "Loading sessions…", curses.A_DIM)
        elif not self.sessions:
            self.put(list_top, 1, "No background sessions.", curses.A_DIM)
            self.put(list_top + 1, 1, "Press n to start one.", curses.A_DIM)
        elif not self.groups:
            self.put(list_top, 1, "No session matches.", curses.A_DIM)
        else:
            peek_rows = self.draw_peek(list_top, list_bottom, w) if self.peeking else 0
            self.draw_list(list_top, list_bottom - peek_rows, w)
        for i, (text, attr) in enumerate(bottom):
            self.put(list_bottom + i, 0, " " + text, attr)
        scr.noutrefresh()
        curses.doupdate()

    def draw_peek(self, top: int, bottom: int, width: int) -> int:
        """Draw the peek box at the foot of the list area and return how many rows it took (0 when it doesn't fit)."""
        session = self.peeked()
        if session is None:
            return 0
        peek = core.peek_content(session, self.transcripts.get(session.id), time.time() * 1000)
        inner = width - 5
        if inner <= 0:
            return 0
        # Wrapping a long reply is the costly part, so it is done again only when the body or the width changes.
        if self.peek_wrapped[0] != (peek.lines, inner):
            self.peek_wrapped = ((peek.lines, inner), core.peek_lines(peek, inner, core.PEEK_BODY_LINES + 1))
        wrapped = self.peek_wrapped[1]
        rows = core.peek_height(bottom - top, len(wrapped))
        if rows == 0:
            return 0
        body = core.cut_lines(wrapped, rows - 3)
        y, box = bottom - rows, width - 1
        muted = self.palette.attr("muted")
        label = core.fit(session.name, box - 6)
        self.put(y, 0, "╭─ " + label + " " + "─" * max(0, box - 5 - core.display_width(label)) + "╮", muted)
        tone = "muted" if core.category(session) == core.STOPPED else CATEGORY_TONE[core.category(session)]
        lines = [(core.fit(peek.title, inner), self.palette.attr(tone) | curses.A_BOLD)]
        lines += [(text, curses.A_DIM if style == "dim" else self.palette.attr(PEEK_TONES[style])) for text, style in body]
        for i in range(rows - 2):
            self.put(y + 1 + i, 0, "│", muted)
            self.put(y + 1 + i, box - 1, "│", muted)
            if i < len(lines):
                self.put(y + 1 + i, 2, lines[i][0], lines[i][1])
        self.put(bottom - 1, 0, "╰" + "─" * max(0, box - 2) + "╯", muted)
        return rows

    def draw_summary(self, width: int) -> None:
        if not self.loaded:
            placed = core.fit_parts([("…", None)], width - 2)
        else:
            placed = core.summary_line(self.sessions, width - 2)
        style = curses.A_BOLD if core.title_style(self.focused is not False) == "bold" else curses.A_DIM
        for x, text, cat in placed:
            tone = "muted" if cat in (None, core.STOPPED) else CATEGORY_TONE[cat]
            self.put(0, 1 + x, text, self.palette.attr(tone) | style)

    def draw_help(self, top: int, bottom: int, width: int) -> None:
        self.put(top, 1, "Keys", curses.A_BOLD)
        y, x = top + 2, 1 + HELP_KEY_WIDTH
        for key, what in HELP:
            lines = textwrap.wrap(what, max(1, width - 1 - x))
            if y + len(lines) > bottom:
                break
            self.put(y, 1, key, self.palette.attr("accent"))
            for line in lines:
                self.put(y, x, line)
                y += 1

    def draw_list(self, top: int, bottom: int, width: int) -> None:
        lines = self.lines()
        height = max(1, bottom - top)
        at = lines.index(self.cursor) if self.cursor in lines else 0
        self.scroll = max(0, min(self.scroll, len(lines) - height))
        self.scroll = min(max(self.scroll, at - height + 1), at)
        main_sid = self.main_pane.session if self.main_pane and not self.main_is_off() else ""
        cursor_group = core.cursor_group(self.cursor, self.groups)
        groups = {g.key: g for g in self.groups}
        group_of = {s.id: g.key for g in self.groups for s in g.sessions}
        aligns = {}
        for g in self.groups:
            column = core.name_column(((s.name, self.worktree(s)) for s in g.sessions), width)
            aligns.update((s.id, column) for s in g.sessions)
        now_ms = time.time() * 1000
        frame = int(time.monotonic() / FRAME_SECONDS)
        for slot, line in enumerate(lines[self.scroll:self.scroll + height]):
            if line is None:
                continue
            y = top + slot
            selected = core.selection_bar(line == self.cursor, self.focused is not False)
            self.row_map[y] = line
            if selected:
                self.put(y, 0, " " * width, self.palette.attr("plain", selected))
            if line[0] == "group":
                self.draw_header(y, groups[line[1]], selected, line[1] == cursor_group, width)
            else:
                session = self.session(line[1])
                if session is not None:
                    in_main = session.id == main_sid
                    armed = self.confirm if self.confirm is not None and self.confirm.id == session.id else None
                    self.draw_session(
                        y, session, selected, in_main, width, aligns.get(session.id, 0), now_ms, frame, armed,
                        group_of.get(session.id, ""),
                    )

    def draw_header(self, y: int, group, selected: str | None, bold: bool, width: int) -> None:
        view = self.view()
        name_tone = CATEGORY_TONE[group.key] if core.state_group(view, group.key) else "plain"
        tones = {"arrow": "muted", "dir": "path", "name": name_tone, "count": "muted"}
        collapsed = group.key in self.collapsed()
        for x, text, part in core.header_layout(view, group.key, len(group.sessions), collapsed, width):
            attr = self.palette.attr(tones[part], selected)
            if part == "name" and bold:
                attr |= curses.A_BOLD
            self.put(y, x, text, attr)

    def worktree(self, s) -> str:
        return self.placements.get(s.cwd, core.Placement(s.cwd)).worktree

    def draw_session(
        self, y: int, s, selected: str | None, in_main: bool, width: int, align: int, now_ms: float, frame: int,
        armed: core.Confirm | None = None, group_key: str = "",
    ) -> None:
        cat = core.category(s)
        stopped = cat == core.STOPPED
        pal = self.palette

        def attr(tone: str, extra: int = 0) -> int:
            if stopped:
                return pal.attr("stopped", selected) | curses.A_DIM | extra
            return pal.attr(tone, selected) | extra

        if in_main:
            self.put(y, 0, "▎", pal.attr("accent", selected) | curses.A_BOLD)
        glyph_tone = "muted" if cat == core.WORKING else CATEGORY_TONE[cat]
        self.put(y, 1, core.glyph(cat, frame), attr(glyph_tone))
        if armed is not None:
            self.put(y, core.ROW_LEAD, core.fit(core.confirm_label(armed), width - 1 - core.ROW_LEAD),
                     pal.attr("error", selected) | curses.A_BOLD)
            return
        word = "" if core.state_group(self.view(), group_key) else core.state_word(s)
        age = core.session_age(s, now_ms)
        detail, tail = core.row_detail(s, self.transcripts.get(s.id))
        parts = core.row_layout(s.name, self.worktree(s), word, detail, age, width, align, tail)
        tones = {"name": "plain", "extra": "path", "status": CATEGORY_TONE[cat], "age": "muted"}
        for x, text, part in parts:
            emphasis = curses.A_BOLD if part == "name" and (selected is not None or in_main) else 0
            self.put(y, x, text, attr(tones[part], emphasis))

    def take_poll(self) -> bool:
        result = self.poller.take()
        if result is None:
            return False
        sessions, error = result
        if error:
            self.list_error = error
            return False
        self.list_error = ""
        self.sessions = sessions
        self.loaded = True
        self.prefs, self.pin_misses, pruned = core.prune_pins(self.prefs, [s.id for s in sessions], self.pin_misses)
        if pruned:
            self.save_prefs()
        self.regroup()
        return True

    def take_placements(self) -> None:
        placements = self.poller.resolver.take()
        if placements is not None:
            self.placements = placements
            self.regroup()

    def take_transcripts(self) -> None:
        found = self.poller.transcripts.take()
        if found is not None:
            self.transcripts = found

    def peeked(self):
        """The session the peek shows: the one under the cursor."""
        if not self.cursor or self.cursor[0] != "session":
            return None
        return self.session(self.cursor[1])

    def open_peek(self) -> None:
        if self.peeked() is None:
            return
        if self.peek_fits():
            self.peeking = True
        else:
            self.say(PEEK_NO_ROOM, seconds=RESTORED_NOTICE_SECONDS)

    def peek_fits(self) -> bool:
        """Whether the pane, at its size now, has room for the peek box under the list."""
        h, w = self.scr.getmaxyx()
        available = h - len(self.bottom_lines(max(1, w - 1), h)) - 2
        return w - 5 > 0 and core.peek_height(available, 1) > 0

    def peek_key(self, key) -> None:
        if key in (" ", "\x1b"):
            self.peeking = False
        else:
            delta = -1 if key in (curses.KEY_UP, "k") else 1
            self.cursor, self.wanted_cursor = core.find_move(self.items(), self.cursor, delta), None

    def refresh(self) -> None:
        try:
            self.finish_start()
            self.finish_check()
            self.finish_close()
            fresh_list = self.take_poll()
            self.take_placements()
            self.take_transcripts()
            if self.peeking and self.peeked() is None:
                self.peeking = False
            elif self.peeking and not self.peek_fits():
                self.peeking = False
                if not self.notice:
                    # A notice that took the room is kept rather than replaced by this one.
                    self.say(PEEK_NO_ROOM, seconds=RESTORED_NOTICE_SECONDS)
            now = time.monotonic()
            if self.confirm is not None and not core.confirm_holds(
                self.confirm, self.cursor, self.session(self.confirm.id), now,
            ):
                self.confirm = None
            if core.notice_expired(self.notice_until, now):
                self.say("")
            if fresh_list or now - self.last_housekeep >= HOUSEKEEP_SECONDS:
                self.last_housekeep = now
                self.housekeep(fresh_list)
            self.draw()
        except Exception as exc:
            self.say(f"emx error: {core.error_line(exc)}", error=True)

    def read_input(self) -> None:
        """Wait briefly for a key, then handle every key already queued, all before the next poll or redraw.

        A click is mapped through the `row_map` of the frame on screen. Applying
        a poll or redrawing between two queued events would send a later click
        to whichever row has since moved under it.
        """
        try:
            for _ in range(MAX_QUEUED_KEYS):
                try:
                    key = self.next_key()
                except curses.error:
                    return
                self.scr.timeout(0)
                try:
                    if self.focus_report(key) is not None:
                        self.read_focus()
                        continue
                    if key not in (curses.KEY_RESIZE, curses.KEY_MOUSE):
                        # A key typed here proves focus; he is already moving, so the cursor stays his.
                        self.set_focus(True)
                    self.handle_key(key)
                except Exception as exc:
                    self.say(f"emx error: {core.error_line(exc)}", error=True)
        finally:
            self.scr.timeout(KEY_WAIT_MS)

    def next_key(self):
        if self.pending_keys:
            return self.pending_keys.pop(0)
        return self.scr.get_wch()

    def focus_report(self, key) -> bool | None:
        """tmux reports focus as ESC [ I or ESC [ O, which curses hands over one key at a time.

        Keys read past the ESC that turn out not to be a report are put back,
        so Esc and anything after it still reach `handle_key`.
        """
        keys = [key]
        while core.may_be_focus_report(keys):
            try:
                keys.append(self.next_key())
            except curses.error:
                break
        focus = core.focus_report(keys)
        if focus is None:
            self.pending_keys[:0] = keys[1:]
        return focus

    def run(self) -> None:
        self.poller.start()
        while True:
            self.refresh()
            self.read_input()


def setup_curses(scr) -> None:
    curses.raw()
    # Asks tmux (focus-events on) to report focus changes, so the panel restyles at once rather than at the next housekeeping.
    os.write(1, FOCUS_REPORTS_ON.encode())
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(KEY_WAIT_MS)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)


class Palette:
    """curses attributes for each tone in TONES, plain or on a selection bar ("focused" or "unfocused", see `core.selection_bar`).

    With 256 colours each bar is a background colour from BAR_BG; with fewer
    the bar is reverse video, dimmed while the panel lacks focus, and the
    greys become dim white.
    """

    def __init__(self):
        self.colored = curses.has_colors()
        self.rich = False
        self.pairs = {}
        if not self.colored:
            return
        curses.start_color()
        curses.use_default_colors()
        self.rich = curses.COLORS >= 256 and curses.COLOR_PAIRS > 3 * len(TONES)
        for i, (tone, (rich, basic)) in enumerate(TONES.items()):
            fg = rich if self.rich else basic
            self.pairs[tone] = {None: 3 * i + 1}
            curses.init_pair(3 * i + 1, fg, -1)
            for j, (bar, bg) in enumerate(BAR_BG.items(), start=2):
                curses.init_pair(3 * i + j, fg, bg if self.rich else -1)
                self.pairs[tone][bar] = 3 * i + j

    def attr(self, tone: str, bar: str | None = None) -> int:
        extra = curses.A_DIM if not self.rich and tone in ("muted", "path", "stopped") else 0
        if bar and not self.rich:
            extra |= curses.A_REVERSE | (curses.A_DIM if bar == "unfocused" else 0)
        if not self.colored:
            return extra | (curses.A_BOLD if tone == "error" else 0)
        return curses.color_pair(self.pairs[tone][bar]) | extra


def run(socket: str) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    claude = os.environ.get("EMX_CLAUDE") or "claude"

    def body(scr):
        setup_curses(scr)
        try:
            App(scr, Tmux(socket), claude).run()
        finally:
            os.write(1, FOCUS_REPORTS_OFF.encode())

    # Stale LINES/COLUMNS inherited from the launching shell would override the pane's real size.
    # curses.use_env(False) is not the fix: it falls back to terminfo's fixed 80x24, not the pane.
    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    curses.wrapper(body)
    return 0
