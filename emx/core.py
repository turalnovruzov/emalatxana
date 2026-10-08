"""Pure logic for emx: parsing, layout math, paths and prefs. No curses, no tmux, no subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    """A state this version doesn't know counts as working, so the row keeps plain colours."""
    if not session.running:
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


def attach_allowed(session: Session | None, trigger: str) -> bool:
    """Whether emx may run `claude attach` on `session` for `trigger`.

    Attaching to a session with no live process resumes it. Only his explicit
    open (Enter, or a click on its row) may do that; every other trigger,
    including any added later, is refused for a stopped session. `session` is
    its row in the latest poll; None means unlisted, and only a session emx
    has just started may be attached before it shows up.
    """
    if session is None:
        return trigger == NEW
    if session.running:
        return trigger in (OPEN, RESTART, NEW)
    return trigger == OPEN


@dataclass(frozen=True)
class Placement:
    repo: str
    worktree: str = ""


def placement_from_git(cwd: str, rev_parse: str | None, branch: str | None) -> Placement:
    """Which repository a session's folder belongs to, from git's own answers.

    `rev_parse` is the output of `git rev-parse --path-format=absolute
    --git-common-dir --show-toplevel` run in `cwd`, or None when git failed
    (not a repo, folder gone, no git). `branch` is `git symbolic-ref --short
    HEAD`, or None when HEAD is detached. A worktree's common dir is its main
    repo's `.git`; any other layout (bare repo, submodule) groups by the
    folder's own top level.
    """
    lines = (rev_parse or "").splitlines()
    if len(lines) < 2 or not all(os.path.isabs(line) for line in lines[:2]):
        return Placement(cwd)
    common, top = os.path.normpath(lines[0]), os.path.normpath(lines[1])
    root = os.path.dirname(common) if os.path.basename(common) == ".git" else top
    if top == root:
        return Placement(root)
    return Placement(root, (branch or "").strip() or os.path.basename(top))


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


def settle_cursor(items: list, cursor, groups):
    """Keep the cursor on its item; a session folded out of sight leaves it on its group's header."""
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


def short_path(path: str, width: int, home: str | None = None) -> str:
    home = (os.path.expanduser("~") if home is None else home).rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        path = "~" + path[len(home):]
    return fit_left(path, width)


ROW_LEAD = 3
NAME_MIN = 12
STATUS_MIN = 5
DETAIL_MIN = 6
WORKTREE_MARK = "⎇ "


def row_layout(name: str, worktree: str, word: str, detail: str, age: str, width: int) -> list:
    """Where a session row's parts go: (x, text, part) for "name", "extra", "status" and "age".

    Columns 0 and 1 hold the in-main marker and the glyph, and the age sits
    right-aligned one column in from the edge. As space runs out, in order:
    the status detail shortens and the worktree goes; the state word
    shortens; a name too long to leave even that is cut, down to NAME_MIN,
    to keep the whole state word; and last the status goes and the name is
    cut to whatever is left.
    """
    usable = width - 1
    age = fit(age, max(0, usable - ROW_LEAD))
    age_x = usable - display_width(age)
    room = age_x - 1 - ROW_LEAD
    if room <= 0:
        return [(age_x, age, "age")] if age else []
    status = f"{word} · {detail}" if detail else word
    extra = WORKTREE_MARK + worktree if worktree else ""
    name_w, word_w, extra_w = display_width(name), display_width(word), display_width(extra)

    if extra and name_w + 1 + extra_w + 2 + word_w <= room:
        status_room = room - name_w - 1 - extra_w - 2
        return _row(name, extra, _fit_status(status, word, status_room), age, age_x)
    if name_w + 2 + word_w <= room:
        return _row(name, "", _fit_status(status, word, room - name_w - 2), age, age_x)
    if room - name_w - 2 >= STATUS_MIN:
        return _row(name, "", fit(word, room - name_w - 2), age, age_x)
    if room - 2 - word_w >= NAME_MIN:
        return _row(fit(name, room - 2 - word_w), "", word, age, age_x)
    return _row(fit(name, room), "", "", age, age_x)


def _fit_status(status: str, word: str, room: int) -> str:
    """The whole status if it fits, else its detail cut short; a detail that would keep fewer than DETAIL_MIN columns is left out."""
    if display_width(status) <= room:
        return status
    if room >= display_width(word) + 3 + DETAIL_MIN:
        return fit(status, room)
    return word


def _row(name: str, extra: str, status: str, age: str, age_x: int) -> list:
    parts = [(ROW_LEAD, name, "name")]
    x = ROW_LEAD + display_width(name)
    if extra:
        parts.append((x + 1, extra, "extra"))
        x += 1 + display_width(extra)
    if status:
        parts.append((x + 2, status, "status"))
    if age:
        parts.append((age_x, age, "age"))
    return parts


def header_layout(view: str, key: str, count: int, collapsed: bool, width: int, home: str | None = None) -> list:
    """(x, text, part) for a group header: "arrow", then "dir" and "name" (the path, folder name last), then "count".

    A long path is cut from the front, so the folder name is the last to go.
    In the state view the name is the state's label and there is no dir.
    """
    usable = width - 1
    arrow = "▸" if collapsed else "▾"
    count_text = str(count)
    room = usable - 2 - 1 - len(count_text)
    if room <= 0:
        return [(0, fit(arrow, usable), "arrow")]
    if view == BY_STATE:
        dir_part, name = "", fit(CATEGORY_LABELS.get(key, key), room)
    else:
        name = os.path.basename(key.rstrip("/")) or key or "?"
        shown = short_path(key.rstrip("/") or key, room, home) if key else name
        if shown.endswith(name) and display_width(shown) > display_width(name):
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


def id_listed(rows: list, session_id: str) -> bool:
    """Whether `session_id` is exactly the `id` of some row from `claude agents --json`."""
    return bool(session_id) and any(row.get("id") == session_id for row in rows)


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
