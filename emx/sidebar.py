"""The live sessions panel: a curses app running in emx's sidebar pane."""

from __future__ import annotations

import curses
import locale
import os
import subprocess
import threading
import time

from emx import core
from emx.tmux import ROLE, SESSION_OPT, Pane, Tmux, TmuxError

POLL_SECONDS = 1.5
HOUSEKEEP_SECONDS = 0.5
FRESH_GRACE_SECONDS = 60
LISTED_WAIT_SECONDS = 5
LISTED_CHECK_SECONDS = 0.5
OFF_STATES = ("agent_view", "mismatch")
BACK_KEY = "C-\\"
FOOTERS = (
    "↵ open  n new  r restart  s side  [ ] main  < > panel  q detach  ^\\ back",
    "n new  r restart  s side  []<> size  q detach  ^\\ back",
    "n new r restart s side []<> q ^\\back",
)
GLYPHS = {"working": "●", "blocked": "◆", "done": "✓"}
ENTER_KEYS = ("\n", "\r", curses.KEY_ENTER)
BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)


def list_rows(claude: str) -> tuple:
    """(rows of `claude agents --json`, None) on success, (None, one-line error) on any failure. Never raises."""
    try:
        done = subprocess.run(
            [claude, "agents", "--json"], capture_output=True, encoding="utf-8", errors="replace",
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
    def __init__(self, claude: str):
        super().__init__(daemon=True)
        self.claude = claude
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.result = None

    def run(self) -> None:
        while True:
            try:
                result = fetch_sessions(self.claude)
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
        self.loaded = False
        self.list_error = ""
        self.notice = ""
        self.notice_error = False
        self.cursor_id = None
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
        self.off_main = None

    def say(self, text: str, error: bool = False) -> None:
        self.notice, self.notice_error = text, error

    def save_prefs(self) -> None:
        try:
            core.save_prefs(self.prefs_file, self.prefs)
        except OSError as exc:
            self.say(f"Couldn't save prefs: {exc}", error=True)

    def layout_panes(self, panes: list) -> tuple:
        """(sidebar, main, spacer) in the sidebar's window. The main slot is whichever pane is neither of the other two, since swap-pane moves pane ids around."""
        mine = next((p for p in panes if p.id == self.me), None)
        if mine is None:
            return None, None, None
        home = [p for p in panes if p.window == mine.window]
        spacer = next((p for p in home if p.role == "spacer"), None)
        main = next((p for p in home if p is not mine and p is not spacer), None)
        return mine, main, spacer

    def session(self, sid: str):
        """The session with this id in the latest successful poll, or None."""
        return next((s for s in self.sessions if s.id == sid), None)

    def cursor_index(self) -> int:
        for i, s in enumerate(self.sessions):
            if s.id == self.cursor_id:
                return i
        return 0

    def housekeep(self, fresh_list: bool) -> None:
        try:
            panes = self.tmux.panes()
            sidebar, main, spacer = self.layout_panes(panes)
            self.main_pane = main
            if sidebar and main and self.bound != (sidebar.id, main.id):
                self.tmux.bind_toggle(BACK_KEY, sidebar.id, main.id)
                self.bound = (sidebar.id, main.id)
            if sidebar and main and spacer:
                self.reconcile_widths(sidebar, main, spacer)
            if fresh_list:
                self.close_stale(panes, main)
            self.check_main(main)
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

    def check_main(self, main: Pane | None) -> None:
        """Flag the main pane once it stops showing the session emx put there: ← opens the agent view, and from there any session."""
        if main is None or main.dead or not main.session:
            self.off_main = None
            return
        state = self.view_state(main)
        if state in OFF_STATES:
            self.off_main = main.id
        elif state == "ok" or self.off_main != main.id:
            self.off_main = None

    def view_state(self, viewer: Pane) -> str:
        session = self.session(viewer.session)
        if session is None:
            return "unknown"
        expected = "" if session.name == session.id else session.name
        return core.main_view_state(self.tmux.capture(viewer.id), expected)

    def main_is_off(self) -> bool:
        return self.main_pane is not None and self.off_main == self.main_pane.id

    def reconcile_widths(self, sidebar: Pane, main: Pane, spacer: Pane) -> None:
        actual = (sidebar.window_width, sidebar.width, main.width)
        action = core.width_action(self.applied, actual)
        if action == "adopt-main":
            self.prefs["main_width"] = main.width
            self.save_prefs()
            self.applied = actual
            return
        if action == "adopt-panel":
            self.prefs["panel_width"] = sidebar.width
            self.save_prefs()
        if action is not None:
            self.apply_layout(sidebar, spacer)

    def apply_layout(self, sidebar: Pane, spacer: Pane) -> None:
        want = core.compute_layout(sidebar.window_width, self.prefs["panel_width"], self.prefs["main_width"])
        self.tmux.resize_width(sidebar.id, want.panel)
        self.tmux.resize_width(spacer.id, want.spacer)
        sidebar, main, _ = self.layout_panes(self.tmux.panes())
        if sidebar and main:
            self.applied = (sidebar.window_width, sidebar.width, main.width)

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

    def switch_to(self, sid: str) -> None:
        try:
            panes = self.tmux.panes()
            _, main, _ = self.layout_panes(panes)
            if main is None:
                self.say("The main pane is missing; restart emx.", error=True)
                return
            viewer = next((p for p in panes if p.session == sid), None)
            command = core.viewer_command(self.claude, sid)
            if viewer is None:
                pane_id = self.tmux.new_hidden_window(command)
                self.tmux.set_pane_option(pane_id, SESSION_OPT, sid)
            else:
                pane_id = viewer.id
                if viewer.dead or pane_id == self.off_main or self.view_state(viewer) in OFF_STATES:
                    if not self.reopen_allowed(sid):
                        return
                    self.tmux.respawn(pane_id, command)
                    self.off_main = None
            if pane_id != main.id:
                self.tmux.swap(pane_id, main.id)
            self.tmux.select(pane_id)
            self.say("")
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
        self.housekeep(fresh_list=False)

    def reopen_allowed(self, sid: str) -> bool:
        """`claude attach` on a stopped session resumes it, so emx reattaches only sessions the latest poll listed."""
        if self.session(sid) is not None:
            return True
        self.say(f"Session {sid} isn't in the latest list, so emx won't reopen it.", error=True)
        return False

    def restart_viewer(self) -> None:
        main = self.main_pane
        if main is None or not main.session:
            self.say("No session is open in the main pane.")
            return
        if not self.reopen_allowed(main.session):
            return
        try:
            self.tmux.respawn(main.id, core.viewer_command(self.claude, main.session))
            self.off_main = None
            self.tmux.select(main.id)
            self.say("Viewer restarted.")
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)

    def flip_side(self) -> None:
        try:
            sidebar, _, spacer = self.layout_panes(self.tmux.panes())
            if sidebar is None or spacer is None:
                return
            self.tmux.swap(sidebar.id, spacer.id)
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
            return
        self.prefs = core.flip_side(self.prefs)
        self.save_prefs()
        self.applied = None
        self.housekeep(fresh_list=False)

    def change_width(self, key: str, delta: int) -> None:
        self.prefs = core.adjust_width(self.prefs, key, delta)
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
        self.cursor_id = sid
        self.poller.wake.set()
        self.switch_to(sid)
        if not self.notice_error:
            self.say("Session started.")

    def handle_mouse(self) -> None:
        try:
            _, _, y, _, state = curses.getmouse()
        except curses.error:
            return
        if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            return
        sid = self.row_map.get(y)
        if sid:
            self.cursor_id = sid
            self.switch_to(sid)

    def handle_key(self, key) -> None:
        if key == curses.KEY_MOUSE:
            self.handle_mouse()
        elif key == curses.KEY_RESIZE:
            self.housekeep(fresh_list=False)
        elif self.prompt is not None:
            self.prompt_key(key)
        elif key in (curses.KEY_UP, "k") and self.sessions:
            self.cursor_id = self.sessions[max(0, self.cursor_index() - 1)].id
        elif key in (curses.KEY_DOWN, "j") and self.sessions:
            self.cursor_id = self.sessions[min(len(self.sessions) - 1, self.cursor_index() + 1)].id
        elif key in ENTER_KEYS and self.sessions:
            self.switch_to(self.sessions[self.cursor_index()].id)
        elif key == "n":
            self.open_prompt()
        elif key == "r":
            self.restart_viewer()
        elif key == "s":
            self.flip_side()
        elif key == "[":
            self.change_width("main_width", -core.WIDTH_STEP)
        elif key == "]":
            self.change_width("main_width", core.WIDTH_STEP)
        elif key in ("<", ","):
            self.change_width("panel_width", -core.WIDTH_STEP)
        elif key in (">", "."):
            self.change_width("panel_width", core.WIDTH_STEP)
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
            warning = f"Main isn't showing {name} — press r to go back"
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
        if name == "done":
            return curses.A_DIM
        index = {"working": 1, "blocked": 2, "error": 3}.get(name)
        if index is None or not curses.has_colors():
            return curses.A_BOLD if name == "error" else 0
        return curses.color_pair(index)

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.row_map = {}
        count = f"{len(self.sessions)}" if self.loaded else "…"
        self.put(0, 0, f" emx · sessions ({count})", curses.A_BOLD)
        self.put(1, 0, "─" * w, curses.A_DIM)
        bottom = self.bottom_lines(max(1, w - 1))
        list_top, list_bottom = 2, h - len(bottom)
        if not self.loaded:
            if not self.list_error:
                self.put(list_top, 1, "Loading sessions…", curses.A_DIM)
        elif not self.sessions:
            self.put(list_top, 1, "No background sessions.", curses.A_DIM)
            self.put(list_top + 1, 1, "Press n to start one.", curses.A_DIM)
        else:
            self.draw_rows(list_top, list_bottom, w)
        for i, (text, attr) in enumerate(bottom):
            self.put(list_bottom + i, 0, " " + text, attr)
        scr.noutrefresh()
        curses.doupdate()

    def draw_rows(self, top: int, bottom: int, width: int) -> None:
        per_row = 3
        visible = max(1, (bottom - top) // per_row)
        cursor = self.cursor_index()
        self.scroll = min(max(self.scroll, cursor - visible + 1), cursor)
        main_sid = self.main_pane.session if self.main_pane and not self.main_is_off() else ""
        for slot, session in enumerate(self.sessions[self.scroll:self.scroll + visible]):
            y = top + slot * per_row
            selected = self.scroll + slot == cursor
            base = curses.A_REVERSE if selected else 0
            in_main = session.id == main_sid
            if selected:
                self.put(y, 0, " " * width, base)
                self.put(y + 1, 0, " " * width, base)
            marker = "▶ " if in_main else "  "
            self.put(y, 0, " " + marker + session.name, base | (curses.A_BOLD if in_main else 0))
            glyph = GLYPHS.get(session.state, "·")
            detail = f"{glyph} {core.state_label(session)}"
            folder = core.folder_tail(session.cwd)
            if folder:
                detail += f" · {folder}"
            self.put(y + 1, 0, "   " + detail, base | self.color(session.state))
            self.row_map[y] = self.row_map[y + 1] = session.id

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
        if self.cursor_id not in {s.id for s in sessions}:
            self.cursor_id = sessions[0].id if sessions else None
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
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)


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
