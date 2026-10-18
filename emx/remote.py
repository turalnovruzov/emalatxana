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

import emx
from emx import __version__, core, install
from emx.tmux import RELAY_HOST_OPT, RELAY_OPT, RELAY_PASTE_OPT, TmuxError

NOTE_SECONDS = 2
VERSION_TIMEOUT = 30
UPDATE_TIMEOUT = 120
LAPTOP_FETCH_TIMEOUT = 10

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


def follow(path: Path, out, tmux, beat: float = core.RELAY_BEAT_SECONDS, tick: float = 0.5, stop: threading.Event | None = None, clock=time.time, host: str | None = None, paste: bool = False) -> int:
    """Stream the relay file from its end to `out`, one JSON line per valid record, plus a beat line every `beat` seconds that also stamps `RELAY_OPT` so the panel knows a laptop is listening, `RELAY_HOST_OPT` with the laptop's `host` when given, and `RELAY_PASTE_OPT` when the laptop can answer a paste (`paste`), which is cleared when this ends. Returns 0 when `out` is gone or `stop` is set."""
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
                _stamp(tmux, clock, host, paste)
            if stop is not None:
                stop.wait(tick)
            else:
                time.sleep(tick)
    except (BrokenPipeError, OSError, ValueError):
        return 0
    finally:
        if handle is not None:
            handle.close()
        if paste:
            try:
                tmux.set_session_option(RELAY_PASTE_OPT, "")
            except (TmuxError, OSError):
                pass
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


def _stamp(tmux, clock, host: str | None = None, paste: bool = False) -> None:
    try:
        if tmux.has_session():
            tmux.set_session_option(RELAY_OPT, str(clock()))
            if host:
                tmux.set_session_option(RELAY_HOST_OPT, core.relay_host_value(host, clock()))
            if paste:
                tmux.set_session_option(RELAY_PASTE_OPT, str(clock()))
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


def clear_stale_sockets(control: str) -> None:
    """Remove the socket a hard-killed `emx remote` left at `control`, and those of other pids that no longer run. Only sockets this user owns, directly inside `control`'s folder."""
    folder = os.path.dirname(control)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        if path != control:
            if not name.isdigit():
                continue
            try:
                os.kill(int(name), 0)
                continue
            except PermissionError:
                continue
            except (ProcessLookupError, OverflowError, OSError):
                pass
        try:
            info = os.lstat(path)
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                os.unlink(path)
        except OSError:
            pass


# What tmux's client turns on that a clean detach would undo: alternate screen, mouse modes, focus events, bracketed paste, extended keys, cursor.
TERMINAL_RESTORE = "\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[>4m\x1b[?1l\x1b>\x1b[?25h"


def _restore_terminal() -> None:
    try:
        if sys.stdout.isatty():
            sys.stdout.write(TERMINAL_RESTORE)
            sys.stdout.flush()
    except (OSError, ValueError):
        pass


# xterm's title stack: push the current title, and pop it back at the end. A terminal without the stack is left with the empty title set before the pop.
TITLE_PUSH = "\x1b[22;0t"
TITLE_POP = "\x1b[23;0t"


def _write_to_terminal(text: str) -> None:
    try:
        if sys.stdout.isatty():
            sys.stdout.write(text)
            sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _show_title(host: str) -> None:
    _write_to_terminal(TITLE_PUSH + "\x1b]0;" + core.window_title(host) + "\x07")


def _put_title_back() -> None:
    _write_to_terminal("\x1b]0;\x07" + TITLE_POP)


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


def _remote_update(ssh: str, host: str, control: str | None, base: str, *args: str) -> tuple:
    """(output, why it didn't run) of the server's `emx update` with `args`. Never raises."""
    argv = core.ssh_client_args(ssh, host, control, core.remote_shell(base, "update", *args))
    try:
        done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", timeout=UPDATE_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return "", (str(exc).splitlines() or ["no answer"])[0]
    return done.stdout + done.stderr, ""


def _laptop_major() -> str | None:
    """The newest major this laptop's install could move to, after a short fetch; a failed fetch still reads the tags it already has."""
    root = emx.ROOT
    if root is None:
        return None
    install.git(root, "fetch", "--quiet", "--tags", "origin", timeout=LAPTOP_FETCH_TIMEOUT)
    found, error = install.versions(root)
    if error:
        return None
    return core.pick_update(emx.RUNNING, found)[1]


def _server_check(ssh: str, host: str, control: str | None, base: str, remote: str) -> tuple:
    """Asks the server to update within its major and says what came of it. Returns (server's version now, the major it has waiting, why it couldn't update or None)."""
    print(f"Checking emx on {host} for updates…", file=sys.stderr)
    out, why = _remote_update(ssh, host, control, base, "--report")
    report = core.parse_update_report(out)
    if report is None:
        reason = why or f"its emx ({remote}) has no emx update; install it there with install.sh"
        return remote, None, f"emx on {host} couldn't update: {reason}"
    if report["status"] == "failed":
        return remote, None, f"emx on {host} couldn't update: {report['reason']}"
    if report["status"] == "updated":
        print(f"Updated emx on {host} to {report['after']}.", file=sys.stderr)
    elif report["status"] == "off":
        print(f"Automatic updates are off on {host}.", file=sys.stderr)
    return report["after"], report["major"], None


def _update_both(ssh: str, host: str, control: str | None, base: str) -> None:
    out, why = _remote_update(ssh, host, control, base)
    lines = [core.clean_notice(l)[:core.UPDATE_REASON_MAX] for l in out.splitlines() if l.strip()] or [why or "no answer"]
    for line in lines:
        print(f"{host}: {line}", file=sys.stderr)
    if emx.ROOT is None:
        print(f"This laptop's emx runs from a checkout ({emx.CODE}), which never updates itself.", file=sys.stderr)
        return
    result, error = install.update(emx.ROOT, emx.RUNNING, allow_major=True)
    if error:
        print(f"This laptop's emx couldn't update: {error}", file=sys.stderr)
    elif result[1] == result[0]:
        print(f"This laptop's emx is up to date ({result[1]}).", file=sys.stderr)
    else:
        print(f"Updated this laptop's emx to {result[1]}.", file=sys.stderr)


def connect(host_arg: str, env=os.environ) -> int:
    """`emx remote <host>`: shows the server's emx in this terminal over ssh, and while it is open speaks or posts the server's notices here. Returns the attach's exit code."""
    host = core.remote_host(host_arg)
    if host is None:
        print("emx: remote needs a host name from your SSH config.", file=sys.stderr)
        return 2
    ssh = env.get("EMX_SSH") or "ssh"
    base = env.get("EMX_REMOTE_COMMAND") or "emx"
    control = control_dir()
    master, follower, note, code, titled = False, None, None, 1, False
    try:
        if control:
            clear_stale_sockets(control)
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
        remote, server_major, failed = _server_check(ssh, host, control, base, remote)
        note = core.version_note(host, __version__, remote)
        for line in (note, failed):
            if line:
                print(line, file=sys.stderr)
        ask = core.remote_update_ask(__version__, remote, _laptop_major(), server_major)
        if ask and sys.stdin.isatty():
            try:
                answer = input(f"Update emx on {host} and this laptop to {ask}? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() in ("y", "yes"):
                _update_both(ssh, host, control, base)
                # Both sides were just moved; the note from before would be stale after the attach.
                note = None
        elif note or failed:
            time.sleep(NOTE_SECONDS)
        os.environ["EMX_REMOTE_HOST"] = host
        if control:
            os.environ["EMX_REMOTE_CONTROL"] = control
        os.environ["EMX_REMOTE_TERMINAL"] = core.terminal_bundle(os.environ)
        socket = env.get("EMX_SOCKET", "emx")
        delivery = Delivery.started(socket if socket not in ("", "default") else "emx")
        follower = Follower(core.ssh_client_args(ssh, host, control, core.follow_shell(base, host), batch=True), delivery)
        _show_title(host)
        titled = True
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
        if titled:
            _put_title_back()
        if master:
            _exit_master(ssh, host, control)
    if note:
        print(note, file=sys.stderr)
    if code == 255:
        _restore_terminal()
        print(
            f"emx: lost the connection to {host}. Your sessions there keep running; emx remote {host} reconnects.",
            file=sys.stderr,
        )
    return code


def open_remote(sid: str, env=os.environ) -> int:
    """What a click on a banner for a server session runs on the laptop: asks the server's panel to open `sid` over ssh, and prints the bundle id of the laptop's terminal for the helper to bring forward. Never touches a local tmux. Never raises."""
    host = core.remote_host(env.get("EMX_REMOTE_HOST", ""))
    if host is None:
        print("emx: open needs EMX_REMOTE_HOST to be a host name from your SSH config.", file=sys.stderr)
        return 2
    if not core.is_session_id(sid):
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
