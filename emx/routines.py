"""Reads every systemd user timer, with its journal. It only reads: `systemctl list-units`, `list-unit-files` and `show`, `journalctl` and `systemd-analyze calendar`, never a verb that changes a unit."""

from __future__ import annotations

import os
import shutil
import subprocess

from emx import core

NO_SYSTEMD = "Routines are read from systemd user timers, and this machine has none."
TIMEOUT = 10
TIMER_PROPS = "Id,Unit,TimersCalendar,NextElapseUSecRealtime,ActiveState,LoadState,ActiveEnterTimestamp"
SERVICE_PROPS = "Id,ActiveState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp"
JOURNAL_FIELDS = "MESSAGE_ID,JOB_TYPE,JOB_RESULT,UNIT_RESULT,EXIT_CODE,EXIT_STATUS,USER_UNIT,USER_INVOCATION_ID"


def _first_line(text: str, fallback: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), fallback)


def _run(argv: list) -> tuple:
    """(stdout, None), or (None, one line). Never raises."""
    env = {**os.environ, "LC_ALL": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=TIMEOUT, start_new_session=True, env=env,
        )
    except FileNotFoundError:
        return None, NO_SYSTEMD if argv[0] == "systemctl" else f"{argv[0]} isn't installed on this machine"
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} took longer than {TIMEOUT} seconds"
    except Exception as exc:
        return None, f"{argv[0]} couldn't run: {exc}"
    if done.returncode != 0:
        return None, _first_line(done.stderr, f"{argv[0]} exited with {done.returncode}")
    return done.stdout, None


def _slots(timer: dict, now: float, cache: dict) -> tuple:
    """(scheduled times since 24 hours ago, whether the calendar ran out of iterations before `now`)."""
    slots, capped = [], False
    for spec in core.calendar_specs(timer.get("TimersCalendar", "")):
        if spec not in cache:
            out, _ = _run(["systemd-analyze", "calendar", f"--base-time=@{int(now - core.ROUTINES_WINDOW)}",
                           f"--iterations={core.CALENDAR_CAP}", spec])
            cache[spec] = core.calendar_times(out or "")
        times = cache[spec]
        slots += times
        capped = capped or (len(times) >= core.CALENDAR_CAP and times[-1] < now)
    return sorted(set(slots)), capped


def read(now: float) -> tuple:
    """(views, None), or (None, one line). No user timers is no routines, not an error. A failed `list-unit-files` comes back as the error beside the views: the loaded timers still show."""
    try:
        if shutil.which("systemctl") is None:
            return None, NO_SYSTEMD
        out, error = _run(["systemctl", "--user", "list-units", "--type=timer", "--all", "--plain", "--no-legend", "--no-pager"])
        if error:
            return None, error
        loaded_timers = core.timer_units(out)
        files_out, files_error = _run(["systemctl", "--user", "list-unit-files", "--type=timer", "--plain", "--no-legend", "--no-pager"])
        if files_error:
            files_error = "Switched-off timers couldn't be read: " + files_error
        asked = sorted(set(loaded_timers) | set(core.timer_unit_files(files_out or "")))
        if not asked:
            return [], files_error
        out, error = _run(["systemctl", "--user", "show", "--timestamp=unix", "-p", TIMER_PROPS, *asked])
        if error:
            return None, error
        timers = core.show_blocks(out)
        names_of = sorted({t["Unit"] for t in timers.values() if t.get("LoadState") != "not-found" and t.get("Unit")})
        services, runs, journal_error = {}, {}, None
        if names_of:
            out, error = _run(["systemctl", "--user", "show", "--timestamp=unix", "-p", SERVICE_PROPS, *names_of])
            if error:
                return None, error
            services = core.show_blocks(out)
            active = frozenset(name for name, props in services.items() if core.service_running(props))
            log, journal_error = _run(["journalctl", "--user", "-o", "json", "--no-pager", "--since", f"@{int(now - core.ROUTINES_WINDOW)}",
                           f"--output-fields={JOURNAL_FIELDS}", *[arg for svc in names_of for arg in ("-u", svc)]])
            runs = core.journal_runs(log or "", active)
        cache, views = {}, []
        for asked_unit in asked:
            name = asked_unit[: -len(".timer")]
            timer = timers.get(asked_unit)
            loaded = timer is not None and timer.get("LoadState") != "not-found"
            unit = timer.get("Unit", "") if loaded else ""
            slots, capped = _slots(timer, now, cache) if loaded else ([], False)
            views.append(core.routine_view(name, timer, services.get(unit), runs.get(unit, []), slots, now,
                                           capped=capped, journal_error=journal_error or ""))
        return views, files_error
    except Exception as exc:
        return None, core.error_line(exc)
