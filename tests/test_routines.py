import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from emx import core, routines

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
ANALYZE = (
    "   Next elapse: Sun 2027-01-17 07:00:00 UTC\n   Iteration #2: Sun 2027-01-17 08:00:00 UTC\n"
)

HOURS_BEFORE_NOW = "".join(
    f"   {'Next elapse' if i == 0 else f'Iteration #{i + 1}'}: Fri 2027-01-15 0{hour}:00:00 UTC\n" for i, hour in enumerate(range(3, 8))
)


class FakeSystemd:
    """Answers each command by its program and records every argv."""

    def __init__(self, timers=TIMERS, services=SERVICES, journal=JOURNAL, journal_code=0, systemctl_error=None, analyze=ANALYZE):
        self.argvs = []
        self.analyze = analyze
        self.timers, self.services, self.journal = timers, services, journal
        self.journal_code, self.systemctl_error = journal_code, systemctl_error

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        done = lambda out="", err="", code=0: SimpleNamespace(stdout=out, stderr=err, returncode=code)
        if argv[0] == "systemctl":
            if self.systemctl_error:
                return done(err=self.systemctl_error, code=1)
            return done(self.services if any(a.endswith(".service") for a in argv) else self.timers)
        if argv[0] == "journalctl":
            return done(self.journal, "no journal", self.journal_code) if self.journal_code else done(self.journal)
        return done(self.analyze)

    def count(self, program):
        return sum(1 for argv in self.argvs if argv[0] == program)


class ReaderCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "routines")
        self.which = mock.patch.object(routines.shutil, "which", lambda name: "/usr/bin/" + name)
        self.which.start()
        self.addCleanup(self.which.stop)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self, fake):
        with mock.patch.object(routines.subprocess, "run", fake):
            return routines.read(self.path, NOW)


class Reading(ReaderCase):
    def test_only_read_commands_are_run(self):
        self.write("backup-nightly\nreport-weekly\ngone\n")
        fake = FakeSystemd()
        views, error = self.read(fake)
        self.assertIsNone(error)
        for argv in fake.argvs:
            self.assertIn(argv[:2], (["systemctl", "--user"], ["journalctl", "--user"], ["systemd-analyze", "calendar"]))
            if argv[0] == "systemctl":
                self.assertEqual(argv[2], "show")
            for word in ("start", "stop", "restart", "reset-failed"):
                self.assertNotIn(word, argv)

    def test_the_calls_do_not_grow_with_the_number_of_routines(self):
        self.write("backup-nightly\nreport-weekly\ngone\n")
        fake = FakeSystemd()
        self.read(fake)
        self.assertEqual((fake.count("systemctl"), fake.count("journalctl")), (2, 1))
        self.write("backup-nightly\n")
        again = FakeSystemd()
        self.read(again)
        self.assertEqual((again.count("systemctl"), again.count("journalctl")), (2, 1))

    def test_when_every_timer_is_missing_systemctl_runs_once_and_journalctl_not_at_all(self):
        self.write("gone\n")
        fake = FakeSystemd(timers="Id=gone.timer\nLoadState=not-found\nUnit=\n")
        views, error = self.read(fake)
        self.assertEqual((fake.count("systemctl"), fake.count("journalctl")), (1, 0))
        self.assertEqual([(v.name, v.tag) for v in views], [("gone", "not found")])

    def test_the_journal_asks_for_the_service_the_timer_names(self):
        self.write("backup-nightly\n")
        fake = FakeSystemd()
        self.read(fake)
        journal = next(a for a in fake.argvs if a[0] == "journalctl")
        self.assertEqual(journal[journal.index("-u") + 1], "backup-job.service")
        self.assertNotIn("backup-nightly.service", journal)

    def test_views_come_back_in_the_files_order(self):
        self.write("report-weekly\ngone\nbackup-nightly\n")
        views, _ = self.read(FakeSystemd())
        self.assertEqual([v.name for v in views], ["report-weekly", "gone", "backup-nightly"])

    def test_a_passed_run_from_the_journal_is_shown(self):
        self.write("backup-nightly\n")
        views, _ = self.read(FakeSystemd())
        self.assertEqual([e[1] for e in views[0].events if e[1] != core.MISSED], [core.PASSED])

    def test_a_name_that_looks_like_an_option_is_never_passed_on(self):
        self.write("--user\nbackup-nightly\n")
        fake = FakeSystemd()
        views, _ = self.read(fake)
        self.assertNotIn("--user.timer", sum(fake.argvs, []))
        self.assertEqual(views[0].tag, "not found")


class Edges(ReaderCase):
    def test_no_systemctl_is_the_one_line_and_runs_nothing(self):
        self.write("backup-nightly\n")
        self.which.stop()
        self.addCleanup(self.which.start)
        with mock.patch.object(routines.shutil, "which", lambda name: None), \
                mock.patch.object(routines.subprocess, "run", side_effect=AssertionError("ran")):
            self.assertEqual(routines.read(self.path, NOW), (None, routines.NO_SYSTEMD))

    def test_a_missing_list_is_no_routines_and_runs_nothing(self):
        with mock.patch.object(routines.subprocess, "run", side_effect=AssertionError("ran")):
            self.assertEqual(routines.read(self.path, NOW), ([], None))

    def test_a_list_that_cannot_be_read_is_one_line(self):
        os.mkdir(self.path)
        views, error = self.read(FakeSystemd())
        self.assertIsNone(views)
        self.assertEqual(len(error.splitlines()), 1)

    def test_commands_that_fail_give_one_line_and_never_raise(self):
        self.write("backup-nightly\n")
        for raised in (OSError("no such thing"), subprocess.TimeoutExpired("systemctl", 10)):
            with mock.patch.object(routines.subprocess, "run", side_effect=raised):
                views, error = routines.read(self.path, NOW)
            self.assertIsNone(views)
            self.assertEqual(len(error.splitlines()), 1)

    def test_a_bus_error_is_returned_as_its_first_line(self):
        self.write("backup-nightly\n")
        fake = FakeSystemd(systemctl_error="Failed to connect to user scope bus via local transport\nmore")
        self.assertEqual(self.read(fake), (None, "Failed to connect to user scope bus via local transport"))

    def test_a_failing_journal_still_gives_views_from_the_service_state(self):
        self.write("backup-nightly\n")
        views, error = self.read(FakeSystemd(journal_code=1, analyze=HOURS_BEFORE_NOW))
        self.assertIsNone(error)
        self.assertEqual(views[0].events, ((1799999000.0, core.PASSED, ""),))
        self.assertEqual(views[0].error, "no journal")

    def test_a_readable_journal_leaves_no_error_and_judges_missed_times(self):
        self.write("backup-nightly\n")
        views, _ = self.read(FakeSystemd(analyze=HOURS_BEFORE_NOW))
        self.assertEqual(views[0].error, "")
        self.assertEqual(sum(1 for e in views[0].events if e[1] == core.MISSED), 4)

    def test_a_journal_missing_a_run_systemd_recorded_names_no_missed_times(self):
        self.write("backup-nightly\n")
        views, error = self.read(FakeSystemd(journal="", analyze=HOURS_BEFORE_NOW))
        self.assertIsNone(error)
        self.assertEqual(views[0].events, ((1799999000.0, core.PASSED, ""),))

    def test_a_routine_that_never_ran_has_its_due_times_missed(self):
        self.write("backup-nightly\n")
        never = SERVICES.replace("ExecMainStartTimestamp=@1799999000\nExecMainExitTimestamp=@1799999002",
                                 "ExecMainStartTimestamp=\nExecMainExitTimestamp=")
        views, error = self.read(FakeSystemd(services=never, journal="", analyze=HOURS_BEFORE_NOW))
        self.assertIsNone(error)
        self.assertEqual(views[0].tag, "missed")
        self.assertEqual(views[0].error, "")
        self.assertTrue(views[0].events)
        self.assertTrue(all(kind == core.MISSED for _, kind, _ in views[0].events))

    def test_no_journalctl_is_named_as_that_not_as_no_systemd(self):
        self.write("backup-nightly\n")
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
        self.write("backup-nightly\n")
        views, _ = self.read(FakeSystemd(services=services))
        return next(e for e in views[0].events if e[1] != core.MISSED)

    def test_an_active_service_whose_main_process_exited_shows_passed(self):
        self.assertEqual(self.newest(self.services_of("active", "@1799999000", "@1799999002"))[1], core.PASSED)

    def test_an_active_service_with_no_exit_shows_running(self):
        self.assertEqual(self.newest(self.services_of("active", "@1799999000", ""))[1], core.RUNNING)


if __name__ == "__main__":
    unittest.main()
