"""Reads every systemd user timer and every long-running user service that started Claude, at any place, with its journal and sessions. It only reads: `systemctl list-units`, `list-unit-files` and `show`, `journalctl`, `systemd-analyze calendar`, and the probe, which opens nothing but session files."""

from __future__ import annotations

import shutil

from emx import core, places, probe

NO_SYSTEMD = "Routines are read from systemd, and this place has none."
TIMER_PROPS = "Id,Unit,TimersCalendar,NextElapseUSecRealtime,ActiveState,LoadState,ActiveEnterTimestamp"
SERVICE_PROPS = "Id,ActiveState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,WorkingDirectory"
LONG_SERVICE_PROPS = ("Id,FragmentPath,ActiveState,SubState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp,"
                      "ActiveEnterTimestamp,TriggeredBy,WorkingDirectory")
JOURNAL_FIELDS = "MESSAGE_ID,JOB_TYPE,JOB_RESULT,UNIT_RESULT,EXIT_CODE,EXIT_STATUS,USER_UNIT,USER_INVOCATION_ID"


def _slots(timer: dict, since: float, now: float, cache: dict, run) -> tuple:
    """(scheduled times since `since`, whether the calendar ran out of iterations before `now`)."""
    slots, capped = [], False
    for spec in core.calendar_specs(timer.get("TimersCalendar", "")):
        if spec not in cache:
            out, _ = run(["systemd-analyze", "calendar", f"--base-time=@{int(since)}", f"--iterations={core.CALENDAR_CAP}", spec])
            cache[spec] = core.calendar_times(out or "")
        times = cache[spec]
        slots += times
        capped = capped or (len(times) >= core.CALENDAR_CAP and times[-1] < now)
    return sorted(set(slots)), capped


def _long_services(run, since: float, skip: set) -> tuple:
    """({name: properties} of the user-made services active since `since`, why they couldn't be read or None)."""
    listing = ["systemctl", "--user", "list-units", "--type=service", "--all", "--plain", "--no-legend", "--no-pager"]
    out, error = run(listing)
    if error:
        return {}, "Long-running services couldn't be read: " + error
    files_out, _ = run(["systemctl", "--user", "list-unit-files", "--type=service", "--plain", "--no-legend", "--no-pager"])
    names = sorted((set(core.service_units(out)) | set(core.service_unit_files(files_out or ""))) - skip)
    if not names:
        return {}, None
    out, error = run(["systemctl", "--user", "show", "--timestamp=unix", "-p", LONG_SERVICE_PROPS, *names])
    if error:
        return {}, "Long-running services couldn't be read: " + error
    return {name: props for name, props in core.show_blocks(out).items() if core.candidate_service(props, since)}, None


def read(now: float, place=places.HERE, prefs=None, exclude=frozenset(), run=None) -> tuple:
    """(views, None), or (None, one line). No user timers is no routines, not an error. A failed `list-unit-files` comes back as the error beside the views: the loaded timers still show. `run(argv)` runs a command at the place; by default `places.run`."""
    if run is None:
        def run(argv):
            return places.run(place, argv, prefs)
    try:
        if place.host is None and place.login is None and shutil.which("systemctl") is None:
            return None, NO_SYSTEMD
        since = core.routines_since(now)
        out, error = run(["systemctl", "--user", "list-units", "--type=timer", "--all", "--plain", "--no-legend", "--no-pager"])
        if error:
            return None, error
        loaded_timers = core.timer_units(out)
        files_out, files_error = run(["systemctl", "--user", "list-unit-files", "--type=timer", "--plain", "--no-legend", "--no-pager"])
        if files_error:
            files_error = "Switched-off timers couldn't be read: " + files_error
        asked = sorted(set(loaded_timers) | set(core.timer_unit_files(files_out or "")))
        timers, services, runs, journal_error, names_of = {}, {}, {}, None, []
        if asked:
            out, error = run(["systemctl", "--user", "show", "--timestamp=unix", "-p", TIMER_PROPS, *asked])
            if error:
                return None, error
            timers = core.show_blocks(out)
            names_of = sorted({t["Unit"] for t in timers.values() if t.get("LoadState") != "not-found" and t.get("Unit")})
        if names_of:
            out, error = run(["systemctl", "--user", "show", "--timestamp=unix", "-p", SERVICE_PROPS, *names_of])
            if error:
                return None, error
            services = core.show_blocks(out)
            active = frozenset(name for name, props in services.items() if core.service_running(props))
            log, journal_error = run(["journalctl", "--user", "-o", "json", "--no-pager", "--since", f"@{int(since)}",
                                      f"--output-fields={JOURNAL_FIELDS}", *[arg for svc in names_of for arg in ("-u", svc)]])
            runs = core.journal_runs(log or "", active)
        long, long_error = _long_services(run, since, set(names_of))
        timer_runs = {}
        for asked_unit in asked:
            timer = timers.get(asked_unit)
            if timer is not None and timer.get("LoadState") != "not-found":
                unit = timer.get("Unit", "")
                found = core.window_runs(runs.get(unit, []), services.get(unit), since)
                workdir = core.workdir_of(services.get(unit) or {})
                timer_runs[asked_unit[: -len(".timer")]] = (workdir, found)
        sessions, sessions_error = [], None
        if any(found for _, found in timer_runs.values()) or long:
            out, sessions_error = run(probe.command("list", str(int(since))))
            if sessions_error:
                sessions_error = "Claude sessions couldn't be read: " + sessions_error
            else:
                sessions = core.probe_sessions(out)
        by_timer, by_service = core.match_runs(timer_runs, long, sessions, now, exclude)
        cache, views = {}, []
        for asked_unit in asked:
            name = asked_unit[: -len(".timer")]
            timer = timers.get(asked_unit)
            loaded = timer is not None and timer.get("LoadState") != "not-found"
            unit = timer.get("Unit", "") if loaded else ""
            slots, capped = _slots(timer, since, now, cache, run) if loaded else ([], False)
            views.append(core.routine_view(name, timer, services.get(unit), runs.get(unit, []), slots, now, capped=capped,
                                           journal_error=journal_error or "", matched=by_timer.get(name)))
        for unit, cycles in by_service.items():
            views.append(core.service_view(unit[: -len(".service")], long[unit], cycles))
        views.sort(key=lambda view: view.name)
        return views, files_error or long_error or sessions_error
    except Exception as exc:
        return None, core.error_line(exc)


def read_places(now: float, place_list: list, prefs=None, exclude=frozenset(), run=None) -> list:
    """One `PlaceView` per place, read one after another; a place that fails has its own error line and the others still show. `run(place, argv)` replaces `places.run`. Only this user leaves out the sessions emx lists itself."""
    found = []
    for place in place_list:
        def at_place(argv, place=place):
            return run(place, argv) if run is not None else places.run(place, argv, prefs)
        views, error = read(now, place, prefs, exclude if place == places.HERE else frozenset(), at_place)
        found.append(core.PlaceView(place, tuple(views or ()), error))
    return found
