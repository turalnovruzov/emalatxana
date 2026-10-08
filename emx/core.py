"""Pure logic for emx: parsing, layout math, paths and prefs. No curses, no tmux, no subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

DEFAULT_PREFS = {"side": "right", "panel_width": 40}
SIDES = ("left", "right")
MIN_PANEL = 16
MIN_MAIN = 20
MAX_WIDTH = 500
WIDTH_STEP = 5
STALE_AFTER_MISSES = 2

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class AgentsError(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    id: str
    name: str
    cwd: str
    state: str
    status: str
    waiting_for: str
    started_at: float | None = None
    running: bool = True


def parse_agents(text: str) -> list:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentsError(f"claude agents returned bad JSON ({exc.msg})") from None
    if not isinstance(data, list):
        raise AgentsError("claude agents returned JSON that is not a list")
    return [row for row in data if isinstance(row, dict)]


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def background_sessions(rows: list) -> list:
    """Background rows with an id, in the order the CLI gave them. Interactive rows have no id and are hidden."""
    sessions = []
    for row in rows:
        sid = row.get("id")
        if row.get("kind") != "background" or not isinstance(sid, str) or not sid:
            continue
        sessions.append(
            Session(
                id=sid,
                name=_text(row.get("name")) or sid,
                cwd=_text(row.get("cwd")),
                state=_text(row.get("state")),
                status=_text(row.get("status")),
                waiting_for=_text(row.get("waitingFor")),
                started_at=_number(row.get("startedAt")),
                running=_is_running(row),
            )
        )
    return sessions


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _is_running(row: dict) -> bool:
    """Whether the session has a live process. A stopped row (and, under --all, a completed one whose process has exited) has no pid."""
    pid = row.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and row.get("state") != "stopped"


NEEDS_INPUT, WORKING, DONE, STOPPED = "needs_input", "working", "done", "stopped"
CATEGORIES = (NEEDS_INPUT, WORKING, DONE, STOPPED)
CATEGORY_LABELS = {NEEDS_INPUT: "Needs input", WORKING: "Working", DONE: "Done", STOPPED: "Stopped"}
SUMMARY_WORDS = {NEEDS_INPUT: "waiting", WORKING: "working", DONE: "done", STOPPED: "stopped"}
SUMMARY_SHORT = {NEEDS_INPUT: "wait", WORKING: "work", DONE: "done", STOPPED: "stop"}
_STATE_CATEGORY = {"blocked": NEEDS_INPUT, "working": WORKING, "done": DONE}
WORKING_FRAMES = "·✢✳✶✻✶✳✢"
GLYPHS = {NEEDS_INPUT: "✻", DONE: "✻", STOPPED: "∙"}


def category(session: Session) -> str:
    """What the row shows, from `state` alone as `claude agents` does: a finished session whose process has exited is still Done.

    Whether emx may reattach is `running`'s business, never this. A state this
    version doesn't know counts as working, so the row keeps plain colours.
    """
    if session.state == "stopped":
        return STOPPED
    return _STATE_CATEGORY.get(session.state, WORKING)


def state_word(session: Session) -> str:
    cat = category(session)
    if cat != WORKING or session.state == "working":
        return CATEGORY_LABELS[cat]
    return session.state or session.status or CATEGORY_LABELS[WORKING]


def glyph(cat: str, frame: int) -> str:
    if cat == WORKING:
        return WORKING_FRAMES[frame % len(WORKING_FRAMES)]
    return GLYPHS.get(cat, "·")


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return ""
    secs = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def session_age(session: Session, now_ms: float) -> str:
    """`startedAt` is epoch milliseconds."""
    if session.started_at is None:
        return ""
    return format_age((now_ms - session.started_at) / 1000)


OPEN, RESTART, NEW = "open", "restart", "new"


def attach_allowed(session: Session | None, trigger: str, fresh: bool = False) -> bool:
    """Whether emx may run `claude attach` on `session` for `trigger`.

    Attaching to a session with no live process resumes it, so only his
    explicit open (Enter, or a click on its row) may do that, judged on the
    row he can see. Every other trigger, including any added later, needs
    `session` to come from a listing taken just before the attach, while the
    panel's own polling is healthy (`fresh`), and to be running in it: the
    panel's rows can be a poll old, or older while listing fails. None means
    the session is not listed.
    """
    if session is None:
        return False
    if trigger == OPEN:
        return True
    return trigger in (RESTART, NEW) and fresh and session.running


def checked_row(sessions: list | None, sid: str) -> tuple:
    """(session, fresh) for `attach_allowed` from a listing just taken: `sessions` is None when that listing failed."""
    if sessions is None:
        return None, False
    return next((s for s in sessions if s.id == sid), None), True


def check_still_applies(asked: tuple, main_id: str | None, main_session: str | None) -> bool:
    """Whether a restart check's answer may act: `asked` is (session id, main pane id) from when `r` was pressed.

    The listing runs on another thread, so by the time it answers he may have
    opened another session in the main pane; the answer is then about a
    session that is no longer the one to restart.
    """
    return bool(asked[0]) and (main_session, main_id) == tuple(asked)


def refusal(name: str, trigger: str, fresh: bool, session: Session | None) -> str:
    """Why `attach_allowed` said no, for the notice line."""
    if trigger != OPEN and not fresh:
        return f"Couldn't list sessions to check {name} is still running, so emx left it alone."
    if session is None:
        return f"{name} isn't listed any more, so emx won't open it."
    return f"{name} isn't running. Press Enter on it to reopen it."


STOP, REMOVE = "stop", "remove"
CONFIRM_SECONDS = 2.0
_CLOSE_SUBCOMMAND = {STOP: "stop", REMOVE: "rm"}
_CLOSE_WORDS = {STOP: ("Stopping", "Stopped"), REMOVE: ("Removing", "Removed")}


@dataclass(frozen=True)
class Confirm:
    """A first Ctrl+X on a session row, waiting for the second. Everything the second press acts on is taken here, never from the cursor then."""

    id: str
    name: str
    running: bool
    at: float

    @property
    def action(self) -> str:
        return close_action(self.running)


def close_action(running: bool) -> str:
    """A session with a live process is stopped, keeping its conversation; any other (stopped, or done with its process gone) is removed."""
    return STOP if running else REMOVE


def close_args(claude: str, confirm: Confirm) -> list:
    """Never with `--discard-unpushed` or `--force-remove-worktree`: when `claude rm` refuses, the row stays and he decides."""
    return [claude, _CLOSE_SUBCOMMAND[confirm.action], confirm.id]


def confirm_label(confirm: Confirm) -> str:
    return f"ctrl+x again to {confirm.action}"


def confirm_holds(confirm: Confirm | None, cursor, session: Session | None, now: float) -> bool:
    """Whether an armed confirm still stands: the cursor is on its row, the panel still lists the session in the same running state, and CONFIRM_SECONDS have not passed.

    The cursor can move without a key (a poll regrouping, ← from main), so
    this is checked on every refresh, not only on keys.
    """
    return (
        confirm is not None
        and cursor == ("session", confirm.id)
        and session is not None
        and session.id == confirm.id
        and session.running == confirm.running
        and 0 <= now - confirm.at < CONFIRM_SECONDS
    )


def press_close(confirm: Confirm | None, cursor, session: Session | None, now: float) -> tuple:
    """What a Ctrl+X does: (confirm to keep armed, confirm to act on).

    `session` is the panel's row under the cursor. A second press on the
    same row while the first still holds acts on the first press's confirm;
    any other press on a session row arms a new one, so a late second press
    only re-arms. On a group header, or with no row, it does nothing.
    """
    if not cursor or cursor[0] != "session" or session is None or session.id != cursor[1]:
        return None, None
    if confirm_holds(confirm, cursor, session, now):
        return None, confirm
    return Confirm(session.id, session.name, session.running, now), None


def confirm_after_key(confirm: Confirm | None, close_key: bool, passive: bool) -> Confirm | None:
    """Any key other than Ctrl+X cancels a confirm; `passive` events (a terminal resize) are not his keypress and leave it."""
    return confirm if close_key or passive else None


def close_blocked(list_failing: bool, busy: Confirm | None) -> str | None:
    """Why a Ctrl+X may not arm or act now, or None. While listing fails the rows may be stale, and one stop or remove runs at a time."""
    if busy is not None:
        return f"Still {_CLOSE_WORDS[busy.action][0].lower()} {busy.name}…"
    if list_failing:
        return "Can't list sessions right now, so emx won't stop or remove one."
    return None


def close_still_applies(confirm: Confirm, session: Session | None, fresh: bool) -> bool:
    """Whether a confirmed stop or remove may run, judged on a listing a worker took just before running it (`checked_row`).

    The panel's row can be a poll old: a session shown stopped may have been
    resumed elsewhere since, and removing it would delete a live session.
    """
    return fresh and session is not None and session.running == confirm.running


def close_refusal(confirm: Confirm, session: Session | None, fresh: bool) -> str:
    verb = confirm.action
    if not fresh:
        return f"Couldn't list sessions to check {confirm.name}, so emx didn't {verb} it."
    if session is None:
        return f"{confirm.name} isn't listed any more, so emx didn't {verb} it."
    now = "running again" if session.running else "no longer running"
    return f"{confirm.name} is {now}, so emx didn't {verb} it."


def closing_text(confirm: Confirm) -> str:
    return f"{_CLOSE_WORDS[confirm.action][0]} {confirm.name}…"


def closed_text(confirm: Confirm) -> str:
    return f"{_CLOSE_WORDS[confirm.action][1]} {confirm.name}."


@dataclass(frozen=True)
class Placement:
    repo: str
    worktree: str = ""


def placement_from_git(cwd: str, rev_parse: str | None, branch: str | None, home: str = "", real: Callable[[str], str] | None = None) -> Placement:
    """Which repository a session's folder belongs to, from git's own answers.

    `rev_parse` is the output of `git rev-parse --path-format=absolute
    --git-common-dir --show-toplevel --show-prefix` run in `cwd`, or None
    when git failed (not a repo, folder gone, no git). `branch` is `git
    symbolic-ref --short HEAD`, or None when HEAD is detached. A worktree's
    common dir is its main repo's `.git`; any other layout (bare repo,
    submodule) groups by the folder's own top level. A repo whose top level
    is `home` (the real path of the home folder) is a dotfiles repo rather
    than a project, so each folder in it groups by itself.

    git answers in real paths (`/private/tmp/...` for `/tmp/...`). The answer
    is put back into the form `cwd` uses, so a group found by git and a group
    keyed by a bare cwd never spell the same parent folder two ways. That
    rewrite is a guess from where `cwd` sits, and guesses wrong when `cwd` is
    itself a symlink to somewhere else; `real`, the caller's realpath, keeps
    the rewritten spelling only when it leads back to git's folder.
    """
    lines = (rev_parse or "").splitlines()
    if len(lines) < 2 or not all(os.path.isabs(line) for line in lines[:2]):
        return Placement(cwd)
    common, top = os.path.normpath(lines[0]), os.path.normpath(lines[1])
    root = os.path.dirname(common) if os.path.basename(common) == ".git" else top
    if home and root == os.path.normpath(home):
        return Placement(cwd)
    repo = _cwd_form(cwd, top, lines[2] if len(lines) > 2 else None)(root)
    if real is not None and repo != root and real(repo) != root:
        repo = root
    if top == root:
        return Placement(repo)
    return Placement(repo, (branch or "").strip() or os.path.basename(top))


def _cwd_form(cwd: str, top: str, prefix: str | None):
    """A function that rewrites a real path into `cwd`'s spelling.

    `prefix` is where `cwd` sits under its real `top`, so cwd minus prefix is
    the top as `cwd` spells it; the leading folders where that and `top`
    differ (`/tmp` against `/private/tmp`) are the swap to make. With no
    prefix, or one `cwd` does not end in, paths are left as git gave them.
    """
    cwd_parts, inner = _parts(cwd), _parts(prefix or "")
    if (prefix is None or not os.path.isabs(cwd) or len(inner) > len(cwd_parts)
            or cwd_parts[len(cwd_parts) - len(inner):] != inner):
        return lambda path: path
    cwd_top, top_parts = cwd_parts[: len(cwd_parts) - len(inner)], _parts(top)
    shared = 0
    while shared < min(len(cwd_top), len(top_parts)) and cwd_top[-1 - shared] == top_parts[-1 - shared]:
        shared += 1
    real_base, cwd_base = top_parts[: len(top_parts) - shared], cwd_top[: len(cwd_top) - shared]

    def convert(path: str) -> str:
        parts = _parts(path)
        if parts[: len(real_base)] != real_base:
            return path
        return "/" + "/".join(cwd_base + parts[len(real_base):])

    return convert


def _parts(path: str) -> list:
    return [p for p in os.path.normpath(path).split("/") if p not in ("", ".")] if path else []


RETRY_FIRST_SECONDS = 5
RETRY_MAX_SECONDS = 300


def retry_after(failures: int) -> float:
    """Seconds to wait before asking git about a folder again after `failures` failed tries in a row: doubling, capped."""
    return float(min(RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** max(0, failures - 1)))


PROJECT, BY_STATE = "project", "state"
VIEWS = (PROJECT, BY_STATE)


@dataclass(frozen=True)
class Group:
    key: str
    sessions: tuple


def _oldest_first(session: Session) -> tuple:
    return (session.started_at is None, session.started_at or 0.0, session.id)


def group_sessions(sessions, placements: Mapping, view: str) -> list:
    """Groups for the panel, each with its sessions oldest first.

    By project: one group per repository, alphabetical by folder name. By
    state: Needs input, Working, Done, Stopped, leaving out empty ones.
    """
    buckets = {}
    for s in sessions:
        key = category(s) if view == BY_STATE else placements.get(s.cwd, Placement(s.cwd)).repo
        buckets.setdefault(key, []).append(s)
    if view == BY_STATE:
        keys = [c for c in CATEGORIES if c in buckets]
    else:
        keys = sorted(buckets, key=lambda k: (os.path.basename(k.rstrip("/")).lower(), k))
    return [Group(k, tuple(sorted(buckets[k], key=_oldest_first))) for k in keys]


def panel_lines(groups, collapsed) -> list:
    """The list area top to bottom: ("group", key) headers, ("session", id) rows, and None for the blank line between groups."""
    lines = []
    for i, group in enumerate(groups):
        if i:
            lines.append(None)
        lines.append(("group", group.key))
        if group.key not in collapsed:
            lines += [("session", s.id) for s in group.sessions]
    return lines


def move_cursor(items: list, cursor, delta: int):
    if not items:
        return None
    if cursor not in items:
        return items[0]
    return items[max(0, min(len(items) - 1, items.index(cursor) + delta))]


def settle_cursor(items: list, cursor, groups, wanted=None):
    """Keep the cursor on its item; a session folded out of sight leaves it on its group's header.

    `wanted` is an item the cursor should move to once it is listed, such as
    a session emx has just started; until then the cursor stays put.
    """
    if wanted is not None and wanted in items:
        return wanted
    if cursor in items:
        return cursor
    if cursor and cursor[0] == "session":
        key = cursor_group(cursor, groups)
        if ("group", key) in items:
            return ("group", key)
    return items[0] if items else None


def cursor_group(cursor, groups) -> str | None:
    if not cursor:
        return None
    if cursor[0] == "group":
        return cursor[1]
    return next((g.key for g in groups if any(s.id == cursor[1] for s in g.sessions)), None)


def summary(sessions, words: Mapping = SUMMARY_WORDS) -> list:
    """(text, category) pairs for the line at the top of the panel, nonzero counts only."""
    counts = {c: 0 for c in CATEGORIES}
    for s in sessions:
        counts[category(s)] += 1
    return [(f"{counts[c]} {words[c]}", c) for c in CATEGORIES if counts[c]]


def summary_line(sessions, width: int) -> list:
    """`fit_parts` placements for the summary, switching to short words before anything is cut."""
    full = summary(sessions)
    if not full:
        return fit_parts([("no sessions", None)], width)
    if display_width(" · ".join(t for t, _ in full)) <= width:
        return fit_parts(full, width)
    return fit_parts(summary(sessions, SUMMARY_SHORT), width)


def display_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def fit(text: str, width: int) -> str:
    """Truncate to at most `width` display columns, ending in an ellipsis when cut."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = _char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + "…"


def wrap(text: str, width: int) -> list:
    """Hard-wrap by display columns; long words are split rather than overflowing."""
    if width <= 0:
        return []
    lines = []
    for paragraph in text.splitlines() or [""]:
        line, used = "", 0
        for ch in paragraph:
            w = _char_width(ch)
            if used + w > width:
                lines.append(line)
                line, used = "", 0
            line += ch
            used += w
        lines.append(line)
    return lines


def fit_left(text: str, width: int) -> str:
    """Like `fit`, but cuts from the front, so the end of a path survives."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for ch in reversed(text):
        w = _char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "…" + "".join(reversed(out))


def wrap_words(text: str, width: int) -> list:
    """Wrap at word boundaries. textwrap counts characters, so a line of wide characters, like a word longer than the line, is then split by display columns."""
    if width <= 0:
        return []
    lines = []
    for paragraph in text.splitlines() or [""]:
        for line in textwrap.wrap(paragraph, width, break_on_hyphens=False) or [""]:
            lines += wrap(line, width)
    return lines


def short_path(path: str, width: int, home: str | None = None) -> str:
    """The path in `width` columns, `~` for the home folder.

    A path too long loses whole folders from the front, keeping as many
    trailing ones as fit (`~/…/parent/name`, then `…/parent/name`, down to
    `…/name`), so it is never cut mid-word; only a last folder too long by
    itself is cut.
    """
    home = (os.path.expanduser("~") if home is None else home).rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        path = "~" + path[len(home):]
    if display_width(path) <= width:
        return path
    parts = path.split("/")
    leads = ("~/…/", "…/") if parts[0] == "~" else ("…/",)
    for keep in range(len(parts) - 2, 0, -1):
        tail = "/".join(parts[-keep:])
        for lead in leads:
            if display_width(lead + tail) <= width:
                return lead + tail
    return fit(parts[-1], width) if parts[-1] else fit_left(path, width)


ROW_LEAD = 3
NAME_MIN = 12
STATUS_MIN = 5
DETAIL_MIN = 6
BRANCH_MIN = 4
WORKTREE_MARK = "⎇ "
BARE_MARK = WORKTREE_MARK.rstrip()


def row_label(name: str, worktree: str) -> str:
    """A row's name with its worktree mark, as wide as `row_layout` draws it when there is room."""
    return f"{name} {WORKTREE_MARK}{worktree}" if worktree else name


def name_column(rows, width: int) -> int:
    """How wide a group's name column is, so its states line up, from the group's (name, worktree) pairs.

    The column is the widest `row_label` within half the row. A worktree
    label too wide for that counts as half the row when its branch can
    shorten to fit, since `row_layout` shortens the branch to the column; one
    too wide even so counts its name and the bare mark, since `row_layout`
    then drops the branch but keeps the mark. A name too wide for that is
    left out, so one long name doesn't push every row. A row alone in its
    group has nothing to line up with, so it gets no column and keeps its
    whole branch.
    """
    rows = list(rows)
    if len(rows) < 2:
        return 0
    cap = max(0, width - ROW_LEAD) // 2
    widths = []
    for name, worktree in rows:
        label = display_width(row_label(name, worktree))
        shortest = display_width(name) + 1 + display_width(WORKTREE_MARK) + _branch_floor(worktree)
        if label > cap and worktree:
            if shortest <= cap:
                label = cap
            elif _bare_label(name) <= cap:
                label = _bare_label(name)
        widths.append(label)
    return max((w for w in widths if w <= cap), default=0)


def _branch_floor(worktree: str) -> int:
    return min(BRANCH_MIN, display_width(worktree))


def _bare_label(name: str) -> int:
    return display_width(name) + 1 + display_width(BARE_MARK)


def row_layout(name: str, worktree: str, word: str, detail: str, age: str, width: int, align: int = 0) -> list:
    """Where a session row's parts go: (x, text, part) for "name", "extra", "status" and "age".

    Columns 0 and 1 hold the in-main marker and the glyph, and the age sits
    right-aligned one column in from the edge. The status starts `align`
    columns after the name's start when the row can afford it, so states
    line up within a group; a worktree's branch shortens to stay inside that
    column, or goes when it can't, as `name_column` assumes. As space runs
    out, in order: the status detail shortens; the worktree's branch
    shortens, down to BRANCH_MIN, then goes, leaving the bare mark; the state
    word goes whole, never cut, since the glyph's colour still shows the
    state; a name too long to leave even STATUS_MIN columns is cut, down to
    NAME_MIN, to keep the state word; and last the status goes and the name
    is cut to whatever is left. A worktree row keeps at least its bare mark
    while its name is whole, and keeps it on a cut name when the whole name
    alone would have fitted.
    An empty `word` (the by-state view, whose header already says it) leaves
    the detail alone as the status.
    """
    usable = width - 1
    age = fit(age, max(0, usable - ROW_LEAD))
    age_x = usable - display_width(age)
    room = age_x - 1 - ROW_LEAD
    if room <= 0:
        return [(age_x, age, "age")] if age else []
    name_w, word_w = display_width(name), display_width(word)
    gap = 2 if word else 0

    extra = ""
    if worktree:
        spare = room - name_w - 1 - gap - word_w - display_width(WORKTREE_MARK)
        in_column = align - name_w - 1 - display_width(WORKTREE_MARK)
        if align:
            spare = min(spare, in_column)
        if spare >= _branch_floor(worktree):
            extra = WORKTREE_MARK + fit(worktree, spare)
    mark_w = _bare_label(name) - name_w if worktree else 0
    if extra or name_w + mark_w + gap + word_w <= room:
        shown, keep_word = name, True
    elif room - name_w - mark_w - 2 >= STATUS_MIN:
        shown, keep_word = name, False
    elif word and NAME_MIN <= room - 2 - word_w < name_w:
        shown, keep_word = fit(name, room - 2 - word_w), True
    elif name_w + mark_w <= room:
        shown, keep_word = name, False
    elif worktree and name_w <= room and room > mark_w:
        # The name would fit only without its mark; cutting it keeps both on screen.
        return _row(fit(name, room - mark_w), BARE_MARK, "", age, age_x)
    else:
        return _row(fit(name, room), "", "", age, age_x)
    if worktree and not extra and shown == name:
        extra = BARE_MARK

    lead_w = display_width(shown) + (1 + display_width(extra) if extra else 0)
    status_room = room - lead_w - 2
    status = _fit_status(word, detail, status_room) if keep_word else ""
    pad = max(0, align - lead_w)
    if status and pad and _fit_status(word, detail, status_room - pad) and status_room - pad >= word_w:
        status = _fit_status(word, detail, status_room - pad)
    else:
        pad = 0
    return _row(shown, extra, status, age, age_x, pad)


def _fit_status(word: str, detail: str, room: int) -> str:
    """The whole status if it fits, else its detail cut short; a detail that would keep fewer than DETAIL_MIN columns is left out."""
    status = f"{word} · {detail}" if word and detail else word or detail
    if display_width(status) <= room:
        return status
    if detail and room >= (display_width(word) + 3 if word else 0) + DETAIL_MIN:
        return fit(status, room)
    return word if display_width(word) <= room else ""


def _row(name: str, extra: str, status: str, age: str, age_x: int, pad: int = 0) -> list:
    parts = [(ROW_LEAD, name, "name")]
    x = ROW_LEAD + display_width(name)
    if extra:
        parts.append((x + 1, extra, "extra"))
        x += 1 + display_width(extra)
    if status:
        parts.append((x + 2 + pad, status, "status"))
    if age:
        parts.append((age_x, age, "age"))
    return parts


def header_layout(view: str, key: str, count: int, collapsed: bool, width: int, home: str | None = None) -> list:
    """(x, text, part) for a group header: "arrow", then "dir" and "name" (the path, folder name last), then "count".

    The count shows only on a collapsed group, as in `claude agents`; an open
    group's rows count themselves. A long path loses folders from the front,
    so the folder name is the last to go. In the state view the name is the
    state's label and there is no dir.
    """
    usable = width - 1
    arrow = "▸" if collapsed else "▾"
    count_text = str(count) if collapsed else ""
    room = usable - 2 - (1 + len(count_text) if count_text else 0)
    if room <= 0:
        return [(0, fit(arrow, usable), "arrow")]
    if view == BY_STATE:
        dir_part, name = "", fit(CATEGORY_LABELS.get(key, key), room)
    else:
        name = os.path.basename(key.rstrip("/")) or key or "?"
        shown = short_path(key.rstrip("/") or key, room, home) if key else name
        if shown.endswith("/" + name) and display_width(shown) > display_width(name):
            dir_part = shown[: len(shown) - len(name)]
        else:
            dir_part, name = "", fit(name, room)
    parts = [(0, arrow, "arrow")]
    x = 2
    if dir_part:
        parts.append((x, dir_part, "dir"))
        x += display_width(dir_part)
    parts.append((x, name, "name"))
    x += display_width(name)
    if count_text:
        parts.append((x + 1, count_text, "count"))
    return parts


def fit_parts(parts: list, width: int, sep: str = " · ") -> list:
    """(x, text, tag) placements for a run of (text, tag) parts joined by `sep`; the part that overflows ends in an ellipsis and the rest are left out."""
    placed, x = [], 0
    for i, (text, tag) in enumerate(parts):
        if i:
            if x + display_width(sep) >= width:
                break
            placed.append((x, sep, None))
            x += display_width(sep)
        shown = fit(text, width - x)
        if shown:
            placed.append((x, shown, tag))
        x += display_width(shown)
        if shown != text:
            break
    return placed


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def first_message(text: str) -> str:
    """The first meaningful line of CLI output, for showing as a one-line error."""
    for line in strip_ansi(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


def one_line(text: str) -> str:
    """All of a CLI message on one line, for a refusal whose reason may come after its first line."""
    return " ".join(line.strip() for line in strip_ansi(text).splitlines() if line.strip())


@dataclass(frozen=True)
class Layout:
    panel: int
    main: int


def compute_layout(window_width: int, panel_width: int) -> Layout:
    """Widths for panel and main; main takes everything the panel leaves. One one-column border sits between the two panes; tmux needs each pane at least one column wide."""
    avail = max(window_width - 1, 2)
    panel = max(1, min(panel_width, avail - MIN_MAIN))
    main = max(1, avail - panel)
    return Layout(panel=panel, main=main)


def width_action(applied: tuple | None, actual: tuple) -> str | None:
    """Decide what a sidebar tick does about pane widths.

    Both tuples are (window_width, panel_width). A new window width means the
    terminal resized, so the layout is reapplied from prefs. The same window
    width with a different panel width means he dragged the panel border, so
    "adopt-panel" makes that width the new pref.
    """
    if applied is None or actual[0] != applied[0]:
        return "apply"
    if actual[1] != applied[1]:
        return "adopt-panel"
    return None


def counts_for_version(relpath: str) -> bool:
    """Whether a file under the code tree feeds the version stamp. Bytecode, dotfiles and editor backups appear on their own, and would change the stamp with no code change."""
    parts = relpath.replace(os.sep, "/").split("/")
    return not (
        "__pycache__" in parts
        or any(p.startswith(".") for p in parts)
        or relpath.endswith((".pyc", "~"))
    )


def code_version(files) -> str:
    """A stamp for the code a workspace runs, from (relative_path, bytes) pairs in any order."""
    digest = hashlib.sha256()
    for name, data in sorted(files):
        digest.update(name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()[:16]


def needs_upgrade(stamp: str, current: str) -> bool:
    return not stamp or stamp != current


def relaunch_plan(panes, upgrade: bool) -> tuple:
    """What the launcher does to a running workspace: (sidebar_ids_to_respawn, spacer_ids_to_kill).

    `panes` are objects with .id, .window, .dead, .role and .session. Without
    an upgrade only a dead sidebar is revived. With one, every sidebar is
    respawned on the new code and every spacer (made only by releases before
    the reading-width cap was removed) is dropped. A pane holding a session
    is never chosen, whatever its role, and neither is the last pane of a
    window, so the server and every viewer survive.
    """
    panes = list(panes)
    ours = [p for p in panes if not p.session]
    respawn = [p.id for p in ours if p.role == "sidebar" and (upgrade or p.dead)]
    drop = []
    if upgrade:
        for p in ours:
            if p.role != "spacer":
                continue
            others = [q for q in panes if q.window == p.window and q.id != p.id and q.id not in drop]
            if others:
                drop.append(p.id)
    return respawn, drop


def prefs_path(env: Mapping | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME", "")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "emx" / "prefs.json"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def normalize_prefs(raw) -> dict:
    prefs = dict(DEFAULT_PREFS)
    if not isinstance(raw, dict):
        return prefs
    if raw.get("side") in SIDES:
        prefs["side"] = raw["side"]
    value = raw.get("panel_width")
    if isinstance(value, int) and not isinstance(value, bool):
        prefs["panel_width"] = _clamp(value, MIN_PANEL, MAX_WIDTH)
    if isinstance(raw.get("last_folder"), str) and raw["last_folder"]:
        prefs["last_folder"] = raw["last_folder"]
    if raw.get("view") in VIEWS:
        prefs["view"] = raw["view"]
    collapsed = raw.get("collapsed")
    if isinstance(collapsed, dict):
        clean = {}
        for view in VIEWS:
            keys = collapsed.get(view)
            if isinstance(keys, list):
                keys = sorted({k for k in keys if isinstance(k, str) and k})
                if keys:
                    clean[view] = keys
        if clean:
            prefs["collapsed"] = clean
    return prefs


def current_view(prefs: dict) -> str:
    return prefs.get("view", PROJECT)


def toggle_view(prefs: dict) -> dict:
    return {**prefs, "view": BY_STATE if current_view(prefs) == PROJECT else PROJECT}


def collapsed_keys(prefs: dict, view: str) -> frozenset:
    return frozenset(prefs.get("collapsed", {}).get(view, ()))


def toggle_collapsed(prefs: dict, view: str, key: str) -> dict:
    """Each view keeps its own folded groups, so switching views and back restores them."""
    keys = set(collapsed_keys(prefs, view)) ^ {key}
    collapsed = {**prefs.get("collapsed", {}), view: sorted(keys)}
    return {**prefs, "collapsed": {v: ks for v, ks in collapsed.items() if ks}}


def load_prefs(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    return normalize_prefs(raw)


def save_prefs(path: Path, prefs: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(normalize_prefs(prefs), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def adjust_panel_width(prefs: dict, delta: int) -> dict:
    return {**prefs, "panel_width": _clamp(prefs["panel_width"] + delta, MIN_PANEL, MAX_WIDTH)}


def flip_side(prefs: dict) -> dict:
    return {**prefs, "side": "left" if prefs["side"] == "right" else "right"}


def resolve_folder(text: str, base: str) -> str:
    """Expand `~` and $VARS; a relative path is taken relative to `base`; empty means `base`."""
    expanded = os.path.expanduser(os.path.expandvars(text.strip()))
    if not expanded:
        return base
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.normpath(expanded)


def complete_dir(text: str, base: str) -> tuple:
    """Tab-complete a directory path. Returns (new_text, matching_names); the text keeps the form he typed, `~` included."""
    if text == "~":
        return "~/", []
    head, sep, tail = text.rpartition("/")
    listing = resolve_folder(head + sep, base) if sep else base
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(listing)
            if entry.name.startswith(tail)
            and (tail.startswith(".") or not entry.name.startswith("."))
            and entry.is_dir()
        )
    except OSError:
        return text, []
    if not names:
        return text, []
    if len(names) == 1:
        return head + sep + names[0] + "/", names
    return head + sep + os.path.commonprefix(names), names


def new_session_args(claude: str, message: str) -> list:
    """`--` stops a first message that starts with `-` being read as a flag."""
    args = [claude, "--bg"]
    if message.strip():
        args += ["--", message]
    return args


def parse_new_session_id(output: str) -> str | None:
    clean = strip_ansi(output)
    match = re.search(r"claude attach (\S+)", clean) or re.search(r"backgrounded\s*·\s*(\S+)", clean)
    return match.group(1) if match else None


def viewer_command(claude: str, session_id: str) -> str:
    return f"exec {shlex.quote(claude)} attach {shlex.quote(session_id)}"


def stale_viewers(viewer_ids, live_ids, misses: dict) -> tuple:
    """Viewers whose session is missing from STALE_AFTER_MISSES polls in a row.

    One missing poll is not enough: a single glitchy listing would otherwise
    close the viewer he is reading.
    Returns (updated_misses, ids_to_close).
    """
    live = set(live_ids)
    updated, close = {}, []
    for sid in viewer_ids:
        if sid in live:
            continue
        count = misses.get(sid, 0) + 1
        if count >= STALE_AFTER_MISSES:
            close.append(sid)
        else:
            updated[sid] = count
    return updated, close


def listed_session(rows: list, session_id: str) -> Session | None:
    """The background session whose `id` is exactly `session_id` in rows from `claude agents --json`, or None."""
    if not session_id:
        return None
    return next((s for s in background_sessions(rows) if s.id == session_id), None)


def error_line(exc: BaseException) -> str:
    return first_message(str(exc)) or type(exc).__name__


_FULL_RULE = re.compile(r"^─+$")
_NAME_RULE = re.compile(r"^─*\s+(\S.*?)\s+─$")
_PROMPT = re.compile(r"^❯(\s|$)")
_AGENT_FOOTER = re.compile(r"^\s*enter to return\b|\bctrl\+x to delete\b")
_AGENT_COUNT = r"\d+ (?:awaiting input|working|completed)"
_AGENT_HEADER = re.compile(rf"^[\s▐▛▜▌▝▘█]*{_AGENT_COUNT}(?: · {_AGENT_COUNT})+$")
_PROMPT_REACH = 12


def main_view_state(captured: str, expected_name: str, pane_width: int) -> str:
    """What a `claude attach` pane is showing, judged from its captured text.

    "agent_view" when ← has turned it into the all-sessions view, "mismatch"
    when its prompt's top rule names a session other than `expected_name`,
    "ok" when the rule names it, and "unknown" otherwise. Only positive
    evidence raises an alarm: dialogs, startup, transcript text and an empty
    `expected_name` (an unnamed session) all come back "unknown".
    `pane_width` is the width tmux reports for the pane the text came from.
    """
    lines = [line for line in (strip_ansi(raw).rstrip() for raw in captured.splitlines()) if line.strip()]
    if not lines:
        return "unknown"
    for i in range(max(1, len(lines) - 2), len(lines)):
        if _AGENT_FOOTER.search(lines[i]) and _FULL_RULE.match(lines[i - 1]):
            return "agent_view"
    above = _line_above_prompt(lines, pane_width)
    if above is None:
        return "unknown"
    if _FULL_RULE.match(above):
        if any(_AGENT_HEADER.match(line) for line in lines[:4]):
            return "agent_view"
        return "unknown"
    match = _NAME_RULE.match(above)
    if not match or not expected_name.strip():
        return "unknown"
    return "ok" if _names_match(match.group(1), expected_name) else "mismatch"


def _line_above_prompt(lines: list, pane_width: int) -> str | None:
    """The line over the input box: the last `❯` line near the bottom with a pane-wide rule under it.

    Both rules of the real input box span the whole pane, so requiring that
    width keeps a rule-and-prompt look-alike quoted in the transcript from counting.
    """
    start = max(1, len(lines) - _PROMPT_REACH)
    for i in range(len(lines) - 2, start - 1, -1):
        if not _PROMPT.match(lines[i]):
            continue
        if any(_FULL_RULE.match(line) and display_width(line) == pane_width for line in lines[i + 1:]):
            above = lines[i - 1]
            return above if display_width(above) == pane_width else None
    return None


def _names_match(shown: str, expected: str) -> bool:
    """Claude Code cuts a long name to fit and ends it with an ellipsis, so a cut name matches by prefix."""
    shown, expected = " ".join(shown.split()), " ".join(expected.split())
    if shown.endswith("…"):
        return expected.startswith(shown[:-1].rstrip())
    return shown == expected


PROMPT_TEXT_X = 2


def empty_prompt(line: str, below: str, cursor_x: int, cursor_shown: bool, pane_width: int) -> bool:
    """Whether the cursor sits in Claude Code's empty input box, where ← would open its agent view.

    `line` is the cursor's line and `below` the line under it, both captured
    with their styling (`capture-pane -e`). The box is empty when its `❯`
    starts the line, the cursor is right after it, nothing after it is typed
    text, and the box's pane-wide bottom rule follows at once. The example
    Claude Code shows in an empty box (`Try "…"`) is drawn dim, which is the
    only thing that tells it from the same words typed and the cursor moved
    back to the start. A dialog or menu indents its `❯`, and a second line of
    input sits between the first and the rule.
    """
    if not cursor_shown or cursor_x != PROMPT_TEXT_X:
        return False
    cells = _styled_cells(line)
    if not cells or cells[0][0] != "❯" or (len(cells) > 1 and not cells[1][0].isspace()):
        return False
    if any(not ch.isspace() and not dim for ch, dim in cells[PROMPT_TEXT_X:]):
        return False
    rule = strip_ansi(below).rstrip()
    return bool(_FULL_RULE.match(rule)) and display_width(rule) == pane_width


_SGR = re.compile(r"\x1b\[([0-9;:]*)m")


def _styled_cells(line: str) -> list:
    """(character, dim) for each character of a `capture-pane -e` line, with the escape sequences applied and dropped."""
    cells, dim, at = [], False, 0
    while at < len(line):
        escape = _ANSI.match(line, at)
        if escape is None:
            cells.append((line[at], dim))
            at += 1
            continue
        sgr = _SGR.fullmatch(escape.group(0))
        if sgr is not None:
            dim = _dim_after(sgr.group(1), dim)
        at = escape.end()
    return cells


def _dim_after(params: str, dim: bool) -> bool:
    """Apply one SGR's parameters to the dim flag. A colour's own numbers (`38;5;2`, `38;2;r;g;b`) are skipped, so a 2 among them is not read as dim."""
    parts = params.split(";")
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("", "0", "22"):
            dim = False
        elif p == "2":
            dim = True
        elif p in ("38", "48", "58") and i + 1 < len(parts):
            i += {"5": 2, "2": 4}.get(parts[i + 1], 0)
        i += 1
    return dim


def focus_report(keys: list) -> bool | None:
    """True for a focus-in report (ESC [ I), False for focus-out (ESC [ O), None for any other keys."""
    if len(keys) == 3 and keys[0] == "\x1b" and keys[1] == "[" and keys[2] in ("I", "O"):
        return keys[2] == "I"
    return None


def may_be_focus_report(keys: list) -> bool:
    """Whether `keys` could still grow into a focus report, so the next key is worth reading."""
    return keys in (["\x1b"], ["\x1b", "["])


def cursor_on_focus(items: list, cursor, groups, main_sid: str):
    """Where the panel's cursor goes when focus arrives from the main pane: on the session showing there, as `claude agents` does on ←.

    A session folded out of sight leaves it on its group's header; with no
    listed session in main the cursor stays put.
    """
    target = ("session", main_sid)
    if not main_sid or cursor_group(target, groups) is None:
        return cursor
    return settle_cursor(items, target, groups)


def came_back(active: bool, marked: bool) -> bool:
    """Whether the panel has just been reached by ←: the binding marks it and selects it in one command list, so a mark on an inactive panel was left behind (focus moved on before the panel read it) and does not count."""
    return active and marked


def selection_bar(selected: bool, focused: bool) -> str | None:
    """Which bar a panel row is drawn on: none, the bright one while the panel has focus, or the dim one."""
    if not selected:
        return None
    return "focused" if focused else "unfocused"


def title_style(focused: bool) -> str:
    """The panel's top line is bold while the panel has focus and dim otherwise."""
    return "bold" if focused else "dim"


RESTORE, GIVE_UP = "restore", "give_up"
RESTORE_BACKOFF_SECONDS = 5.0
RESTORES_BEFORE_GIVING_UP = 2


def auto_restore(state: str, pane_id: str, tried: bool, checking: bool, last_restore: tuple | None, now: float) -> str | None:
    """What emx does by itself about the main pane: RESTORE its session, GIVE_UP restoring it, or nothing (None).

    Only the agent view qualifies: Claude Code opened it in place of the
    session, and the session itself is untouched. A different session showing
    ("mismatch") is left to him, and so is a viewer emx already tried, so a
    refusal is said once rather than every half second. `last_restore` is
    (pane id, time, count) of emx's latest automatic restores of one pane
    (`record_restore`), kept until an "ok" read of that pane
    (`last_restore_after`). The agent view back on that pane within
    RESTORE_BACKOFF_SECONDS, or back after RESTORES_BEFORE_GIVING_UP restores
    with no "ok" read between them, means the detection is probably wrong, and
    restoring again would respawn main and take his focus on every cycle, so
    emx gives up and leaves the "press r" warning to him.
    """
    if state != "agent_view" or tried or checking:
        return None
    if last_restore is not None and last_restore[0] == pane_id:
        _, at, count = last_restore
        if now - at < RESTORE_BACKOFF_SECONDS or count >= RESTORES_BEFORE_GIVING_UP:
            return GIVE_UP
    return RESTORE


def record_restore(last_restore: tuple | None, pane_id: str, now: float) -> tuple:
    """`last_restore` after emx restores `pane_id` at `now`: restores of the same pane add up until an "ok" read resets them."""
    count = last_restore[2] + 1 if last_restore is not None and last_restore[0] == pane_id else 1
    return (pane_id, now, count)


def restore_still_wanted(auto: bool, state: str | None) -> bool:
    """Whether a check's answer may still respawn main: always for his r, and for emx's own restore only while main still shows the agent view.

    Otherwise he has moved on inside Claude Code meanwhile (picked a session,
    or went back to work), and `state` (main's view now) says so.
    """
    return not auto or state == "agent_view"


def notice_expired(until: float | None, now: float) -> bool:
    """Whether a notice shown only for a while (`until` is when it ends, None for one that stays) is due to go."""
    return until is not None and now >= until


def last_restore_after(last_restore: tuple | None, pane_id: str, state: str) -> tuple | None:
    """An "ok" read of the restored pane shows the restore worked, so a later agent view there is restored again."""
    if last_restore is not None and last_restore[0] == pane_id and state == "ok":
        return None
    return last_restore


def still_tried(tried: frozenset, flagged: frozenset) -> frozenset:
    """The panes emx already tried to restore, kept only while they stay flagged; once a flag clears (an "ok" read or a respawn) the next agent view there is a new episode."""
    return frozenset(tried) & frozenset(flagged)


def restoring_main(asked: tuple, auto: bool, main_id: str | None) -> bool:
    """Whether a running check is emx's own restore of the pane now in main; `asked` is the check's (session id, pane id)."""
    return auto and main_id is not None and asked[1] == main_id


def off_main_warning(flagged: frozenset, main_id: str | None, restoring: bool) -> bool:
    """Whether the panel says main isn't showing its session: it is flagged, and emx is not already bringing it back."""
    return main_id is not None and main_id in flagged and not restoring


def latch_off_main(flagged: frozenset, pane_id: str, state: str) -> tuple:
    """Track viewers caught showing something other than their own session.

    A pane id joins the set on "agent_view" or "mismatch" and leaves it only on
    "ok" (or when the caller respawns it), so the flag survives the viewer
    leaving the main pane and an "unknown" read such as a dialog. Returns
    (new_flagged, respawn): respawn says the viewer needs a fresh
    `claude attach` before it is shown again.
    """
    if state in ("agent_view", "mismatch"):
        flagged = flagged | {pane_id}
    elif state == "ok":
        flagged = flagged - {pane_id}
    return frozenset(flagged), pane_id in flagged
