"""emx on a server, seen from a laptop: the relay file the server's panel writes, `emx follow` that streams it, and (laptop side) `emx remote`."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from emx import core
from emx.tmux import RELAY_OPT, TmuxError

_append_lock = threading.Lock()


def append_relay(path: Path, record: dict) -> None:
    """Add one record to the relay file, rotating it once past `RELAY_MAX_BYTES`. Never raises: a notice must not be lost to a full disk, nor the panel to it."""
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with _append_lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                if path.stat().st_size >= core.RELAY_MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except (OSError, ValueError):
        pass


def follow(path: Path, out, tmux, beat: float = core.RELAY_BEAT_SECONDS, tick: float = 0.5, stop: threading.Event | None = None, clock=time.time) -> int:
    """Stream the relay file from its end to `out`, one JSON line per valid record, plus a beat line every `beat` seconds that also stamps `RELAY_OPT` so the panel knows a laptop is listening. Returns 0 when `out` is gone or `stop` is set."""
    handle = None
    pending = b""
    try:
        handle = open(path, "rb")
        handle.seek(0, os.SEEK_END)
    except OSError:
        handle = None
    next_beat = time.monotonic()
    try:
        while stop is None or not stop.is_set():
            try:
                found = os.stat(path)
            except OSError:
                found = None
            if handle is not None and found is not None and os.fstat(handle.fileno()).st_ino != found.st_ino:
                _emit(handle.read(), pending, out)
                handle.close()
                handle, pending = None, b""
            if handle is None and found is not None:
                try:
                    handle = open(path, "rb")
                except OSError:
                    handle = None
            if handle is not None:
                if os.fstat(handle.fileno()).st_size < handle.tell():
                    handle.seek(0)
                    pending = b""
                pending = _emit(handle.read(), pending, out)
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + beat
                out.write(json.dumps(core.RELAY_BEAT) + "\n")
                out.flush()
                _stamp(tmux, clock)
            if stop is not None:
                stop.wait(tick)
            else:
                time.sleep(tick)
    except (BrokenPipeError, OSError, ValueError):
        return 0
    finally:
        if handle is not None:
            handle.close()
    return 0


def _emit(data: bytes, pending: bytes, out) -> bytes:
    """Write each complete line of `pending + data` that parses as a record; returns the unfinished tail."""
    data = pending + data
    *lines, tail = data.split(b"\n")
    for raw in lines:
        record = core.parse_relay(raw.decode("utf-8", errors="replace"))
        if record is not None and record["kind"] != "beat":
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
            out.flush()
    return tail


def _stamp(tmux, clock) -> None:
    try:
        if tmux.has_session():
            tmux.set_session_option(RELAY_OPT, str(clock()))
    except (TmuxError, OSError):
        pass
