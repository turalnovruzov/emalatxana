import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from emx import __version__, cli, core
from emx.tmux import VERSION_OPT, Pane, TmuxError


class Attached(Exception):
    pass


class FakeTmux:
    """A running workspace on old code whose config no longer sources."""

    def __init__(self, socket, config=None):
        self.socket = socket
        self.options = {VERSION_OPT: "old"}

    def argv(self, *args):
        return ["tmux", "-L", self.socket, *args]

    def has_session(self):
        return True

    def session_option(self, name):
        return self.options.get(name, "")

    def set_session_option(self, name, value):
        self.options[name] = value

    def panes(self):
        return [Pane("%1", "@1", False, "sidebar", "", 30, 200)]

    def kill_pane(self, pane):
        pass

    def respawn(self, pane, command):
        pass

    def source_config(self):
        raise TmuxError("emx.tmux.conf:9: invalid option: bogus\nsecond line")


class FailingUpgrade(unittest.TestCase):
    def test_still_attaches_and_leaves_the_stamp_stale(self):
        made = []

        def make(*args, **kwargs):
            made.append(FakeTmux(*args, **kwargs))
            return made[-1]

        err = io.StringIO()
        with mock.patch.object(cli, "Tmux", make), \
                mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict(cli.os.environ, {"EMX_SOCKET": "emx-test-unit"}), \
                mock.patch.object(cli.os, "execvpe", side_effect=Attached) as execvpe, \
                redirect_stderr(err):
            with self.assertRaises(Attached):
                cli.launch()
        self.assertIn("attach-session", execvpe.call_args.args[1])
        self.assertEqual(
            err.getvalue(),
            "emx: couldn't update the running workspace: emx.tmux.conf:9: invalid option: bogus second line\n",
        )
        self.assertEqual(made[0].options[VERSION_OPT], "old")


class LeftCheck(unittest.TestCase):
    """`emx left` answers tmux's if-shell with an exit code; anything wrong means ← goes to Claude Code, silently."""

    def run_check(self, args, lines=None, error=None):
        def cursor_lines(tmux, pane, y):
            if error:
                raise error
            return lines

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.Tmux, "cursor_lines", cursor_lines), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["left", *args])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        return code

    def test_an_empty_prompt_answers_0(self):
        lines = ["❯\xa0", "─" * 30]
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "2", "5", "1", "30"], lines), 0)
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "2", "5", "1", "30"], ["❯ typed", "─" * 30]), 1)

    def test_anything_wrong_answers_1(self):
        good = ["%2", "2", "5", "1", "30"]
        for args, error in (
            (["default", *good], None),
            (["", *good], None),
            (["emx-test-unit", "%2", "two", "5", "1", "30"], None),
            (["emx-test-unit", "%2"], None),
            (["emx-test-unit", *good], TmuxError("no server")),
        ):
            with self.subTest(args=args, error=error):
                self.assertEqual(self.run_check(args, ["❯\xa0", "─" * 30], error), 1)


class Version(unittest.TestCase):
    def test_version_prints_and_never_starts_tmux(self):
        out = io.StringIO()
        with mock.patch.object(cli, "launch", side_effect=AssertionError("launched")), \
                mock.patch.object(cli, "Tmux", side_effect=AssertionError("tmux")), \
                mock.patch.object(cli.os, "execvpe", side_effect=AssertionError("exec")), redirect_stdout(out):
            code = cli.main(["--version"])
        self.assertEqual((code, out.getvalue()), (0, f"emx {__version__}\n"))

    def test_the_version_is_semver_and_in_the_usage(self):
        self.assertIsNotNone(core.parse_version(__version__))
        self.assertIn("--version", cli.USAGE)

    def test_the_real_launcher_prints_the_version_with_no_tmux_on_path(self):
        done = subprocess.run(
            [sys.executable, cli.BIN, "--version"], capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": "/nonexistent"}, stdin=subprocess.DEVNULL,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, f"emx {__version__}\n", ""))

    def test_read_changelog_is_empty_when_the_file_is_missing(self):
        with mock.patch.object(cli, "CHANGELOG", "/nonexistent/CHANGELOG.md"):
            self.assertEqual(cli.read_changelog(), "")
        self.assertIn(f"## {__version__}", cli.read_changelog())


if __name__ == "__main__":
    unittest.main()
