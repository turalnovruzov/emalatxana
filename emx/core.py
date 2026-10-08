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
            )
        )
    return sessions


def state_label(session: Session) -> str:
    label = {"working": "working", "blocked": "waiting for you", "done": "done"}.get(
        session.state, session.state or session.status or "unknown"
    )
    if session.waiting_for:
        label = f"{label}: {session.waiting_for}"
    return label


def folder_tail(cwd: str, home: str | None = None) -> str:
    if not cwd:
        return ""
    home = os.path.expanduser("~") if home is None else home
    stripped = cwd.rstrip("/") or "/"
    if home and stripped == home.rstrip("/"):
        return "~"
    return os.path.basename(stripped) or stripped


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
    return "".join(out) + "…"


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
    """Whether a file under the code tree feeds the version stamp. Bytecode and dotfiles appear on their own, and would change the stamp with no code change."""
    parts = relpath.replace(os.sep, "/").split("/")
    return not (
        "__pycache__" in parts or any(p.startswith(".") for p in parts) or relpath.endswith(".pyc")
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
    the reading-width cap was removed) is dropped. A pane holding a session is never chosen, whatever
    its role, and neither is the last pane of a window, so the server and
    every viewer survive.
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
    return prefs


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
