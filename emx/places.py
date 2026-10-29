"""Where routines are read: this machine as this user, or any server and any user on it. Everything a place runs goes through `run`, which never raises."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Mapping

from emx import core

Place = core.Place
HERE = Place("this user")
TIMEOUT = 10
RUNTIME_WRAP = 'export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus; exec "$@"'
DEFAULT_USER_COMMAND = core.SETTING_BY_KEY["place_user_command"].default


def from_prefs(prefs: Mapping | None) -> list:
    """This user first, then each configured place in file order."""
    lines = (prefs or {}).get("place", [])
    return [HERE, *[place for place in (core.parse_place(line)[0] for line in lines) if place is not None]]


def find(prefs: Mapping | None, name: str):
    return next((place for place in from_prefs(prefs) if place.name == name), None)


def place_key(place: Place) -> str:
    return place.name


def argv(place: Place, cmd: list, user_command: str = DEFAULT_USER_COMMAND, ssh: str = "ssh") -> list:
    """The command line that runs `cmd` at `place`. Another user's systemd needs its runtime folder named, since sudo drops the environment and a non-interactive ssh may not set it."""
    if place.host is None and place.login is None:
        return list(cmd)
    inner = ["sh", "-c", RUNTIME_WRAP, "sh", *cmd]
    if place.login:
        inner = [*shlex.split(user_command), place.login, *inner]
    if place.host:
        return core.ssh_client_args(ssh, place.host, None, shlex.join(inner), batch=True)
    return inner


def _first_line(text: str, fallback: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), fallback)


def run(place: Place, cmd: list, prefs: Mapping | None = None, env: Mapping | None = None, timeout: float = TIMEOUT) -> tuple:
    """(stdout, None), or (None, one line). Never raises."""
    prefs = prefs or {}
    base = os.environ if env is None else env
    full = {**base, "LC_ALL": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
    line = list(cmd)
    try:
        line = argv(place, cmd, prefs.get("place_user_command") or DEFAULT_USER_COMMAND, base.get("EMX_SSH") or "ssh")
        done = subprocess.run(
            line, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=timeout, start_new_session=True, env=full,
        )
    except FileNotFoundError:
        return None, f"{line[0]} isn't installed on this machine"
    except subprocess.TimeoutExpired:
        return None, f"{line[0]} took longer than {timeout:g} seconds"
    except Exception as exc:
        return None, f"{cmd[0]} couldn't run: {exc}"
    if done.returncode == 0:
        return done.stdout, None
    why = _first_line(done.stderr, f"{cmd[0]} exited with {done.returncode}")
    if place.host and done.returncode == 255:
        return None, f"Can't reach {place.host}: {why}"
    if place.login and done.returncode in (1, 255) and ("a password is required" in why or "unknown user" in why):
        return None, f"Can't run as {place.login}: {why}"
    return None, why
