"""The reader: what `emx read` shows in the big pane, a Claude session behind a routine's run, read-only.

It only fetches (`probe read` and `probe stat` at the run's place) and draws. No key
reaches the session, and nothing here resumes, sends to or stops one.
"""

from __future__ import annotations

import curses
import json
import locale
import os
import time

from emx import core, places, probe
from emx.tmux import VIEW_OPT, Tmux, TmuxError

READER_SECONDS = 3
READER_TIMEOUT = 60
KEY_WAIT_MS = 500
EARLIER = "  ↑ {} earlier step{}"
GONE = "That place isn't in the config any more."
CHROME_ROWS = core.READER_HEADER_ROWS + 1


class Doc:
    """One session as read: its text, steps, title, ending, and the file's size and mtime when it was read, or why it couldn't be."""

    def __init__(self, text: str = "", error: str = "", stamp=None, now: float = 0.0, local=time.localtime):
        self.error, self.stamp = error, stamp
        self.steps = core.reader_steps(text)
        self.title = core.reader_title(text)
        self.ending = core.reader_ending(text, now, local)


class Reader:
    """`fetch(sid)` returns (text, error), `stat(sid)` returns ((size, mtime), error); both are the caller's to run, so this class makes no process of its own."""

    def __init__(self, scr, place, label: str, sids: list, fetch, stat, palette, on_show=None,
                 clock=time.monotonic, now=time.time, local=time.localtime):
        self.scr, self.place, self.label, self.sids = scr, place, label, list(sids)
        self.fetch, self.stat, self.palette, self.on_show = fetch, stat, palette, on_show
        self.clock, self.now, self.local = clock, now, local
        self.docs, self.at, self.top, self.pinned, self.checked = {}, len(self.sids) - 1, 0, True, clock()

    @property
    def sid(self) -> str:
        return self.sids[self.at]

    @property
    def doc(self) -> Doc:
        return self.docs[self.sid]

    def load(self, i: int | None = None) -> None:
        """Read session `i` (the one shown by default) and show its end, or keep the place if the view was above the end."""
        if i is not None:
            self.at, self.pinned = i, True
        sid = self.sid
        stamp = None
        if self.place.host is None:
            stamp, _ = self.stat(sid)
        text, error = self.fetch(sid)
        self.docs[sid] = Doc(text or "", error or "", stamp, self.now(), self.local)
        if self.on_show:
            self.on_show(sid)

    def header(self) -> tuple:
        n = len(self.sids)
        title = self.docs[self.sid].title if self.sid in self.docs else ""
        if title and title in self.label:
            title = ""
        note = "read-only"
        if n > 1:
            note += f" · session {self.at + 1} of {n}" + (f" ({title})" if title else "")
        elif title:
            note += f" · {title}"
        return self.label, note

    def rows(self, width: int) -> list:
        doc = self.doc
        if doc.error:
            head = core.reader_rows([], self.header(), None, width, self.local)
            return head + [(core.fit(doc.error, width), "error", (), -1)]
        return core.reader_rows(doc.steps, self.header(), doc.ending, width, self.local)

    def body_height(self) -> int:
        return max(1, self.scr.getmaxyx()[0] - CHROME_ROWS)

    def last_top(self, rows: list) -> int:
        return max(0, len(rows) - core.READER_HEADER_ROWS - self.body_height())

    def draw(self) -> None:
        height, width = self.scr.getmaxyx()
        width = max(1, width - 1)
        rows = self.rows(width)
        body = rows[core.READER_HEADER_ROWS:]
        room = self.body_height()
        last = self.last_top(rows)
        self.top = last if self.pinned else max(0, min(self.top, last))
        self.scr.erase()
        for y, row in enumerate(rows[:core.READER_HEADER_ROWS]):
            self.put_row(y, row)
        if self.top and body:
            above = body[self.top][3]
            above = len(self.doc.steps) if above < 0 else above
            self.put(core.READER_HEADER_ROWS, EARLIER.format(above, "" if above == 1 else "s"), self.palette.attr("muted"))
        for y, row in enumerate(body[self.top:self.top + room], start=CHROME_ROWS):
            self.put_row(y, row)
        self.scr.refresh()

    def put(self, y: int, text: str, attr: int, x: int = 0) -> None:
        height, width = self.scr.getmaxyx()
        if 0 <= y < height and x < width - 1:
            try:
                self.scr.addstr(y, x, core.fit(text, width - 1 - x), attr)
            except curses.error:
                pass

    def put_row(self, y: int, row: tuple) -> None:
        text, tone, spans, _ = row
        self.put(y, text, self.palette.attr(tone))
        for start, end, over in spans:
            attr = curses.A_BOLD if over == "bold" else self.palette.attr(over)
            self.put(y, text[start:end], attr, start)

    def key(self, key) -> bool:
        """Whether the reader goes on. Every key but the ones below is swallowed."""
        rows = self.rows(max(1, self.scr.getmaxyx()[1] - 1))
        last, page = self.last_top(rows), max(1, self.body_height() - 1)
        if self.pinned:
            self.top = last
        if key == "\x1b":
            return False
        if key in (curses.KEY_UP, "k"):
            self.top -= 1
        elif key in (curses.KEY_DOWN, "j"):
            self.top += 1
        elif key == curses.KEY_PPAGE:
            self.top -= page
        elif key == curses.KEY_NPAGE:
            self.top += page
        elif key == "g":
            self.top = 0
        elif key == "G":
            self.top = last
        elif key == "\t" and len(self.sids) > 1:
            self.load((self.at + 1) % len(self.sids))
            return True
        self.top = max(0, min(self.top, last))
        self.pinned = self.top >= last
        return True

    def tick(self) -> None:
        """A run still going, at a place reached without ssh, is read again when its file changed; over ssh the session is read when opened and on Tab only."""
        doc = self.doc
        if self.place.host is not None or doc.ending is None or doc.ending[0] != core.GOING:
            return
        if self.clock() - self.checked < READER_SECONDS:
            return
        self.checked = self.clock()
        stamp, error = self.stat(self.sid)
        if error is None and stamp != doc.stamp:
            self.load()

    def loop(self) -> int:
        self.load()
        while True:
            self.draw()
            try:
                key = self.scr.get_wch()
            except curses.error:
                key = None
            if key is not None and not self.key(key):
                return 0
            self.tick()


def runner(place, prefs: dict):
    def fetch(sid: str) -> tuple:
        if place is None:
            return None, GONE
        return places.run(place, probe.command("read", sid), prefs, timeout=READER_TIMEOUT)

    def stat(sid: str) -> tuple:
        if place is None:
            return None, GONE
        out, error = places.run(place, probe.command("stat", sid), prefs, timeout=READER_TIMEOUT)
        try:
            found = json.loads(out or "")
            return (found["size"], found["mtime"]), None
        except (ValueError, KeyError, TypeError):
            return None, error or "stat gave no answer"

    return fetch, stat


def run(socket: str, place_name: str, label: str, sids: list) -> int:
    from emx.sidebar import Palette

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    prefs = core.load_prefs(core.prefs_files())
    place = places.find(prefs, place_name)
    fetch, stat = runner(place, prefs)
    pane = os.environ.get("TMUX_PANE", "")

    def tag(sid: str) -> None:
        try:
            if pane:
                Tmux(socket).set_pane_option(pane, VIEW_OPT, sid)
        except (TmuxError, OSError):
            pass

    def body(scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.keypad(True)
        scr.timeout(KEY_WAIT_MS)
        Reader(scr, place or places.HERE, label, sids, fetch, stat, Palette(core.scheme(prefs)), tag).loop()

    os.environ.pop("LINES", None)
    os.environ.pop("COLUMNS", None)
    curses.wrapper(body)
    return 0
