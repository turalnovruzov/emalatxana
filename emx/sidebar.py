"""The live sessions panel: a curses app running in emx's sidebar pane."""

from __future__ import annotations

import copy
import curses
import json
import locale
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path

import emx
from emx import __version__, core, install, notify, remote, routines
from emx.cli import CONFIG, PLACEHOLDER_TEXT, read_changelog, self_command
from emx.engine import AGENT, Engine, allowed_transcript, claude_home
from emx.tmux import (
    AGENT_OPT, HUSH_OPT, OPEN_OPT, RELAY_HOST_OPT, RELAY_OPT, RELAY_PASTE_OPT, REOPEN_OPT, ROLE, SESSION, SESSION_OPT,
    TERMINAL_OPT, VIEW_OPT, Pane, Tmux, TmuxError, is_view_session,
)

POLL_SECONDS = 1.5
HOUSEKEEP_SECONDS = 0.5
FRESH_GRACE_SECONDS = 60
LISTED_WAIT_SECONDS = 5
LISTED_CHECK_SECONDS = 0.5
PROMPT_SETTLE_SECONDS = 1.0
PROMPT_SETTLE_STEP = 0.05
# How long a close waits for a cancelled interrupt to finish its listing and tmux calls before giving up on the close.
INTERRUPT_JOIN_SECONDS = 20
GIT_SECONDS = 5
LISTING_FRESH_SECONDS = 5
MAX_LISTINGS = 4
TRANSCRIPT_SECONDS = 3
SUMMARY_TIMEOUT = 30
SUMMARY_TICK_SECONDS = 1
SUMMARY_CALLS_KEPT = 500
NOTICE_TIMEOUT = 15
NOTICE_TICK_SECONDS = 0.5
SPEAK_TIMEOUT = 60
NOTIFY_STATUS_SECONDS = 60
# The permission prompt waits on him; the helper gives up on its own after 300 s.
AUTHORIZE_TIMEOUT = 310
VOICES_TIMEOUT = 10
BACK_KEY = "C-\\"
COLLAPSE_KEY = "C-q"
PASTE_KEY = "C-v"
RESTORED_NOTICE_SECONDS = 4
MODE_MESSAGES = {core.VOICE_MODE: "Voice mode", core.NOTIFICATION_MODE: "Notification mode"}
WHATS_NEW_SECONDS = 60
UPDATER_TICK_SECONDS = 5
UPDATE_FIRST_SECONDS = 30
UPDATE_MIN_SECONDS = 5
# A relaunch that did not take (the new tree failed to start) is tried again for the same version only after this long.
RELAUNCH_RETRY_SECONDS = 300
ITERM_BUNDLE = core.TERM_PROGRAM_BUNDLES["iTerm.app"]
ITERM_STEAL_FOCUS = b"\x1b]1337;StealFocus\x07"
SUMMARIES_FILE = "summaries.json"
HELP = (
    ("→ Enter", "open the session and go to it"),
    ("←", "back here, from an empty prompt"),
    ("↑↓ j k", "move"),
    ("Enter click", "fold a group; a click opens a session"),
    ("space", "peek at what the session asks or did; ↑↓ next, space or Esc close"),
    ("n", "new session where the cursor is; Tab there picks another folder"),
    ("", "in its folder list ^t stars a favourite; ★ and recent folders list first"),
    ("^x", "interrupt a working session; twice closes it"),
    ("", "on a project header, twice closes the group"),
    ("^f /", "find a session by name or folder"),
    ("^t", "pin to the top (in emx; doesn't keep it running)"),
    ("^r", "rename, via /rename in the session; Tab there suggests a name"),
    ("^s", "by project / by state"),
    ("N", "notifications: the last 20; Enter opens one"),
    ("R", "routines: what ran in the past 24 hours; R or Esc back"),
    ("m", "voice mode or notification mode"),
    ("h", "stop the voice now (voice mode)"),
    ("s", "panel to the other side"),
    ("< >", "narrower / wider panel"),
    ("^\\", "jump to main and back"),
    ("^q", "hide the panel; ^q again, ^\\ or ← brings it back"),
    ("q", "detach; sessions keep running"),
    ("?", "close this help"),
)
HELP_KEY_WIDTH = 13
ENTER_KEYS = ("\n", "\r", curses.KEY_ENTER)
FOCUS_REPORTS_ON, FOCUS_REPORTS_OFF = "\x1b[?1004h", "\x1b[?1004l"
BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)
CTRL_S = "\x13"
CTRL_X = "\x18"
CTRL_F = "\x06"
CTRL_T = "\x14"
CTRL_R = "\x12"
CTRL_U = "\x15"
CONFIRM_FOOTER = "^x to confirm"
RENAME_FOOTER = "Tab suggest · Enter save · Esc cancel"
NAME_ASKING = "Asking Haiku for a name…"
NAME_STILL_FINISHING = "Still finishing an earlier Haiku call; Tab again in a moment."
FIND_HINT = "↑↓ move · Enter open · Esc clear"
HISTORY_HINT = "↑↓ move · Enter open · Esc back"
HISTORY_EMPTY = "No notifications yet."
ROUTINES_SECONDS = 60
ROUTINES_HINT = "↑↓ scroll · R or Esc back"
ROUTINES_TITLE = "Routines · past 24 hours"
ROUTINES_NONE = "No systemd user timers on this machine."
ROUTINES_LOADING = "Reading routines…"
OPEN_GONE = "That session isn't listed any more."
PEEK_KEYS = (" ", "\x1b", curses.KEY_UP, curses.KEY_DOWN, "k", "j")
PEEK_TONES = {"body": "plain", "code": "accent", "dim": "muted"}
PEEK_NO_ROOM = "Too short to peek."
FORGET_TRIES = 3
FRAME_SECONDS = 0.15
KEY_WAIT_MS = 200
REPUSH_SECONDS = 10
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
    "copper": (173, curses.COLOR_YELLOW),
    "remote": (141, curses.COLOR_MAGENTA),
    "idle": (103, curses.COLOR_BLUE),
    "plain": (-1, -1),
}
BAR_BG = {"focused": 24, "unfocused": 236}
CATEGORY_TONE = {core.NEEDS_INPUT: "needs", core.WORKING: "plain", core.DONE: "done", core.STOPPED: "stopped"}


def session_tone(s) -> str:
    return "idle" if core.idle(s) else CATEGORY_TONE[core.category(s)]


def bar_format(parts: list) -> str:
    """The bar's tmux status-format: every `#` and `%` in a part is doubled, so text from a session can never run a command, style the bar or be read as a time."""
    out = []
    for text, tone, bold in parts:
        colour = TONES[tone][0]
        fg = "default" if colour < 0 else f"colour{colour}"
        out.append(f"#[fg={fg},{'bold' if bold else 'nobold'}]" + text.replace("#", "##").replace("%", "%%"))
    return "".join(out) + "#[default]"


class StatusBar:
    """Writes the bar into the tmux status line, only when its text changes, and again after `REPUSH_SECONDS` as a safety net in case a push was lost."""

    def __init__(self, tmux):
        self.tmux = tmux
        self.last = None
        self.at = 0.0

    def push(self, text: str) -> None:
        now = time.monotonic()
        if self.tmux is None or (text == self.last and now - self.at < REPUSH_SECONDS):
            return
        try:
            self.tmux.set_bar(text)
        except TmuxError:
            # Runs every frame: reporting each failure would put an error line up every 200 ms.
            return
        self.last, self.at = text, now


def fetch_sessions(engine: Engine) -> tuple:
    """(emx's own sessions, None), or (None, one-line error). Off the curses thread only: it reads files and asks tmux."""
    return engine.list_sessions()


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


def wait_until_running(engine: Engine, sid: str) -> tuple:
    """(session, None) once emx's listing shows `sid` running, else (None, error) after a few seconds. The listing that finds it running is the fresh one it opens on."""
    deadline = time.monotonic() + LISTED_WAIT_SECONDS
    seen = False
    while True:
        sessions, _ = fetch_sessions(engine)
        session, _ = core.checked_row(sessions, sid)
        if session is not None and session.running:
            return session, None
        seen = seen or session is not None
        if time.monotonic() >= deadline:
            if seen:
                return None, f"Session {sid} started, but it isn't running, so it wasn't opened"
            return None, f"Session {sid} started, but emx doesn't list it, so it wasn't opened"
        time.sleep(LISTED_CHECK_SECONDS)


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


def park_slot(tmux: Tmux, slot: str) -> None:
    """Put the placeholder in a view's slot pane."""
    tmux.respawn(slot, self_command("blank", PLACEHOLDER_TEXT))
    tmux.set_pane_option(slot, ROLE, "placeholder")
    tmux.unset_pane_option(slot, VIEW_OPT)


class Closer(threading.Thread):
    """Closes confirmed sessions one after another off the main thread, each only after a fresh listing still shows it as he saw it.

    Closing ends the session and removes it from emx: a running one's pane
    is killed (every view showing it gets the placeholder first, so the
    layout keeps its two columns), and once a listing shows it no longer
    running its folder under emx's state folder goes. The conversation
    stays in Claude Code's history. `wait_for` holds the Interrupters of any
    of them still running, cancelled when the close began; the close waits
    for them to end, so no Esc lands in a pane being killed. `current` is
    the session being closed now. `result` is (all closed, message, {id:
    closed}).
    """

    def __init__(self, engine: Engine, targets: tuple, wait_for: tuple = (), panel: str = ""):
        super().__init__(daemon=True)
        self.engine, self.targets, self.wait_for = engine, tuple(targets), tuple(wait_for)
        self.panel = panel
        self.current = self.targets[0]
        self.result = None

    def run(self) -> None:
        finished = {}
        try:
            self.result = self.attempt(finished)
        except Exception as exc:
            finished.update((t.id, False) for t in self.targets if t.id not in finished)
            self.result = (False, f"close failed: {core.error_line(exc)}", finished)

    def attempt(self, finished: dict) -> tuple:
        deadline = time.monotonic() + INTERRUPT_JOIN_SECONDS
        what = "it" if len(self.targets) == 1 else "any of them"
        for interrupter in self.wait_for:
            interrupter.join(max(0.0, deadline - time.monotonic()))
            if interrupter.is_alive():
                finished.update((t.id, False) for t in self.targets)
                return False, f"emx is still interrupting {interrupter.interrupt.name}, so it didn't close {what}.", finished
        messages = []
        for confirm in self.targets:
            self.current = confirm
            closed, message = self.close_one(confirm)
            finished[confirm.id] = closed
            messages.append(message)
        return all(finished.values()), " ".join(messages), finished

    def close_one(self, confirm: core.Confirm) -> tuple:
        sessions, _ = fetch_sessions(self.engine)
        session, fresh = core.checked_row(sessions, confirm.id)
        if not core.close_still_applies(confirm, session, fresh):
            return False, core.close_refusal(confirm, session, fresh)
        if session.running:
            try:
                self.end(confirm.id)
            except TmuxError as exc:
                return False, f"tmux: {exc}"
            if not self.wait_until_ended(confirm.id):
                return False, f"{confirm.name} is still running, so emx didn't remove it."
        error = self.forget(confirm.id)
        if error:
            return False, error
        return True, core.closed_text(confirm)

    def end(self, sid: str) -> None:
        """Kill the session's own pane. It sits in a hidden window, never in a view's, so no view loses its layout.

        Every view showing it gets the placeholder first, so none shows its
        viewer's "has closed" line while the kill lands.
        """
        tmux = self.engine.tmux
        for view in tmux.views():
            if view.shows == sid and view.slot:
                park_slot(tmux, view.slot)
        for pane in tmux.panes():
            if pane.session == sid and pane.agent and not pane.dead:
                tmux.kill_pane(pane.id)

    def wait_until_ended(self, sid: str) -> bool:
        deadline = time.monotonic() + LISTED_WAIT_SECONDS
        while True:
            sessions, _ = fetch_sessions(self.engine)
            session, fresh = core.checked_row(sessions, sid)
            if fresh and (session is None or not session.running):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(LISTED_CHECK_SECONDS)

    def forget(self, sid: str) -> str | None:
        """None once the session's folder is gone, else the error. A few tries: the killed claude's last hook can still be writing into the folder as it goes."""
        error = None
        for _ in range(FORGET_TRIES):
            done, error = self.engine.forget(sid)
            if done:
                return None
            time.sleep(LISTED_CHECK_SECONDS)
        return error


class Renamer(threading.Thread):
    """Renames a session off the curses thread by typing `/rename` into its own pane.

    It types only once a fresh listing shows the session running, its pane
    (tagged with its key and agent) shows an empty prompt, and tmux, in the
    same command that types, finds none of that changed and his focus still
    on the panel. A working session is typed into too: Claude Code runs
    `/rename` as a command without ending the turn, queueing it when it must.
    The line goes in without Enter. Enter follows only after a second look
    finds the prompt holding that line alone, again through tmux's check; a
    prompt that changed in between (a dialog opened, a draft added) gets no
    Enter, and the line is cleared if the prompt still holds it alone. Then
    it waits for the listing to show the new name, which the transcript
    reader records from Claude's `custom-title` entry; `named` wakes it.
    `result` is (renamed, message, typed).
    """

    def __init__(self, engine: Engine, tmux: Tmux, rename: core.Rename, panel: str,
                 named: threading.Event | None = None):
        super().__init__(daemon=True)
        self.engine, self.tmux, self.rename, self.panel = engine, tmux, rename, panel
        self.named = named or threading.Event()
        self.result = None

    def run(self) -> None:
        try:
            self.result = self.attempt()
        except Exception as exc:
            self.result = (False, f"rename failed: {core.error_line(exc)}", False)

    def attempt(self) -> tuple:
        sessions, _ = fetch_sessions(self.engine)
        session, fresh = core.checked_row(sessions, self.rename.id)
        refusal = core.rename_listing_refusal(self.rename, session, fresh)
        if refusal:
            return False, refusal, False
        try:
            viewer = core.live_viewer(self.tmux.panes(), self.rename.id)
            if viewer is None:
                return False, core.viewer_closed_text(self.rename), False
            screen = self.read_screen(viewer)
            refusal = core.rename_screen_refusal(self.rename, screen)
            if refusal:
                return False, refusal, False
            gate = core.type_gate(viewer.id, self.panel, self.rename.id, screen.cursor_y, SESSION_OPT, screen.cursor_x)
            if gate is None:
                return False, f"emx can't check {self.rename.old}'s session safely, so it didn't rename it.", False
            report = self.tmux.type_if(
                viewer.id, gate, core.rename_line(self.rename.new), core.TYPED, core.gate_report(SESSION_OPT),
                enter=False,
            )
            refusal = core.gate_refusal(self.rename, report, screen.cursor_y)
            if refusal:
                return False, refusal, False
        except TmuxError as exc:
            return False, f"tmux: {exc}", False
        unsent = self.submit(viewer)
        if unsent is not None:
            return unsent
        name, last = self.wait_for_name()
        prompt_empty = True if name is not None else self.prompt_empty(viewer.id)
        queued = last is not None and core.category(last) == core.WORKING
        done = name is not None or (queued and prompt_empty)
        return done, core.renamed_text(self.rename, name, prompt_empty, queued), True

    def read_screen(self, viewer: Pane) -> core.RenameScreen:
        x, y, shown, width, in_mode = self.tmux.prompt_probe(viewer.id)
        line, below = (self.tmux.cursor_lines(viewer.id, y) + ["", ""])[:2]
        return core.RenameScreen(line, below, x, y, shown, width, in_mode)

    def submit(self, viewer: Pane) -> tuple | None:
        """None once Enter went in on the line alone, else the result for a line left unsent. Nothing here may press Enter on a prompt that changed since the line was typed."""
        unconfirmed = False
        try:
            screen = self.read_screen(viewer)
            deadline = time.monotonic() + PROMPT_SETTLE_SECONDS
            # Claude Code can draw the line in steps, `/rename` with its grey `[name]` hint first, so a line not yet alone is read again.
            while not core.prompt_has_only_line(self.rename, screen) and time.monotonic() < deadline:
                time.sleep(PROMPT_SETTLE_STEP)
                screen = self.read_screen(viewer)
            if core.prompt_has_only_line(self.rename, screen):
                gate = core.type_gate(
                    viewer.id, self.panel, self.rename.id, screen.cursor_y, SESSION_OPT, screen.cursor_x,
                )
                if gate is not None:
                    unconfirmed = True
                    report = self.tmux.submit_if(viewer.id, gate, core.TYPED, core.gate_report(SESSION_OPT))
                    unconfirmed = False
                    if core.gate_refusal(self.rename, report, screen.cursor_y, screen.cursor_x) is None:
                        return None
        except TmuxError:
            pass
        cleared = self.clear_line(viewer)
        return False, core.line_unsent_text(self.rename, cleared, unconfirmed), not cleared

    def clear_line(self, viewer: Pane) -> bool:
        """Whether the typed line was taken back: only while the prompt holds it alone, through tmux's check, and then seen empty."""
        try:
            screen = self.read_screen(viewer)
            if not core.prompt_has_only_line(self.rename, screen):
                return False
            gate = core.type_gate(viewer.id, self.panel, self.rename.id, screen.cursor_y, SESSION_OPT, screen.cursor_x)
            if gate is None:
                return False
            report = self.tmux.clear_if(viewer.id, gate, core.TYPED, core.gate_report(SESSION_OPT))
            if report != core.TYPED:
                return False
            after = self.read_screen(viewer)
            return core.empty_prompt(after.line, after.below, after.cursor_x, after.cursor_shown, after.width)
        except TmuxError:
            return False

    def prompt_empty(self, pane: str) -> bool:
        """Whether the viewer's prompt is empty now; True when tmux can't tell, so only text seen there is reported."""
        try:
            x, y, shown, width, _ = self.tmux.prompt_probe(pane)
            line, below = (self.tmux.cursor_lines(pane, y) + ["", ""])[:2]
        except TmuxError:
            return True
        return core.empty_prompt(line, below, x, shown, width)

    def wait_for_name(self) -> tuple:
        """(the new name or None, the last row of the session seen listed or None)."""
        deadline = time.monotonic() + LISTED_WAIT_SECONDS
        last = None
        while True:
            self.named.set()
            sessions, _ = fetch_sessions(self.engine)
            session, fresh = core.checked_row(sessions, self.rename.id)
            if fresh and session is not None:
                last = session
            name = core.renamed_to(self.rename, session)
            if name is not None or time.monotonic() >= deadline:
                return name, last
            time.sleep(LISTED_CHECK_SECONDS)


class Interrupter(threading.Thread):
    """Interrupts a working session off the curses thread by sending Esc into its own pane, as Claude Code's own Esc does.

    It sends only once a fresh listing shows the session running and
    working, its pane (the one tagged with its key and agent) is not in a
    mode and shows the cursor, and tmux, in the same command that sends,
    finds none of that changed and his focus still on the panel. A session
    with no live pane is not running, so nothing is sent. Then it waits for
    the listing to show the session stopped working. `cancel` is set when a
    close of the session starts: from then on it sends nothing. `result` is
    (stopped, message).
    """

    def __init__(self, engine: Engine, tmux: Tmux, interrupt: core.Interrupt, panel: str):
        super().__init__(daemon=True)
        self.engine, self.tmux, self.interrupt, self.panel = engine, tmux, interrupt, panel
        self.cancel = threading.Event()
        self.result = None

    def run(self) -> None:
        try:
            self.result = self.attempt()
        except Exception as exc:
            self.result = (False, f"interrupt failed: {core.error_line(exc)}")

    def attempt(self) -> tuple:
        interrupt = self.interrupt
        sessions, _ = fetch_sessions(self.engine)
        session, fresh = core.checked_row(sessions, interrupt.id)
        refusal = core.interrupt_listing_refusal(interrupt, session, fresh)
        if refusal == core.NOTHING_TO_INTERRUPT:
            return True, core.nothing_to_interrupt_text(interrupt)
        if refusal:
            return False, refusal
        try:
            pane = core.live_viewer(self.tmux.panes(), interrupt.id)
            if pane is None:
                return False, f"{interrupt.name} isn't running, so emx didn't interrupt it."
            screen = self.screen(pane.id)
            refusal = core.interrupt_screen_refusal(interrupt, screen)
            if refusal:
                return False, refusal
            if self.cancel.is_set():
                return False, ""
            gate = core.type_gate(pane.id, self.panel, interrupt.id, screen.cursor_y, SESSION_OPT, cursor_x=screen.cursor_x)
            if gate is None:
                return False, f"emx can't check {interrupt.name}'s session safely, so it didn't interrupt it."
            report = self.tmux.key_if(pane.id, gate, "Escape", core.TYPED, core.gate_report(SESSION_OPT))
            refusal = core.gate_refusal(interrupt, report, screen.cursor_y, cursor_x=screen.cursor_x)
            if refusal:
                return False, refusal
        except TmuxError as exc:
            return False, f"tmux: {exc}"
        stopped = self.wait_until_stopped()
        if stopped is None:
            return True, core.left_listing_text(interrupt)
        return stopped, core.interrupted_text(interrupt, stopped)

    def screen(self, pane: str) -> core.RenameScreen:
        x, y, shown, width, in_mode = self.tmux.prompt_probe(pane)
        return core.RenameScreen("", "", x, y, shown, width, in_mode)

    def wait_until_stopped(self) -> bool | None:
        """True once a listing shows it stopped working, False if it still is at the deadline, None once a listing that worked no longer shows it."""
        deadline = time.monotonic() + LISTED_WAIT_SECONDS
        while not self.cancel.is_set():
            sessions, _ = fetch_sessions(self.engine)
            session, fresh = core.checked_row(sessions, self.interrupt.id)
            if fresh and session is None:
                return None
            if session is not None and not core.interruptible(session):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(LISTED_CHECK_SECONDS)
        return False


class Starter(threading.Thread):
    """Starts a session off the main thread, so the panel keeps drawing while its window opens.

    It waits for a listing that shows the session's pane running, not for a
    hook: Claude reports nothing until its folder-trust question is answered.
    """

    def __init__(self, engine: Engine, folder: str, message: str):
        super().__init__(daemon=True)
        self.engine, self.folder, self.message = engine, folder, message
        self.result = None

    def run(self) -> None:
        try:
            started, error = self.engine.start(self.folder, self.message)
            session = None
            if started is not None:
                session, error = wait_until_running(self.engine, started.id)
        except Exception as exc:
            session, error = None, f"starting a session failed: {core.error_line(exc)}"
        self.result = (session, error)


class RoutinesReader(threading.Thread):
    """Reads the routines off the main thread: the answer comes from three systemd tools, each of which can be slow."""

    def __init__(self):
        super().__init__(daemon=True)
        self.result = None

    def run(self) -> None:
        try:
            self.result = routines.read(time.time())
        except Exception as exc:
            self.result = (None, core.error_line(exc))


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

    def snapshot(self) -> dict:
        """A copy of the placements resolved so far, leaving `take`'s changed flag alone."""
        with self.lock:
            return dict(self.placements)

    def take(self):
        """A copy of the placements when any resolved since the last take, else None."""
        with self.lock:
            if not self.changed:
                return None
            self.changed = False
            return dict(self.placements)


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


def read_continuation(path: str) -> str | None:
    """The id of the session the transcript at `path` continued into, from the end of the file, or None. Never raises, and never writes."""
    try:
        size = os.stat(path).st_size
        want = min(size, core.CONTINUED_TAIL_BYTES)
        with open(path, "rb") as f:
            f.seek(size - want)
            lines = f.read(want).decode("utf-8", errors="replace").split("\n")
    except (OSError, ValueError):
        return None
    if want < size:
        lines = lines[1:]
    return core.continued_in(lines, os.path.basename(path)[:-len(".jsonl")])


def summary_scratch() -> str:
    """The empty private folder every summary call runs in, one per user so a panel killed mid-run leaves nothing new behind."""
    path = os.path.join(tempfile.gettempdir(), f"emx-summary-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"{path} is not a private folder of yours")
    return path


def summarise(claude: str, excerpt: str, cwd: str) -> tuple:
    """(core.SummaryCall, None) from one `claude -p` call that has Haiku sum up `excerpt`, or (None, one-line error). Never raises."""
    return _run_haiku(core.summary_args(claude), excerpt, cwd)


def suggest_name(claude: str, excerpt: str, cwd: str) -> tuple:
    """(core.SummaryCall, None) from one `claude -p` call that has Haiku suggest a session name from `excerpt`, or (None, one-line error). Never raises."""
    call, error = _run_haiku(core.name_args(claude), excerpt, cwd)
    return (call, error) if error != core.HAIKU_DECLINED else (None, NAME_REFUSED)


def announce(claude: str, excerpt: str, cwd: str, mode: str = core.VOICE_MODE) -> tuple:
    """(core.SummaryCall, None) from one `claude -p` call that has Haiku word a notice from `excerpt`, to be heard or read as `mode` wants, or (None, one-line error). Never raises."""
    return _run_haiku(core.notice_args(claude, mode), excerpt, cwd, NOTICE_TIMEOUT, core.clean_notice)


ASK_UNREADABLE = "Haiku's answer was not NEEDS or DONE"


def classify(claude: str, excerpt: str, cwd: str) -> tuple:
    """(core.VERDICT_NEEDS or core.VERDICT_DONE, None) from one `claude -p` call that has Haiku read the end of a reply, or (None, one-line error). Never raises."""
    call, error = _run_haiku(core.ask_args(claude), excerpt, cwd, core.ASK_TIMEOUT, str.strip)
    if error:
        return None, error
    verdict = core.parse_verdict(call.text)
    return (verdict, None) if verdict else (None, ASK_UNREADABLE)


def say_command() -> str:
    return os.environ.get("EMX_SAY") or "say"


def under_test() -> bool:
    """An emx a live check started: it must never make real sound on his Mac."""
    return os.environ.get("EMX_SHOW_TEST_SESSIONS") == "1"


def list_voices(say: str) -> list:
    """The names of the voices `say` offers, or [] when it can't say. Never raises."""
    try:
        done = subprocess.run(
            [say, "-v", "?"], capture_output=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=VOICES_TIMEOUT,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return core.parse_voices(done.stdout) if done.returncode == 0 else []


def _run_haiku(args: list, text: str, cwd: str, timeout: float = SUMMARY_TIMEOUT, clean=None) -> tuple:
    """The text goes on stdin so text starting with `-` can't be read as a flag, and `cwd` is an empty folder so no CLAUDE.md of his is loaded."""
    started = time.monotonic()
    try:
        done = subprocess.run(
            args, input=text, cwd=cwd, capture_output=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"can't find the claude command ({args[0]})"
    except subprocess.TimeoutExpired:
        return None, "claude -p timed out"
    except (OSError, ValueError) as exc:
        return None, f"can't run claude: {exc}"
    wall_ms = (time.monotonic() - started) * 1000
    if done.returncode != 0:
        call, error = core.parse_summary_output(done.stdout, wall_ms, clean)
        if error and done.stdout.strip().startswith("{"):
            return None, error
        return None, core.first_message(done.stderr) or f"claude -p exited with {done.returncode}"
    return core.parse_summary_output(done.stdout, wall_ms, clean)


def read_requests(path: str) -> tuple:
    """(his requests in the transcript at `path`, oldest first; the index of the first one read after an unread middle, or None; whether the read succeeded). Never raises, and never writes.

    A file over `core.NAME_READ_MAX_BYTES` is read as its head and its tail,
    so a single line longer than its window is lost.
    """
    requests, gap_at = [], None
    try:
        size = os.stat(path).st_size
        with open(path, "rb") as f:
            for start, length, drop_first, drop_last in core.name_read_ranges(size):
                f.seek(start)
                # Split on newlines only: Claude Code writes U+2028 unescaped, and `splitlines` would cut a line there.
                lines = f.read(length).decode("utf-8", errors="replace").split("\n")
                if drop_first:
                    lines, gap_at = lines[1:], len(requests)
                if drop_last:
                    lines = lines[:-1]
                requests += core.user_requests(lines)
    except (OSError, ValueError):
        return None, None, False
    return requests, gap_at, True


def read_latest_request(path: str) -> str:
    """The last request he typed in the transcript at `path`, or "" when the path is not a transcript `allowed_transcript` passes, can't be read, or holds none in its last `core.ASK_REQUEST_MAX_BYTES`. Never raises, and never writes.

    The tail read starts at `core.TRANSCRIPT_FIRST_BYTES` and doubles until a
    request turns up, since one long turn of tool output can bury it.
    """
    try:
        real = allowed_transcript(path)
        if real is None:
            return ""
        with open(real, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            window = core.TRANSCRIPT_FIRST_BYTES
            while True:
                window = min(window, size, core.ASK_REQUEST_MAX_BYTES)
                start = size - window
                f.seek(start)
                # Split on newlines only: Claude Code writes U+2028 unescaped, and `splitlines` would cut a line there.
                lines = f.read(window).decode("utf-8", errors="replace").split("\n")
                found = core.latest_request(lines[1:] if start > 0 else lines)
                if found or start == 0 or window >= core.ASK_REQUEST_MAX_BYTES:
                    return found
                window *= 2
    except (OSError, ValueError, TypeError):
        return ""


NAME_NO_TRANSCRIPT = "emx hasn't found this session's transcript yet."
NAME_UNREADABLE = "emx couldn't read this session's transcript."
NAME_NO_REQUESTS = "No requests of yours in this session yet."
NAME_REFUSED = "Haiku wouldn't suggest a name for this session; type one instead."
NAME_UNFIT = "Haiku's name didn't fit; Tab for another."


class Namer(threading.Thread):
    """Asks Haiku for a name, on Tab in the rename box, off the curses thread. It only reads: the name goes into the box, and only Enter renames.

    `result` is (name or None, error line or None, the call or None).
    """

    def __init__(self, claude: str, path: str | None, sid: str, current: str, unnamed: bool, offered):
        super().__init__(daemon=True)
        self.claude, self.path, self.sid = claude, path, sid
        self.current, self.unnamed, self.offered = current, unnamed, tuple(offered)
        self.result = None

    def run(self) -> None:
        try:
            self.result = self.ask()
        except Exception as exc:
            self.result = (None, f"asking for a name failed: {core.error_line(exc)}", None)

    def ask(self) -> tuple:
        if self.path is None:
            return None, NAME_NO_TRANSCRIPT, None
        requests, gap_at, ok = read_requests(self.path)
        if not ok:
            return None, NAME_UNREADABLE, None
        excerpt = core.name_excerpt(self.current, self.unnamed, requests, gap_at, self.offered)
        if not excerpt:
            return None, NAME_NO_REQUESTS, None
        call, error = suggest_name(self.claude, excerpt, summary_scratch())
        if error:
            return None, error, None
        name = core.clean_name(call.text, self.sid, self.current, self.offered)
        return (name, None, call) if name else (None, NAME_UNFIT, call)


class Transcripts(threading.Thread):
    """Reads each listed session's transcript on its own thread, so a large file or a slow disk never holds up listing or drawing.

    A session is read again at most every TRANSCRIPT_SECONDS, and only when
    its file's size or time changed. The transcript is looked for where
    Claude Code keeps it for the session's folder; failing that, by its file
    name in each project folder, which is retried later, backing off, rather
    than on every pass.

    The name set by `/rename` (his own, or the one emx typed) and the title
    Claude gives an unnamed session are recorded into emx's registry when
    they differ from the row's, each once per value, so the next listing
    carries them; `wake` cuts the wait short while a rename waits for it.

    A session reopened after Claude Code continued it elsewhere can have a
    file with no turn in it; the earlier transcript names it in a
    `continued-in` entry. Then the transcript read is the nearest earlier
    one that says something, found in the same project folder and never
    merged with another, so no request is counted twice. A session whose own
    file says something never looks.
    """

    def __init__(self, root: str, engine: Engine | None = None):
        super().__init__(daemon=True)
        self.root, self.engine = root, engine
        self.wake = threading.Event()
        self.recorded = {}
        self.names = {}
        self.lock = threading.Lock()
        self.wanted = {}
        self.found = {}
        self.stamps = {}
        self.paths = {}
        self.misses = {}
        self.reads = {}
        self.links = {}
        self.previous = {}
        self.link_misses = {}
        self.reached = set()
        self.touched = set()
        self.changed = False

    def want(self, sessions) -> None:
        with self.lock:
            self.wanted = {s.id: (s.cwd, s.session_id, s.transcript) for s in sessions if s.session_id}
            self.names = {s.id: (s.name, s.named) for s in sessions}

    def run(self) -> None:
        while True:
            try:
                self.read_all()
            except Exception:
                pass
            self.wake.wait(TRANSCRIPT_SECONDS)
            self.wake.clear()

    def read_all(self) -> None:
        with self.lock:
            wanted, names = dict(self.wanted), dict(self.names)
        self.reached, self.touched = set(), set()
        for sid, (cwd, uuid, hinted) in wanted.items():
            try:
                self.read_one(sid, cwd, uuid, names.get(sid), hinted)
            except Exception:
                continue
        for sid in set(self.recorded) - set(wanted):
            del self.recorded[sid]
        uuids = {uuid for _, uuid, _ in wanted.values()}
        for uuid in set(self.paths) - uuids:
            del self.paths[uuid]
        for uuid in set(self.misses) - uuids:
            del self.misses[uuid]
        for table in (self.previous, self.link_misses):
            for uuid in set(table) - self.reached:
                del table[uuid]
        for path in set(self.reads) - self.touched:
            del self.reads[path]
        with self.lock:
            gone = (set(self.found) | set(self.stamps)) - set(wanted)
            for sid in gone:
                self.stamps.pop(sid, None)
                if self.found.pop(sid, None) is not None:
                    self.changed = True

    def read_one(self, sid: str, cwd: str, uuid: str, shown: tuple | None = None, hinted: str = "") -> None:
        path = self.locate(cwd, uuid, hinted)
        if path is None:
            return
        kept = self.read_file(path)
        if kept is None:
            return
        if kept[2] is not None and shown is not None:
            self.record_names(sid, kept[2], *shown)
        # A file holding only titles says nothing about what the session did.
        found = kept[2] if kept[2] is not None and kept[2].latest else None
        seen, current, hops = {path}, path, 0
        while found is None and hops < core.MAX_CONTINUATION_HOPS:
            hops += 1
            earlier = self.predecessor(os.path.dirname(current), os.path.basename(current)[:-len(".jsonl")])
            if earlier is None or earlier in seen:
                break
            seen.add(earlier)
            read = self.read_file(earlier)
            if read is None:
                break
            current = earlier
            if read[2] is not None and read[2].latest:
                kept, found = read, read[2]
        path, stamp, _ = kept
        with self.lock:
            self.stamps[sid] = (path, *stamp)
            if self.found.get(sid) != found:
                self.found[sid] = found
                self.changed = True

    def record_names(self, sid: str, found, name: str, named: bool) -> None:
        """Record the transcript's latest `/rename` name, or for an unnamed session Claude's title, when the row shows another. A value written once is not written again, though the listing may not show it yet."""
        if self.engine is None:
            return
        done = self.recorded.setdefault(sid, {})
        if found.title and found.title != name and done.get("name") != found.title:
            if self.engine.set_name(sid, found.title)[0]:
                done["name"] = found.title
        elif not named and found.ai_title and found.ai_title != name and done.get("title") != found.ai_title:
            if self.engine.set_title(sid, found.ai_title)[0]:
                done["title"] = found.ai_title

    def read_file(self, path: str) -> tuple | None:
        """(path, (mtime_ns, size), what its end says) for the transcript at `path`, read again only when its stat changed; None when it can't be read."""
        info = os.stat(path)
        stamp = (info.st_mtime_ns, info.st_size)
        self.touched.add(path)
        cached = self.reads.get(path)
        if cached is not None and cached[0] == stamp:
            return path, stamp, cached[1]
        found, ok = read_transcript(path, info.st_size)
        if not ok:
            return None
        self.reads[path] = (stamp, found)
        return path, stamp, found

    def predecessor(self, folder: str, uuid: str) -> str | None:
        """The transcript in `folder` whose `continued-in` entry names `uuid`, or None. Only this folder is looked in, a link once found is kept, and a miss backs off.

        Candidates are taken in name order and the first to name `uuid` wins,
        so two that claim the same session resolve the same way every pass.
        Each candidate's end is read again only when its stat changed.
        """
        self.reached.add(uuid)
        known = self.previous.get(uuid)
        if known and os.path.isfile(known):
            return known
        tries, after = self.link_misses.get(uuid, (0, 0.0))
        if after > time.monotonic():
            return None
        # Recorded before the scan, so a scan that raises backs off too.
        self.link_misses[uuid] = (tries + 1, time.monotonic() + core.retry_after(tries + 1))
        with os.scandir(folder) as entries:
            names = sorted(e.name for e in entries if e.name.endswith(".jsonl") and e.is_file())
        paths = [os.path.join(folder, n) for n in names]
        for path in set(self.links) - set(paths):
            if os.path.dirname(path) == folder:
                del self.links[path]
        for name, path in zip(names, paths):
            if name == uuid + ".jsonl" or not core.is_session_id(name[:-len(".jsonl")]):
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            stamp = (info.st_mtime_ns, info.st_size)
            cached = self.links.get(path)
            if cached is None or cached[0] != stamp:
                cached = self.links[path] = (stamp, read_continuation(path))
            if cached[1] == uuid:
                self.previous[uuid] = path
                self.link_misses.pop(uuid, None)
                return path
        return None

    def locate(self, cwd: str, uuid: str, hinted: str = "") -> str | None:
        """The session's transcript: first the path its hooks reported, only when it lies under Claude's projects folder and is named for this session; then where Claude Code keeps it for the folder; then by its file name in any project folder."""
        if not core.is_session_id(uuid):
            return None
        name = uuid + ".jsonl"
        if hinted and os.path.basename(hinted) == name:
            real = os.path.realpath(hinted)
            if real.startswith(os.path.realpath(os.path.join(self.root, "projects")) + os.sep) and os.path.isfile(real):
                return real
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

    def path(self, sid: str) -> str | None:
        """The transcript file last read for a listed session, or None while none has been."""
        with self.lock:
            stamp = self.stamps.get(sid)
        return stamp[0] if stamp else None

    def snapshot(self) -> dict:
        """A copy of what each session's transcript says now, leaving `take`'s changed flag alone."""
        with self.lock:
            return dict(self.found)

    def take(self):
        """A copy of what each session's transcript says when any of it changed since the last take, else None."""
        with self.lock:
            if not self.changed:
                return None
            self.changed = False
            return dict(self.found)


class Summaries(threading.Thread):
    """Has Haiku write each session's row line, one call at a time on its own thread, so a slow call never holds up listing or drawing.

    A session is summarised again only when what Haiku would be shown
    changed, at most every `core.SUMMARY_EVERY_SECONDS` unless its state
    changed. A failed call keeps the earlier line and backs off.
    """

    def __init__(self, claude: str, transcripts: Transcripts):
        super().__init__(daemon=True)
        self.claude = claude
        self.transcripts = transcripts
        self.lock = threading.Lock()
        self.wanted = {}
        self.found = {}
        self.calls = []
        self.changed = False
        self.failures = {}
        self.tried = {}
        self.scratch = None

    def want(self, sessions) -> None:
        with self.lock:
            self.wanted = {s.id: s for s in sessions}

    def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception:
                pass
            time.sleep(SUMMARY_TICK_SECONDS)

    def step(self) -> None:
        """Summarises the one session most overdue, if any is due."""
        now = time.monotonic()
        with self.lock:
            wanted = dict(self.wanted)
            found = dict(self.found)
        transcripts = self.transcripts.snapshot()
        due = []
        for sid, session in wanted.items():
            excerpt = core.summary_excerpt(session, transcripts.get(sid))
            count, failed_at = self.failures.get(sid, (0, None))
            if core.summary_due(session, excerpt, found.get(sid), count, failed_at, now, transcripts.get(sid)):
                due.append((self.tried.get(sid, 0.0), sid, session, excerpt))
        with self.lock:
            for sid in set(self.found) - set(wanted):
                del self.found[sid]
                self.changed = True
        for table in (self.failures, self.tried):
            for sid in set(table) - set(wanted):
                del table[sid]
        if not due:
            return
        _, sid, session, excerpt = min(due, key=lambda d: d[0])
        state = core.summary_state(session, transcripts.get(sid))
        self.tried[sid] = now
        try:
            if self.scratch is None:
                self.scratch = summary_scratch()
            call, error = summarise(self.claude, excerpt, self.scratch)
        except OSError as exc:
            call, error = None, f"no private folder to run in: {exc}"
        with self.lock:
            if call is None:
                count = self.failures.get(sid, (0, None))[0]
                self.failures[sid] = (count + 1, time.monotonic())
                return
            self.failures.pop(sid, None)
            self.calls = (self.calls + [call])[-SUMMARY_CALLS_KEPT:]
            if sid in self.wanted:
                # Kept even when empty, so the unchanged excerpt isn't asked about again every tick.
                self.found[sid] = core.Summary(call.text, state, excerpt, time.monotonic())
                self.changed = True

    def take(self):
        """A copy of each session's line when any changed since the last take, else None."""
        with self.lock:
            if not self.changed:
                return None
            self.changed = False
            return dict(self.found)


class Asks(threading.Thread):
    """Has Haiku say whether a session that stopped with a reply needs him, one call at a time on its own thread, and records the verdict.

    A stop is asked about once. A failed call backs off as summaries do, and
    after `core.ASK_MAX_TRIES` failures the stop is recorded
    `VERDICT_UNKNOWN`, which reads Done and lets its Done notice go out.
    """

    def __init__(self, engine: Engine, wake: threading.Event):
        super().__init__(daemon=True)
        self.engine = engine
        self.wake = wake
        self.lock = threading.Lock()
        self.wanted = {}
        self.failures = {}
        self.tried = {}
        self.settled = set()
        self.scratch = None

    def want(self, sessions) -> None:
        with self.lock:
            self.wanted = {(s.id, s.stop_key): s for s in sessions if s.asking}

    def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception:
                pass
            time.sleep(SUMMARY_TICK_SECONDS)

    def step(self) -> None:
        """Asks about the stop tried longest ago whose backoff has passed, if any."""
        now = time.monotonic()
        with self.lock:
            wanted = dict(self.wanted)
        for table in (self.failures, self.tried):
            for stop in set(table) - set(wanted):
                del table[stop]
        self.settled &= set(wanted)
        due = []
        for stop, session in wanted.items():
            count, failed_at = self.failures.get(stop, (0, None))
            if stop in self.settled or (failed_at is not None and now - failed_at < core.retry_after(count)):
                continue
            due.append((self.tried.get(stop, 0.0), stop, session))
        if not due:
            return
        _, stop, session = min(due, key=lambda d: d[0])
        self.tried[stop] = now
        try:
            if self.scratch is None:
                self.scratch = summary_scratch()
            request = read_latest_request(session.transcript)
            verdict, _ = classify(self.engine.claude, core.ask_excerpt(session, request), self.scratch)
        except OSError:
            verdict = None
        if verdict is None:
            count = self.failures.get(stop, (0, None))[0] + 1
            if count < core.ASK_MAX_TRIES:
                self.failures[stop] = (count, time.monotonic())
                return
            verdict = core.VERDICT_UNKNOWN
        # Settled even when the write fails: the session's folder is gone or unwritable, and asking again won't mend it.
        self.engine.set_verdict(session.id, session.stop_key, verdict)
        self.settled.add(stop)
        self.failures.pop(stop, None)
        self.wake.set()


class Voice(threading.Thread):
    """Speaks notices with `say`, one at a time on its own thread, so two voices never overlap and a slow `say` never holds up drawing."""

    def __init__(self):
        super().__init__(daemon=True)
        self.say = say_command()
        self.voice = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = []
        self.generation = 0
        self.playing = None
        self.muted = False
        self.hushed_at = float("-inf")
        self.fallback_post = None

    def speak(self, text: str, at: float | None = None, fallback: tuple | None = None) -> None:
        """Dropped while muted, so a mute between a notice being read and spoken lets nothing through. `at` is when the notice's transition happened: a hush after that drops it too.

        `fallback` is (title, plain line, session id): what `fallback_post` is
        given when `say` fails, so voice mode still reaches him as a notification.
        """
        if not self.muted:
            self.put(("say", text, fallback and (*fallback, at)), at)

    def put(self, item: tuple, at: float | None = None) -> None:
        with self.lock:
            if self.muted or (at is not None and at <= self.hushed_at):
                return
            self.queue.append(item)
        self.wake.set()

    def silence(self) -> None:
        """Stop what is being said and drop the speech queued. A notice whose transition came before this is dropped too, if it is still being worded."""
        with self.lock:
            self.hushed_at = time.monotonic()
            self.generation += 1
            self.queue = []
            proc = self.playing
        if proc is not None:
            self.stop(proc)

    @staticmethod
    def stop(proc) -> None:
        try:
            proc.terminate()
        except OSError:
            pass

    def run(self) -> None:
        try:
            if under_test() and not os.environ.get("EMX_SAY"):
                raise OSError("no real say under test")
            self.voice = core.pick_voice(list_voices(self.say))
        except Exception:
            self.voice = None
        while True:
            try:
                self.step()
            except Exception:
                pass

    def step(self) -> None:
        """Plays the oldest queued item, or waits for one."""
        with self.lock:
            item = self.queue.pop(0) if self.queue else None
            generation = self.generation
        if item is None:
            self.wake.wait()
            self.wake.clear()
            return
        spoken = self.speak_aloud(item[1], generation)
        with self.lock:
            hushed = generation != self.generation
        fallback = item[2] if len(item) > 2 else None
        if not spoken and not hushed and fallback and self.fallback_post is not None:
            self.fallback_post(*fallback)

    def speak_aloud(self, text: str, generation: int) -> bool:
        """False when `say` could not speak it: it would not start, timed out or exited non-zero, a stop by `silence` aside. The text goes on stdin, never in the command line, so a text starting with `-` can't be read as a flag."""
        if under_test() and not os.environ.get("EMX_SAY"):
            return True
        argv = [self.say] + (["-v", self.voice] if self.voice else [])
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace",
            )
        except (OSError, ValueError):
            return False
        with self.lock:
            # A silence that came while this one was starting must still stop it.
            muted = generation != self.generation
            if not muted:
                self.playing = proc
        spoken = True
        try:
            if muted:
                self.stop(proc)
            proc.communicate("" if muted else text, timeout=SPEAK_TIMEOUT)
        except subprocess.TimeoutExpired:
            spoken = False
            proc.kill()
            proc.communicate()
        except (OSError, ValueError):
            spoken = False
        finally:
            with self.lock:
                if self.playing is proc:
                    self.playing = None
                silenced = generation != self.generation
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return silenced or (spoken and proc.returncode == 0)


class Notifier(threading.Thread):
    """Shows notifications through emx-notify.app, one helper call at a time on its own thread: builds the helper, asks the permission, posts, and keeps `state` fresh.

    Building it here does nothing: only `run` builds or calls the helper. An
    emx a live check started (`under_test`) never builds one, and posts only
    to the command `EMX_NOTIFY` names, or nowhere.
    """

    def __init__(self, socket: str = "emx"):
        super().__init__(daemon=True)
        self.socket = socket
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = []
        self.mode = core.NOTIFICATION_MODE
        self.command = None
        self.app = None
        self.asking = False
        self.asked = False
        self.checked_at = None
        self.state = core.HELPER_PENDING
        self.undecided = True

    def settle(self, report, error) -> None:
        """Takes the helper's answer as `state`. A permission still undecided after it was asked for counts as blocked, so an unanswered prompt does not leave the mode silently dead; `undecided` keeps the helper's real answer, so a later switch into notification mode asks again."""
        state = notify.helper_state(report, error)
        self.undecided = state == core.HELPER_PENDING
        self.state = core.HELPER_BLOCKED if state == core.HELPER_PENDING and self.asked else state

    def post(self, title: str, body: str, sid: str, at: float | None = None) -> None:
        with self.lock:
            self.queue.append((title, body, sid))
        self.wake.set()

    def authorize(self) -> None:
        """Ask the permission once it is known not to be settled; nothing happens when it is."""
        with self.lock:
            self.asking = True
        self.wake.set()

    def run(self) -> None:
        try:
            self.prepare()
        except Exception:
            self.command = None
            self.state = core.HELPER_UNAVAILABLE
        while True:
            try:
                self.step()
            except Exception:
                pass

    def prepare(self) -> None:
        env = os.environ
        if env.get("EMX_NOTIFY"):
            self.command = env["EMX_NOTIFY"]
        elif not under_test():
            home = notify.helper_home(env)
            app, _ = notify.ensure_built(home, notify.bundle_id(env)) if home is not None else (None, None)
            self.command = notify.helper_command(env, home) if app is not None else None
            self.app = app
        if self.command is None:
            self.state = core.HELPER_UNAVAILABLE
            return
        self.check()
        with self.lock:
            if self.mode == core.NOTIFICATION_MODE:
                self.asking = True

    def check(self) -> None:
        self.checked_at = time.monotonic()
        report, error = notify.run(self.command, "status")
        self.settle(report, error)

    def step(self) -> None:
        """Asks the permission when wanted, else posts the oldest queued notification, else refreshes `state` every NOTIFY_STATUS_SECONDS."""
        with self.lock:
            asking, self.asking = self.asking and self.undecided, False
            item = self.queue.pop(0) if self.queue and not asking else None
        if self.command is None:
            with self.lock:
                self.queue = []
            self.wake.wait()
            self.wake.clear()
        elif asking:
            report, error = notify.authorize(self.command, self.app, AUTHORIZE_TIMEOUT)
            self.asked = True
            self.settle(report, error)
        elif item is not None:
            title, body, sid = item
            ident = f"emx-{sid}-{time.time_ns()}"
            report, error = notify.run(self.command, "post", notify.payload(title, body, ident, sid, self.socket))
            self.settle(report, error)
            self.checked_at = time.monotonic()
        elif self.checked_at is None or time.monotonic() - self.checked_at >= NOTIFY_STATUS_SECONDS:
            self.check()
        else:
            self.wake.wait(max(0.0, NOTIFY_STATUS_SECONDS - (time.monotonic() - self.checked_at)))
            self.wake.clear()


class Notices(threading.Thread):
    """Words each notice with Haiku, one call at a time on its own thread, and hands the text on for showing and speaking.

    A notice waits until the session's transcript holds the words for its
    state (or `core.NOTICE_SETTLE_SECONDS` pass), and is dropped when the
    listing shows the session in another state by then. A failed call
    never loses it: a plain line takes the place of Haiku's.
    """

    def __init__(self, claude: str, transcripts: Transcripts, resolver: Resolver, speaker: Voice, notifier=None, relay: Path | None = None):
        super().__init__(daemon=True)
        self.claude = claude
        self.transcripts = transcripts
        self.resolver = resolver
        self.speaker = speaker
        self.notifier = notifier
        self.relay = relay
        self.mode = core.NOTIFICATION_MODE
        self.lock = threading.Lock()
        self.wanted = {}
        self.pending = []
        self.outbox = []
        self.calls = []
        self.scratch = None

    def want(self, sessions) -> None:
        with self.lock:
            self.wanted = {s.id: s for s in sessions}

    def add(self, notice: core.Notice) -> None:
        with self.lock:
            self.pending.append(notice)

    def take(self) -> list:
        """The (session id, kind, title, text, transition time) of each notice worded since the last take, oldest first."""
        with self.lock:
            out, self.outbox = self.outbox, []
        return out

    def run(self) -> None:
        while True:
            try:
                worked = self.step()
            except Exception:
                worked = False
            if not worked:
                time.sleep(NOTICE_TICK_SECONDS)

    def session_for(self, notice: core.Notice, wanted: dict):
        """The listed session, or while it is absent from the listing, one built from what the notice remembers."""
        listed = wanted.get(notice.id)
        if listed is not None:
            return listed
        state = "blocked" if notice.kind == core.NOTICE_NEEDS else "done"
        return core.Session(notice.id, notice.name, notice.cwd, state, "", "")

    def stale(self, notice: core.Notice) -> bool:
        with self.lock:
            listed = self.wanted.get(notice.id)
        return listed is not None and core.category(listed) != notice.kind

    def step(self) -> bool:
        """Sends the oldest notice that is ready, if any is. True when it handled one."""
        now = time.monotonic()
        with self.lock:
            pending, wanted = list(self.pending), dict(self.wanted)
        transcripts = self.transcripts.snapshot()
        for notice in pending:
            if self.stale(notice):
                self.drop(notice)
                return True
            session = self.session_for(notice, wanted)
            transcript = transcripts.get(notice.id)
            if not core.notice_ready(notice.kind, session, transcript, now - notice.at):
                continue
            self.send(notice, session, transcript)
            return True
        return False

    def drop(self, notice: core.Notice) -> None:
        with self.lock:
            if notice in self.pending:
                self.pending.remove(notice)

    def send(self, notice: core.Notice, session: core.Session, transcript) -> None:
        with self.lock:
            mode = self.mode
        placements = self.resolver.snapshot()
        project = core.notice_project(session, placements)
        title = core.notice_title(session.name, project)
        plain = core.clean_notice(core.fallback_notice(project, notice.name, notice.named, notice.kind))
        excerpt = core.notice_excerpt(project, session, transcript, notice.named)
        call, text = None, ""
        if excerpt:
            try:
                if self.scratch is None:
                    self.scratch = summary_scratch()
                call, _ = announce(self.claude, excerpt, self.scratch, mode)
            except OSError:
                call = None
            text = core.clean_notice(call.text) if call is not None else ""
        if not text:
            text = plain
        if self.stale(notice):
            self.drop(notice)
            return
        with self.lock:
            if notice not in self.pending:
                return
            self.pending.remove(notice)
            if call is not None:
                self.calls = (self.calls + [call])[-SUMMARY_CALLS_KEPT:]
            self.outbox.append((notice.id, notice.kind, title, text, notice.at))
            mode = self.mode
        if self.relay is not None:
            remote.append_relay(self.relay, core.relay_notice(title, text, plain, notice.id, mode, time.time()))
        if not text:
            return
        if mode == core.VOICE_MODE:
            self.speaker.speak(text, notice.at, fallback=(title, plain, notice.id))
        elif self.notifier is not None:
            self.notifier.post(title, text, notice.id, notice.at)


class Poller(threading.Thread):
    """Lists sessions, and hands each folder to the resolver, and each session to the transcript reader, without waiting for either.

    Only the `primary` panel's poller runs Haiku and the notices: the panel of
    every other view lists and reads transcripts for itself, and takes the
    row lines from the file the primary writes.
    """

    def __init__(self, engine: Engine, primary: bool = True):
        super().__init__(daemon=True)
        self.engine = engine
        self.primary = primary
        self.started = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.result = None
        self.resolver = Resolver(os.path.realpath(os.path.expanduser("~")))
        self.transcripts = Transcripts(claude_home(), engine)
        self.summaries = Summaries(engine.claude, self.transcripts)
        self.asks = Asks(engine, self.wake)
        self.notifier = Notifier(getattr(getattr(engine, "tmux", None), "socket", None) or "emx")
        self.voice = Voice()
        self.voice.fallback_post = self.notifier.post
        self.notices = Notices(engine.claude, self.transcripts, self.resolver, self.voice, self.notifier, core.relay_path(engine.state))

    def poll(self) -> tuple:
        sessions, error = fetch_sessions(self.engine)
        if error:
            return None, error
        if sessions:
            self.resolver.want(s.cwd for s in sessions)
            self.transcripts.want(sessions)
            if self.primary:
                self.summaries.want(sessions)
                self.asks.want(sessions)
                self.notices.want(sessions)
        return sessions, None

    def workers(self) -> list:
        mine = [self.resolver, self.transcripts]
        if self.primary:
            mine += [self.summaries, self.asks, self.notifier, self.voice, self.notices]
        return mine

    def run(self) -> None:
        for worker in self.workers():
            worker.start()
            self.started.append(worker)
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


def update_interval(env) -> float:
    try:
        seconds = int(env.get("EMX_UPDATE_SECONDS", ""))
    except ValueError:
        return core.UPDATE_CHECK_SECONDS
    return seconds if seconds >= UPDATE_MIN_SECONDS else core.UPDATE_CHECK_SECONDS


class Updater(threading.Thread):
    """Checks the installed emx for a newer version off the curses thread and unpacks it; the panel decides when to switch to it.

    `take()` hands (version `current` names, newest major waiting, last
    error) whenever one of them changed, else None.
    """

    def __init__(self, interval: float | None = None):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.interval = update_interval(os.environ) if interval is None else interval
        self.next_check = time.monotonic() + UPDATE_FIRST_SECONDS
        self.pruned = False
        self.major = None
        self.error = None
        self.on_disk = None
        self.published = None
        self.result = None
        self.stopping = threading.Event()

    def step(self, now: float) -> None:
        root, running = emx.ROOT, emx.RUNNING
        self.on_disk = install.current(root)
        if self.on_disk == running and not self.pruned:
            self.pruned = True
            install.prune(root, running)
        if now >= self.next_check and install.updates_on(root):
            self.next_check = now + self.interval
            result, error = install.update(root, running, allow_major=False, apply_only_if_running_current=True, wait=False)
            if error != install.BUSY:
                self.error = error
                if result:
                    self.major = result[2]
                self.on_disk = install.current(root)
        self.publish()

    def publish(self) -> None:
        result = (self.on_disk, self.major, self.error)
        if result != self.published:
            self.published = result
            with self.lock:
                self.result = result

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.step(time.monotonic())
            except Exception as exc:
                self.error = f"checking for updates failed: {core.error_line(exc)}"
                self.publish()
            self.stopping.wait(UPDATER_TICK_SECONDS)

    def take(self):
        with self.lock:
            result, self.result = self.result, None
        return result


class App:
    def __init__(self, scr, tmux: Tmux, claude: str):
        self.scr = scr
        self.tmux = tmux
        self.claude = claude
        self.state = core.state_path()
        self.relay_file = core.relay_path(self.state)
        self.relayed = False
        self.relay_host = None
        self.legacy_cleared = False
        self.ground_reset = False
        self.window_width = 0
        self.bar = StatusBar(tmux)
        self.me = os.environ.get("TMUX_PANE", "")
        # The panel of `emx` itself, the laptop's first view, runs what is done once per workspace; any other view's is a follower.
        self.primary = getattr(tmux, "view", SESSION) == SESSION
        self.prefs_file = core.prefs_path()
        self.prefs_mtime = self.file_mtime(self.prefs_file)
        self.prefs = core.load_prefs(self.prefs_file)
        self.prefs_base = copy.deepcopy(self.prefs)
        self.summaries_file = self.state / SUMMARIES_FILE
        self.summaries_mtime = None
        self.attached = None
        self.own_width = None
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
        self.held_cursor = None
        self.bound = None
        self.prompt = None
        self.row_map = {}
        self.scroll = 0
        self.last_housekeep = 0.0
        self.poller = Poller(Engine(tmux, claude, self.state), primary=self.primary)
        self.apply_mode()
        self.notice_seen = None
        self.hush_seen = None
        self.history = []
        self.history_misses = {}
        self.history_view = None
        self.history_scroll = 0
        self.routines_view = None
        self.routine_views = None
        self.routines_error = None
        self.routines_reader = None
        self.routines_read_at = 0.0
        self.opened = {}
        self.listings = Listings()
        self.home = os.path.expanduser("~")
        self.starter = None
        self.started = None
        self.closer = None
        self.interrupters = {}
        self.closing = {}
        self.confirm = None
        self.find = None
        self.rename_edit = None
        self.rename_gen = 0
        self.renamer = None
        self.namer = None
        self.namer_gen = None
        self.pin_misses = {}
        self.rename_misses = {}
        self.focused = None
        self.pending_keys = []
        self.transcripts = {}
        self.summaries = {}
        self.peeking = False
        self.peek_wrapped = (None, [])
        self.whats_new = None
        self.whats_new_waits = False
        self.updater = Updater() if emx.ROOT and self.primary else None
        self.on_disk = None
        self.majors_said = set()
        self.relaunched = {}
        self.note_new_version()

    @property
    def engine(self) -> Engine:
        return Engine(self.tmux, self.claude, self.state)

    def note_new_version(self) -> None:
        """Record the version this panel runs, showing its changelog lines when it is newer than the one recorded."""
        due = core.whats_new_due(self.prefs.get("last_version"), __version__)
        if due is None:
            return
        text = core.whats_new_since(read_changelog(), self.prefs.get("last_version"), __version__) if due == core.SHOW_NEW else ""
        if text:
            self.whats_new = core.updated_heading(__version__) + "\n" + text
            # Its timer starts once someone is attached, so a server that updated alone still shows it when he comes back.
            self.whats_new_waits = True
            self.say(self.whats_new)
        self.prefs = {**self.prefs, "last_version": __version__}
        # Saved after the note, so a failure to save replaces it with the error.
        self.save_prefs()

    def say(self, text: str, error: bool = False, seconds: float | None = None) -> None:
        """Show a notice; with `seconds` it goes by itself after that long, unless another replaces it first."""
        self.notice, self.notice_error = text, error
        self.notice_until = time.monotonic() + seconds if seconds is not None else None

    @staticmethod
    def file_mtime(path) -> float | None:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def save_prefs(self) -> None:
        """Write this panel's changes over what is on file now, so a change another view's panel saved meanwhile is kept."""
        try:
            merged = core.merge_prefs(self.prefs_base, self.prefs, core.load_prefs(self.prefs_file))
            core.save_prefs(self.prefs_file, merged)
        except OSError as exc:
            self.say(f"Couldn't save prefs: {exc}", error=True)
            return
        self.prefs, self.prefs_base = merged, copy.deepcopy(merged)
        self.prefs_mtime = self.file_mtime(self.prefs_file)

    def reload_prefs(self) -> None:
        """Take in what another view's panel saved, keeping any change of this panel's own not saved yet."""
        mtime = self.file_mtime(self.prefs_file)
        if mtime == self.prefs_mtime:
            return
        self.prefs_mtime = mtime
        disk = core.load_prefs(self.prefs_file)
        self.prefs = core.merge_prefs(self.prefs_base, self.prefs, disk)
        self.prefs_base = copy.deepcopy(disk)
        self.apply_mode()
        self.regroup()

    def my_panes(self) -> list:
        """The panes of this panel's own view window."""
        return self.tmux.window_panes(self.me) if self.me else []

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
        before = self.items()
        self.groups = self.grouped()
        held = self.held_cursor
        self.held_cursor = core.hold_cursor(held, bool(self.sessions), self.cursor, before)
        if held and self.sessions:
            self.cursor = core.settle_cursor(self.items(), held[0], self.groups, self.wanted_cursor, held[1])
        else:
            self.cursor = core.settle_cursor(self.items(), self.cursor, self.groups, self.wanted_cursor, before)
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
        elif item[1] in self.closing:
            # Opening a session that is being stopped would resume it.
            shown = self.session(item[1])
            self.say(core.still_closing(shown.name if shown else item[1]))
        else:
            self.switch_to(item[1], core.OPEN)
            self.remember_open(item[1])

    def remember_open(self, sid: str) -> None:
        session = self.session(sid)
        if session is None or not session.cwd:
            return
        folder = core.opened_folder(session.cwd, self.placements.get(session.cwd))
        new = core.remember_opened(self.prefs, folder)
        if new is not self.prefs:
            self.prefs = new
            self.save_prefs()

    def housekeep(self, fresh_list: bool) -> None:
        self.reload_prefs()
        try:
            sidebar, main = self.layout_panes(self.my_panes())
            if self.primary and not self.legacy_cleared:
                main = self.clear_legacy_viewers(main)
            self.main_pane = main
            if sidebar and main and not self.bound:
                self.bind_keys()
            if sidebar:
                self.window_width = sidebar.window_width
                self.reset_ground(sidebar)
                self.take_focus(sidebar)
                # The hidden panel saved in prefs is the laptop's, the home view's; another view's stays its own.
                if self.primary:
                    followed = core.follow_zoom(self.prefs, sidebar.zoomed)
                    if followed is not self.prefs:
                        self.prefs = followed
                        self.save_prefs()
            if sidebar and main:
                self.reconcile_widths(sidebar)
                self.tend_slot(main)
            if fresh_list and self.primary:
                self.close_stale(self.main_pane)
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
        if self.primary:
            self.check_hush()
        self.check_relay()
        self.check_open()
        self.time_whats_new()
        self.check_update()

    def bind_keys(self) -> None:
        """emx's root keys, the same for every view: they act on the window they are pressed in, so binding them again changes nothing."""
        socket = self.tmux.socket
        self.tmux.bind_toggle(BACK_KEY)
        self.tmux.bind_left(self_command("left", socket))
        self.tmux.bind_collapse(COLLAPSE_KEY)
        self.tmux.bind_paste(PASTE_KEY, self_command("paste", socket, str(self.relay_file)))
        self.tmux.bind_copy(self_command("copied", socket))
        self.tmux.bind_menu()
        self.bound = True

    def tend_slot(self, slot: Pane) -> None:
        """Keep the slot showing something that is there, and hand it back and forth with the view's clients.

        A viewer whose session's pane has gone, or that has exited, gives way
        to the placeholder. A view other than `emx` with no client attached
        parks what it shows in REOPEN_OPT, so a device that went away holds no
        claim on that session's width; when a client attaches again, or at
        this panel's start with one attached (an upgrade respawns the panel
        under an attached laptop), the session is shown again. The home view
        keeps its viewer while the laptop is away, so the session is there the
        moment he comes back, as before 2.10.0; on his return its viewer
        claims the session's width back from a device that used it meanwhile.
        Showing it again never starts a session's process.
        """
        if slot.role == "viewer" and slot.view:
            held = core.live_or_dead_agent(self.tmux.panes(), slot.view)
            if slot.dead or held is None:
                self.park(slot)
                slot = self.main_pane
        attached = self.tmux.client_state()[0]
        was, self.attached = self.attached, attached
        if not attached:
            if not self.primary and slot.role == "viewer" and slot.view:
                self.tmux.set_view_option(REOPEN_OPT, slot.view)
                self.park(slot)
            return
        if was:
            return
        if self.primary and slot.role == "viewer" and slot.view:
            try:
                self.tmux.claim_width(slot.id, slot.view)
            except TmuxError:
                # Best effort: the session still shows, at whatever width its window has.
                pass
        key = self.tmux.view_option(REOPEN_OPT)
        if not key:
            return
        self.tmux.set_view_option(REOPEN_OPT, "")
        held = core.live_or_dead_agent(self.tmux.panes(), key) if core.is_session_id(key) else None
        if held is not None:
            self.show(slot, key, held.window)

    def show(self, slot: Pane, key: str, window: str) -> None:
        """Show session `key`, whose pane is in `window`, in the slot through a viewer; the session's pane itself never moves."""
        if slot.role == "viewer" and slot.view == key and not slot.dead:
            return
        self.tmux.link_agent(window, key, self_command("blank", ""))
        self.tmux.respawn(slot.id, self.tmux.viewer_command(key))
        self.tmux.set_pane_option(slot.id, ROLE, "viewer")
        self.tmux.set_pane_option(slot.id, VIEW_OPT, key)
        self.main_pane = replace(slot, role="viewer", view=key, dead=False)

    def park(self, slot: Pane) -> None:
        """Put the placeholder in the slot."""
        park_slot(self.tmux, slot.id)
        if slot.session:
            self.tmux.unset_pane_option(slot.id, SESSION_OPT)
        self.main_pane = replace(slot, role="placeholder", view="", session="", dead=False)

    def reset_ground(self, sidebar) -> None:
        """2.6.0 set the pane's background; a workspace upgrading from it keeps the option until it is unset here."""
        if not self.ground_reset:
            self.tmux.unset_pane_option(sidebar.id, "window-style")
            self.ground_reset = True

    def time_whats_new(self) -> None:
        if not self.whats_new_waits:
            return
        if self.notice != self.whats_new:
            self.whats_new_waits = False
            return
        try:
            attached = self.tmux.client_state()[0]
        except TmuxError:
            return
        if attached > 0:
            self.whats_new_waits = False
            self.notice_until = time.monotonic() + WHATS_NEW_SECONDS

    def update_busy(self) -> bool:
        """Whether something open in the panel, or a worker it started, would be lost to a switch onto a new version now."""
        held = (self.prompt, self.find, self.rename_edit, self.confirm, self.history_view, self.routines_view,
                self.starter, self.closer, self.renamer, self.namer)
        return any(v is not None for v in held) or self.help or self.peeking or bool(self.interrupters) or self.awaiting_started_row()

    def check_update(self) -> None:
        """Announces a waiting major once, and once `current` has moved past this panel's code, starts the new code's relaunch at a quiet moment."""
        if self.updater is None:
            return
        taken = self.updater.take()
        if taken:
            self.on_disk, major, _ = taken
            if major and major not in self.majors_said:
                self.majors_said.add(major)
                self.say(f"emx {major} available: run emx update")
        socket = getattr(self.tmux, "socket", None)
        if not self.on_disk or self.on_disk == emx.RUNNING or not socket:
            return
        now = time.monotonic()
        last = self.relaunched.get(self.on_disk)
        if last is not None and now - last < RELAUNCH_RETRY_SECONDS:
            return
        try:
            attached, idle = core.views_quiet(self.tmux.views(), time.time())
        except TmuxError:
            return
        if not core.update_quiet(attached, idle, self.update_busy()):
            return
        self.relaunched[self.on_disk] = now
        try:
            subprocess.Popen(
                [sys.executable, emx.BIN, "relaunch", socket], start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def check_hush(self) -> None:
        """`emx hush` from outside writes a fresh token to a session option; a change since the last read stops the voice. A failed read changes nothing."""
        try:
            value = self.tmux.session_option(HUSH_OPT)
        except TmuxError:
            return
        self.hush_seen, due = core.hush_due(self.hush_seen, value)
        if due:
            self.hush()

    def check_relay(self) -> None:
        """The server's `emx follow` stamps a session option while a laptop is connected; fresh means banners are showing there. A failed read changes nothing."""
        try:
            value = self.tmux.session_option(RELAY_OPT)
        except TmuxError:
            return
        now = time.time()
        self.relayed = core.relay_live(value, now)
        try:
            host = self.tmux.session_option(RELAY_HOST_OPT)
        except TmuxError:
            self.relay_host = None
            return
        self.relay_host = core.relay_host(host, now)
        if self.primary:
            self.carry_paste_stamp(now)

    def carry_paste_stamp(self, now: float) -> None:
        """An `emx follow` from before 2.10.0, still running on the server after an update, stamps its paste offer on the `emx` session, which a viewer's session does not see; copied to the global option, which every session sees."""
        try:
            stamp = self.tmux.session_option(RELAY_PASTE_OPT)
            if core.relay_live(stamp, now) and stamp != self.tmux.global_option(RELAY_PASTE_OPT):
                self.tmux.set_global_option(RELAY_PASTE_OPT, stamp)
        except TmuxError:
            pass

    def check_open(self) -> None:
        """`emx open` from outside (a notification click) writes a session id to an option of the view it picked; it is read once and cleared. Unlike the hush token it is no baseline: a request written before the panel's first read still counts, so it waits for the first listing. A failed read changes nothing."""
        if not self.loaded:
            return
        try:
            value = self.tmux.view_option(OPEN_OPT)
            if not value:
                return
            sid = core.open_request(value)
            if sid:
                self.tmux.clear_session_option_if(OPEN_OPT, sid, session=self.tmux.view)
            else:
                self.tmux.set_view_option(OPEN_OPT, "")
        except TmuxError:
            return
        if sid:
            self.open_listed(sid)
            self.bring_tab_forward()

    def bring_tab_forward(self) -> None:
        """In iTerm2, bring forward the tab emx runs in: the helper can only activate the app, which lands on whichever tab was last active.

        The escape goes straight to the client's tty, so it bypasses tmux and
        needs no tmux passthrough. Best effort: nothing here fails the open.
        """
        try:
            if self.tmux.session_option(TERMINAL_OPT) != ITERM_BUNDLE:
                return
            ttys = self.tmux.client_ttys()
        except TmuxError:
            return
        for tty in ttys:
            try:
                fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                os.write(fd, ITERM_STEAL_FOCUS)
            except OSError:
                pass
            finally:
                os.close(fd)

    def hush(self) -> None:
        """`h` and `emx hush` only stop speech, so in notification mode they do nothing at all."""
        if self.mode() == core.NOTIFICATION_MODE:
            return
        self.poller.voice.silence()
        remote.append_relay(self.relay_file, core.relay_hush(time.time()))
        self.say("Stopped speaking.", seconds=RESTORED_NOTICE_SECONDS)

    def set_focus(self, focused: bool, back: bool = False) -> None:
        """Note whether the panel has focus.

        Coming back from main with ← (`back`) puts the cursor on the session
        there, as `claude agents` does; Ctrl+\\ or a click leaves it where he
        left it.
        """
        if focused and self.focused is False and back:
            self.cursor = core.cursor_on_focus(self.items(), self.cursor, self.groups, self.main_session())
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
            sidebar, _ = self.layout_panes(self.my_panes())
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

    def main_session(self) -> str:
        """The key of the session the slot shows, from the slot's own tag: "" when it shows none (the placeholder, or an old viewer)."""
        main = self.main_pane
        return main.view if main is not None and main.role == "viewer" else ""

    def main_id(self) -> str | None:
        return self.main_pane.id if self.main_pane is not None else None

    def reconcile_widths(self, sidebar: Pane) -> None:
        action = core.width_action(self.applied, (sidebar.window_width, sidebar.width), zoomed=sidebar.zoomed)
        if action == "hold":
            self.applied = None
            return
        if action == "adopt-panel":
            if self.primary:
                self.prefs["panel_width"] = sidebar.width
                self.save_prefs()
            else:
                self.own_width = sidebar.width
        if action is not None:
            self.apply_layout(sidebar)

    def panel_width(self) -> int:
        """The width this view's panel is kept at: the saved one, which is the laptop's, until another view's panel is given its own."""
        return self.prefs["panel_width"] if self.primary or self.own_width is None else self.own_width

    def apply_layout(self, sidebar: Pane) -> None:
        want = core.compute_layout(sidebar.window_width, self.panel_width())
        self.tmux.resize_width(sidebar.id, want.panel)
        sidebar, _ = self.layout_panes(self.my_panes())
        if sidebar:
            self.applied = (sidebar.window_width, sidebar.width)

    def clear_legacy_viewers(self, main: Pane | None) -> Pane | None:
        """Once per panel start, kill the `claude attach` viewers an emx before 2.0.0 left: a pane with a session key and no agent tag. Returns the slot as it is after.

        `close_stale` would never close them, since emx's listing does not
        hold their sessions and an empty listing counts as no news. Killing a
        viewer leaves its background session running in Claude Code. One in
        the slot gets the placeholder instead, so the layout keeps its two columns.
        """
        legacy = [p for p in self.tmux.panes() if p.session and not p.agent]
        for pane in legacy:
            if main is not None and pane.id == main.id:
                self.park(main)
                main = self.main_pane
            else:
                self.tmux.kill_pane(pane.id)
        self.legacy_cleared = True
        return main

    def close_stale(self, main: Pane | None) -> None:
        """A pane holding one of emx's own sessions (agent-tagged) is the session itself, so it is never closed here: only a double Ctrl+X ends a session."""
        panes = self.tmux.panes()
        now = time.monotonic()
        live = {s.id for s in self.sessions}
        self.fresh = {sid: t for sid, t in self.fresh.items() if sid not in live and now - t < FRESH_GRACE_SECONDS}
        viewers = {p.session for p in panes if p.session and not p.agent and p.session not in self.fresh}
        dead = {p.session for p in panes if p.session and not p.agent and p.dead}
        self.misses, close = core.stale_viewers(viewers, live, self.misses, dead)
        self.close_viewers(panes, main, close)

    def close_viewers(self, panes: list, main: Pane | None, sids) -> None:
        """Kill the panes left holding these sessions, and give the slot the placeholder when it shows one of them."""
        for pane in panes:
            if pane.session in sids and (main is None or pane.id != main.id):
                self.tmux.kill_pane(pane.id)
        if main is not None and (main.view in sids or (main.session in sids and not main.agent)):
            self.park(main)

    def switch_to(self, sid: str, trigger: str, checked: tuple | None = None) -> None:
        """Show session `sid` in this view's slot and go to it, resuming its process first only when his open allows it."""
        try:
            _, main = self.layout_panes(self.my_panes())
            if main is None:
                self.say("The main pane is missing; restart emx.", error=True)
                return
            panes = self.tmux.panes()
            held = core.live_or_dead_agent(panes, sid)
            if held is None or held.dead:
                pane_id = self.resume(sid, trigger, checked, held)
                if pane_id is None:
                    return
                held = next((p for p in self.tmux.panes() if p.id == pane_id), None)
                if held is None:
                    self.say("The session's pane went away as it started; open it again.", error=True)
                    return
            self.show(main, sid, held.window)
            self.tmux.select(main.id)
            if trigger == core.OPEN:
                self.opened[sid] = time.monotonic()
                self.history = core.history_read(self.history, sid)
            self.say("")
        except TmuxError as exc:
            self.say(f"tmux: {exc}", error=True)
        self.housekeep(fresh_list=False)

    def resume(self, sid: str, trigger: str, checked: tuple | None, dead: Pane | None) -> str | None:
        """Continue a session whose process has ended, in its dead pane or else a new hidden window, and return that pane; None when it may not or could not.

        Its own small files under emx's state folder are read and rewritten on
        this thread, as the prefs are; no file of Claude's is. A new pane is
        tagged before a slot shows it, since the ← check and `cursor_on_focus`
        read the tag; one that can't be tagged is killed, or
        a second open would resume the same conversation again. Only his
        open resumes: any other trigger finding no live pane is refused, even
        when the listing it took still showed the session running.
        """
        if not self.confirm_attach(sid, trigger, checked):
            return None
        if trigger != core.OPEN:
            shown = self.session(sid) or (checked[0] if checked else None)
            self.say(core.refusal(shown.name if shown else sid, trigger, True, shown), error=True)
            return None
        command, error = self.engine.resume_command(sid)
        if error:
            self.say(error, error=True)
            return None
        shown = self.session(sid) or (checked[0] if checked else None)
        cwd = shown.cwd if shown is not None and shown.cwd else None
        if dead is not None:
            self.tmux.respawn(dead.id, command, cwd=cwd)
            return dead.id
        pane_id = self.tmux.new_hidden_window(command, cwd=cwd)
        try:
            # Agent first: a key alone marks an old `claude attach` viewer, which the panel kills.
            self.tmux.set_pane_option(pane_id, AGENT_OPT, AGENT)
            self.tmux.set_pane_option(pane_id, SESSION_OPT, sid)
        except TmuxError:
            try:
                self.tmux.kill_pane(pane_id)
            except TmuxError:
                pass
            raise
        return pane_id

    def confirm_attach(self, sid: str, trigger: str, checked: tuple | None = None) -> bool:
        """Ask core whether emx may start or resume the session's process, and say why not when it may not.

        `checked` is (session, fresh) from a listing a worker thread took just
        now (`core.checked_row`); this thread never lists, so a slow listing
        can't freeze the panel. A trigger other than OPEN without one
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

    def group(self, key: str):
        return next((g for g in self.groups if g.key == key), None)

    def confirm_row(self, confirm):
        """What `confirm` was armed on, as the panel lists it now: the session, or the group for a header."""
        if isinstance(confirm, core.GroupConfirm):
            return self.group(confirm.key)
        return self.session(confirm.id)

    def press_close(self) -> None:
        cursor = self.cursor
        if not cursor:
            return
        if cursor[0] == "group":
            row = self.group(cursor[1])
            if row is None:
                return
            if not core.group_closable(self.view(), row.key):
                self.confirm = None
                self.say(core.group_close_refusal(row.key))
                return
        else:
            row = self.session(cursor[1])
            if row is None:
                return
            if row.id in self.closing:
                self.confirm = None
                self.say(core.still_closing(row.name))
                return
        blocked = core.close_blocked(bool(self.list_error), self.closer.current if self.closer else None)
        if blocked:
            self.confirm = None
            self.say(blocked)
            return
        self.confirm, act = core.press_close(self.confirm, cursor, row, time.monotonic(), self.view())
        if act is None:
            if isinstance(self.confirm, core.GroupConfirm) and row.key in self.collapsed():
                self.toggle_group(row.key)
            elif isinstance(self.confirm, core.Confirm) and core.interruptible(row):
                self.start_interrupt(row)
            return
        self.start_close(act)

    def start_close(self, act) -> None:
        targets = core.close_targets(act)
        ids = {t.id for t in targets}
        wait_for = tuple(self.interrupters[sid] for sid in sorted(ids) if sid in self.interrupters)
        for interrupter in wait_for:
            interrupter.cancel.set()
        self.closing.update((sid, None) for sid in ids)
        self.closer = Closer(self.engine, targets, wait_for, self.me)
        self.closer.start()
        self.say(core.closing_text(act))

    def start_interrupt(self, session: core.Session) -> None:
        """One interrupt per session at a time: two Escs in quick succession open Claude Code's rewind."""
        if session.id in self.interrupters:
            return
        interrupter = self.interrupters[session.id] = Interrupter(
            self.engine, self.tmux, core.Interrupt(session.id, session.name), self.me,
        )
        interrupter.start()
        self.say(f"Interrupting {session.name}…")

    def finish_interrupt(self) -> None:
        for sid, interrupter in list(self.interrupters.items()):
            if interrupter.is_alive():
                continue
            del self.interrupters[sid]
            if interrupter.cancel.is_set():
                continue
            done, message = interrupter.result or (False, "interrupt failed")
            self.say(message, error=not done)
            self.poller.wake.set()

    def finish_close(self) -> None:
        closer = self.closer
        if closer is None or closer.is_alive():
            return
        self.closer = None
        done, message, finished = closer.result or (False, "close failed", {t.id: False for t in closer.targets})
        self.say(message, error=not done)
        self.closing = core.closing_after(self.closing, finished, None)
        self.poller.wake.set()
        closed = {sid for sid, ok in finished.items() if ok}
        if closed:
            try:
                _, main = self.layout_panes(self.my_panes())
                self.close_viewers(self.tmux.panes(), main, closed)
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
        elif key == CTRL_U:
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

    def start_rename(self) -> None:
        session = self.session(self.cursor[1]) if self.cursor and self.cursor[0] == "session" else None
        if session is None:
            return
        if session.id in self.closing:
            self.say(core.still_closing(session.name))
            return
        blocked = core.rename_blocked(bool(self.list_error), self.renamer.rename if self.renamer else None, session)
        if blocked:
            self.say(blocked, error=True)
            return
        self.rename_gen += 1
        self.rename_edit = {
            "id": session.id, "old": session.name, "text": core.rename_start(session), "hint": "",
            "offered": [], "gen": self.rename_gen, "unnamed": core.unnamed(session),
        }
        self.rename_misses = {}
        self.say("")

    def rename_key(self, key) -> None:
        edit = self.rename_edit
        if key == "\x1b":
            self.rename_edit = None
        elif key in ENTER_KEYS:
            self.submit_rename()
        elif key == "\t":
            self.ask_name()
        elif key in BACKSPACE_KEYS:
            edit.update(text=edit["text"][:-1], hint="")
        elif key == CTRL_U:
            edit.update(text="", hint="")
        elif isinstance(key, str) and core.name_char_allowed(key):
            edit.update(text=edit["text"] + key, hint="")

    def submit_rename(self) -> None:
        edit = self.rename_edit
        rename, error = core.rename_request(edit["id"], edit["old"], edit["text"])
        if error:
            edit["hint"] = error
            return
        self.rename_edit = None
        if rename is None:
            return
        self.renamer = Renamer(self.engine, self.tmux, rename, self.me, self.poller.transcripts.wake)
        self.renamer.start()
        self.say(f"Renaming {rename.old}…")

    def ask_name(self) -> None:
        """Tab in the rename box: one Haiku call for a name, which lands in the box and renames nothing."""
        edit = self.rename_edit
        if self.namer is not None and not self.namer.is_alive():
            # An answer for this box that arrived since the last frame is shown, not replaced unseen by another call.
            answered_here = self.namer_gen == edit["gen"]
            self.finish_name()
            if answered_here:
                return
        if self.namer is not None:
            if self.namer_gen != edit["gen"]:
                edit["hint"] = NAME_STILL_FINISHING
            return
        sid = edit["id"]
        self.namer = Namer(
            self.claude, self.poller.transcripts.path(sid), sid, edit["old"], edit["unnamed"], edit["offered"],
        )
        self.namer_gen = edit["gen"]
        edit["hint"] = ""
        edit["asking"] = (edit["gen"], edit["text"])
        self.namer.start()

    def finish_name(self) -> None:
        """Puts a finished call's name into the box, only while it is the box that asked and he hasn't typed since."""
        namer = self.namer
        if namer is None or namer.is_alive():
            return
        self.namer = None
        edit = self.rename_edit
        if edit is None or edit["gen"] != self.namer_gen:
            return
        asking = edit.pop("asking", None)
        if asking is None or asking[1] != edit["text"]:
            return
        name, error, _ = namer.result or (None, "asking for a name failed", None)
        if name:
            edit.update(text=name, hint="")
            edit["offered"].append(name)
        else:
            edit["hint"] = error

    def finish_rename(self) -> None:
        renamer = self.renamer
        if renamer is None or renamer.is_alive():
            return
        self.renamer = None
        done, message, typed = renamer.result or (False, "rename failed", False)
        self.say(message, error=not done)
        self.poller.wake.set()

    def flip_side(self) -> None:
        try:
            sidebar, main = self.layout_panes(self.my_panes())
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
        if self.primary:
            self.prefs = core.adjust_panel_width(self.prefs, delta)
            self.save_prefs()
        else:
            self.own_width = core.adjust_panel_width({"panel_width": self.panel_width()}, delta)["panel_width"]
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
        elif key == CTRL_U:
            p.update(text="", picked=None, hint="")
        elif key == "\t":
            if p["stage"] == "message":
                self.open_picker()
            else:
                self.complete_suggestion()
        elif p["stage"] == "folder" and key == CTRL_T:
            self.toggle_favourite()
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
        favourites = core.favourite_folders(self.prefs)
        readable = {f: self.listings.readable(f) for f in favourites}
        present = 0
        for folder in recent:
            if present >= core.RECENT_SHOWN:
                break
            if folder not in readable:
                readable[folder] = self.listings.readable(folder)
            present += readable[folder] is not False
        return core.folder_suggestions(
            p["text"], folders, core.present_folders(recent, readable), listed, self.home,
            roots=roots, favourites=core.present_folders(favourites, readable),
        )

    def toggle_favourite(self) -> None:
        """Ctrl+T in the picker: star or unstar the highlighted folder, else the top row (as Tab picks)."""
        rows = self.suggestions()
        target = self.prompt.get("picked") or (rows[0].path if rows else None)
        if target is None:
            return
        self.prefs, on = core.toggle_favourite(self.prefs, target)
        self.save_prefs()
        name = core.tilde_path(target, self.home)
        hint = f"★ {name} is a favourite" if on else f"{name} is no longer a favourite"
        still_listed = target in {r.path for r in self.suggestions()}
        self.prompt.update(picked=target if still_listed else None, hint=hint)

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
        self.starter = Starter(self.engine, p["folder"], p["text"])
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
        if self.routines_view is not None:
            return
        if self.history_view is not None:
            if item and item[0] == "entry":
                self.history_view = item[1]
                self.open_entry()
            return
        if self.find is not None:
            # A header click would fold a group he cannot see folded until find closes.
            if not item or item[0] == "group":
                return
            self.close_find()
        self.activate(item)

    def handle_key(self, key) -> None:
        if self.whats_new is not None and key != curses.KEY_RESIZE:
            if self.notice == self.whats_new:
                self.say("")
            self.whats_new = None
        self.confirm = core.confirm_after_key(self.confirm, key == CTRL_X, key == curses.KEY_RESIZE)
        if self.peeking and key not in PEEK_KEYS and key != curses.KEY_RESIZE:
            self.peeking = False
        if key == curses.KEY_MOUSE:
            self.handle_mouse()
        elif key == curses.KEY_RESIZE:
            self.housekeep(fresh_list=False)
            if self.peeking and not self.peek_fits():
                self.peeking = False
                if not self.notice:
                    # A notice that took the room is kept rather than replaced by this one.
                    self.say(PEEK_NO_ROOM, seconds=RESTORED_NOTICE_SECONDS)
        elif self.prompt is not None:
            self.prompt_key(key)
        elif self.rename_edit is not None:
            self.rename_key(key)
        elif self.find is not None:
            self.find_key(key)
        elif self.history_view is not None:
            self.history_key(key)
        elif self.routines_view is not None:
            self.routines_key(key)
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
        elif key == CTRL_R:
            self.start_rename()
        elif key == "n":
            self.open_prompt()
        elif key == "N":
            self.open_history()
        elif key == "R":
            self.open_routines()
        elif key == "m":
            self.toggle_mode()
        elif key == "h":
            self.hush()
        elif key == "s":
            self.flip_side()
        elif key in ("<", ","):
            self.change_panel_width(-core.WIDTH_STEP)
        elif key in (">", "."):
            self.change_panel_width(core.WIDTH_STEP)
        elif key == "q":
            self.detach()

    def open_history(self) -> None:
        """Opening the view marks nothing read: only opening a session does."""
        self.history_view, self.history_scroll = 0, 0
        self.peeking = False

    def history_key(self, key) -> None:
        if key == "\x1b":
            self.history_view = None
        elif key in (curses.KEY_UP, "k"):
            self.history_view = max(0, self.history_view - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.history_view = max(0, min(len(self.history) - 1, self.history_view + 1))
        elif key in ENTER_KEYS or key == curses.KEY_RIGHT:
            self.open_entry()

    def open_routines(self) -> None:
        self.routines_view = 0
        self.peeking = False
        if self.routines_reader is None:
            self.start_routines()

    def start_routines(self) -> None:
        self.routines_reader = RoutinesReader()
        self.routines_read_at = time.monotonic()
        self.routines_reader.start()

    def routines_key(self, key) -> None:
        if key in ("\x1b", "R"):
            self.routines_view = None
        elif key in (curses.KEY_UP, "k"):
            self.routines_view = max(0, self.routines_view - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.routines_view += 1

    def finish_routines(self) -> None:
        reader = self.routines_reader
        if reader is not None and not reader.is_alive():
            self.routines_reader = None
            if self.routines_view is not None and reader.result is not None:
                self.routine_views, self.routines_error = reader.result
        if (self.routines_view is not None and self.routines_reader is None
                and time.monotonic() - self.routines_read_at >= ROUTINES_SECONDS):
            self.start_routines()

    def open_entry(self) -> None:
        if not 0 <= self.history_view < len(self.history):
            return
        if self.open_listed(self.history[self.history_view].id):
            self.history_view = None

    def open_listed(self, sid: str) -> bool:
        """Open a session from outside the list (the history, a notification click) as the list would; False, saying so, when it isn't listed."""
        if self.session(sid) is None:
            self.say(OPEN_GONE, seconds=RESTORED_NOTICE_SECONDS)
            return False
        if self.find is not None:
            self.close_find()
        self.activate(("session", sid))
        return True

    def mode(self) -> str:
        return core.notice_mode(self.prefs)

    def apply_mode(self) -> None:
        mode = self.mode()
        self.poller.notices.mode = mode
        self.poller.notifier.mode = mode
        self.poller.voice.muted = mode == core.NOTIFICATION_MODE

    def toggle_mode(self) -> None:
        self.prefs = core.toggle_mode(self.prefs)
        self.save_prefs()
        self.apply_mode()
        if self.mode() == core.NOTIFICATION_MODE:
            self.poller.voice.silence()
            remote.append_relay(self.relay_file, core.relay_hush(time.time()))
            self.poller.notifier.authorize()
        self.say(MODE_MESSAGES[self.mode()], seconds=RESTORED_NOTICE_SECONDS)

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addstr(y, x, core.fit(text, w - x), attr)
            except curses.error:
                pass

    def bottom_lines(self, width: int, height: int | None = None) -> list:
        """(text, attr) lines for the area under the list: list error, notice, and the prompt, rename or find lines with their hints. With the pane's `height`, the picker's rows fit above the bottom."""
        lines = []
        if self.list_error:
            lines += [(t, self.color("error")) for t in core.wrap_words(f"! {self.list_error}", width)[:3]]
        if self.notice:
            attr = self.color("error") if self.notice_error else curses.A_DIM
            shown = core.wrap_words(self.notice, width)
            if self.notice == self.whats_new:
                shown = core.cut_with_pointer(shown, max(6, (height or 0) // 2), "CHANGELOG.md")
            else:
                shown = shown[:6]
            lines += [(t, attr) for t in shown]
        if self.prompt is not None:
            room = None if height is None else height - len(lines)
            return lines + self.prompt_lines(width, room)
        if self.rename_edit is not None:
            hint = self.rename_edit["hint"] or (NAME_ASKING if self.rename_edit.get("asking") else "")
            lines += [(t, curses.A_DIM) for t in core.wrap_words(hint, width)[:3]] if hint else []
            return lines + [(t, curses.A_DIM) for t in core.wrap_words(RENAME_FOOTER, width)[:2]]
        if self.find is not None:
            lines += [("Find a session:", curses.A_BOLD)]
            lines += [(t, 0) for t in core.wrap("> " + self.find + "▏", width)[-3:]]
            return lines + [(t, curses.A_DIM) for t in core.wrap_words(FIND_HINT, width)[:2]]
        return lines

    def bar_hints(self) -> tuple:
        """The key hints the bar offers now, longest first; none while typing, when the panel's own prompt carries them."""
        if self.confirm is not None:
            return (CONFIRM_FOOTER,)
        if self.peeking:
            return core.hint_variants(core.PEEK_HINTS)
        if self.history_view is not None:
            return (HISTORY_HINT, "Esc back")
        if self.routines_view is not None:
            return (ROUTINES_HINT, "Esc back")
        if self.help:
            return ("? close",)
        if self.prompt is not None or self.find is not None or self.rename_edit is not None:
            return ()
        if not self.loaded:
            return core.hint_variants(core.LOADING_HINTS)
        if not self.sessions:
            return core.hint_variants(core.EMPTY_HINTS)
        listing = not self.list_error
        if self.focused is False or self.cursor not in self.items():
            return core.hint_variants(core.rest_hints(listing))
        if self.cursor[0] == "group":
            return core.hint_variants(core.header_hints(self.view(), self.cursor[1], self.cursor[1] in self.collapsed(), listing))
        s = self.session(self.cursor[1])
        if s is None:
            return core.hint_variants(core.rest_hints(listing))
        if s.id in self.closing:
            return core.hint_variants(core.CLOSING_HINTS)
        return core.hint_variants(core.session_hints(s, s.id in core.pinned_ids(self.prefs), listing))

    def notice_key_live(self) -> bool:
        """Whether `N` opens the notifications now: not while a prompt, find, rename, help, the history or the routines view has the keys."""
        return (
            self.history_view is None and self.routines_view is None and not self.help
            and self.prompt is None and self.find is None and self.rename_edit is None
        )

    def update_bar(self) -> None:
        if not self.window_width:
            return
        tag = core.mode_tag(self.mode(), self.poller.notifier.state, self.relayed)
        parts = core.bar_parts(
            __version__, core.bar_counts(self.sessions), self.history[0] if self.history else None,
            time.monotonic(), tag, self.relay_host, self.bar_hints(), self.window_width, self.loaded, self.notice_key_live(),
        )
        self.bar.push(bar_format(parts))

    def prompt_lines(self, width: int, room: int | None = None) -> list:
        """The prompt's lines; the picker's suggestions get only what `room` leaves after the title, input and hints."""
        p = self.prompt
        if p["stage"] == "message":
            title = "New session in "
            head = [(title + core.short_path(p["folder"], width - core.display_width(title), self.home), curses.A_BOLD)]
            hint = "Enter start · Tab folder · Esc cancel"
        else:
            head = [("New session · pick a folder:", curses.A_BOLD)]
            hint = "↑↓ pick · Tab fill · Enter use · ^t ★ · Esc " + ("back" if p.get("back") else "cancel")
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
        starred = set(core.favourite_folders(self.prefs))
        for row in shown:
            mark = "★" if row.path in starred else " "
            text = core.fit((mark + core.short_path(row.path, width - 1, self.home)).ljust(width), width)
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
        bottom = self.bottom_lines(max(1, w - 1), h)
        list_top, list_bottom = 0, h - len(bottom)
        if self.history_view is not None:
            self.draw_history(list_top, list_bottom, w)
        elif self.routines_view is not None:
            self.draw_routines(list_top, list_bottom, w)
        elif self.help:
            self.draw_help(list_top, list_bottom, w)
        elif not self.loaded:
            if not self.list_error:
                self.put(list_top, 1, "Loading sessions…", curses.A_DIM)
        elif not self.sessions:
            self.put(list_top + 1, 1, "No sessions yet.", curses.A_DIM)
            self.put(list_top + 2, 1, "Press ", curses.A_DIM)
            self.put(list_top + 2, 7, "n", curses.A_BOLD)
            self.put(list_top + 2, 8, " to start one in a folder.", curses.A_DIM)
        elif not self.groups:
            self.put(list_top, 1, "No session matches.", curses.A_DIM)
        else:
            layout = self.list_layout(w, max(1, list_bottom - list_top))
            room = core.peek_room(h, w, len(bottom))
            peek_rows = self.draw_peek(list_bottom, w, room, self.cursor_rows(layout)) if self.peeking else 0
            self.draw_list(list_top, list_bottom - peek_rows, w, layout)
        for i, (text, attr) in enumerate(bottom):
            self.put(list_bottom + i, 0, " " + text, attr)
        scr.noutrefresh()
        curses.doupdate()

    def draw_peek(self, bottom: int, width: int, room: int, cursor_rows: int = 1) -> int:
        """Draw the peek box ending above row `bottom`, in the `room` rows `core.peek_room` gives, and return how many rows it took (0 when it doesn't fit)."""
        session = self.peeked()
        if session is None or room <= 0:
            return 0
        peek = core.peek_content(session, self.transcripts.get(session.id), time.time() * 1000)
        inner = width - 5
        # Wrapping a long reply is the costly part, so it is done again only when the body or the width changes.
        if self.peek_wrapped[0] != (peek.lines, inner):
            self.peek_wrapped = ((peek.lines, inner), core.peek_lines(peek, inner, core.PEEK_BODY_LINES + 1))
        wrapped = self.peek_wrapped[1]
        rows = core.peek_height(room, len(wrapped), cursor_rows)
        if rows == 0:
            return 0
        body = core.cut_lines(wrapped, rows - 3)
        y, box = bottom - rows, width - 1
        muted = self.palette.attr("muted")
        label = core.fit(session.name, box - 6)
        self.put(y, 0, "╭─ " + label + " " + "─" * max(0, box - 5 - core.display_width(label)) + "╮", muted)
        tone = "muted" if core.category(session) == core.STOPPED else session_tone(session)
        lines = [(core.fit(peek.title, inner), self.palette.attr(tone) | curses.A_BOLD)]
        lines += [(text, curses.A_DIM if style == "dim" else self.palette.attr(PEEK_TONES[style])) for text, style in body]
        for i in range(rows - 2):
            self.put(y + 1 + i, 0, "│", muted)
            self.put(y + 1 + i, box - 1, "│", muted)
            if i < len(lines):
                self.put(y + 1 + i, 2, lines[i][0], lines[i][1])
        keys_at = 3 + core.display_width(core.PEEK_EDGE) + 1
        if box - keys_at - 1 >= 1:
            self.put(bottom - 1, 0, "╰─ ", muted)
            self.put(bottom - 1, 3, core.PEEK_EDGE, muted | curses.A_DIM)
            self.put(bottom - 1, keys_at - 1, " " + "─" * (box - keys_at - 1) + "╯", muted)
        else:
            self.put(bottom - 1, 0, "╰" + "─" * max(0, box - 2) + "╯", muted)
        return rows

    def draw_history(self, top: int, bottom: int, width: int) -> None:
        self.put(top, 1, core.HISTORY_TITLE, curses.A_BOLD)
        if not self.history:
            self.put(top + 1, 1, HISTORY_EMPTY, curses.A_DIM)
            return
        self.history_view = max(0, min(self.history_view, len(self.history) - 1))
        rows = core.history_rows(self.history, time.monotonic(), max(1, width - 2))
        lines = [("entry", i) for i in range(len(rows))]
        heights = [len(r) for r in rows]
        top, height = top + 1, max(1, bottom - top - 1)
        self.history_scroll = core.list_window(heights, self.history_view, self.history_scroll, height)
        self.row_map = core.row_hits(lines, heights, self.history_scroll, top, height)
        y = top - self.history_scroll
        for i, entry in enumerate(self.history):
            for j, text in enumerate(rows[i]):
                if top <= y < top + height:
                    selected = core.selection_bar(i == self.history_view, self.focused is not False)
                    if selected:
                        self.put(y, 0, " " * width, self.palette.attr("plain", selected))
                    if entry.read:
                        attr = self.palette.attr("muted", selected) | curses.A_DIM
                    elif j == 0:
                        attr = self.palette.attr(CATEGORY_TONE.get(entry.kind, "plain"), selected) | curses.A_BOLD
                    else:
                        attr = self.palette.attr("plain", selected)
                    self.put(y, 1, text, attr)
                y += 1

    def routine_rows(self, width: int) -> list:
        """(text, tone) for each row of the view, or the one message that stands in for them."""
        inner = max(1, width - 2)
        if self.routine_views is None and self.routines_error is None:
            return [(ROUTINES_LOADING, "muted")]
        if self.routines_error:
            return [(t, "error") for t in core.wrap_words(self.routines_error, inner)]
        if not self.routine_views:
            return [(t, "muted") for t in core.wrap_words(ROUTINES_NONE, inner)]
        rows, now = [], time.time()
        for view in self.routine_views:
            if rows:
                rows.append(("", "plain"))
            rows += core.routine_lines(view, inner, now)
        return rows

    def draw_routines(self, top: int, bottom: int, width: int) -> None:
        self.put(top, 1, ROUTINES_TITLE, curses.A_BOLD)
        rows = self.routine_rows(width)
        top, height = top + 1, max(1, bottom - top - 1)
        self.routines_view = core.list_window([1] * len(rows), -1, self.routines_view, height)
        for y, (text, tone) in enumerate(rows[self.routines_view:self.routines_view + height], start=top):
            self.put(y, 1, text, self.palette.attr(tone))

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

    def list_layout(self, width: int, height: int = 0) -> tuple:
        """(lines, each session line's laid-out rows, every line's height in screen rows) for the list at `width`; a session row is at most `height` rows."""
        lines = self.lines()
        group_of = {s.id: g.key for g in self.groups for s in g.sessions}
        now_ms = time.time() * 1000
        rows = {}
        for line in lines:
            session = self.session(line[1]) if line and line[0] == "session" else None
            if session is not None:
                armed = self.confirm if isinstance(self.confirm, core.Confirm) and self.confirm.id == session.id else None
                inline = core.session_inline(session, not self.list_error) if line == self.cursor and self.focused is not False else None
                rows[line] = self.session_lines(session, width, now_ms, armed, group_of.get(session.id, ""), height, inline)
        return lines, rows, [len(rows.get(line, [None])) for line in lines]

    def cursor_rows(self, layout: tuple) -> int:
        """How many screen rows the cursor's line takes in `layout`, 1 when the cursor is not on a listed line."""
        lines, _, heights = layout
        return heights[lines.index(self.cursor)] if self.cursor in lines else 1

    def draw_list(self, top: int, bottom: int, width: int, layout: tuple) -> None:
        lines, rows, heights = layout
        height = max(1, bottom - top)
        main_sid = self.main_session()
        cursor_group = core.cursor_group(self.cursor, self.groups)
        groups = {g.key: g for g in self.groups}
        frame = int(time.monotonic() / FRAME_SECONDS)
        at = lines.index(self.cursor) if self.cursor in lines else 0
        self.scroll = core.list_window(heights, at, self.scroll, height)
        self.row_map = core.row_hits(lines, heights, self.scroll, top, height)
        y = top - self.scroll
        for line, rows_taken in zip(lines, heights):
            first, y = y, y + rows_taken
            if line is None or y <= top or first >= top + height:
                continue
            selected = core.selection_bar(line == self.cursor, self.focused is not False)
            shown = [r for r in range(first, y) if top <= r < top + height]
            if selected:
                for r in shown:
                    self.put(r, 0, " " * width, self.palette.attr("plain", selected))
            if line[0] == "group":
                self.draw_header(first, groups[line[1]], selected, line[1] == cursor_group, width,
                                 core.header_label(self.confirm, groups[line[1]], self.closing))
            elif line in rows:
                session = self.session(line[1])
                self.draw_session(first, shown, rows[line], session, selected, session.id == main_sid, frame)

    def draw_header(self, y: int, group, selected: str | None, bold: bool, width: int, label: str | None = None) -> None:
        if label is not None:
            arrow = "▸" if group.key in self.collapsed() else "▾"
            self.put(y, 0, arrow, self.palette.attr("muted", selected))
            self.put(y, 2, core.fit(label, max(1, width - 3)), self.palette.attr("error", selected) | curses.A_BOLD)
            return
        view = self.view()
        name_tone = CATEGORY_TONE[group.key] if core.state_group(view, group.key) else "plain"
        tones = {"arrow": "muted", "dir": "path", "name": name_tone, "count": "muted", "hint": "muted"}
        collapsed = group.key in self.collapsed()
        parts = core.header_layout(view, group.key, len(group.sessions), collapsed, width)
        if selected and self.focused is not False:
            parts = core.place_inline(parts, core.header_inline(collapsed), width)
        for x, text, part in parts:
            attr = self.palette.attr(tones[part], selected)
            if part == "name" and bold:
                attr |= curses.A_BOLD
            elif part == "hint":
                attr |= curses.A_DIM
            self.put(y, x, text, attr)

    def worktree(self, s) -> str:
        return self.placements.get(s.cwd, core.Placement(s.cwd)).worktree

    def session_lines(
        self, s, width: int, now_ms: float, armed: core.Confirm | None = None, group_key: str = "",
        max_lines: int = 0, inline: str | None = None,
    ) -> list:
        """The row's lines as `core.row_lines` gives them, with `inline` placed on the last one when it fits, or, while it is closing, armed or renamed, that label's or the rename edit's padded to as many."""
        word = "" if core.state_group(self.view(), group_key) and not core.idle(s) else core.state_word(s)
        age = core.session_age(s, now_ms)
        detail = core.row_detail(s, self.transcripts.get(s.id), self.summaries.get(s.id))
        lines = core.row_lines(s.name, self.worktree(s), word, detail, age, width, core.category(s) == core.STOPPED, max_lines)
        if s.id in self.closing:
            return core.confirm_lines(core.CLOSING_LABEL, width, len(lines))
        if armed is not None:
            return core.confirm_lines(core.confirm_label(armed), width, len(lines))
        if self.rename_edit is not None and self.rename_edit["id"] == s.id:
            edit = (core.ROW_LEAD, core.fit_left(self.rename_edit["text"] + "▏", width - 1 - core.ROW_LEAD), "rename")
            return [[edit]] + [[] for _ in lines[1:]]
        if inline:
            lines[-1] = core.place_inline(lines[-1], inline, width)
        return lines

    def draw_session(
        self, y: int, shown: list, rows: list, s, selected: str | None, in_main: bool, frame: int,
    ) -> None:
        """Draw the row's `rows` from screen row `y`, only on the screen rows in `shown`."""
        cat = core.category(s)
        stopped = cat == core.STOPPED
        pal = self.palette

        def attr(tone: str, extra: int = 0) -> int:
            if stopped:
                return pal.attr("stopped", selected) | curses.A_DIM | extra
            return pal.attr(tone, selected) | extra

        glyph_tone = "idle" if core.idle(s) else "muted" if cat == core.WORKING else CATEGORY_TONE[cat]
        tones = {"name": "plain", "extra": "path", "status": session_tone(s), "sep": "muted", "detail": "muted", "age": "muted", "hint": "muted"}
        for i, parts in enumerate(rows):
            if y + i not in shown:
                continue
            if i == 0:
                if in_main:
                    self.put(y, 0, "▎", pal.attr("accent", selected) | curses.A_BOLD)
                self.put(y, 1, core.session_glyph(s, frame), attr(glyph_tone))
            for x, text, part in parts:
                if part in ("confirm", "rename"):
                    tone = "error" if part == "confirm" else "accent"
                    self.put(y + i, x, text, pal.attr(tone, selected) | curses.A_BOLD)
                    continue
                emphasis = curses.A_BOLD if part == "name" and (selected is not None or in_main) else 0
                if part == "hint":
                    emphasis = curses.A_DIM
                self.put(y + i, x, text, attr(tones[part], emphasis))

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
        self.history, self.history_misses = core.history_prune(self.history, [s.id for s in sessions], self.history_misses)
        self.closing = core.closing_after(self.closing, {}, [s.id for s in sessions])
        if self.rename_edit is None:
            self.rename_misses = {}
        else:
            self.rename_misses, gone = core.edit_session_gone(
                self.rename_edit["id"], [s.id for s in sessions], self.rename_misses,
            )
            if gone:
                self.say(f"{self.rename_edit['old']} isn't listed any more, so emx dropped the rename.", error=True)
                self.rename_edit = None
                self.rename_misses = {}
        self.loaded = True
        self.prefs, self.pin_misses, pruned = core.prune_pins(self.prefs, [s.id for s in sessions], self.pin_misses)
        if pruned:
            self.save_prefs()
        self.regroup()
        self.queue_notices(sessions)
        return True

    def queue_notices(self, sessions: list) -> None:
        """Hands the notices this listing earned to the worker that words them. Haiku is never called here."""
        quiet = set(self.closing) | set(self.interrupters)
        if self.renamer is not None:
            quiet.add(self.renamer.rename.id)
        self.notice_seen, events = core.notice_transitions(self.notice_seen, sessions, quiet)
        if not events:
            return
        if not self.primary:
            return
        try:
            views = self.tmux.views()
        except TmuxError:
            views = []
        now, wall = time.monotonic(), time.time()
        for session in events:
            if not core.in_front_any(session.id, views, wall):
                self.poller.notices.add(core.Notice(session.id, core.category(session), session.name, session.cwd, now, session.named))

    def take_notices(self) -> None:
        for sid, kind, title, text, at in self.poller.notices.take():
            self.history = core.history_add(self.history, core.Entry(sid, kind, title, text, at), self.opened)

    def take_placements(self) -> None:
        placements = self.poller.resolver.take()
        if placements is not None:
            self.placements = placements
            self.regroup()

    def take_transcripts(self) -> None:
        found = self.poller.transcripts.take()
        if found is not None:
            self.transcripts = found

    def take_summaries(self) -> None:
        """The primary panel's Haiku lines, which it also writes for the other views' panels; any other panel reads them from that file."""
        if not self.primary:
            self.read_summaries()
            return
        found = self.poller.summaries.take()
        if found is not None:
            self.summaries = found
            self.write_summaries(found)

    def write_summaries(self, found: dict) -> None:
        tmp = None
        try:
            os.makedirs(self.state, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state, prefix=".summaries.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(core.summaries_record(found), fh)
            os.replace(tmp, self.summaries_file)
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def read_summaries(self) -> None:
        mtime = self.file_mtime(self.summaries_file)
        if mtime is None or mtime == self.summaries_mtime:
            return
        self.summaries_mtime = mtime
        try:
            with open(self.summaries_file, encoding="utf-8") as fh:
                self.summaries = core.summaries_from(json.load(fh))
        except (OSError, ValueError):
            pass

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
        return core.peek_height(core.peek_room(h, w, len(self.bottom_lines(max(1, w - 1), h))), 1) > 0

    def peek_key(self, key) -> None:
        if key in (" ", "\x1b"):
            self.peeking = False
        else:
            delta = -1 if key in (curses.KEY_UP, "k") else 1
            self.cursor, self.wanted_cursor = core.find_move(self.items(), self.cursor, delta), None

    def refresh(self) -> None:
        try:
            self.finish_start()
            self.finish_close()
            self.finish_interrupt()
            self.finish_rename()
            self.finish_name()
            self.finish_routines()
            peeked = self.peeked()
            fresh_list = self.take_poll()
            self.take_placements()
            self.take_transcripts()
            self.take_summaries()
            self.take_notices()
            # The cursor moves to a neighbour when its session goes; the peek closes rather than follow it.
            if self.peeking and (self.peeked() is None or self.peeked().id != getattr(peeked, "id", None)):
                self.peeking = False
            now = time.monotonic()
            if self.confirm is not None and not core.confirm_holds(
                self.confirm, self.cursor, self.confirm_row(self.confirm), now,
            ):
                self.confirm = None
            if core.notice_expired(self.notice_until, now):
                self.say("")
            if fresh_list or now - self.last_housekeep >= HOUSEKEEP_SECONDS:
                self.last_housekeep = now
                self.housekeep(fresh_list)
            if self.peeking and not self.peek_fits():
                # Lines under the list squeezed the box to nothing; the pane is not too short, so no notice says it is.
                self.peeking = False
            self.draw()
            self.update_bar()
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
        if self.updater is not None:
            self.updater.start()
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


def own_view(socket: str) -> str:
    """The view session this panel's pane is in; `emx` when tmux cannot say."""
    pane = os.environ.get("TMUX_PANE", "")
    try:
        name = Tmux(socket).run("display-message", "-p", "-t", pane, "#{session_name}").strip() if pane else ""
    except TmuxError:
        name = ""
    return name if is_view_session(name) else SESSION


def run(socket: str) -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    claude = os.environ.get("EMX_CLAUDE") or "claude"

    view = own_view(socket)

    def body(scr):
        setup_curses(scr)
        try:
            App(scr, Tmux(socket, CONFIG, view=view), claude).run()
        finally:
            os.write(1, FOCUS_REPORTS_OFF.encode())

    # Stale LINES/COLUMNS inherited from the launching shell would override the pane's real size.
    # curses.use_env(False) is not the fix: it falls back to terminfo's fixed 80x24, not the pane.
    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    curses.wrapper(body)
    return 0
