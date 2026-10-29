import json
import os
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from emx import core, places, probe, routines

NOW = 1_800_000_000.0

TIMERS = (
    "Id=report-weekly.timer\nLoadState=loaded\nActiveState=active\nUnit=report-job.service\n"
    "TimersCalendar={ OnCalendar=Mon *-*-* 08:00:00 ; next_elapse=@1 }\nNextElapseUSecRealtime=@1800100000\n"
    "ActiveEnterTimestamp=@1799000000\n\n"
    "Id=gone.timer\nLoadState=not-found\nActiveState=inactive\nUnit=\n\n"
    "Id=backup-nightly.timer\nLoadState=loaded\nActiveState=active\nUnit=backup-job.service\n"
    "TimersCalendar={ OnCalendar=hourly ; next_elapse=@1 }\nNextElapseUSecRealtime=@1800001800\n"
    "ActiveEnterTimestamp=@1799000000\n"
)
SERVICES = (
    "Id=backup-job.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
    "ExecMainStartTimestamp=@1799999000\nExecMainExitTimestamp=@1799999002\n\n"
    "Id=report-job.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\nExecMainStartTimestamp=\nExecMainExitTimestamp=\n"
)
JOURNAL = "\n".join(
    json.dumps({"MESSAGE_ID": mid, "USER_UNIT": "backup-job.service", "USER_INVOCATION_ID": "i1",
                "__REALTIME_TIMESTAMP": str(int(at * 1e6)), **extra})
    for mid, at, extra in ((core.MSG_START, 1799999000, {}), (core.MSG_DONE, 1799999002, {"JOB_RESULT": "done"}))
)
LISTING = (
    "backup-nightly.timer loaded active waiting Nightly backup\n"
    "report-weekly.timer loaded active waiting Weekly report\n"
    "● gone.timer not-found inactive dead gone.timer\n"
)
UNIT_FILES = (
    "backup-nightly.timer enabled enabled\n"
    "report-weekly.timer enabled enabled\n"
    "idle-off.timer disabled enabled\n"
    "fetch@.timer disabled enabled\n"
)
IDLE_TIMER = (
    "\nId=idle-off.timer\nLoadState=loaded\nActiveState=inactive\nUnit=idle-job.service\n"
    "TimersCalendar={ OnCalendar=hourly ; next_elapse=@1 }\nNextElapseUSecRealtime=\nActiveEnterTimestamp=\n"
)
IDLE_SERVICE = (
    "\nId=idle-job.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
    "ExecMainStartTimestamp=\nExecMainExitTimestamp=\n"
)
ANALYZE = (
    "   Next elapse: Sun 2027-01-17 07:00:00 UTC\n   Iteration #2: Sun 2027-01-17 08:00:00 UTC\n"
)

HOURS_BEFORE_NOW = "".join(
    f"   {'Next elapse' if i == 0 else f'Iteration #{i + 1}'}: Fri 2027-01-15 0{hour}:00:00 UTC\n" for i, hour in enumerate(range(3, 8))
)


class FakeSystemd:
    """Answers each command by its program and records every argv."""

    def __init__(self, timers=TIMERS, services=SERVICES, journal=JOURNAL, journal_code=0, systemctl_error=None, analyze=ANALYZE,
                 list_units=LISTING, list_error=None, unit_files="", unit_files_error=None,
                 service_list="", service_files="", service_show="", probe_out="", probe_error=None):
        self.argvs = []
        self.service_list, self.service_files, self.service_show = service_list, service_files, service_show
        self.probe_out, self.probe_error = probe_out, probe_error
        self.unit_files, self.unit_files_error = unit_files, unit_files_error
        self.list_units, self.list_error = list_units, list_error
        self.analyze = analyze
        self.timers, self.services, self.journal = timers, services, journal
        self.journal_code, self.systemctl_error = journal_code, systemctl_error

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        done = lambda out="", err="", code=0: SimpleNamespace(stdout=out, stderr=err, returncode=code)
        if argv[:3] == ["python3", "-I", "-c"]:
            return done(err=self.probe_error, code=1) if self.probe_error else done(self.probe_out)
        if argv[0] == "systemctl":
            if "--type=service" in argv:
                return done(self.service_files if "list-unit-files" in argv else self.service_list)
            if "show" in argv and "FragmentPath" in argv[argv.index("-p") + 1]:
                return done(self.service_show)
            if "list-unit-files" in argv:
                return done(err=self.unit_files_error, code=1) if self.unit_files_error else done(self.unit_files)
            if "list-units" in argv:
                return done(err=self.list_error, code=1) if self.list_error else done(self.list_units)
            if self.systemctl_error:
                return done(err=self.systemctl_error, code=1)
            return done(self.services if any(a.endswith(".service") for a in argv) else self.timers)
        if argv[0] == "journalctl":
            return done(self.journal, "no journal", self.journal_code) if self.journal_code else done(self.journal)
        return done(self.analyze)

    def count(self, program):
        return sum(1 for argv in self.argvs if argv[0] == program)


def pin_utc(case):
    """The window opens at local midnight, so these tests fix the zone instead of using the machine's."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()

    def restore():
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    case.addCleanup(restore)


def as_run(fake):
    """The runner `routines.read` takes, answering from a `FakeSystemd`."""
    def run(argv):
        done = fake(argv)
        if done.returncode != 0:
            return None, next((line.strip() for line in done.stderr.splitlines() if line.strip()), "failed")
        return done.stdout, None
    return run


SINCE = 1_799_884_800
LONG_LIST = "loop.service loaded active running Loop\n"
LONG_SHOW = (
    "Id=loop.service\nFragmentPath=/home/x/.config/systemd/user/loop.service\nActiveState=active\nSubState=running\nResult=success\n"
    "ExecMainStartTimestamp=@1799000000\nExecMainExitTimestamp=\nActiveEnterTimestamp=@1799000000\nTriggeredBy=\nWorkingDirectory=\n"
)


def tail_entry(kind="assistant", text="Done.", **extra):
    entry = {"type": kind, "timestamp": "2027-01-15T07:00:00.000Z",
             "message": {"content": [{"type": "text", "text": text}] if text is not None else []}}
    entry.update(extra)
    return entry


def session(sid, first, last=None, cwd="/w/a", entrypoint="sdk-cli", title="", tail=None):
    """One line of the probe's `list`, `first` and `last` in epoch seconds."""
    return {"id": sid, "slug": "-w-a", "cwd": cwd, "first": int(first * 1000), "last": int((last or first + 60) * 1000),
            "entrypoint": entrypoint, "title": title, "tail": [tail_entry()] if tail is None else tail}


def session_lines(*sessions):
    return "".join(json.dumps(one) + "\n" for one in sessions)


def sid(n):
    return f"{n:08d}-0000-0000-0000-000000000000"


class ReaderCase(unittest.TestCase):
    def setUp(self):
        pin_utc(self)
        self.which = mock.patch.object(routines.shutil, "which", lambda name: "/usr/bin/" + name)
        self.which.start()
        self.addCleanup(self.which.stop)

    def read(self, fake, place=places.HERE, exclude=frozenset()):
        if place == places.HERE:
            with mock.patch.object(places.subprocess, "run", fake):
                return routines.read(NOW, place, None, exclude)
        return routines.read(NOW, place, None, exclude, run=as_run(fake))


class Reading(ReaderCase):
    def test_only_read_commands_are_run(self):
        fake = FakeSystemd(service_list=LONG_LIST, service_show=LONG_SHOW, probe_out=session_lines(session(sid(1), 1799999001)))
        views, error = self.read(fake)
        self.assertIsNone(error)
        probes = [argv for argv in fake.argvs if argv[:2] == ["python3", "-I"]]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0], probe.command("list", str(SINCE)))
        for argv in fake.argvs:
            if argv in probes:
                continue
            self.assertIn(argv[:2], (["systemctl", "--user"], ["journalctl", "--user"], ["systemd-analyze", "calendar"]))
            if argv[0] == "systemctl":
                self.assertIn(argv[2], ("list-units", "list-unit-files", "show"))
            for word in ("start", "stop", "restart", "reset-failed"):
                self.assertNotIn(word, argv)
        files = ["systemctl", "--user", "list-unit-files", "--type=timer", "--plain", "--no-legend", "--no-pager"]
        self.assertEqual(fake.argvs.count(files), 1)

    def test_the_calls_do_not_grow_with_the_number_of_routines(self):
        fake = FakeSystemd()
        self.read(fake)
        self.assertEqual((fake.count("systemctl"), fake.count("journalctl")), (6, 1))
        again = FakeSystemd(list_units="backup-nightly.timer loaded active waiting x\n")
        self.read(again)
        self.assertEqual((again.count("systemctl"), again.count("journalctl")), (6, 1))

    def test_every_listed_timer_becomes_a_routine(self):
        views, _ = self.read(FakeSystemd())
        self.assertEqual([v.name for v in views], [u[: -len(".timer")] for u in core.timer_units(LISTING)])
        self.assertEqual(len(views), 3)

    def switched_off(self, **kwargs):
        kwargs.setdefault("unit_files", UNIT_FILES)
        return FakeSystemd(timers=TIMERS + IDLE_TIMER, services=SERVICES + IDLE_SERVICE, **kwargs)

    def test_every_timer_of_both_listings_becomes_a_routine(self):
        views, _ = self.read(self.switched_off())
        union = sorted(set(core.timer_units(LISTING)) | set(core.timer_unit_files(UNIT_FILES)))
        self.assertEqual([v.name for v in views], [u[: -len(".timer")] for u in union])

    def test_a_switched_off_timer_only_in_the_unit_files_shows_timer_off(self):
        views, _ = self.read(self.switched_off())
        idle = next(v for v in views if v.name == "idle-off")
        self.assertEqual((idle.tag, idle.tone), ("timer off", "muted"))

    def test_a_switched_off_timer_shows_its_journal_runs(self):
        journal = "\n".join(
            json.dumps({"MESSAGE_ID": mid, "USER_UNIT": "idle-job.service", "USER_INVOCATION_ID": "i2",
                        "__REALTIME_TIMESTAMP": str(int(at * 1e6)), **extra})
            for mid, at, extra in ((core.MSG_START, 1799999500, {}), (core.MSG_DONE, 1799999502, {"JOB_RESULT": "done"})))
        fake = self.switched_off(journal=journal)
        views, _ = self.read(fake)
        idle = next(v for v in views if v.name == "idle-off")
        self.assertEqual(idle.events, ((1799999500.0, core.PASSED, ""),))
        asked = next(a for a in fake.argvs if a[0] == "journalctl")
        self.assertIn("idle-job.service", [asked[i + 1] for i, a in enumerate(asked) if a == "-u"])

    def test_a_switched_off_timer_has_no_missed_times(self):
        views, _ = self.read(self.switched_off(analyze=HOURS_BEFORE_NOW))
        idle = next(v for v in views if v.name == "idle-off")
        self.assertFalse(any(kind == core.MISSED for _, kind, _ in idle.events))

    def test_a_timer_in_both_listings_is_one_routine(self):
        views, _ = self.read(self.switched_off())
        names = [v.name for v in views]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names.count("backup-nightly"), 1)

    def test_names_from_both_listings_come_in_name_order(self):
        zz = "\nId=zz-off.timer\nLoadState=loaded\nActiveState=inactive\nUnit=\n"
        fake = FakeSystemd(list_units="report-weekly.timer loaded active waiting x\n",
                           unit_files="backup-nightly.timer enabled enabled\nzz-off.timer disabled enabled\n",
                           timers=TIMERS + zz)
        views, _ = self.read(fake)
        self.assertEqual([v.name for v in views], ["backup-nightly", "report-weekly", "zz-off"])

    def test_a_template_never_reaches_show(self):
        fake = self.switched_off()
        views, _ = self.read(fake)
        self.assertFalse(any(a == "show" and any("fetch@" in x for x in argv) for argv in fake.argvs for a in argv[2:3]))
        self.assertFalse(any(v.name.startswith("fetch") for v in views))

    def test_a_failing_unit_file_listing_keeps_the_loaded_timers(self):
        fake = FakeSystemd(unit_files_error="Access denied\nmore")
        views, error = self.read(fake)
        self.assertEqual([v.name for v in views], [u[: -len(".timer")] for u in core.timer_units(LISTING)])
        self.assertEqual(error, "Switched-off timers couldn't be read: Access denied")
        self.assertEqual(fake.count("journalctl"), 1)

    def test_a_failing_first_listing_skips_the_second(self):
        fake = FakeSystemd(list_error="Failed to connect\nmore", unit_files=UNIT_FILES)
        self.assertEqual(self.read(fake), (None, "Failed to connect"))
        self.assertFalse(any("list-unit-files" in argv for argv in fake.argvs))

    def test_a_timer_gone_from_the_listing_is_gone_from_the_view(self):
        first, _ = self.read(FakeSystemd())
        self.assertIn("report-weekly", [v.name for v in first])
        fake = FakeSystemd(list_units="backup-nightly.timer loaded active waiting x\n● gone.timer not-found inactive dead x\n")
        second, _ = self.read(fake)
        self.assertNotIn("report-weekly", [v.name for v in second])
        self.assertFalse(any("report-weekly.timer" in a for argv in fake.argvs for a in argv))

    def test_a_timer_added_to_the_listing_shows_on_the_next_read(self):
        first, _ = self.read(FakeSystemd(list_units="backup-nightly.timer loaded active waiting x\n"))
        self.assertNotIn("report-weekly", [v.name for v in first])
        second, _ = self.read(FakeSystemd())
        self.assertIn("report-weekly", [v.name for v in second])

    def test_routines_come_in_name_order(self):
        listing = "report-weekly.timer loaded active waiting x\nbackup-nightly.timer loaded active waiting x\n"
        views, _ = self.read(FakeSystemd(list_units=listing))
        self.assertEqual([v.name for v in views], ["backup-nightly", "report-weekly"])

    def test_no_user_timers_is_no_routines(self):
        fake = FakeSystemd(list_units="")
        self.assertEqual(self.read(fake), ([], None))
        self.assertEqual((fake.count("systemctl"), fake.count("journalctl"), fake.count("systemd-analyze")), (4, 0, 0))

    def test_a_failing_listing_is_its_first_line(self):
        fake = FakeSystemd(list_error="Failed to connect to user scope bus\nmore")
        self.assertEqual(self.read(fake), (None, "Failed to connect to user scope bus"))

    def test_when_every_timer_is_missing_systemctl_runs_three_times_and_journalctl_not_at_all(self):
        fake = FakeSystemd(list_units="gone.timer not-found inactive dead x\n", timers="Id=gone.timer\nLoadState=not-found\nUnit=\n")
        views, error = self.read(fake)
        self.assertEqual((fake.count("systemctl"), fake.count("journalctl")), (5, 0))
        self.assertEqual([(v.name, v.tag) for v in views], [("gone", "not found")])

    def test_the_journal_asks_for_the_service_the_timer_names(self):
        fake = FakeSystemd()
        self.read(fake)
        journal = next(a for a in fake.argvs if a[0] == "journalctl")
        self.assertEqual(journal[journal.index("-u") + 1], "backup-job.service")
        self.assertNotIn("backup-nightly.service", journal)

    def test_a_passed_run_from_the_journal_is_shown(self):
        views, _ = self.read(FakeSystemd())
        backup = next(v for v in views if v.name == "backup-nightly")
        self.assertEqual([e[1] for e in backup.events if e[1] != core.MISSED], [core.PASSED])

    def test_a_name_that_looks_like_an_option_is_never_passed_on(self):
        fake = FakeSystemd(list_units="--user.timer loaded active waiting x\nbackup-nightly.timer loaded active waiting x\n")
        self.read(fake)
        self.assertNotIn("--user.timer", sum(fake.argvs, []))


class Edges(ReaderCase):
    def test_no_systemctl_is_the_one_line_and_runs_nothing(self):
        self.which.stop()
        self.addCleanup(self.which.start)
        with mock.patch.object(routines.shutil, "which", lambda name: None), \
                mock.patch.object(places.subprocess, "run", side_effect=AssertionError("ran")):
            self.assertEqual(routines.read(NOW), (None, routines.NO_SYSTEMD))

    def test_commands_that_fail_give_one_line_and_never_raise(self):
        for raised in (OSError("no such thing"), subprocess.TimeoutExpired("systemctl", 10)):
            with mock.patch.object(places.subprocess, "run", side_effect=raised):
                views, error = routines.read(NOW)
            self.assertIsNone(views)
            self.assertEqual(len(error.splitlines()), 1)

    def test_a_bus_error_is_returned_as_its_first_line(self):
        fake = FakeSystemd(systemctl_error="Failed to connect to user scope bus via local transport\nmore")
        self.assertEqual(self.read(fake), (None, "Failed to connect to user scope bus via local transport"))

    def test_a_failing_journal_still_gives_views_from_the_service_state(self):
        views, error = self.read(FakeSystemd(journal_code=1, analyze=HOURS_BEFORE_NOW))
        self.assertIsNone(error)
        self.assertEqual(views[0].events, ((1799999000.0, core.PASSED, ""),))
        self.assertEqual(views[0].error, "no journal")

    def test_a_readable_journal_leaves_no_error_and_judges_missed_times(self):
        views, _ = self.read(FakeSystemd(analyze=HOURS_BEFORE_NOW))
        self.assertEqual(views[0].error, "")
        self.assertEqual(sum(1 for e in views[0].events if e[1] == core.MISSED), 4)

    def test_a_journal_missing_a_run_systemd_recorded_names_no_missed_times(self):
        views, error = self.read(FakeSystemd(journal="", analyze=HOURS_BEFORE_NOW))
        self.assertIsNone(error)
        self.assertEqual(views[0].events, ((1799999000.0, core.PASSED, ""),))

    def test_a_routine_that_never_ran_has_its_due_times_missed(self):
        never = SERVICES.replace("ExecMainStartTimestamp=@1799999000\nExecMainExitTimestamp=@1799999002",
                                 "ExecMainStartTimestamp=\nExecMainExitTimestamp=")
        views, error = self.read(FakeSystemd(services=never, journal="", analyze=HOURS_BEFORE_NOW))
        self.assertIsNone(error)
        self.assertEqual(views[0].tag, "missed")
        self.assertEqual(views[0].error, "")
        self.assertTrue(views[0].events)
        self.assertTrue(all(kind == core.MISSED for _, kind, _ in views[0].events))

    def test_no_journalctl_is_named_as_that_not_as_no_systemd(self):
        fake = FakeSystemd(analyze=HOURS_BEFORE_NOW)

        def run(argv, **kwargs):
            if argv[0] == "journalctl":
                raise FileNotFoundError(2, "No such file or directory", "journalctl")
            return fake(argv, **kwargs)

        views, error = self.read(run)
        self.assertIsNone(error)
        self.assertEqual(views[0].error, "journalctl isn't installed on this machine")
        self.assertEqual(views[0].events, ((1799999000.0, core.PASSED, ""),))


class Running(ReaderCase):
    def services_of(self, state, started, exited):
        return (f"Id=backup-job.service\nActiveState={state}\nResult=success\nExecMainStatus=0\n"
                f"ExecMainStartTimestamp={started}\nExecMainExitTimestamp={exited}\n")

    def newest(self, services):
        views, _ = self.read(FakeSystemd(services=services))
        return next(e for e in views[0].events if e[1] != core.MISSED)

    def test_an_active_service_whose_main_process_exited_shows_passed(self):
        self.assertEqual(self.newest(self.services_of("active", "@1799999000", "@1799999002"))[1], core.PASSED)

    def test_an_active_service_with_no_exit_shows_running(self):
        self.assertEqual(self.newest(self.services_of("active", "@1799999000", ""))[1], core.RUNNING)


def minutes_text(start, count):
    return "".join(
        f"   {'Next elapse' if i == 0 else f'Iteration #{i + 1}'}: {time.strftime('%a %Y-%m-%d %H:%M:%S', time.gmtime(start + 60 * i))} UTC\n"
        for i in range(count))


TICK = (
    "Id=tick.timer\nLoadState=loaded\nActiveState=active\nUnit=tick-job.service\n"
    "TimersCalendar={ OnCalendar=*-*-* *:*:00 ; next_elapse=@1 }\nNextElapseUSecRealtime=@1800000060\nActiveEnterTimestamp=@1799000000\n"
)
TICK_SERVICE = "Id=tick-job.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\nExecMainStartTimestamp=\nExecMainExitTimestamp=\n"


class Window(ReaderCase):
    def test_window_starts_yesterday_midnight(self):
        fake = FakeSystemd()
        self.read(fake)
        journal = next(a for a in fake.argvs if a[0] == "journalctl")
        self.assertEqual(journal[journal.index("--since") + 1], f"@{SINCE}")
        analyze = next(a for a in fake.argvs if a[0] == "systemd-analyze")
        self.assertIn(f"--base-time=@{SINCE}", analyze)

    def test_every_minute_timer_is_not_capped_over_two_days(self):
        fake = FakeSystemd(timers=TICK, services=TICK_SERVICE, list_units="tick.timer loaded active waiting x\n", journal="",
                           analyze=minutes_text(SINCE, core.CALENDAR_CAP))
        views, error = self.read(fake)
        self.assertIsNone(error)
        analyze = next(a for a in fake.argvs if a[0] == "systemd-analyze")
        self.assertIn("--iterations=3000", analyze)
        self.assertEqual(core.CALENDAR_CAP, 3000)
        (tick,) = views
        self.assertEqual({kind for _, kind, _ in tick.events}, {core.MISSED})
        self.assertEqual(len(tick.events), (int(NOW) - core.MISSED_SLACK - SINCE) // 60 + 1)


class Sessions(ReaderCase):
    def passed_sessions(self, views, name="backup-nightly"):
        view = next(v for v in views if v.name == name)
        return next((view.sessions[i] for i, event in enumerate(view.events) if event[1] == core.PASSED), None)

    def test_a_timer_run_gets_its_session_by_time(self):
        one = session(sid(1), 1799999001, 1799999001, title="Digest")
        far = session(sid(2), 1799990000)
        views, _ = self.read(FakeSystemd(probe_out=session_lines(one, far)))
        (found,) = self.passed_sessions(views)
        self.assertEqual((found.id, found.title, found.ending), (sid(1), "Digest", core.FINISHED))

    def test_working_directory_narrows_the_match(self):
        services = SERVICES.replace("Id=backup-job.service\n", "Id=backup-job.service\nWorkingDirectory=/w/a\n")
        mine = session(sid(1), 1799999001, cwd="/w/a/sub")
        other = session(sid(2), 1799999001, cwd="/w/b")
        views, _ = self.read(FakeSystemd(services=services, probe_out=session_lines(mine, other)))
        self.assertEqual([found.id for found in self.passed_sessions(views)], [sid(1)])

    def test_interactive_and_listed_sessions_never_match(self):
        interactive = session(sid(1), 1799999001, entrypoint="cli")
        listed = session(sid(2), 1799999001)
        fake = FakeSystemd(probe_out=session_lines(interactive, listed))
        views, _ = self.read(fake, exclude=frozenset({sid(2)}))
        self.assertEqual(self.passed_sessions(views), ())

    def test_session_without_entrypoint_is_eligible(self):
        views, _ = self.read(FakeSystemd(probe_out=session_lines(session(sid(1), 1799999001, entrypoint=None))))
        self.assertEqual([found.id for found in self.passed_sessions(views)], [sid(1)])

    def test_no_session_says_so(self):
        views, _ = self.read(FakeSystemd(probe_out=""))
        view = next(v for v in views if v.name == "backup-nightly")
        self.assertTrue(view.events)
        self.assertTrue(all(found == () for found in view.sessions))
        self.assertEqual(len(view.sessions), len(view.events))

    def test_probe_not_run_without_runs(self):
        never = SERVICES.replace("ExecMainStartTimestamp=@1799999000\nExecMainExitTimestamp=@1799999002",
                                 "ExecMainStartTimestamp=\nExecMainExitTimestamp=")
        fake = FakeSystemd(services=never, journal="")
        self.read(fake)
        self.assertFalse(any(argv[:2] == ["python3", "-I"] for argv in fake.argvs))

    def test_a_failing_probe_keeps_the_routines_and_names_why(self):
        views, error = self.read(FakeSystemd(probe_error="python3: command not found\nmore"))
        self.assertTrue(views)
        self.assertEqual(error, "Claude sessions couldn't be read: python3: command not found")


class Services(ReaderCase):
    def read_service(self, show=LONG_SHOW, sessions=(), **kwargs):
        fake = FakeSystemd(service_list=LONG_LIST, service_show=show, probe_out=session_lines(*sessions), **kwargs)
        views, error = self.read(fake)
        return fake, views, error

    def test_a_long_running_service_is_a_routine(self):
        _, views, _ = self.read_service(sessions=[session(sid(1), 1799990000, 1799990100, title="Pick")])
        loop = next(v for v in views if v.name == "loop")
        self.assertEqual((loop.kind, loop.tag), ("service", "finished"))
        self.assertEqual([found.id for found in loop.sessions[0]], [sid(1)])

    def test_a_distro_service_is_not(self):
        shown = LONG_SHOW.replace("/home/x/.config/systemd/user/loop.service", "/usr/lib/systemd/user/loop.service")
        _, views, _ = self.read_service(show=shown, sessions=[session(sid(1), 1799990000)])
        self.assertNotIn("loop", [v.name for v in views])

    def test_a_timer_started_service_is_not(self):
        shown = LONG_SHOW.replace("TriggeredBy=\n", "TriggeredBy=loop.timer\n")
        _, views, _ = self.read_service(show=shown, sessions=[session(sid(1), 1799990000)])
        self.assertNotIn("loop", [v.name for v in views])

    def test_a_service_with_no_session_is_not_shown(self):
        fake, views, _ = self.read_service(sessions=[])
        self.assertNotIn("loop", [v.name for v in views])
        self.assertTrue(any(argv[:2] == ["python3", "-I"] for argv in fake.argvs))

    def test_sessions_group_into_cycles(self):
        pick = session(sid(1), NOW - 4680, NOW - 4560, title="pick")
        build = session(sid(2), NOW - 4500, NOW - 4200, title="build")
        again = session(sid(3), NOW - 3000, NOW - 2900, title="pick")
        _, views, _ = self.read_service(sessions=[again, build, pick])
        loop = next(v for v in views if v.name == "loop")
        self.assertEqual([len(found) for found in loop.sessions], [1, 2])
        self.assertEqual([found.id for found in loop.sessions[1]], [sid(1), sid(2)])
        self.assertEqual(len(loop.events), 2)

    def test_a_service_session_still_going_shows_working(self):
        midturn = [tail_entry(text=None, message={"content": [{"type": "tool_use", "name": "Bash"}]})]
        _, views, _ = self.read_service(sessions=[session(sid(1), NOW - 120, NOW - 60, tail=midturn)])
        loop = next(v for v in views if v.name == "loop")
        self.assertEqual((loop.tag, loop.tone, loop.detail), ("working", "plain", "working"))

    def test_a_stopped_service_is_stopped(self):
        shown = LONG_SHOW.replace("ActiveState=active", "ActiveState=inactive").replace("ExecMainExitTimestamp=", "ExecMainExitTimestamp=@1799999000")
        _, views, _ = self.read_service(show=shown, sessions=[session(sid(1), 1799990000)])
        loop = next(v for v in views if v.name == "loop")
        self.assertEqual((loop.tag, loop.tone), ("stopped", "muted"))

    def test_a_usage_limit_names_when_it_waits_till(self):
        limit = tail_entry(text="You've hit your session limit", isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429,
                           apiErrorParams={"rate_limit_info": {"resetsAt": 1799962800}})
        _, views, _ = self.read_service(sessions=[session(sid(1), NOW - 4000, NOW - 3900, tail=[limit])])
        loop = next(v for v in views if v.name == "loop")
        self.assertEqual((loop.tag, loop.tone, loop.detail), ("usage limit", "needs", "waiting on the usage limit till 21:40"))


class Places(ReaderCase):
    def test_unreachable_place_still_shows_others(self):
        here, far = places.HERE, core.Place("server-2", "srv2", None)
        mine = FakeSystemd()
        refused = "Can't reach srv2: ssh: connect to host srv2 port 22: Connection refused"

        def run(place, argv):
            if place == far:
                return None, refused
            return as_run(mine)(argv)

        found = routines.read_places(NOW, [here, far], {}, frozenset(), run=run)
        self.assertEqual([pv.place for pv in found], [here, far])
        self.assertEqual(len(found[0].views), 3)
        self.assertIsNone(found[0].error)
        self.assertEqual((found[1].views, found[1].error), ((), refused))

    def test_only_this_user_leaves_out_the_listed_sessions(self):
        other = core.Place("worker", None, "bot")
        fake = FakeSystemd(probe_out=session_lines(session(sid(1), 1799999001)))
        found = routines.read_places(NOW, [places.HERE, other], {}, frozenset({sid(1)}), run=lambda place, argv: as_run(fake)(argv))

        def count(place_view):
            return sum(len(run_sessions) for view in place_view.views for run_sessions in view.sessions)

        self.assertEqual((count(found[0]), count(found[1])), (0, 1))

    def test_the_commands_reach_the_place_through_the_runner(self):
        seen = []

        def run(place, argv):
            seen.append(place.name)
            return None, "nope"

        routines.read_places(NOW, [places.HERE, core.Place("worker", None, "bot")], {}, frozenset(), run=run)
        self.assertEqual(seen, ["this user", "worker"])


if __name__ == "__main__":
    unittest.main()
