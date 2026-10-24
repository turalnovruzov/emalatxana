"""emx-notify.app, the helper that shows emx's notifications as emx's own: where it lives, building it, and running its verbs (ADR 0006)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from emx import BIN, core

HERE = os.path.dirname(os.path.realpath(__file__))
SOURCE = os.path.join(HERE, "emx-notify.swift")
BUNDLE_ID = "com.emalatxana.emx-notify"
TEST_BUNDLE_ID = "com.emalatxana.emx-notify-test"
NAMES = {BUNDLE_ID: "emx", TEST_BUNDLE_ID: "emx test"}
APP = "emx-notify.app"
EXECUTABLE = "emx-notify"
ICON = "emx-notify"
HASH_FILE = "emx-notify.sha256"
LOCK_FILE = ".build.lock"
SWIFTC = "/usr/bin/swiftc"
XCODE_SELECT = "/usr/bin/xcode-select"
ICONUTIL = "/usr/bin/iconutil"
CODESIGN = "/usr/bin/codesign"
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
SWIFT_FLAGS = ("-O", "-framework", "AppKit", "-framework", "UserNotifications")
# Part of the hash, so a change to how the app is built rebuilds it too.
BUILD_RECIPE = f"swiftc {' '.join(SWIFT_FLAGS)}; icon; iconutil -c icns; codesign --force --sign -"
NO_TOOLS = "Xcode command line tools not installed"
BUILD_TIMEOUT = 600
RUN_TIMEOUT = 15
OPEN_ENV = (
    "PATH", "EMX_SOCKET", "TMUX_TMPDIR", "EMX_CONFIG_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
    "EMX_SHOW_TEST_SESSIONS", "EMX_NOTIFY", "EMX_NOTIFY_HOME",
    "EMX_SSH", "EMX_REMOTE_HOST", "EMX_REMOTE_CONTROL", "EMX_REMOTE_TERMINAL",
)


OPEN = "/usr/bin/open"
# The helper's exit codes; through `open -W` they are lost, so a refusal is read from the report's `error`.
EXIT_OK, EXIT_FAILED, EXIT_NOT_AUTHORIZED, EXIT_REFUSED = 0, 1, 3, 4


@dataclass(frozen=True)
class HelperReport:
    """What a verb reported. `posted` is only `add`'s completion; `shown` is the request found among the delivered ones; `error` is the system's refusal of `authorize`."""
    auth: str
    alerts: str
    posted: bool
    shown: bool
    delivered: int
    error: str | None = None


def real_home() -> Path:
    return Path(os.path.expanduser("~")) / "Library" / "Application Support" / "emx"


def helper_home(env: Mapping | None = None) -> Path | None:
    """Where the helper is built: `EMX_NOTIFY_HOME` when absolute, nowhere for an emx a live check started without one, else his Application Support folder."""
    env = os.environ if env is None else env
    override = env.get("EMX_NOTIFY_HOME", "")
    if override and os.path.isabs(override):
        return Path(override)
    if env.get("EMX_SHOW_TEST_SESSIONS") == "1":
        return None
    return real_home()


def bundle_id(env: Mapping | None = None) -> str:
    """A helper built anywhere but his own folder gets the test id, so it can never take over his registration."""
    env = os.environ if env is None else env
    override = env.get("EMX_NOTIFY_HOME", "")
    return TEST_BUNDLE_ID if override and os.path.isabs(override) else BUNDLE_ID


def helper_command(env: Mapping | None, home: Path | None) -> str | None:
    env = os.environ if env is None else env
    if env.get("EMX_NOTIFY"):
        return env["EMX_NOTIFY"]
    if home is None:
        return None
    return str(home / APP / "Contents" / "MacOS" / EXECUTABLE)


def info_plist(bundle: str, name: str) -> bytes:
    return plistlib.dumps({
        "CFBundleIdentifier": bundle,
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIconFile": ICON,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,
        "NSPrincipalClass": "NSApplication",
    }, sort_keys=True)


def source_hash(bundle: str) -> str:
    digest = hashlib.sha256()
    with open(SOURCE, "rb") as fh:
        digest.update(fh.read())
    digest.update(info_plist(bundle, NAMES[bundle]))
    digest.update(BUILD_RECIPE.encode("utf-8"))
    return digest.hexdigest()


def _tool(argv: list, timeout: float = BUILD_TIMEOUT) -> str | None:
    """None when the tool ran and exited 0, else a one-line error."""
    try:
        done = subprocess.run(
            argv, capture_output=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"{os.path.basename(argv[0])} failed: {exc}"
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or [""])[-1]
        return f"{os.path.basename(argv[0])} exited {done.returncode}: {last}".rstrip(": ")
    return None


def ensure_built(home: Path, bundle: str, register: bool = True) -> tuple:
    """(the app's path, None) once the helper in `home` is built from the current source, or (None, one-line error). Never raises.

    A matching hash file means it is already built. On a Mac without Apple's
    command line tools `/usr/bin/swiftc` is a shim that pops the install
    dialog, so `xcode-select -p` is asked first and nothing else runs when it
    fails. The new app is built beside the old and swapped in by rename; the
    hash is written last, so a build that fails partway is tried again.
    """
    try:
        home = Path(home)
        if bundle not in NAMES:
            return None, f"unknown bundle id {bundle}"
        if bundle == BUNDLE_ID and home != real_home():
            return None, "the real helper is built only in its own folder"
        if bundle == TEST_BUNDLE_ID and home == real_home():
            return None, "the test helper is never built in the real folder"
        home.mkdir(parents=True, exist_ok=True)
        with open(home / LOCK_FILE, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _build(home, bundle, register)
    except Exception as exc:
        return None, f"building the notification helper failed: {exc}"


def _build(home: Path, bundle: str, register: bool) -> tuple:
    app = home / APP
    digest = source_hash(bundle)
    hash_file = home / HASH_FILE
    try:
        built = hash_file.read_text(encoding="utf-8").strip()
    except OSError:
        built = ""
    if built == digest and (app / "Contents" / "MacOS" / EXECUTABLE).is_file():
        return app, None
    if _tool([XCODE_SELECT, "-p"], timeout=RUN_TIMEOUT):
        return None, NO_TOOLS
    work = Path(tempfile.mkdtemp(prefix=".build-", dir=home))
    try:
        new = work / APP
        (new / "Contents" / "MacOS").mkdir(parents=True)
        (new / "Contents" / "Resources").mkdir(parents=True)
        executable = new / "Contents" / "MacOS" / EXECUTABLE
        error = _tool([SWIFTC, *SWIFT_FLAGS, "-o", str(executable), SOURCE])
        if error:
            return None, error
        (new / "Contents" / "Info.plist").write_bytes(info_plist(bundle, NAMES[bundle]))
        iconset = work / f"{ICON}.iconset"
        error = (
            _tool([str(executable), "icon", str(iconset)], timeout=RUN_TIMEOUT * 4)
            or _tool([ICONUTIL, "-c", "icns", "-o", str(new / "Contents" / "Resources" / f"{ICON}.icns"), str(iconset)])
            or _tool([CODESIGN, "--force", "--sign", "-", str(new)])
        )
        if error:
            return None, error
        if app.exists():
            os.rename(app, work / "old.app")
        os.rename(new, app)
        if register:
            error = _tool([LSREGISTER, "-f", str(app)])
            if error:
                return None, error
        tmp = home / (HASH_FILE + ".tmp")
        tmp.write_text(digest + "\n", encoding="utf-8")
        os.replace(tmp, hash_file)
        return app, None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def parse_report(text: str) -> HelperReport | None:
    for line in reversed(text.splitlines()):
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        auth, alerts, delivered = raw.get("auth"), raw.get("alerts"), raw.get("delivered")
        if not isinstance(auth, str) or not isinstance(alerts, str) or not isinstance(delivered, int):
            continue
        error = raw.get("error")
        return HelperReport(
            auth, alerts, raw.get("posted") is True, raw.get("shown") is True, delivered,
            error if isinstance(error, str) and error else None,
        )
    return None


def run(command: str, verb: str, payload: dict | None = None, timeout: float = RUN_TIMEOUT) -> tuple:
    """(HelperReport, None) from one verb, or (None, one-line error). The payload goes on stdin as JSON, never in the command line. Never raises."""
    try:
        done = subprocess.run(
            [command, verb], input=json.dumps(payload) if payload is not None else "", capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"emx-notify {verb} failed: {exc}"
    found = parse_report(done.stdout)
    if found is None:
        last = (done.stderr.strip().splitlines() or [""])[-1]
        return None, f"emx-notify {verb} exited {done.returncode}: {last}".rstrip(": ")
    return found, None


def open_command(env: Mapping | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("EMX_OPEN") or OPEN


def authorize_argv(command: str, app: Path | None, result: str, env: Mapping | None = None) -> list:
    """How `authorize` is launched: through LaunchServices for a built app, since macOS refuses the request from a directly run executable; a fake `EMX_NOTIFY` is run directly and never through `open`."""
    env = os.environ if env is None else env
    if env.get("EMX_NOTIFY") or app is None:
        return [command, "authorize", result]
    return [open_command(env), "-W", "-n", "-a", str(app), "--args", "authorize", result]


def authorize(command: str, app: Path | None, timeout: float, env: Mapping | None = None) -> tuple:
    """(HelperReport, None) once the permission was asked for, or (None, one-line error). Never raises.

    The helper writes its report into a 0600 file in the helper's folder
    (the temp folder when there is none), which is removed afterwards.
    """
    try:
        fd, result = tempfile.mkstemp(prefix=".authorize-", suffix=".json", dir=str(app.parent) if app else None)
    except OSError as exc:
        return None, f"emx-notify authorize failed: {exc}"
    os.close(fd)
    try:
        try:
            done = subprocess.run(
                authorize_argv(command, app, result, env), capture_output=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=timeout,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return None, f"emx-notify authorize failed: {exc}"
        try:
            with open(result, encoding="utf-8", errors="replace") as fh:
                found = parse_report(fh.read())
        except OSError:
            found = None
        if found is None:
            last = (done.stderr.strip().splitlines() or [""])[-1]
            return None, f"emx-notify authorize wrote no report, exited {done.returncode}: {last}".rstrip(": ")
        return found, None
    finally:
        try:
            os.unlink(result)
        except OSError:
            pass


def payload(title: str, body: str, ident: str, sid: str, socket: str, env: Mapping | None = None) -> dict:
    """What `post` is given: the banner's words, and what a click runs (`emx open <sid>`) with absolute paths and only the environment it needs.

    macOS launches the helper for a click with launchd's bare PATH, so the
    folder of the tmux this emx uses is added to it.
    """
    env = os.environ if env is None else env
    keep = {k: env[k] for k in OPEN_ENV if env.get(k)}
    path = keep.get("PATH") or os.defpath
    tmux = shutil.which("tmux", path=path) or shutil.which("tmux")
    if tmux and os.path.dirname(tmux) not in path.split(os.pathsep):
        path = os.pathsep.join([path, os.path.dirname(tmux)])
    keep["PATH"] = path
    keep["EMX_SOCKET"] = socket
    return {
        "title": title,
        "body": body,
        "id": ident,
        "open": {"python": os.path.abspath(sys.executable), "emx": os.path.abspath(BIN), "sid": sid, "env": keep},
    }


def helper_state(report: HelperReport | None, error: str | None = None) -> str:
    if report is None:
        return core.HELPER_UNAVAILABLE
    if report.error or report.auth == "denied" or report.alerts == "disabled":
        return core.HELPER_BLOCKED
    if report.auth == "notDetermined":
        return core.HELPER_PENDING
    return core.HELPER_OK
