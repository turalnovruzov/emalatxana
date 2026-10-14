"""emx on a server, seen from a laptop: the relay file the server's panel writes, `emx follow` that streams it, and (laptop side) `emx remote`."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from emx import __version__, core
from emx.tmux import RELAY_OPT, TmuxError

NOTE_SECONDS = 2
VERSION_TIMEOUT = 30

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


class Delivery:
    """Hands the server's records to the laptop's own voice and banners, in the mode the server's panel is in."""

    def __init__(self, voice, notifier):
        self.voice = voice
        self.notifier = notifier

    @classmethod
    def started(cls, socket: str) -> "Delivery":
        from emx.sidebar import Notifier, Voice

        voice, notifier = Voice(), Notifier(socket)
        voice.fallback_post = notifier.post
        notifier.start()
        voice.start()
        return cls(voice, notifier)

    def handle(self, record: dict) -> None:
        kind = record.get("kind")
        if kind == "hush":
            self.voice.silence()
        elif kind == "notice":
            if record["mode"] == core.VOICE_MODE:
                self.voice.speak(record["text"], None, fallback=(record["title"], record["plain"], record["sid"]))
            else:
                self.notifier.post(record["title"], record["text"], record["sid"])


class Follower:
    """The `emx follow` on the server, read over its own ssh channel by a thread that hands each record on. Not retried: it ends with the connection."""

    def __init__(self, argv: list, delivery: Delivery):
        self.argv = argv
        self.delivery = delivery
        self.proc = None
        self.thread = None

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace",
            )
        except (OSError, ValueError):
            self.proc = None
            return
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self) -> None:
        try:
            for line in self.proc.stdout:
                record = core.parse_relay(line.rstrip("\n"))
                if record is not None:
                    try:
                        self.delivery.handle(record)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        """Ends the ssh child and waits for the reader, so a line already read is delivered before the caller goes on."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except OSError:
            pass
        if self.thread is not None:
            self.thread.join(2)


def control_dir(uid: int | None = None, pid: int | None = None, root: str = "/tmp") -> str | None:
    """This process's ssh control socket path inside a private per-user folder, or None when the folder isn't private or the path is too long for a socket."""
    uid = os.getuid() if uid is None else uid
    pid = os.getpid() if pid is None else pid
    folder = os.path.dirname(core.control_path(uid, pid, root))
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        info = os.lstat(folder)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        return None
    path = core.control_path(uid, pid, root)
    return path if len(path) <= core.CONTROL_PATH_MAX else None


def _exit_master(ssh: str, host: str, control: str) -> None:
    try:
        subprocess.run(
            core.ssh_exit_args(ssh, host, control), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _remote_version(ssh: str, host: str, control: str | None, base: str) -> tuple:
    """(version, why not): emx's version on the server, asked through the same connection. Never raises."""
    argv = core.ssh_client_args(ssh, host, control, core.remote_shell(base, "--version"))
    try:
        done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=VERSION_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, (str(exc).splitlines() or ["no answer"])[0]
    version = core.remote_version(done.stdout)
    if version is not None:
        return version, ""
    for text in (done.stderr, done.stdout):
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if lines:
            return None, lines[-1]
    return None, "no answer"


def connect(host_arg: str, env=os.environ) -> int:
    """`emx remote <host>`: shows the server's emx in this terminal over ssh, and while it is open speaks or posts the server's notices here. Returns the attach's exit code."""
    host = core.remote_host(host_arg)
    if host is None:
        print("emx: remote needs a host name from your SSH config.", file=sys.stderr)
        return 2
    ssh = env.get("EMX_SSH") or "ssh"
    base = env.get("EMX_REMOTE_COMMAND") or "emx"
    control = control_dir()
    master, follower, note, code = False, None, None, 1
    try:
        if control:
            try:
                made = subprocess.run(core.ssh_master_args(ssh, host, control)).returncode == 0
            except OSError:
                made = False
            if not made:
                print(f"emx: couldn't connect to {host}.", file=sys.stderr)
                return 1
            master = True
        remote, why = _remote_version(ssh, host, control, base)
        if remote is None:
            print(
                f"emx: couldn't run emx on {host}: {why}. If it is installed somewhere the login shell doesn't find, "
                "set EMX_REMOTE_COMMAND to it.",
                file=sys.stderr,
            )
            return 1
        note = core.version_note(host, __version__, remote)
        if note:
            print(note, file=sys.stderr)
            time.sleep(NOTE_SECONDS)
        os.environ["EMX_REMOTE_HOST"] = host
        if control:
            os.environ["EMX_REMOTE_CONTROL"] = control
        os.environ["EMX_REMOTE_TERMINAL"] = core.terminal_bundle(os.environ)
        socket = env.get("EMX_SOCKET", "emx")
        delivery = Delivery.started(socket if socket not in ("", "default") else "emx")
        follower = Follower(core.ssh_client_args(ssh, host, control, core.remote_shell(base, "follow"), batch=True), delivery)
        follower.start()
        try:
            code = subprocess.run(core.ssh_client_args(ssh, host, control, core.remote_shell(base), tty=True)).returncode
        except OSError as exc:
            print(f"emx: couldn't run ssh: {exc}", file=sys.stderr)
            code = 1
    except KeyboardInterrupt:
        code = 130
    finally:
        if follower is not None:
            follower.stop()
        if master:
            _exit_master(ssh, host, control)
    if note:
        print(note, file=sys.stderr)
    if code == 255:
        print(
            f"emx: lost the connection to {host}. Your sessions there keep running; emx remote {host} reconnects.",
            file=sys.stderr,
        )
    return code


def open_remote(sid: str, env=os.environ) -> int:
    """What a click on a banner for a server session runs on the laptop: asks the server's panel to open `sid` over ssh, and prints the bundle id of the laptop's terminal for the helper to bring forward. Never touches a local tmux. Never raises."""
    host = core.remote_host(env.get("EMX_REMOTE_HOST", ""))
    if host is None or not core.is_session_id(sid):
        print("emx: open needs a session id.", file=sys.stderr)
        return 2
    bundle = core.terminal_bundle({"__CFBundleIdentifier": env.get("EMX_REMOTE_TERMINAL", "")})
    if bundle:
        print(bundle, flush=True)
    ssh = env.get("EMX_SSH") or "ssh"
    base = env.get("EMX_REMOTE_COMMAND") or "emx"
    argv = core.ssh_client_args(
        ssh, host, env.get("EMX_REMOTE_CONTROL") or None, core.remote_shell(base, "open", "--no-window", sid), batch=True
    )
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=4)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1
    return 0 if done.returncode == 0 else 1
