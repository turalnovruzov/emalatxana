import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from emx import core, places, routines

TESTS = Path(__file__).parent
PY = sys.executable
SUDO = f"{PY} {TESTS / 'fake_sudo.py'}"
NOW = 1_800_000_000.0


def clean_env(**more):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_")}
    env.update(more)
    return env


class Fakes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)

    def test_fake_systemd_answers_from_its_dir(self):
        folder = os.path.join(self.dir, "fx")
        os.makedirs(folder)
        Path(folder, "timers.list").write_text("a.timer loaded active waiting A\n")
        link = os.path.join(self.dir, "systemctl")
        os.symlink(TESTS / "fake_systemd.py", link)
        env = clean_env(FAKE_SYSTEMD_DIR=folder)
        ok = subprocess.run([PY, link, "--user", "list-units", "--type=timer", "--all"], capture_output=True, text=True, env=env)
        self.assertEqual((ok.returncode, ok.stdout), (0, "a.timer loaded active waiting A\n"))
        missing = subprocess.run([PY, link, "--user", "list-units", "--type=service"], capture_output=True, text=True, env=env)
        self.assertEqual((missing.returncode, missing.stderr.strip()), (1, "fake systemd: no fixture"))
        bare = subprocess.run([PY, link, "--user", "list-units"], capture_output=True, text=True, env=clean_env())
        self.assertEqual((bare.returncode, bare.stderr.strip()), (1, "fake systemd: no fixture"))

    def test_fake_sudo_moves_both_homes(self):
        code = "import os;print(os.environ['FAKE_SYSTEMD_DIR'], os.environ['CLAUDE_CONFIG_DIR'])"
        env = clean_env(FAKE_SUDO_SYSTEMD_DIR="/x/systemd", FAKE_SUDO_CLAUDE_HOME="/x/claude")
        for lead in (["bot"], ["-n", "-u", "bot"]):
            with self.subTest(lead=lead):
                done = subprocess.run([PY, str(TESTS / "fake_sudo.py"), *lead, PY, "-c", code], capture_output=True, text=True, env=env)
                self.assertEqual(done.stdout.strip(), "/x/systemd /x/claude")

    def test_server_and_user_place_runs_end_to_end(self):
        far = core.Place("far", "srv2", "bot")
        env = clean_env(EMX_SSH=str(TESTS / "fake_ssh.py"))
        out, error = places.run(far, [PY, "-c", "import os;print(os.environ['XDG_RUNTIME_DIR'])"],
                                {"place_user_command": SUDO}, env=env)
        self.assertIsNone(error)
        self.assertEqual(out.strip(), f"/run/user/{os.getuid()}")


class Built(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        old = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self.restore, old)
        done = subprocess.run([PY, str(TESTS / "live_routines.py"), "build", self.dir, str(NOW)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    @staticmethod
    def restore(old):
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    def test_built_fixture_reads_as_two_places(self):
        env = clean_env(
            PATH=f"{self.dir}/bin:{os.environ['PATH']}", FAKE_SYSTEMD_DIR=f"{self.dir}/here",
            FAKE_SUDO_SYSTEMD_DIR=f"{self.dir}/worker", FAKE_SUDO_CLAUDE_HOME=f"{self.dir}/worker-claude",
            CLAUDE_CONFIG_DIR=f"{self.dir}/nothing")
        prefs = {"place_user_command": SUDO}
        with mock.patch.dict(os.environ, {"PATH": env["PATH"]}):
            found = routines.read_places(NOW, [places.HERE, core.Place("worker", None, "bot")], prefs,
                                         run=lambda place, argv: places.run(place, argv, prefs, env=env))
        here, worker = found
        self.assertEqual((here.views, here.error), ((), None))
        self.assertEqual(worker.error, None)
        self.assertEqual([view.name for view in worker.views], ["daily-digest", "loop"])
        loop = worker.views[1]
        self.assertEqual((loop.tag, len(loop.events)), ("usage limit", 2))
        self.assertEqual([len(group) for group in loop.sessions], [2, 1])
        self.assertEqual(loop.sessions[0][-1].ending, core.LIMIT)
        self.assertEqual(worker.views[0].tag, "passed")
        self.assertEqual(len(worker.views[0].sessions[0]), 1)
        self.assertRegex(core.problems_line(found, time.gmtime), r"^⚠ 1 usage limit · worker, \d\d:\d\d$")
