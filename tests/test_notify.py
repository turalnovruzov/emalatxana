import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emx import BIN, core, notify


def tool_ok(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 0, "", "")


class Paths(unittest.TestCase):
    def test_helper_home_honours_the_override_and_test_runs_get_none(self):
        self.assertEqual(notify.helper_home({"EMX_NOTIFY_HOME": "/tmp/h", "EMX_SHOW_TEST_SESSIONS": "1"}), Path("/tmp/h"))
        self.assertIsNone(notify.helper_home({"EMX_SHOW_TEST_SESSIONS": "1"}))
        self.assertIsNone(notify.helper_home({"EMX_SHOW_TEST_SESSIONS": "1", "EMX_NOTIFY_HOME": "relative/h"}))
        home = notify.helper_home({})
        self.assertEqual(home.parts[-3:], ("Library", "Application Support", "emx"))
        self.assertEqual(home, notify.real_home())

    def test_an_override_always_gets_the_test_bundle_id(self):
        self.assertEqual(notify.bundle_id({"EMX_NOTIFY_HOME": "/tmp/h"}), notify.TEST_BUNDLE_ID)
        self.assertEqual(notify.bundle_id({"EMX_NOTIFY_HOME": str(notify.real_home())}), notify.TEST_BUNDLE_ID)
        self.assertEqual(notify.bundle_id({}), notify.BUNDLE_ID)

    def test_the_command_can_be_replaced_by_emx_notify(self):
        self.assertEqual(notify.helper_command({"EMX_NOTIFY": "/x/fake"}, Path("/h")), "/x/fake")
        self.assertEqual(notify.helper_command({"EMX_NOTIFY": "/x/fake"}, None), "/x/fake")
        self.assertEqual(notify.helper_command({}, Path("/h")), "/h/emx-notify.app/Contents/MacOS/emx-notify")
        self.assertIsNone(notify.helper_command({}, None))


class Bundle(unittest.TestCase):
    def test_info_plist_names_emx_and_hides_the_dock_icon(self):
        info = plistlib.loads(notify.info_plist(notify.BUNDLE_ID, "emx"))
        self.assertEqual(info["CFBundleDisplayName"], "emx")
        self.assertEqual(info["CFBundleName"], "emx")
        self.assertEqual(info["CFBundleIdentifier"], notify.BUNDLE_ID)
        self.assertIs(info["LSUIElement"], True)
        self.assertEqual(info["CFBundleExecutable"], notify.EXECUTABLE)
        self.assertEqual(info["NSPrincipalClass"], "NSApplication")
        self.assertEqual(notify.NAMES[notify.TEST_BUNDLE_ID], "emx test")

    def test_the_hash_changes_with_the_source_and_the_bundle_id(self):
        real, test = notify.source_hash(notify.BUNDLE_ID), notify.source_hash(notify.TEST_BUNDLE_ID)
        self.assertNotEqual(real, test)
        self.assertEqual(real, notify.source_hash(notify.BUNDLE_ID))
        with tempfile.TemporaryDirectory() as tmp:
            changed = os.path.join(tmp, "emx-notify.swift")
            shutil.copy(notify.SOURCE, changed)
            with open(changed, "a", encoding="utf-8") as fh:
                fh.write("// touched\n")
            with mock.patch.object(notify, "SOURCE", changed):
                self.assertNotEqual(notify.source_hash(notify.BUNDLE_ID), real)
        with mock.patch.object(notify, "BUILD_RECIPE", notify.BUILD_RECIPE + " -g"):
            self.assertNotEqual(notify.source_hash(notify.BUNDLE_ID), real)


class Payload(unittest.TestCase):
    ENV = {
        "PATH": "/usr/bin:/bin", "EMX_SOCKET": "emx", "TMUX_TMPDIR": "/tmp/t", "XDG_STATE_HOME": "/tmp/s",
        "HOME": "/Users/x", "SECRET_TOKEN": "s3cret", "EMX_SHOW_TEST_SESSIONS": "1", "EMX_SAY": "/x/say",
    }

    def test_payload_carries_absolute_paths_and_only_allowlisted_env(self):
        body = "--tea poem is done; rm -rf ~"
        with mock.patch.object(notify.shutil, "which", return_value="/opt/brew/bin/tmux"):
            got = notify.payload("tea poem · emx", body, "n1", "sid-1", "emx-test-43", self.ENV)
        self.assertEqual((got["title"], got["body"], got["id"]), ("tea poem · emx", body, "n1"))
        opener = got["open"]
        self.assertTrue(os.path.isabs(opener["python"]))
        self.assertEqual(opener["emx"], os.path.abspath(BIN))
        self.assertEqual(opener["sid"], "sid-1")
        self.assertLessEqual(set(opener["env"]), set(notify.OPEN_ENV))
        self.assertNotIn("SECRET_TOKEN", opener["env"])
        self.assertNotIn("EMX_SAY", opener["env"])
        self.assertEqual(opener["env"]["EMX_SOCKET"], "emx-test-43")
        self.assertEqual(opener["env"]["PATH"], "/usr/bin:/bin:/opt/brew/bin")
        self.assertNotIn(body, json.dumps(opener))

    def test_a_path_already_holding_tmux_is_left_as_it_is(self):
        with mock.patch.object(notify.shutil, "which", return_value="/usr/bin/tmux"):
            got = notify.payload("t", "b", "n1", "sid", "emx", self.ENV)
        self.assertEqual(got["open"]["env"]["PATH"], "/usr/bin:/bin")


class Running(unittest.TestCase):
    GOOD = '{"alerts": "enabled", "auth": "authorized", "delivered": 2, "posted": true, "shown": true}'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fake(self, body):
        path = os.path.join(self.tmp, "fake-notify")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, stat.S_IRWXU)
        return path

    def test_run_never_raises_and_parses_the_report(self):
        good = self.fake(f"cat > {self.tmp}/stdin; echo noise; echo '{self.GOOD}'")
        report, error = notify.run(good, "post", {"title": "t", "body": "-b"})
        self.assertIsNone(error)
        self.assertEqual(report, notify.HelperReport("authorized", "enabled", True, True, 2))
        with open(os.path.join(self.tmp, "stdin"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"title": "t", "body": "-b"})
        cases = {
            "garbage": self.fake("echo not json; echo '[1]'"),
            "exit 1": self.fake("echo boom >&2; exit 1"),
            "timeout": self.fake("sleep 5"),
            "missing": os.path.join(self.tmp, "nowhere"),
        }
        for name, command in cases.items():
            with self.subTest(name):
                report, error = notify.run(command, "status", timeout=0.5)
                self.assertIsNone(report)
                self.assertIsInstance(error, str)
                self.assertTrue(error)

    def test_a_not_authorized_exit_still_gives_its_report(self):
        denied = self.fake("echo '{\"alerts\": \"disabled\", \"auth\": \"denied\", \"delivered\": 0, \"posted\": false, \"shown\": false}'; exit 3")
        report, error = notify.run(denied, "post", {})
        self.assertIsNone(error)
        self.assertEqual(notify.helper_state(report), core.HELPER_BLOCKED)

    def test_helper_state_maps_each_report(self):
        def r(auth, alerts="enabled"):
            return notify.HelperReport(auth, alerts, False, False, 0)

        self.assertEqual(notify.helper_state(None, "boom"), core.HELPER_UNAVAILABLE)
        self.assertEqual(notify.helper_state(r("denied")), core.HELPER_BLOCKED)
        self.assertEqual(notify.helper_state(r("authorized", "disabled")), core.HELPER_BLOCKED)
        self.assertEqual(notify.helper_state(r("notDetermined")), core.HELPER_PENDING)
        self.assertEqual(notify.helper_state(r("authorized")), core.HELPER_OK)
        self.assertEqual(notify.helper_state(r("provisional")), core.HELPER_OK)


class Authorizing(unittest.TestCase):
    REFUSED = (
        '{"alerts": "notSupported", "auth": "notDetermined", "delivered": 0, "posted": false, "shown": false, '
        '"error": "Error Domain=UNErrorDomain Code=1 \\"Notifications are not allowed for this application\\""}'
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Path(self.tmp) / "home"
        self.app = self.home / notify.APP
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        self.command = str(self.app / "Contents" / "MacOS" / notify.EXECUTABLE)

    def fake_open(self, report, code=0):
        """A stand-in for /usr/bin/open: records its argv and the result file's mode, then writes `report` into the last argument."""
        self.argv_log = os.path.join(self.tmp, "argv")
        path = os.path.join(self.tmp, "fake-open")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                'for a in "$@"; do printf "%s\\n" "$a"; done > ' + shlex.quote(self.argv_log) + "\n"
                'for last in "$@"; do :; done\n'
                'stat -f %Lp "$last" >> ' + shlex.quote(self.argv_log) + "\n"
                "printf '%s' " + shlex.quote(report) + ' > "$last"\n'
                f"exit {code}\n"
            )
        os.chmod(path, stat.S_IRWXU)
        return {"EMX_OPEN": path}

    def recorded(self):
        with open(self.argv_log, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        return lines[:-1], lines[-1]

    def test_authorize_goes_through_open_and_reads_the_result_file(self):
        env = self.fake_open(Running.GOOD)
        report, error = notify.authorize(self.command, self.app, 5, env)
        self.assertIsNone(error)
        self.assertEqual(report, notify.HelperReport("authorized", "enabled", True, True, 2))
        argv, mode = self.recorded()
        self.assertEqual(argv[:5], ["-W", "-a", str(self.app), "--args", "authorize"])
        self.assertEqual(len(argv), 6)
        result = argv[5]
        self.assertEqual(os.path.dirname(result), str(self.home))
        self.assertEqual(mode, "600")
        self.assertFalse(os.path.exists(result))
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [notify.APP])

    def test_the_argv_is_open_wait_app_for_a_built_helper_and_direct_for_a_fake(self):
        argv = notify.authorize_argv(self.command, self.app, "/r/f.json", {})
        self.assertEqual(argv, [notify.OPEN, "-W", "-a", str(self.app), "--args", "authorize", "/r/f.json"])
        argv = notify.authorize_argv(self.command, self.app, "/r/f.json", {"EMX_OPEN": "/x/open"})
        self.assertEqual(argv[0], "/x/open")
        self.assertEqual(notify.authorize_argv("/x/fake", None, "/r/f.json", {"EMX_NOTIFY": "/x/fake"}),
                         ["/x/fake", "authorize", "/r/f.json"])
        self.assertEqual(notify.authorize_argv("/x/fake", self.app, "/r/f.json", {"EMX_NOTIFY": "/x/fake", "EMX_OPEN": "/x/open"}),
                         ["/x/fake", "authorize", "/r/f.json"])

    def test_a_refusal_is_reported_with_its_error_and_reads_blocked(self):
        report, error = notify.authorize(self.command, self.app, 5, self.fake_open(self.REFUSED))
        self.assertIsNone(error)
        self.assertEqual(report.auth, "notDetermined")
        self.assertIn("UNErrorDomain Code=1", report.error)
        self.assertEqual(notify.helper_state(report), core.HELPER_BLOCKED)
        self.assertIsNone(notify.parse_report(Running.GOOD).error)
        self.assertEqual(notify.helper_state(notify.parse_report(self.REFUSED.replace("notDetermined", "authorized").replace("notSupported", "enabled"))),
                         core.HELPER_BLOCKED)

    def test_no_report_in_the_file_is_an_error_and_the_file_is_still_removed(self):
        cases = {"empty": ("", 0), "garbage": ("not json", 1)}
        for name, (body, code) in cases.items():
            with self.subTest(name):
                report, error = notify.authorize(self.command, self.app, 5, self.fake_open(body, code))
                self.assertIsNone(report)
                self.assertIn(f"exited {code}", error)
                self.assertFalse(os.path.exists(self.recorded()[0][5]))
        timeout = os.path.join(self.tmp, "slow-open")
        with open(timeout, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nsleep 5\n")
        os.chmod(timeout, stat.S_IRWXU)
        report, error = notify.authorize(self.command, self.app, 0.5, {"EMX_OPEN": timeout})
        self.assertIsNone(report)
        self.assertTrue(error)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [notify.APP])

    def test_the_helper_exit_codes_keep_refused_apart_from_not_authorized(self):
        self.assertEqual(len({notify.EXIT_OK, notify.EXIT_FAILED, notify.EXIT_NOT_AUTHORIZED, notify.EXIT_REFUSED}), 4)
        with open(notify.SOURCE, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn(f"let refusedCode: Int32 = {notify.EXIT_REFUSED}", source)
        self.assertIn(f"? {notify.EXIT_OK} : {notify.EXIT_NOT_AUTHORIZED})", source)
        self.assertIn('line["error"]', source)


class Building(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_no_tools_means_unavailable_and_swiftc_never_runs(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 2, "", "xcode-select: error: no developer tools")

        with mock.patch.object(notify.subprocess, "run", run):
            app, error = notify.ensure_built(self.home, notify.TEST_BUNDLE_ID, register=False)
        self.assertIsNone(app)
        self.assertEqual(error, notify.NO_TOOLS)
        self.assertEqual(calls, [[notify.XCODE_SELECT, "-p"]])

    def test_ensure_built_refuses_the_real_bundle_id_outside_the_real_home(self):
        with mock.patch.object(notify.subprocess, "run", side_effect=AssertionError("ran a tool")) as run:
            app, error = notify.ensure_built(self.home, notify.BUNDLE_ID)
        self.assertIsNone(app)
        self.assertTrue(error)
        run.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_ensure_built_refuses_the_test_bundle_id_in_the_real_home(self):
        with mock.patch.object(notify.subprocess, "run", side_effect=AssertionError("ran a tool")) as run, \
                mock.patch.object(notify.Path, "mkdir", side_effect=AssertionError("mkdir")) as mkdir:
            app, error = notify.ensure_built(notify.real_home(), notify.TEST_BUNDLE_ID)
        self.assertIsNone(app)
        self.assertTrue(error)
        run.assert_not_called()
        mkdir.assert_not_called()

    def test_no_test_touches_the_real_home(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return tool_ok(argv)

        with mock.patch.object(notify.subprocess, "run", run):
            app, error = notify.ensure_built(self.home, notify.TEST_BUNDLE_ID)
        self.assertIsNone(error)
        self.assertEqual(app, self.home / notify.APP)
        self.assertEqual([os.path.basename(c[0]) for c in calls],
                         ["xcode-select", "swiftc", "emx-notify", "iconutil", "codesign", "lsregister"])
        tools = {notify.XCODE_SELECT, notify.SWIFTC, notify.ICONUTIL, notify.CODESIGN, notify.LSREGISTER, notify.SOURCE}
        real = str(notify.real_home())
        for argv in calls:
            for arg in argv:
                self.assertFalse(arg.startswith(real), argv)
                if os.path.isabs(arg) and arg not in tools:
                    self.assertTrue(arg.startswith(str(self.home)), argv)

    def test_a_failed_step_leaves_no_hash_and_no_half_built_app(self):
        def run(argv, **kwargs):
            code = 1 if os.path.basename(argv[0]) == "codesign" else 0
            return subprocess.CompletedProcess(argv, code, "", "codesign: no")

        with mock.patch.object(notify.subprocess, "run", run):
            app, error = notify.ensure_built(self.home, notify.TEST_BUNDLE_ID, register=False)
        self.assertIsNone(app)
        self.assertIn("codesign", error)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [notify.LOCK_FILE])

    def test_the_helper_compiles_into_a_temp_home(self):
        # Fails rather than skips: CI and every Mac emx builds on must have the tools.
        self.assertIsNotNone(shutil.which("swiftc"), "swiftc is not on PATH")
        self.assertEqual(subprocess.run(["xcode-select", "-p"], capture_output=True).returncode, 0, "xcode-select -p failed")
        verbs = []
        real_tool = notify._tool

        def tool(argv, timeout=notify.BUILD_TIMEOUT):
            verbs.append(argv)
            return real_tool(argv, timeout)

        with mock.patch.object(notify, "_tool", tool):
            app, error = notify.ensure_built(self.home, notify.TEST_BUNDLE_ID, register=False)
        self.assertIsNone(error)
        executable = app / "Contents" / "MacOS" / notify.EXECUTABLE
        self.assertTrue(executable.is_file())
        self.assertEqual(len(list((app / "Contents" / "Resources").glob("*.icns"))), 1)
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], notify.TEST_BUNDLE_ID)
        self.assertEqual(subprocess.run(["codesign", "-dv", str(app)], capture_output=True).returncode, 0)
        ran = [a[1] if os.path.basename(a[0]) == notify.EXECUTABLE else os.path.basename(a[0]) for a in verbs]
        self.assertNotIn("status", ran)
        self.assertNotIn("post", ran)
        self.assertNotIn("authorize", ran)
        self.assertNotIn("lsregister", ran)
        built_at = executable.stat().st_mtime_ns
        again, error = notify.ensure_built(self.home, notify.TEST_BUNDLE_ID, register=False)
        self.assertEqual((again, error), (app, None))
        self.assertEqual(executable.stat().st_mtime_ns, built_at)


if __name__ == "__main__":
    unittest.main()
