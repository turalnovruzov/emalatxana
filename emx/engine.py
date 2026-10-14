"""The own engine's side effects: the registry of sessions under emx's state folder, starting them in tmux, and gathering what they report.

Gathers, never decides: every rule about what the files and panes mean is in
`emx.core`. Everything lives under `core.state_path()`; nothing here writes
under `~/.claude`, whose transcripts are only read. Wrappers return
`(value, error)` and never raise, except `hook_command`, which is pure.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
import uuid

from emx import BIN, core
from emx.tmux import AGENT_OPT, SESSION_OPT, Tmux, TmuxError

AGENT = "claude"
READ_TAIL_BYTES = 64 * 1024


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def sessions_root(state) -> str:
    return os.path.join(str(state), "sessions")


def session_folder(state, key: str) -> str:
    return os.path.join(sessions_root(state), key)


def hook_command(state, key: str) -> str:
    """The command every hook of one session runs. The state folder is written in so a hook never re-derives it from the pane's environment, which comes from the tmux server, not from the emx that wrote the file. The shell forces exit 0 and silence: an interpreter that cannot open its script exits 2, which Claude Code reads as "block"."""
    words = " ".join(shlex.quote(a) for a in [sys.executable, "-I", BIN, "hook", str(state), key])
    return f"{words} >/dev/null 2>&1; exit 0"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_atomic(path: str, data: dict, mode: int = 0o600) -> None:
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_record(state, key: str) -> dict:
    with open(os.path.join(session_folder(state, key), "session.json"), encoding="utf-8") as fh:
        record = json.load(fh)
    if not isinstance(record, dict) or record.get("key") != key:
        raise ValueError("not this session's record")
    return record


def _write_settings(state, key: str) -> None:
    _write_atomic(os.path.join(session_folder(state, key), "settings.json"), core.hook_settings(hook_command(state, key)))


def _tail_lines(path: str, limit: int = READ_TAIL_BYTES) -> list:
    """The complete lines in the last `limit` bytes of the file; a line cut by the window is dropped. A missing file reads as no lines."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - limit)
            fh.seek(max(0, start - 1))
            window = fh.read()
            on_boundary = start == 0 or window[:1] == b"\n"
            text = (window if start == 0 else window[1:]).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines = text.splitlines()
    return lines if on_boundary else lines[1:]


def _events(state, key: str) -> list:
    records = []
    for line in _tail_lines(os.path.join(session_folder(state, key), "events.jsonl")):
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def start(tmux: Tmux, claude: str, folder: str, message: str, state=None) -> tuple:
    """Start `claude` as a new session in `folder`: (Session, None), or (None, error) with nothing left behind."""
    state = core.state_path() if state is None else state
    if not os.path.isdir(folder):
        return None, f"The folder {folder} doesn't exist."
    key, name, now = str(uuid.uuid4()), core.test_session_name(), _now_ms()
    where = session_folder(state, key)
    pane = None
    try:
        os.makedirs(sessions_root(state), mode=0o700, exist_ok=True)
        os.mkdir(where, 0o700)
        _write_settings(state, key)
        record = {
            "key": key, "agent": AGENT, "name": name, "cwd": os.path.abspath(folder), "session_id": key,
            "started_at": now, "run_at": now,
        }
        _write_atomic(os.path.join(where, "session.json"), record)
        args = core.AGENTS[AGENT].launch(claude, key, os.path.join(where, "settings.json"), message, name)
        pane = tmux.new_hidden_window(core.pane_command(args), cwd=record["cwd"])
        tmux.set_pane_option(pane, SESSION_OPT, key)
        tmux.set_pane_option(pane, AGENT_OPT, AGENT)
    except (OSError, TmuxError) as exc:
        if pane:
            try:
                tmux.kill_pane(pane)
            except TmuxError:
                pass
        shutil.rmtree(where, ignore_errors=True)
        return None, f"Couldn't start the session: {' '.join(str(exc).split())}"
    session = core.Session(
        id=key, name=name or key[:8], cwd=record["cwd"], state="starting", status="", waiting_for="",
        started_at=float(now), running=True, session_id=key, turned=False,
    )
    return session, None


def resume_command(key: str, claude: str, state=None) -> tuple:
    """(the pane command that continues the session `key`, None), or (None, error). Rewrites the settings file for this emx and marks a new run, so what the last run reported no longer counts."""
    state = core.state_path() if state is None else state
    if not core.is_session_id(key):
        return None, "That is not a session of emx's."
    try:
        record = _read_record(state, key)
        folded = core.fold_events(_events(state, key))
        _write_settings(state, key)
        _write_atomic(os.path.join(session_folder(state, key), "session.json"), {**record, "run_at": _now_ms()})
    except (OSError, ValueError) as exc:
        return None, f"Couldn't read the session's record: {' '.join(str(exc).split())}"
    settings = os.path.join(session_folder(state, key), "settings.json")
    sid = folded.claude_sid or record.get("session_id") or key
    return core.pane_command(core.AGENTS[AGENT].resume(claude, sid, settings)), None


def _transcript_lines(path: str) -> list:
    """The tail of the transcript at `path` when it lies under Claude's projects folder and is named `<uuid>.jsonl`; otherwise nothing is opened."""
    if not path or not os.path.basename(path).endswith(".jsonl") or not core.is_session_id(os.path.basename(path)[:-6]):
        return []
    projects = os.path.realpath(os.path.join(claude_home(), "projects"))
    real = os.path.realpath(path)
    if not real.startswith(projects + os.sep):
        return []
    try:
        return _tail_lines(real)
    except OSError:
        return []


def list_sessions(tmux: Tmux, state=None) -> tuple:
    """(sessions, None), or (None, error) when the registry or tmux can't be read: an unreadable state folder is never an empty list."""
    state = core.state_path() if state is None else state
    try:
        names = os.listdir(sessions_root(state))
    except FileNotFoundError:
        names = []
    except OSError as exc:
        return None, f"Couldn't read emx's sessions folder: {' '.join(str(exc).split())}"
    records, hook_states, interrupted = [], {}, {}
    for key in sorted(n for n in names if core.is_session_id(n)):
        try:
            records.append(_read_record(state, key))
            folded = core.fold_events(_events(state, key))
        except (OSError, ValueError):
            continue
        hook_states[key] = folded
    try:
        panes = tmux.panes()
    except TmuxError as exc:
        return None, str(exc)
    live = {p.session for p in panes if not p.dead and p.session and p.agent}
    for key, folded in hook_states.items():
        if key in live and folded.state in ("working", "blocked"):
            interrupted[key] = core.interrupted_after(_transcript_lines(folded.transcript), folded.work_at)
    return core.engine_sessions(records, hook_states, panes, interrupted), None


def forget(key: str, state=None) -> tuple:
    """Remove the session `key` from emx: its own folder only, never anything of Claude's. (True, None) or (False, error)."""
    state = core.state_path() if state is None else state
    if not core.is_session_id(key):
        return False, "That is not a session of emx's."
    try:
        shutil.rmtree(session_folder(state, key))
    except FileNotFoundError:
        pass
    except OSError as exc:
        return False, f"Couldn't remove the session: {' '.join(str(exc).split())}"
    return True, None


def set_name(key: str, name: str, state=None) -> tuple:
    """Record `name` as the session's own. (True, None) or (False, error)."""
    state = core.state_path() if state is None else state
    if not core.is_session_id(key):
        return False, "That is not a session of emx's."
    try:
        record = _read_record(state, key)
        _write_atomic(os.path.join(session_folder(state, key), "session.json"), {**record, "name": name})
    except (OSError, ValueError) as exc:
        return False, f"Couldn't record the name: {' '.join(str(exc).split())}"
    return True, None
