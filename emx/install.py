"""emx's installed copy: a bare clone of the repo, one folder per pushed tag, and a `current` link flipped last.

Every function returns `(value, None)` or `(None, one-line error)` and never raises.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import time

from emx import core

SOURCE = "source.git"
VERSIONS = "versions"
CURRENT = "current"
CURRENT_TMP = ".current.tmp"
LOCK = "lock"
UPDATES_OFF = "updates-off"
MADE = (SOURCE, VERSIONS, CURRENT, CURRENT_TMP, LOCK, UPDATES_OFF)
CLONE_TMP = f".{SOURCE}.tmp-"
VERSION_TMP = ".tmp-"

GIT_TIMEOUT = 60
CLONE_TIMEOUT = 300
LOCK_WAIT_SECONDS = 90
STALE_TMP_SECONDS = 3600
BUSY = "another update is running"


def _error_line(text: str, fallback: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    # git ends a transport failure with advice ("...and the repository exists."); the cause is the first diagnostic line.
    for line in lines:
        if line.startswith(("warning:", "hint:", "Please make sure you have the correct access rights", "and the repository exists")):
            continue
        return line
    return lines[-1] if lines else fallback


def _run(argv: list, timeout: float) -> tuple:
    # No stdin and a new session: with no controlling terminal, ssh or a credential helper can't prompt into the panel's pane.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=timeout, start_new_session=True, env=env,
        )
    except FileNotFoundError:
        return None, f"{argv[0]} is not installed"
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} took longer than {timeout:g} seconds"
    except Exception as exc:
        return None, f"{argv[0]} couldn't run: {exc}"
    if done.returncode != 0:
        return None, _error_line(done.stderr, f"{argv[0]} exited with {done.returncode}")
    return done.stdout, None


def git(root, *args: str, timeout: float = GIT_TIMEOUT) -> tuple:
    return _run(["git", "--git-dir", os.path.join(root, SOURCE), *args], timeout)


def untar(tar: str, dest: str) -> tuple:
    return _run(["tar", "-xf", tar, "-C", dest], GIT_TIMEOUT)


def _remove(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def clone(root, url: str) -> tuple:
    dest = os.path.join(root, SOURCE)
    tmp = os.path.join(root, f"{CLONE_TMP}{os.getpid()}")
    try:
        os.makedirs(root, exist_ok=True)
        _remove(tmp)
        # The `--` keeps a URL that starts with `-` from being read as an option.
        _, error = _run(["git", "clone", "--bare", "--quiet", "--", url, tmp], CLONE_TIMEOUT)
        if error:
            _remove(tmp)
            return None, f"couldn't clone {url}: {error}"
        os.rename(tmp, dest)
        return dest, None
    except Exception as exc:
        with contextlib.suppress(Exception):
            _remove(tmp)
        return None, f"couldn't clone {url}: {exc}"


def fetch(root) -> tuple:
    _, error = git(root, "fetch", "--quiet", "--tags", "origin")
    if error:
        return None, f"couldn't fetch updates: {error}"
    return True, None


def versions(root) -> tuple:
    out, error = git(root, "tag", "-l", "v*")
    if error:
        return None, f"couldn't list emx's versions: {error}"
    return core.tag_versions(out), None


def _clear_stale(base: str) -> None:
    now = time.time()
    for name in os.listdir(base):
        path = os.path.join(base, name)
        with contextlib.suppress(OSError):
            if name.startswith(VERSION_TMP) and now - os.lstat(path).st_mtime > STALE_TMP_SECONDS:
                _remove(path)


def prepare(root, version: str) -> tuple:
    """`versions/<version>` holding the tag's files, made under a temporary name and renamed into place only once complete."""
    if core.parse_version(version) is None:
        return None, f"{version!r} is not a version"
    base = os.path.join(root, VERSIONS)
    folder = os.path.join(base, version)
    tmp = os.path.join(base, f"{VERSION_TMP}{version}-{os.getpid()}")
    tar = tmp + ".tar"
    try:
        if os.path.isfile(os.path.join(folder, "bin", "emx")):
            return folder, None
        os.makedirs(base, exist_ok=True)
        _clear_stale(base)
        _remove(tmp)
        os.mkdir(tmp)
        _, error = git(root, "archive", "--format=tar", "-o", tar, f"refs/tags/v{version}")
        if not error:
            _, error = untar(tar, tmp)
        if not error and not os.path.isfile(os.path.join(tmp, "bin", "emx")):
            error = f"v{version} has no bin/emx"
        if error:
            return None, f"couldn't unpack emx {version}: {error}"
        _remove(folder)
        os.rename(tmp, folder)
        return folder, None
    except Exception as exc:
        return None, f"couldn't unpack emx {version}: {exc}"
    finally:
        for path in (tar, tmp):
            with contextlib.suppress(Exception):
                _remove(path)


def flip(root, version: str) -> tuple:
    try:
        if not os.path.isfile(os.path.join(root, VERSIONS, version, "bin", "emx")):
            return None, f"emx {version} is not unpacked"
        tmp = os.path.join(root, CURRENT_TMP)
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(os.path.join(VERSIONS, version), tmp)
        os.replace(tmp, os.path.join(root, CURRENT))
        return version, None
    except Exception as exc:
        return None, f"couldn't switch to emx {version}: {exc}"


def current(root) -> str | None:
    try:
        name = os.path.basename(os.path.normpath(os.readlink(os.path.join(root, CURRENT))))
    except (OSError, ValueError):
        return None
    return name if core.parse_version(name) else None


@contextlib.contextmanager
def locked(root, wait: bool):
    """Yields None while holding the install's lock, else the reason it couldn't be had: at once when `wait` is false, after `LOCK_WAIT_SECONDS` when true."""
    fd, error = None, None
    try:
        os.makedirs(root, exist_ok=True)
        fd = os.open(os.path.join(root, LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + (LOCK_WAIT_SECONDS if wait else 0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    error = BUSY
                    break
                time.sleep(0.2)
    except OSError as exc:
        error = f"couldn't take the update lock: {exc}"
    try:
        yield error
    finally:
        if fd is not None:
            os.close(fd)


def prune(root, running, keep_lagging: bool = False) -> tuple:
    """Deletes the version folders `core.prune_plan` names; skipped while a prepare and its flip hold the lock.

    With `keep_lagging`, nothing at or above the newest version below `running` goes: a panel that has not
    switched yet may still run from the one before the CLI's own.
    """
    try:
        with locked(root, wait=False) as busy:
            now = current(root)
            if busy or now is None:
                return [], None
            base = os.path.join(root, VERSIONS)
            gone = []
            plan = core.prune_plan(os.listdir(base), now, running)
            if keep_lagging:
                run = core.parse_version(running)
                older = [core.parse_version(v) for v in os.listdir(base) if core.parse_version(v) and core.parse_version(v) < run]
                if older:
                    plan = [v for v in plan if core.parse_version(v) < max(older)]
            for version in plan:
                shutil.rmtree(os.path.join(base, version))
                gone.append(version)
            return gone, None
    except Exception as exc:
        return None, f"couldn't remove old versions: {exc}"


def updates_on(root) -> bool:
    return not os.path.lexists(os.path.join(root, UPDATES_OFF))


def set_updates(root, on: bool) -> tuple:
    marker = os.path.join(root, UPDATES_OFF)
    try:
        if on:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(marker)
        else:
            with open(marker, "w", encoding="utf-8"):
                pass
        return on, None
    except OSError as exc:
        return None, f"couldn't turn automatic updates {'on' if on else 'off'}: {exc}"


def _foreign(root: str) -> bool:
    """Whether `root` holds something emx did not make, so installing there would mix into a folder of his."""
    if not os.path.isdir(root) or os.path.isdir(os.path.join(root, SOURCE)):
        return False
    return any(name not in MADE and not name.startswith(CLONE_TMP) for name in os.listdir(root))


def _link_bin(bindir: str, root: str, out) -> tuple:
    target = os.path.join(root, CURRENT, "bin", "emx")
    path = os.path.join(bindir, "emx")
    os.makedirs(bindir, exist_ok=True)
    if os.path.lexists(path):
        if not os.path.islink(path):
            return None, f"{path} is a file emx did not make; move it away and run install.sh again"
        old = os.readlink(path)
        if old != target:
            print(f"Replaced the link {path}, which pointed at {old}.", file=out)
    tmp = os.path.join(bindir, f".emx.tmp-{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)
    return path, None


def install(url: str, env, out) -> tuple:
    """Clone (or fetch) `url`, unpack its newest tag, point `current` at it and link `<bin>/emx`. Returns the version installed."""
    root, _ = core.install_root(env)
    made = root is not None and not os.path.lexists(str(root))
    version, error = _install(url, env, out)
    if error and made:
        # A first install that failed leaves the folder it made holding only its lock.
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(str(root), LOCK))
            os.rmdir(str(root))
    return version, error


def _install(url: str, env, out) -> tuple:
    try:
        root, error = core.install_root(env)
        if error:
            return None, error
        bindir, error = core.bin_home(env)
        if error:
            return None, error
        root, bindir = str(root), str(bindir)
        if _foreign(root):
            return None, f"{root} already holds files that are not emx's; set EMX_HOME to an empty folder"
        with locked(root, wait=True) as busy:
            if busy:
                return None, busy
            if os.path.isdir(os.path.join(root, SOURCE)):
                old, _ = git(root, "config", "--get", "remote.origin.url")
                if old and old.strip() != url:
                    print(f"emx is already installed from {old.strip()}; updating from there.", file=out)
                _, error = fetch(root)
            else:
                _, error = clone(root, url)
            if error:
                return None, error
            found, error = versions(root)
            if error:
                return None, error
            version = core.newest(found)
            if version is None:
                lowest = ".".join(map(str, core.MIN_INSTALLABLE))
                return None, f"{url} has no tag v{lowest} or newer to install"
            for step in (prepare, flip):
                _, error = step(root, version)
                if error:
                    return None, error
        link, error = _link_bin(bindir, root, out)
        if error:
            return None, error
        print(f"Installed emx {version} in {root}, run as {link}.", file=out)
        line = core.path_line(env.get("PATH", ""), bindir)
        if line:
            print(f"{bindir} is not on your PATH. Add this line to your shell's startup file:\n  {line}", file=out)
        return version, None
    except Exception as exc:
        return None, f"couldn't install emx: {exc}"


def update(root, running, allow_major: bool, apply_only_if_running_current: bool = False, wait: bool = True) -> tuple:
    """((before, after, major), None): fetch, unpack and flip to the newest version an update may take.

    A major waits unless `allow_major`; `major` is the newest one still
    waiting. With `apply_only_if_running_current`, nothing is unpacked or
    flipped while `running` is not what `current` names, so a panel that has
    not switched yet never gets a third version under it.
    """
    try:
        with locked(root, wait) as busy:
            if busy:
                return None, busy
            before = current(root)
            if before is None:
                return None, f"{os.path.join(root, CURRENT)} names no emx version"
            _, error = fetch(root)
            if error:
                return None, error
            found, error = versions(root)
            if error:
                return None, error
            target, major = core.pick_update(before, found)
            if allow_major and major:
                target, major = major, None
            if target is None or (apply_only_if_running_current and before != running):
                return (before, before, major), None
            for step in (prepare, flip):
                _, error = step(root, target)
                if error:
                    return None, error
            return (before, target, major), None
    except Exception as exc:
        return None, f"couldn't update emx: {exc}"


def uninstall(env, root) -> tuple:
    """Removes `<bin>/emx` when it links into `root`, then only what emx made in `root`, and `root` itself once empty. Prefs and state stay."""
    try:
        if root is None:
            return None, "this emx is not installed"
        root = str(root)
        bindir, error = core.bin_home(env)
        if error:
            return None, error
        removed = []
        link = os.path.join(str(bindir), "emx")
        if os.path.islink(link):
            target = os.path.realpath(os.path.join(str(bindir), os.readlink(link)))
            if target.startswith(os.path.realpath(root) + os.sep):
                os.unlink(link)
                removed.append(link)
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                if name in MADE or name.startswith(CLONE_TMP):
                    _remove(os.path.join(root, name))
                    removed.append(os.path.join(root, name))
            if not os.listdir(root):
                os.rmdir(root)
                removed.append(root)
        return removed, None
    except Exception as exc:
        return None, f"couldn't uninstall emx: {exc}"
