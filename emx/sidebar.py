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
from emx.tmux import ROLE, SESSION_OPT, Pane, Tmux, TmuxError

POLL_SECONDS = 1.5
HOUSEKEEP_SECONDS = 0.5
FRESH_GRACE_SECONDS = 60
LISTED_WAIT_SECONDS = 5
LISTED_CHECK_SECONDS = 0.5
GIT_SECONDS = 5
BACK_KEY = "C-\\"
FOOTERS = (
    "↵ open  n new  ^s view  ? keys",
    "↵ open n new ^s view ? keys",
    "n new  ? keys",
    "? keys",
)
HELP = (
    ("↑↓ j k", "move"),
    ("↵ click", "open a session, fold a group"),
    ("n", "new session"),
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
BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)
CTRL_S = "\x13"
FRAME_SECONDS = 0.15

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
SELECTED_BG = 237
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
    """(stdout, None) from `git -C cwd ...`, or (None, one-line error). Never raises."""
    if not cwd or not os.path.isdir(cwd):
        return None, f"folder not found: {cwd}"
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


def git_placement(cwd: str) -> core.Placement:
    rev_parse, _ = run_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel")
    branch = None
    if rev_parse is not None:
        branch, _ = run_git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    return core.placement_from_git(cwd, rev_parse, branch)


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


def wait_until_listed(claude: str, sid: str) -> tuple:
    """(sid, None) once `claude agents --json` lists `sid` exactly, else (None, error) after a few seconds."""
    deadline = time.monotonic() + LISTED_WAIT_SECONDS
    while True:
        rows, _ = list_rows(claude)
        if rows is not None and core.id_listed(rows, sid):
            return sid, None
        if time.monotonic() >= deadline:
            return None, f"claude --bg reported session {sid}, but claude agents doesn't list it, so no viewer was opened"
        time.sleep(LISTED_CHECK_SECONDS)


class Starter(threading.Thread):
    """Starts a session off the main thread, so the panel keeps drawing while `claude --bg` runs."""

    def __init__(self, claude: str, folder: str, message: str):
        super().__init__(daemon=True)
        self.claude, self.folder, self.message = claude, folder, message
        self.result = None

    def run(self) -> None:
        try:
            sid, error = start_session(self.claude, self.folder, self.message)
            if sid:
                sid, error = wait_until_listed(self.claude, sid)
        except Exception as exc:
            sid, error = None, f"starting a session failed: {core.error_line(exc)}"
        self.result = (sid, error)


class Poller(threading.Thread):
    """Lists sessions, and works out each new folder's repository here so git never holds up drawing."""

    def __init__(self, claude: str):
        super().__init__(daemon=True)
        self.claude = claude
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.result = None
        self.placements = {}

    def poll(self) -> tuple:
        sessions, error = fetch_sessions(self.claude)
        if error:
            return None, None, error
        for s in sessions:
            if s.cwd not in self.placements:
                self.placements[s.cwd] = git_placement(s.cwd)
        return sessions, dict(self.placements), None

    def run(self) -> None:
        while True:
            try:
                result = self.poll()
            except Exception as exc:
                result = None, None, f"listing sessions failed: {core.error_line(exc)}"
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
        self.cursor = None
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
        self.poller = Poller(claude)
        self.starter = None
        self.flagged_viewers = frozenset()

    def say(self, text: str, error: bool = False) -> None:
        self.notice, self.notice_error = text, error

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
        return core.collapsed_keys(self.prefs, self.view())

    def lines(self) -> list:
        return core.panel_lines(self.groups, self.collapsed())

    def items(self) -> list:
        return [line for line in self.lines() if line]

    def regroup(self) -> None:
        self.groups = core.group_sessions(self.sessions, self.placements, self.view())
        self.cursor = core.settle_cursor(self.items(), self.cursor, self.groups)

    def toggle_group(self, key: str) -> None:
        self.prefs = core.toggle_collapsed(self.prefs, self.view(), key)
        self.save_prefs()
        self.regroup()

    def toggle_view(self) -> None:
        group = core.cursor_group(self.cursor, self.groups)
        on_session = self.cursor if self.cursor and self.cursor[0] == "session" else None
        self.prefs = core.toggle_view(self.prefs)
        self.save_prefs()
        self.groups = core.group_sessions(self.sessions, self.placements, self.view())
        # The session under the cursor stays under it; a header has no counterpart in the other view.
        self.cursor = core.settle_cursor(self.items(), on_session or ("group", group), self.groups)

    def activate(self, item) -> None:
        if not item:
            return
        self.cursor = item
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
                self.bound = (sidebar.id, main.id)
            if sidebar and main:
                self.reconcile_widths(sidebar)
            if fresh_list:
                self.close_stale(panes, main)
            self.flagged_viewers &= {p.id for p in panes}
            self.check_main(main)
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

    def check_main(self, main: Pane | None) -> None:
        """Flag the main pane once it stops showing the session emx put there: ← opens the agent view, and from there any session."""
        if main is None or main.dead or not main.session:
            return
        self.flagged_viewers, _ = core.latch_off_main(self.flagged_viewers, main.id, self.view_state(main))

    def view_state(self, viewer: Pane) -> str:
        session = self.session(viewer.session)
        if session is None:
            return "unknown"
        expected = "" if session.name == session.id else session.name
        return core.main_view_state(self.tmux.capture(viewer.id), expected, viewer.width)

    def main_is_off(self) -> bool:
        return self.main_pane is not None and self.main_pane.id in self.flagged_viewers

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
        for pane in panes:
            if pane.session not in close:
                continue
            if main is not None and pane.id == main.id:
                placeholder = next((p for p in panes if p.role == "placeholder"), None)
                if placeholder is None:
                    continue
                self.tmux.swap(placeholder.id, pane.id)
            self.tmux.kill_pane(pane.id)

    def switch_to(self, sid: str, trigger: str) -> None:
        try:
            panes = self.tmux.panes()
            _, main = self.layout_panes(panes)
            if main is None:
                self.say("The main pane is missing; restart emx.", error=True)
                return
            viewer = next((p for p in panes if p.session == sid), None)
            command = core.viewer_command(self.claude, sid)
            if viewer is None:
                if not self.attach_allowed(sid, trigger):
                    return
                pane_id = self.tmux.new_hidden_window(command)
                self.tmux.set_pane_option(pane_id, SESSION_OPT, sid)
            else:
                pane_id = viewer.id
                self.flagged_viewers, respawn = core.latch_off_main(self.flagged_viewers, pane_id, self.view_state(viewer))
                if viewer.dead or respawn:
                    if not self.attach_allowed(sid, trigger):
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

    def attach_allowed(self, sid: str, trigger: str) -> bool:
        session = self.session(sid)
        if core.attach_allowed(session, trigger):
            return True
        if session is None:
            self.say(f"Session {sid} isn't in the latest list, so emx won't reopen it.", error=True)
        else:
            self.say(f"{session.name} is stopped. Press Enter on it to reopen it.", error=True)
        return False

    def restart_viewer(self) -> None:
        main = self.main_pane
        if main is None or not main.session:
            self.say("No session is open in the main pane.")
            return
        if not self.attach_allowed(main.session, core.RESTART):
            return
        try:
            self.tmux.respawn(main.id, core.viewer_command(self.claude, main.session))
            self.flagged_viewers -= {main.id}
            self.tmux.select(main.id)
            self.say("Viewer restarted.")
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

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
        if self.starter is not None:
            self.say("Still starting the last session…")
            return
        folder = self.prefs.get("last_folder", "~")
        home = os.path.expanduser("~")
        if folder.startswith(home + "/") or folder == home:
            folder = "~" + folder[len(home):]
        self.prompt = {"stage": "folder", "text": folder.rstrip("/") + "/", "folder": "", "hint": ""}
        self.say("")

    def prompt_key(self, key) -> None:
        p = self.prompt
        if key == "\x1b":
            self.prompt = None
            return
        if key in ENTER_KEYS:
            self.prompt_submit()
        elif key in BACKSPACE_KEYS:
            p["text"] = p["text"][:-1]
        elif key == "\x15":
            p["text"] = ""
        elif key == "\t" and p["stage"] == "folder":
            p["text"], names = core.complete_dir(p["text"], os.path.expanduser("~"))
            p["hint"] = "  ".join(names) if len(names) > 1 else ("no match" if not names else "")
        elif isinstance(key, str) and key.isprintable():
            p["text"] += key
            p["hint"] = ""

    def prompt_submit(self) -> None:
        p = self.prompt
        if p["stage"] == "folder":
            folder = core.resolve_folder(p["text"], os.path.expanduser("~"))
            if not os.path.isdir(folder):
                p["hint"] = "not a folder"
                return
            self.prompt = {"stage": "message", "text": "", "folder": folder, "hint": ""}
            return
        self.prompt = None
        self.starter = Starter(self.claude, p["folder"], p["text"])
        self.starter.start()
        self.say("Starting a session…")

    def finish_start(self) -> None:
        starter = self.starter
        if starter is None or starter.is_alive():
            return
        self.starter = None
        sid, error = starter.result or (None, "starting a session failed")
        if error:
            self.say(error, error=True)
            return
        self.prefs["last_folder"] = starter.folder
        self.save_prefs()
        self.fresh[sid] = time.monotonic()
        self.cursor = ("session", sid)
        self.poller.wake.set()
        self.switch_to(sid, core.NEW)
        if not self.notice_error:
            self.say("Session started.")

    def handle_mouse(self) -> None:
        try:
            _, _, y, _, state = curses.getmouse()
        except curses.error:
            return
        if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            return
        self.activate(self.row_map.get(y))

    def handle_key(self, key) -> None:
        if key == curses.KEY_MOUSE:
            self.handle_mouse()
        elif key == curses.KEY_RESIZE:
            self.housekeep(fresh_list=False)
        elif self.prompt is not None:
            self.prompt_key(key)
        elif self.help or key == "?":
            # Any key closes the help, so Enter can't open a session hidden behind it.
            self.help = not self.help
        elif key in (curses.KEY_UP, "k"):
            self.cursor = core.move_cursor(self.items(), self.cursor, -1)
        elif key in (curses.KEY_DOWN, "j"):
            self.cursor = core.move_cursor(self.items(), self.cursor, 1)
        elif key in ENTER_KEYS:
            self.activate(self.cursor)
        elif key == CTRL_S:
            self.toggle_view()
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

    def bottom_lines(self, width: int) -> list:
        """(text, attr) lines for the area under the list: notice, prompt, footer."""
        lines = []
        if self.main_is_off():
            session = self.session(self.main_pane.session)
            name = session.name if session else self.main_pane.session
            # An unnamed session's screen can't confirm it is back, so the flag may outlive the cross-wire.
            verb = "isn't" if session and session.name != session.id else "may not be"
            warning = f"Main {verb} showing {name} — press r to go back"
            lines += [(t, self.color("error")) for t in core.wrap(warning, width)[:3]]
        if self.list_error:
            lines += [(t, self.color("error")) for t in core.wrap(f"! {self.list_error}", width)[:3]]
        if self.notice:
            attr = self.color("error") if self.notice_error else curses.A_DIM
            lines += [(t, attr) for t in core.wrap(self.notice, width)[:6]]
        if self.prompt is not None:
            title = "New session · folder (Tab completes):" if self.prompt["stage"] == "folder" else (
                "First message (Enter for none):"
            )
            lines.append((title, curses.A_BOLD))
            lines += [(t, 0) for t in core.wrap("> " + self.prompt["text"] + "▏", width)[-3:]]
            if self.prompt["hint"]:
                lines += [(t, curses.A_DIM) for t in core.wrap(self.prompt["hint"], width)[:3]]
            lines.append(("Enter next · Esc cancel", curses.A_DIM))
        footer = next((f for f in FOOTERS if core.display_width(f) <= width - 1), FOOTERS[-1])
        lines.append((footer, curses.A_DIM))
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
        bottom = self.bottom_lines(max(1, w - 1))
        list_top, list_bottom = 2, h - len(bottom)
        if self.help:
            self.draw_help(list_top, list_bottom, w)
        elif not self.loaded:
            if not self.list_error:
                self.put(list_top, 1, "Loading sessions…", curses.A_DIM)
        elif not self.sessions:
            self.put(list_top, 1, "No background sessions.", curses.A_DIM)
            self.put(list_top + 1, 1, "Press n to start one.", curses.A_DIM)
        else:
            self.draw_list(list_top, list_bottom, w)
        for i, (text, attr) in enumerate(bottom):
            self.put(list_bottom + i, 0, " " + text, attr)
        scr.noutrefresh()
        curses.doupdate()

    def draw_summary(self, width: int) -> None:
        if not self.loaded:
            placed = core.fit_parts([("…", None)], width - 2)
        else:
            placed = core.summary_line(self.sessions, width - 2)
        for x, text, cat in placed:
            tone = "muted" if cat in (None, core.STOPPED) else CATEGORY_TONE[cat]
            self.put(0, 1 + x, text, self.palette.attr(tone))

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
        now_ms = time.time() * 1000
        frame = int(time.monotonic() / FRAME_SECONDS)
        for slot, line in enumerate(lines[self.scroll:self.scroll + height]):
            if line is None:
                continue
            y = top + slot
            selected = line == self.cursor
            self.row_map[y] = line
            if selected:
                self.put(y, 0, " " * width, self.palette.attr("plain", True))
            if line[0] == "group":
                self.draw_header(y, groups[line[1]], selected, line[1] == cursor_group, width)
            else:
                session = self.session(line[1])
                if session is not None:
                    self.draw_session(y, session, selected, session.id == main_sid, width, now_ms, frame)

    def draw_header(self, y: int, group, selected: bool, bold: bool, width: int) -> None:
        view = self.view()
        name_tone = CATEGORY_TONE[group.key] if view == core.BY_STATE else "plain"
        tones = {"arrow": "muted", "dir": "path", "name": name_tone, "count": "muted"}
        collapsed = group.key in self.collapsed()
        for x, text, part in core.header_layout(view, group.key, len(group.sessions), collapsed, width):
            attr = self.palette.attr(tones[part], selected)
            if part == "name" and bold:
                attr |= curses.A_BOLD
            self.put(y, x, text, attr)

    def draw_session(self, y: int, s, selected: bool, in_main: bool, width: int, now_ms: float, frame: int) -> None:
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
        worktree = self.placements.get(s.cwd, core.Placement(s.cwd)).worktree
        parts = core.row_layout(s.name, worktree, core.state_word(s), s.waiting_for, core.session_age(s, now_ms), width)
        tones = {"name": "plain", "extra": "path", "status": CATEGORY_TONE[cat], "age": "muted"}
        for x, text, part in parts:
            emphasis = curses.A_BOLD if part == "name" and (selected or in_main) else 0
            self.put(y, x, text, attr(tones[part], emphasis))

    def take_poll(self) -> bool:
        result = self.poller.take()
        if result is None:
            return False
        sessions, placements, error = result
        if error:
            self.list_error = error
            return False
        self.list_error = ""
        self.sessions = sessions
        self.placements = placements
        self.loaded = True
        self.regroup()
        return True

    def run(self) -> None:
        self.poller.start()
        last_housekeep = 0.0
        while True:
            try:
                self.finish_start()
                fresh_list = self.take_poll()
                now = time.monotonic()
                if fresh_list or now - last_housekeep >= HOUSEKEEP_SECONDS:
                    last_housekeep = now
                    self.housekeep(fresh_list)
                self.draw()
            except Exception as exc:
                self.say(f"emx error: {core.error_line(exc)}", error=True)
            try:
                key = self.scr.get_wch()
            except curses.error:
                continue
            try:
                self.handle_key(key)
            except Exception as exc:
                self.say(f"emx error: {core.error_line(exc)}", error=True)


def setup_curses(scr) -> None:
    curses.raw()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(200)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)


class Palette:
    """curses attributes for each tone in TONES, plain or on the selection bar.

    With 256 colours the bar is a dark grey background; with fewer it is
    reverse video, and the greys become dim white.
    """

    def __init__(self):
        self.colored = curses.has_colors()
        self.rich = False
        self.pairs = {}
        if not self.colored:
            return
        curses.start_color()
        curses.use_default_colors()
        self.rich = curses.COLORS >= 256 and curses.COLOR_PAIRS > 2 * len(TONES)
        bar = SELECTED_BG if self.rich else -1
        for i, (tone, (rich, basic)) in enumerate(TONES.items()):
            fg = rich if self.rich else basic
            curses.init_pair(2 * i + 1, fg, -1)
            curses.init_pair(2 * i + 2, fg, bar)
            self.pairs[tone] = (2 * i + 1, 2 * i + 2)

    def attr(self, tone: str, selected: bool = False) -> int:
        dim = curses.A_DIM if not self.rich and tone in ("muted", "path", "stopped") else 0
        bar = curses.A_REVERSE if selected and not self.rich else 0
        if not self.colored:
            return dim | bar | (curses.A_BOLD if tone == "error" else 0)
        return curses.color_pair(self.pairs[tone][1 if selected else 0]) | dim | bar


def run(socket: str) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    claude = os.environ.get("EMX_CLAUDE") or "claude"

    def body(scr):
        setup_curses(scr)
        App(scr, Tmux(socket), claude).run()

    # Stale LINES/COLUMNS inherited from the launching shell would override the pane's real size.
    # curses.use_env(False) is not the fix: it falls back to terminfo's fixed 80x24, not the pane.
    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    curses.wrapper(body)
    return 0
