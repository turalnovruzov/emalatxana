"""The own engine's side effects: the registry of sessions under emx's state folder, starting them in tmux, and gathering what they report.

Gathers, never decides: every rule about what the files and panes mean is in
`emx.core`. Everything lives under `core.state_path()`; nothing here writes
under `~/.claude`, whose transcripts are only read. Wrappers return
`(value, error)` and never raise, except `hook_command`, which is pure.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

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


@contextmanager
def _record_lock(state, key: str):
    """Held across every read-modify-write of a session's `session.json`, so two writers never lose each other's field.

    The lock is a file of its own: `_write_atomic` replaces `session.json`'s
    inode, which would drop a lock taken on it. Opening it never creates the
    session's folder, so a session forgotten meanwhile stays gone.
    """
    fd = os.open(os.path.join(session_folder(state, key), "session.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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


def _read_verdict(state, key: str) -> tuple | None:
    """(stop_key, verdict) from the session's `verdict.json`, or None when it is missing or not one. Never raises."""
    try:
        with open(os.path.join(session_folder(state, key), "verdict.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stop_key"), str) or data.get("verdict") not in core.VERDICTS:
        return None
    return data["stop_key"], data["verdict"]


def set_verdict(key: str, stop_key: str, verdict: str, state=None) -> tuple:
    """Record Haiku's verdict on the stop `stop_key` of the session `key`. (True, None) or (False, error)."""
    state = core.state_path() if state is None else state
    if not core.is_session_id(key):
        return False, "That is not a session of emx's."
    if verdict not in core.VERDICTS or not isinstance(stop_key, str):
        return False, f"Not a verdict emx knows: {verdict}"
    try:
        with _record_lock(state, key):
            _write_atomic(os.path.join(session_folder(state, key), "verdict.json"), {"stop_key": stop_key, "verdict": verdict})
    except OSError as exc:
        return False, f"Couldn't record the verdict: {' '.join(str(exc).split())}"
    return True, None


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
        # Agent first: a key alone marks an old `claude attach` viewer, which the panel kills.
        tmux.set_pane_option(pane, AGENT_OPT, AGENT)
        tmux.set_pane_option(pane, SESSION_OPT, key)
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
        with _record_lock(state, key):
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
    records, hook_states, interrupted, verdicts = [], {}, {}, {}
    for key in sorted(n for n in names if core.is_session_id(n)):
        try:
            records.append(_read_record(state, key))
            folded = core.fold_events(_events(state, key))
        except (OSError, ValueError):
            continue
        hook_states[key] = folded
        verdict = _read_verdict(state, key)
        if verdict is not None:
            verdicts[key] = verdict
    try:
        panes = tmux.panes()
    except TmuxError as exc:
        return None, str(exc)
    live = {p.session for p in panes if not p.dead and p.session and p.agent}
    for key, folded in hook_states.items():
        if key in live and folded.state in ("working", "blocked"):
            interrupted[key] = core.interrupted_after(_transcript_lines(folded.transcript), folded.work_at)
    return core.engine_sessions(records, hook_states, panes, interrupted, verdicts=verdicts), None


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


def _set_field(key: str, field: str, value: str, state) -> tuple:
    state = core.state_path() if state is None else state
    if not core.is_session_id(key):
        return False, "That is not a session of emx's."
    try:
        with _record_lock(state, key):
            record = _read_record(state, key)
            _write_atomic(os.path.join(session_folder(state, key), "session.json"), {**record, field: value})
    except (OSError, ValueError) as exc:
        return False, f"Couldn't record the name: {' '.join(str(exc).split())}"
    return True, None


def set_name(key: str, name: str, state=None) -> tuple:
    """Record `name` as the session's own. (True, None) or (False, error)."""
    return _set_field(key, "name", name, state)


def set_title(key: str, title: str, state=None) -> tuple:
    """Record the title Claude gave the session, shown while it has no name of its own. (True, None) or (False, error)."""
    return _set_field(key, "title", title, state)


@dataclass(frozen=True)
class Engine:
    """What the panel's workers are handed in place of the `claude` command: emx's tmux server, the claude command it launches and the state folder."""

    tmux: Tmux
    claude: str
    state: object

    def list_sessions(self) -> tuple:
        return list_sessions(self.tmux, self.state)

    def start(self, folder: str, message: str) -> tuple:
        return start(self.tmux, self.claude, folder, message, self.state)

    def resume_command(self, key: str) -> tuple:
        return resume_command(key, self.claude, self.state)

    def forget(self, key: str) -> tuple:
        return forget(key, self.state)

    def set_name(self, key: str, name: str) -> tuple:
        return set_name(key, name, self.state)

    def set_title(self, key: str, title: str) -> tuple:
        return set_title(key, title, self.state)

    def set_verdict(self, key: str, stop_key: str, verdict: str) -> tuple:
        return set_verdict(key, stop_key, verdict, self.state)
