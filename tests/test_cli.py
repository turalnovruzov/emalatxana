import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import emx
from emx import __version__, cli, core
from emx.tmux import OPEN_OPT, RELAY_PASTE_OPT, SESSION, TERMINAL_OPT, VERSION_OPT, Pane, Tmux, TmuxError, View

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_install import GIT_ENV, add_version, make_source, make_versions, run_git, temp_homes  # noqa: E402


class Attached(Exception):
    pass


class FakeTmux:
    """A running workspace on old code whose config no longer sources; its one view, `emx`, has no client."""

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

    def panes(self, session=SESSION):
        return [Pane("%1", "@1", False, "sidebar", "", 30, 200)]

    def agent_keys(self):
        return set()

    def view_names(self):
        return [SESSION]

    def views(self):
        return [View(SESSION, 0, "", None, 0, "", False, "%2")]

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
    """`emx left` answers tmux's if-shell with an exit code, 0 only for ← to reach Claude Code; anything wrong means ← goes to the panel, silently, since Claude Code forks the conversation on a ← at an empty prompt."""

    KEY = "6f1c9d52-3a7e-4c1b-9e0d-2b8a4f5c7d10"

    def run_check(self, args, lines=None, error=None, shows=KEY, agents=None, cursor=(2, 5)):
        """`args` name the slot; the check reads the prompt from the agent pane holding the key the slot shows."""
        read = []
        if agents is None:
            agents = [Pane("%9", "@3", False, "", self.KEY, 30, 30, agent="claude")]

        def cursor_lines(tmux, pane, y):
            read.append((pane, y))
            if error:
                raise error
            return lines

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.Tmux, "cursor_lines", cursor_lines), \
                mock.patch.object(cli.Tmux, "pane_view", lambda tmux, pane: shows), \
                mock.patch.object(cli.Tmux, "panes", lambda tmux: agents), \
                mock.patch.object(cli.Tmux, "prompt_probe", lambda tmux, pane: (cursor[0], cursor[1], True, 30, False)), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["left", *args])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.read = read
        return code

    def test_an_empty_prompt_answers_1_and_typed_text_0(self):
        lines = ["❯\xa0", "─" * 30]
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "2", "5", "1", "30"], lines), 1)
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "2", "5", "1", "30"], ["❯ typed", "─" * 30]), 0)
        self.assertEqual(self.read, [("%9", 5)])

    def test_an_empty_prompt_a_column_over_answers_1(self):
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "3", "5", "1", "30"], [" ❯\xa0", "─" * 30], cursor=(3, 5)), 1)

    def test_a_slot_showing_no_live_session_answers_1_without_reading_a_screen(self):
        typed = ["❯ typed", "─" * 30]
        args = ["emx-test-unit", "%2", "2", "5", "1", "30"]
        dead = [Pane("%9", "@3", True, "", self.KEY, 30, 30, agent="claude")]
        viewer_only = [Pane("%9", "@3", False, "", self.KEY, 30, 30)]
        for kwargs in ({"shows": ""}, {"shows": "s1"}, {"agents": []}, {"agents": dead}, {"agents": viewer_only}):
            with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
                self.assertEqual(self.run_check(args, typed, **kwargs), 1)
                self.assertEqual(self.read, [])

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


class Hush(unittest.TestCase):
    """Every test runs with a temporary TMUX_TMPDIR and no real tmux process, so none can reach a live emx."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"TMUX_TMPDIR": tmp.name, "EMX_SOCKET": "emx-test-x"}),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.calls = []

    def run_hush(self, run=None, **env):
        def record(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch("emx.tmux.subprocess.run", run or record), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["hush"])
        return code, out.getvalue(), err.getvalue()

    def test_default_and_empty_sockets_refuse_with_2_and_run_no_tmux(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("tmux ran")

        for socket in ("default", ""):
            with self.subTest(socket=socket):
                code, out, err = self.run_hush(forbidden, EMX_SOCKET=socket)
                self.assertEqual((code, out, err), (2, "", cli.SOCKET_REFUSAL + "\n"))

    def test_it_sets_the_option_on_the_named_socket_only(self):
        code, out, err = self.run_hush()
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(self.calls)
        for argv in self.calls:
            self.assertEqual(argv[:3], ["tmux", "-L", "emx-test-x"])
            self.assertNotIn("-S", argv)
            self.assertNotIn("-f", argv)
        (setting,) = [argv for argv in self.calls if "set-option" in argv]
        self.assertEqual(setting[3:7], ["set-option", "-t", "=emx:", "@emx_hush"])
        self.assertTrue(setting[7].isdigit())
        self.assertEqual(len(setting), 8)

    def test_with_no_workspace_it_says_so_and_returns_1_without_setting(self):
        def no_session(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "no server running")

        code, out, err = self.run_hush(no_session)
        self.assertEqual((code, out, err), (1, "", "emx isn't running.\n"))
        self.assertFalse([argv for argv in self.calls if "set-option" in argv])

    def test_with_no_tmux_on_path_it_says_so(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("tmux ran")

        with mock.patch.object(cli.shutil, "which", return_value=None):
            code, out, err = self.run_hush(forbidden)
        self.assertEqual((code, out, err), (1, "", "emx needs tmux, and it is not on PATH.\n"))

    def test_a_failed_set_prints_one_line_and_returns_1(self):
        def fail_on_set(argv, **kwargs):
            done = 1 if "set-option" in argv else 0
            return subprocess.CompletedProcess(argv, done, "", "bad option" if done else "")

        code, out, err = self.run_hush(fail_on_set)
        self.assertEqual((code, out, err), (1, "", "emx: bad option\n"))

    def test_unset_socket_means_emx(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("EMX_SOCKET")
            code, _, _ = self.run_hush()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0][:3], ["tmux", "-L", "emx"])

    def test_the_usage_names_hush(self):
        self.assertIn("hush", cli.USAGE)


SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class OpenSession(unittest.TestCase):
    """`emx open <sid>`, what a notification click runs. Every test runs with a temporary TMUX_TMPDIR and no real tmux or `open`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, "state")
        for patch in (
            mock.patch.dict(os.environ, {"TMUX_TMPDIR": tmp.name, "EMX_SOCKET": "emx-test-x", "EMX_OPEN": "/x/fake-open",
                                         "XDG_STATE_HOME": self.state, "EMX_SHOW_TEST_SESSIONS": "1"}),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.calls = []

    def run_open(self, sid=SID, attached=1, terminal="com.example.term", server=True):
        def record(argv, **kwargs):
            self.calls.append(argv)
            if argv[0] != "tmux":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if "has-session" in argv:
                return subprocess.CompletedProcess(argv, 0 if server else 1, "", "" if server else "no server running")
            if "display-message" in argv:
                return subprocess.CompletedProcess(argv, 0, f"{attached}\t1\t%2\t1\n", "")
            if "show-options" in argv and TERMINAL_OPT in argv:
                return subprocess.CompletedProcess(argv, 0, terminal + "\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("emx.tmux.subprocess.run", record), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["open", sid])
        return code, out.getvalue(), err.getvalue()

    def test_open_writes_the_option_and_prints_the_terminal_for_the_click_to_bring_forward(self):
        code, out, err = self.run_open()
        self.assertEqual((code, out, err), (0, "com.example.term\n", ""))
        (setting,) = [argv for argv in self.calls if "set-option" in argv]
        self.assertEqual(setting, ["tmux", "-L", "emx-test-x", "set-option", "-t", "=emx:", OPEN_OPT, SID])
        self.assertEqual(self.opened(), [])

    def test_open_without_a_known_terminal_only_writes_the_option(self):
        code, out, _ = self.run_open(terminal="")
        self.assertEqual((code, out), (0, ""))
        self.assertTrue([argv for argv in self.calls if OPEN_OPT in argv])
        self.assertFalse([argv for argv in self.calls if argv[0] != "tmux"])

    def opened(self):
        return [argv for argv in self.calls if argv[0] != "tmux"]

    def command_files(self):
        return sorted(Path(self.state, "emx", "open").glob("*.command"))

    def launch_file(self):
        return Path(self.state, "emx", "open", "emx.command")

    def pending_file(self):
        return Path(self.state, "emx", "open", "pending")

    def create_workspace(self):
        made = RecordingWorkspace("emx-test-x")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.state}):
            os.environ.pop("EMX_CONFIG_HOME", None)
            cli.create_workspace(made)
        return made

    def test_detached_open_writes_the_option_and_opens_a_new_window_in_the_recorded_terminal(self):
        code, out, err = self.run_open(attached=0, terminal="com.googlecode.iterm2")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue([argv for argv in self.calls if "set-option" in argv and OPEN_OPT in argv and SID in argv])
        self.assertEqual(self.command_files(), [self.launch_file()])
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.googlecode.iterm2", str(self.launch_file())]])

    def test_detached_open_without_a_recorded_terminal_uses_the_fallback(self):
        self.run_open(attached=0, terminal="")
        (path,) = self.command_files()
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.apple.Terminal", str(path)]])

    def test_the_command_file_execs_emx_with_only_allowlisted_env_and_no_session_text(self):
        with mock.patch.dict(os.environ, {"SECRET_TOKEN": "s3cret", "EMX_SAY": "/x/say"}):
            self.run_open(attached=0)
        (path,) = self.command_files()
        self.assertEqual(path, self.launch_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertEqual(lines[-1], f"exec {shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(cli.BIN))}")
        for line in lines[1:-1]:
            self.assertTrue(line.startswith("export "), line)
            self.assertIn(line.split("=", 1)[0][len("export "):], cli.OPEN_ENV)
        self.assertNotIn(SID, text)
        self.assertNotIn("s3cret", text)
        self.assertIn("export EMX_SOCKET=emx-test-x", lines)

    def test_emx_command_goes_through_current(self):
        root = os.path.join(self.state, "install")
        make_versions(root, ["9.0.0"], "9.0.0")
        _, _, bin_path = emx._layout(os.path.join(root, "versions", "9.0.0"))
        with mock.patch.object(cli, "BIN", bin_path):
            self.run_open(attached=0)
        last = self.launch_file().read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(last, f"exec {shlex.quote(sys.executable)} {shlex.quote(os.path.join(root, 'current', 'bin', 'emx'))}")
        self.assertNotIn("/versions/", last)

    def test_every_click_rewrites_the_one_launch_file_in_place(self):
        self.run_open(attached=0, terminal="com.googlecode.iterm2")
        first = self.launch_file().read_text(encoding="utf-8")
        with mock.patch.dict(os.environ, {"EMX_CLAUDE": "/x/other-claude"}):
            self.run_open(attached=0, terminal="com.googlecode.iterm2")
        self.run_open(server=False)
        self.assertEqual(self.command_files(), [self.launch_file()])
        self.assertEqual(self.launch_file().stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(p.name for p in self.launch_file().parent.iterdir()), ["emx.command", "pending"])
        self.assertEqual(self.launch_file().read_text(encoding="utf-8"), first)
        self.assertEqual({tuple(argv) for argv in self.opened()},
                         {("/x/fake-open", "-b", runner, str(self.launch_file())) for runner in ("com.googlecode.iterm2", "com.apple.Terminal")})

    def test_launch_files_of_earlier_builds_are_removed(self):
        folder = Path(self.state, "emx", "open")
        folder.mkdir(parents=True)
        for name in ("emx-1.command", "emx-2.command"):
            (folder / name).write_text("#!/bin/sh\n", encoding="utf-8")
        self.run_open(attached=0)
        self.assertEqual(self.command_files(), [self.launch_file()])

    def test_a_terminal_that_is_not_a_known_runner_gets_its_new_window_in_terminal_app(self):
        code, _, _ = self.run_open(attached=0, terminal="com.microsoft.VSCode")
        self.assertEqual(code, 0)
        (path,) = self.command_files()
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.apple.Terminal", str(path)]])

    def test_an_attached_open_prints_any_recorded_terminal_and_opens_nothing(self):
        code, out, _ = self.run_open(attached=1, terminal="com.microsoft.VSCode")
        self.assertEqual((code, out), (0, "com.microsoft.VSCode\n"))
        self.assertEqual(self.opened(), [])

    def test_an_existing_open_folder_is_tightened_to_0700(self):
        folder = Path(self.state, "emx", "open")
        folder.mkdir(parents=True)
        os.chmod(folder, 0o755)
        self.run_open(attached=0)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_with_no_server_the_session_waits_in_the_pending_file_and_create_workspace_takes_it(self):
        code, out, err = self.run_open(server=False)
        self.assertEqual((code, out, err), (0, "", ""))
        text = self.launch_file().read_text(encoding="utf-8")
        self.assertNotIn(SID, text)
        self.assertNotIn("EMX_OPEN_SESSION", text)
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.apple.Terminal", str(self.launch_file())]])
        self.assertFalse([argv for argv in self.calls if "set-option" in argv])
        self.assertEqual(self.pending_file().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pending_file().read_text(encoding="utf-8"), SID + "\n")
        self.assertEqual(self.create_workspace().options.get(OPEN_OPT), SID)
        self.assertFalse(self.pending_file().exists())
        self.assertNotIn(OPEN_OPT, self.create_workspace().options)

    def test_create_workspace_ignores_and_removes_a_pending_file_that_is_not_a_session_id(self):
        for bad in ("", "garbage", "-x", SID + "; rm", "../" + SID):
            with self.subTest(value=bad):
                self.pending_file().parent.mkdir(parents=True, exist_ok=True)
                self.pending_file().write_text(bad, encoding="utf-8")
                self.assertNotIn(OPEN_OPT, self.create_workspace().options)
                self.assertFalse(self.pending_file().exists())

    def test_create_workspace_ignores_and_removes_a_stale_pending_file_and_honours_a_fresh_one(self):
        for age, honoured in ((cli.PENDING_MAX_AGE + 1, False), (3 * 86400, False), (cli.PENDING_MAX_AGE - 10, True)):
            with self.subTest(age=age):
                self.pending_file().parent.mkdir(parents=True, exist_ok=True)
                self.pending_file().write_text(SID + "\n", encoding="utf-8")
                then = time.time() - age
                os.utime(self.pending_file(), (then, then))
                self.assertEqual(self.create_workspace().options.get(OPEN_OPT), SID if honoured else None)
                self.assertFalse(self.pending_file().exists())

    def test_create_workspace_without_a_pending_file_requests_nothing(self):
        self.assertNotIn(OPEN_OPT, self.create_workspace().options)

    def test_create_workspace_no_longer_reads_emx_open_session(self):
        with mock.patch.dict(os.environ, {"EMX_OPEN_SESSION": SID}):
            self.assertNotIn(OPEN_OPT, self.create_workspace().options)

    def test_a_failed_window_leaves_no_pending_session(self):
        def record(argv, **kwargs):
            self.calls.append(argv)
            if argv[0] != "tmux":
                return subprocess.CompletedProcess(argv, 1, "", "no such app")
            return subprocess.CompletedProcess(argv, 1, "", "no server running")

        with mock.patch("emx.tmux.subprocess.run", record), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["open", SID]), 1)
        self.assertFalse(self.pending_file().exists())

    def test_a_failed_open_says_so(self):
        def record(argv, **kwargs):
            self.calls.append(argv)
            if argv[0] != "tmux":
                return subprocess.CompletedProcess(argv, 1, "", "no such app")
            return subprocess.CompletedProcess(argv, 1, "", "no server running")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("emx.tmux.subprocess.run", record), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["open", SID])
        self.assertEqual((code, err.getvalue()), (1, "emx: couldn't open a new window.\n"))

    def test_open_refuses_a_non_session_id_and_writes_nothing(self):
        for bad in ("", "garbage", "-x", "/tmp/x", SID + "x"):
            with self.subTest(sid=bad):
                self.calls.clear()
                code, out, err = self.run_open(sid=bad)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err)
                self.assertEqual(self.calls, [])

    def test_open_refuses_a_default_socket(self):
        with mock.patch.dict(os.environ, {"EMX_SOCKET": "default"}):
            code, _, err = self.run_open()
        self.assertEqual((code, err), (2, cli.SOCKET_REFUSAL + "\n"))
        self.assertEqual(self.calls, [])

    def test_open_never_starts_a_server_and_uses_no_config(self):
        self.run_open()
        self.run_open(attached=0)
        code, _, err = self.run_open(server=False)
        self.assertEqual((code, err), (0, ""))
        for argv in self.calls:
            if argv[0] == "tmux":
                self.assertEqual(argv[:3], ["tmux", "-L", "emx-test-x"])
                self.assertNotIn("-f", argv)
                self.assertNotIn("new-session", argv)

    def test_the_usage_names_open_as_internal(self):
        self.assertIn("emx open <session id> is internal", cli.USAGE)


class FollowCommand(unittest.TestCase):
    def test_a_default_socket_is_refused_and_nothing_runs(self):
        from emx import remote

        err = io.StringIO()
        with mock.patch.dict(os.environ, {"EMX_SOCKET": "default"}), mock.patch.object(remote, "follow") as follow, \
                redirect_stderr(err):
            code = cli.main(["follow"])
        self.assertEqual((code, err.getvalue()), (2, cli.SOCKET_REFUSAL + "\n"))
        follow.assert_not_called()

    def test_it_follows_the_state_folders_relay_file_without_a_config(self):
        from emx import remote

        with mock.patch.dict(os.environ, {"EMX_SOCKET": "emx-test-x", "XDG_STATE_HOME": "/x/state"}), \
                mock.patch.object(remote, "follow", return_value=0) as follow:
            code = cli.main(["follow"])
        self.assertEqual(code, 0)
        path, out, tmux = follow.call_args.args
        self.assertEqual(str(path), "/x/state/emx/relay.jsonl")
        self.assertEqual((tmux.socket, tmux.config), ("emx-test-x", None))

    def follow_host(self, value):
        from emx import remote

        env = {"EMX_SOCKET": "emx-test-x", "XDG_STATE_HOME": "/x/state", "EMX_RELAY_HOST": value}
        with mock.patch.dict(os.environ, env), mock.patch.object(remote, "follow", return_value=0) as follow:
            code = cli.main(["follow"])
        follow.assert_called_once()
        return code, follow.call_args.kwargs.get("host")

    def test_the_host_the_laptop_sent_is_handed_on(self):
        self.assertEqual(self.follow_host("emx-test-host"), (0, "emx-test-host"))

    def test_a_host_that_could_be_an_option_is_dropped_and_it_still_follows(self):
        self.assertEqual(self.follow_host("-oX"), (0, None))

    def test_the_usage_names_it_as_internal(self):
        self.assertIn("emx follow is internal", cli.USAGE)

    def test_the_paste_flag_in_the_environment_is_handed_on(self):
        from emx import remote

        for flag, expected in (("1", True), ("", False)):
            env = {"EMX_SOCKET": "emx-test-x", "XDG_STATE_HOME": "/x/state", core.RELAY_PASTE_ENV: flag}
            with mock.patch.dict(os.environ, env), mock.patch.object(remote, "follow", return_value=0) as follow:
                cli.main(["follow"])
            self.assertEqual(follow.call_args.kwargs.get("paste"), expected)


PASTE_SID = "0b6f4f0c-6b0a-4c4e-9a53-2f0e3c1d7a11"
PASTE_ID = "20261018-120000-0a1b2c3d"
PNG = core.PNG_SIGNATURE + b"rest of a picture"


class Copied(unittest.TestCase):
    """`emx copied` runs on every copy tmux makes; only one made in a viewer, whose session's environment names its key, is handed on."""

    KEY = "6f1c9d52-3a7e-4c1b-9e0d-2b8a4f5c7d10"

    def run_copied(self, args, env, data=b"copied text"):
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock(buffer=io.BytesIO(data))
        with mock.patch.object(cli.Tmux, "hand_on_copy") as hand_on, mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            if "EMX_AGENT_KEY" not in env:
                os.environ.pop("EMX_AGENT_KEY", None)
            code = cli.main(["copied", *args])
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "", ""))
        return hand_on

    def test_a_copy_in_a_viewer_is_handed_on_with_its_key(self):
        hand_on = self.run_copied(["emx-test-unit"], {"EMX_AGENT_KEY": self.KEY})
        hand_on.assert_called_once_with(self.KEY, b"copied text")

    def test_anything_else_hands_on_nothing(self):
        for args, env, data in (
            (["emx-test-unit"], {}, b"x"),
            (["emx-test-unit"], {"EMX_AGENT_KEY": "s1"}, b"x"),
            (["default"], {"EMX_AGENT_KEY": self.KEY}, b"x"),
            ([], {"EMX_AGENT_KEY": self.KEY}, b"x"),
            (["emx-test-unit"], {"EMX_AGENT_KEY": self.KEY}, b""),
        ):
            with self.subTest(args=args, env=env, data=data):
                self.run_copied(args, env, data).assert_not_called()

    def test_a_failure_still_exits_0_silently(self):
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.Mock(buffer=io.BytesIO(b"x"))
        with mock.patch.object(cli.Tmux, "hand_on_copy", side_effect=TmuxError("gone")), mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch.dict(os.environ, {"EMX_AGENT_KEY": self.KEY}), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["copied", "emx-test-unit"]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


class PasteCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.tmux = Tmux(f"emx-test-{uuid.uuid4().hex[:8]}", os.devnull)
        self.addCleanup(self.kill)
        self.tmux.run("new-session", "-d", "-s", SESSION, "sleep 60")
        self.relay = self.dir / "relay.jsonl"

    def kill(self):
        try:
            self.tmux.run("kill-server")
        except TmuxError:
            pass

    def check(self, socket=None, path=None, pane="%0", sid=PASTE_SID):
        socket = self.tmux.socket if socket is None else socket
        return cli.main(["paste", socket, str(self.relay) if path is None else path, pane, sid])

    def test_a_fresh_stamp_writes_the_record_and_exits_0(self):
        self.tmux.set_global_option(RELAY_PASTE_OPT, str(time.time()))
        self.assertEqual(self.check(pane="%7"), 0)
        record = core.parse_relay(self.relay.read_text().splitlines()[-1])
        self.assertEqual((record["kind"], record["pane"], record["sid"]), ("paste", "%7", PASTE_SID))

    def test_a_stale_stamp_exits_1_and_writes_nothing(self):
        self.tmux.set_global_option(RELAY_PASTE_OPT, str(time.time() - 20))
        self.assertEqual(self.check(), 1)
        self.assertFalse(self.relay.exists())

    def test_no_stamp_exits_1(self):
        self.assertEqual(self.check(), 1)
        self.assertFalse(self.relay.exists())

    def test_bad_arguments_exit_1_and_write_nothing(self):
        self.tmux.set_global_option(RELAY_PASTE_OPT, str(time.time()))
        for kwargs in ({"socket": "default"}, {"socket": ""}, {"path": "relay.jsonl"}, {"pane": "12"}, {"sid": "x"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.check(**kwargs), 1)
        self.assertFalse(self.relay.exists())
        self.assertFalse((Path.cwd() / "relay.jsonl").exists())

    def test_a_tmux_that_is_not_running_exits_1(self):
        self.kill()
        self.assertEqual(self.check(), 1)
        self.assertFalse(self.relay.exists())

    def test_it_prints_nothing(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.check()
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


class PasteImage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name, "EMX_SOCKET": "emx-test-x"})
        env.start()
        self.addCleanup(env.stop)
        self.images = self.state / "emx" / "images"
        paste = mock.patch.object(Tmux, "paste_if", return_value=True)
        message = mock.patch.object(Tmux, "show_message")
        holds = mock.patch.object(Tmux, "holds_session", return_value=True)
        self.paste, self.message, self.holds = paste.start(), message.start(), holds.start()
        self.addCleanup(paste.stop)
        self.addCleanup(message.stop)
        self.addCleanup(holds.stop)

    def run_with(self, data, args=("%3", PASTE_SID, PASTE_ID)):
        stdin = mock.Mock(buffer=io.BytesIO(data))
        with mock.patch.object(sys, "stdin", stdin):
            return cli.main(["paste-image", *args])

    def test_a_png_is_saved_private_and_its_path_pasted(self):
        self.assertEqual(self.run_with(PNG), 0)
        target = self.images / f"{PASTE_ID}.png"
        self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.images.stat().st_mode & 0o777, 0o700)
        self.paste.assert_called_once_with("%3", PASTE_SID, "emx-paste-" + PASTE_ID, str(target))
        self.message.assert_not_called()

    def test_a_pane_that_does_not_hold_the_session_gets_no_file_and_no_message(self):
        self.holds.return_value = False
        self.assertEqual(self.run_with(PNG), 0)
        self.holds.assert_called_once_with("%3", PASTE_SID)
        self.assertEqual(list(self.images.glob("*")) if self.images.exists() else [], [])
        self.paste.assert_not_called()
        self.message.assert_not_called()
        self.assertEqual(self.run_with(b""), 0)
        self.message.assert_not_called()

    def test_a_stdin_that_cannot_be_read_is_one_line_not_a_traceback(self):
        class Broken:
            def read(self, n=-1):
                raise OSError("broken pipe")

        err = io.StringIO()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=Broken())), redirect_stderr(err):
            self.assertEqual(cli.main(["paste-image", "%3", PASTE_SID, PASTE_ID]), 1)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("couldn't paste the image", err.getvalue())

    def test_nothing_on_stdin_says_so_and_saves_nothing(self):
        self.assertEqual(self.run_with(b""), 0)
        self.message.assert_called_once_with("%3", "no image in clipboard")
        self.assertEqual(list(self.images.glob("*.png")), [])
        self.paste.assert_not_called()

    def test_something_that_is_not_a_png_says_so_too(self):
        self.assertEqual(self.run_with(b"GIF89a....."), 0)
        self.message.assert_called_once_with("%3", "no image in clipboard")
        self.assertEqual(list(self.images.glob("*.png")), [])

    def test_too_large_says_so_and_saves_nothing(self):
        with mock.patch.object(core, "PASTE_MAX_BYTES", 10):
            self.assertEqual(self.run_with(PNG), 0)
        self.message.assert_called_once_with("%3", "image too large to paste (over 32 MB)")
        self.assertEqual(list(self.images.glob("*.png")), [])
        self.paste.assert_not_called()

    def test_the_same_id_twice_pastes_once(self):
        self.run_with(PNG)
        self.assertEqual(self.run_with(PNG + b"other"), 0)
        self.paste.assert_called_once()
        self.assertEqual((self.images / f"{PASTE_ID}.png").read_bytes(), PNG)

    def test_an_empty_answer_after_the_image_says_nothing(self):
        self.run_with(PNG)
        self.assertEqual(self.run_with(b""), 0)
        self.message.assert_not_called()

    def test_images_older_than_7_days_are_deleted_and_newer_stay(self):
        self.images.mkdir(parents=True, mode=0o700)
        old, new = self.images / "20260101-000000-aaaaaaaa.png", self.images / "20261017-000000-bbbbbbbb.png"
        for path, days in ((old, 8), (new, 1)):
            path.write_bytes(PNG)
            stamp = time.time() - days * 86400
            os.utime(path, (stamp, stamp))
        self.run_with(PNG)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_a_folder_others_can_read_is_refused(self):
        self.images.mkdir(parents=True)
        os.chmod(self.images, 0o755)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_with(PNG), 1)
        self.assertEqual(list(self.images.glob("*.png")), [])
        self.paste.assert_not_called()

    def test_bad_arguments_exit_2(self):
        for args in (("12", PASTE_SID, PASTE_ID), ("%3", "x", PASTE_ID), ("%3", PASTE_SID, "../x")):
            with self.subTest(args=args):
                self.assertEqual(self.run_with(PNG, args), 2)
        self.assertFalse(self.images.exists())

    def test_a_tmux_error_exits_1(self):
        self.paste.side_effect = TmuxError("gone")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_with(PNG), 1)


class OpenRemote(unittest.TestCase):
    """A click on a banner for a server session: ssh to the server, never a local tmux."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = os.path.join(tmp.name, "ssh.log")
        fake = str(Path(__file__).resolve().parent / "fake_ssh.py")
        env = {"EMX_REMOTE_HOST": "srv", "EMX_SSH": fake, "EMX_REMOTE_TERMINAL": "com.googlecode.iterm2",
               "EMX_REMOTE_COMMAND": "true", "FAKE_SSH_LOG": self.log, "EMX_SOCKET": "emx-test-x"}
        for patch in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(cli, "Tmux", side_effect=AssertionError("a local tmux was touched")),
            mock.patch.object(cli, "_run_open", side_effect=AssertionError("a local window was opened")),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_open(self, sid=SID):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.open_session(sid)
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh]

    def test_it_prints_the_laptops_terminal_and_asks_the_server_in_batch_mode(self):
        code, out, _ = self.run_open()
        self.assertEqual((code, out), (0, "com.googlecode.iterm2\n"))
        (call,) = self.calls()
        self.assertIn("BatchMode=yes", call)
        self.assertTrue(call[-1].endswith(f"open --no-window {SID}'"))

    def test_a_non_id_is_refused_and_nothing_runs(self):
        code, out, err = self.run_open("garbage")
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err)
        self.assertEqual(self.calls(), [])

    def test_a_terminal_that_is_not_a_bundle_id_prints_nothing(self):
        with mock.patch.dict(os.environ, {"EMX_REMOTE_TERMINAL": "not a bundle; rm"}):
            code, out, _ = self.run_open()
        self.assertEqual((code, out), (0, ""))


class OpenNoWindowTmux:
    instances = []

    def __init__(self, socket, config=None, view=SESSION):
        self.socket = socket
        self.view = view
        self.options = {}
        self.exists = True
        OpenNoWindowTmux.instances.append(self)

    def views(self):
        return [View(SESSION, 0, "", None, 0, "", False, "%2")]

    def set_view_option(self, name, value):
        self.options[name] = value

    def has_session(self):
        return self.exists

    def set_session_option(self, name, value):
        self.options[name] = value

    def client_state(self):
        return (0, "", "", None)

    def session_option(self, name):
        return ""


class OpenNoWindow(unittest.TestCase):
    def setUp(self):
        OpenNoWindowTmux.instances = []
        for patch in (
            mock.patch.dict(os.environ, {"EMX_SOCKET": "emx-test-x"}),
            mock.patch.object(cli, "Tmux", OpenNoWindowTmux),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(cli, "_run_open", side_effect=AssertionError("a window was opened")),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("EMX_REMOTE_HOST", None)

    def run_main(self, exists=True):
        original = OpenNoWindowTmux.has_session
        OpenNoWindowTmux.has_session = lambda self: exists
        self.addCleanup(setattr, OpenNoWindowTmux, "has_session", original)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["open", "--no-window", SID])
        return code, out.getvalue(), err.getvalue()

    def test_it_sets_the_open_option_prints_nothing_and_opens_no_window(self):
        code, out, _ = self.run_main()
        self.assertEqual((code, out), (0, ""))
        written = [(t.view, t.options) for t in OpenNoWindowTmux.instances if t.options]
        self.assertEqual(written, [(SESSION, {OPEN_OPT: SID})])

    def test_with_no_session_it_says_so_and_opens_no_window(self):
        code, out, err = self.run_main(exists=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("isn't running", err)


class RemoteCommand(unittest.TestCase):
    def test_no_host_is_a_usage_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["remote"])
        self.assertEqual(code, 2)
        self.assertIn("remote <host>", err.getvalue())

    def test_the_usage_names_the_command_and_its_setting(self):
        self.assertIn("remote <host>", cli.USAGE)
        self.assertIn("EMX_REMOTE_COMMAND", cli.USAGE)


class RecordingWorkspace:
    """What `create_workspace` asks of tmux, recorded: no server is started."""

    def __init__(self, socket):
        self.socket = socket
        self.options = {}

    def run(self, *args):
        return "%1\n"

    def set_pane_option(self, pane, name, value):
        pass

    def toggle_zoom(self, pane):
        pass

    def set_session_option(self, name, value):
        self.options[name] = value


class RecordsTerminal(unittest.TestCase):
    def launch(self, **env):
        made = []

        def make(*args, **kwargs):
            made.append(FakeTmux(*args, **kwargs))
            return made[-1]

        clean = {k: v for k, v in os.environ.items() if k not in ("__CFBundleIdentifier", "TERM_PROGRAM")}
        with mock.patch.object(cli, "Tmux", make), \
                mock.patch.object(cli, "refresh_workspace", lambda tmux: None), \
                mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict(cli.os.environ, {**clean, "EMX_SOCKET": "emx-test-unit", **env}, clear=True), \
                mock.patch.object(cli.os, "execvpe", side_effect=Attached):
            with self.assertRaises(Attached):
                cli.launch()
        return made[0].options

    def test_launch_records_the_terminal_bundle(self):
        self.assertEqual(self.launch(__CFBundleIdentifier="com.googlecode.iterm2")[TERMINAL_OPT], "com.googlecode.iterm2")
        self.assertEqual(self.launch(TERM_PROGRAM="Apple_Terminal")[TERMINAL_OPT], "com.apple.Terminal")

    def test_launch_skips_it_when_unknown(self):
        self.assertNotIn(TERMINAL_OPT, self.launch(TERM_PROGRAM="tmux"))

    def test_a_failed_record_still_attaches(self):
        class Refusing(FakeTmux):
            def set_session_option(self, name, value):
                raise TmuxError("gone")

        with mock.patch.object(cli, "Tmux", Refusing), \
                mock.patch.object(cli, "refresh_workspace", lambda tmux: None), \
                mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict(cli.os.environ, {"EMX_SOCKET": "emx-test-unit", "__CFBundleIdentifier": "com.example.term"}), \
                mock.patch.object(cli.os, "execvpe", side_effect=Attached):
            with self.assertRaises(Attached):
                cli.launch()


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class CreateWorkspace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.dir, "XDG_CONFIG_HOME": os.path.join(self.dir, "config")})
        env.start()
        self.addCleanup(env.stop)
        # A harmless command, so no real panel starts, polls claude agents or speaks.
        quiet = mock.patch.object(cli, "self_command", lambda *args: "exec sleep 60")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.tmux = Tmux(f"emx-test-{uuid.uuid4().hex[:8]}", os.devnull)
        self.addCleanup(self.kill_server)

    def kill_server(self):
        try:
            self.tmux.run("kill-server")
        except TmuxError:
            pass

    def show(self, fmt):
        return self.tmux.run("display-message", "-p", "-t", f"={SESSION}:", fmt).strip()

    def test_starts_hidden_when_left_hidden(self):
        core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "panel_collapsed": True})
        cli.create_workspace(self.tmux)
        self.assertEqual(self.show("#{window_zoomed_flag}"), "1")
        self.assertEqual(self.show("#{@emx_role}"), "placeholder")

    def test_starts_shown_by_default(self):
        cli.create_workspace(self.tmux)
        self.assertEqual(self.show("#{window_zoomed_flag}"), "0")


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


class InstalledBin(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, _ = temp_homes(self)

    def test_a_versions_tree_gives_bin_through_current(self):
        make_versions(self.root, ["9.0.0"], "9.0.0")
        got = emx._layout(os.path.join(self.root, "versions", "9.0.0"))
        self.assertEqual(got, (self.root, "9.0.0", os.path.join(self.root, "current", "bin", "emx")))

    def test_running_is_the_folder_name_not___version__(self):
        make_versions(self.root, ["9.0.0"], "9.0.0")
        tree = os.path.join(self.root, "versions", "9.0.0")
        with open(emx.__file__, encoding="utf-8") as fh:
            source = fh.read().replace(f'__version__ = "{__version__}"', '__version__ = "2.3.0"')
        os.makedirs(os.path.join(tree, "emx"))
        with open(os.path.join(tree, "emx", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write(source)
        done = subprocess.run(
            [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import emx; "
             "print(emx.__version__, emx.RUNNING, emx.ROOT, emx.BIN)", tree],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(done.stdout.split(), ["2.3.0", "9.0.0", self.root, os.path.join(self.root, "current", "bin", "emx")])

    def test_a_checkout_keeps_its_own_bin(self):
        code = os.path.join(self.tmp, "checkout")
        self.assertEqual(emx._layout(code), (None, None, os.path.join(code, "bin", "emx")))
        self.assertIsNone(emx.ROOT)
        self.assertEqual(emx.BIN, os.path.join(emx.CODE, "bin", "emx"))

    def test_a_versions_tree_without_current_or_source_is_dev(self):
        code = os.path.join(self.root, "versions", "9.0.0")
        dev = (None, None, os.path.join(code, "bin", "emx"))
        os.makedirs(code)
        self.assertEqual(emx._layout(code), dev)
        os.symlink(os.path.join("versions", "9.0.0"), os.path.join(self.root, "current"))
        self.assertEqual(emx._layout(code), dev)
        os.unlink(os.path.join(self.root, "current"))
        os.makedirs(os.path.join(self.root, "source.git"))
        self.assertEqual(emx._layout(code), dev)
        os.makedirs(os.path.join(self.root, "current"))
        self.assertEqual(emx._layout(code), dev)

    def test_the_code_stamp_of_a_checkout_is_unchanged(self):
        root = os.path.dirname(cli.HERE)
        files = []
        for path in [emx.BIN] + [os.path.join(d, f) for d, _, names in os.walk(cli.HERE) for f in names]:
            rel = os.path.relpath(path, root)
            if core.counts_for_version(rel):
                with open(path, "rb") as fh:
                    files.append((rel, fh.read()))
        self.assertEqual(cli.code_version(), core.code_version(files))


class RelaunchTmux:
    def __init__(self, socket, config=None):
        self.socket = socket
        self.options = {VERSION_OPT: "old"}
        self.respawned, self.sourced = [], 0

    def has_session(self):
        return True

    def session_option(self, name):
        return self.options.get(name, "")

    def set_session_option(self, name, value):
        self.options[name] = value

    def panes(self, session=SESSION):
        return [
            Pane("%1", "@1", False, "sidebar", "", 30, 200),
            Pane("%2", "@1", False, "sidebar", SID, 170, 200),
            Pane("%3", "@2", False, "", SID, 200, 200),
        ]

    def agent_keys(self):
        return set()

    def view_names(self):
        return [SESSION]

    def kill_pane(self, pane):
        raise AssertionError(f"killed {pane}")

    def respawn(self, pane, command):
        self.respawned.append((pane, command))

    def source_config(self):
        self.sourced += 1


class UpdateCommands(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.bin = temp_homes(self)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def installed(self, versions=("2.3.0", "2.3.1")):
        self.source = make_source(self.tmp, versions)
        version, error = cli.install.install(self.source, dict(os.environ), io.StringIO())
        self.assertIsNone(error)
        for name, value in (("ROOT", self.root), ("RUNNING", version)):
            patch = mock.patch.object(emx, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def report(self):
        code, out, err = self.run_main("update", "--report")
        self.assertEqual((code, err, len(out.splitlines())), (0, "", 1))
        return core.parse_update_report(out)

    def test_update_from_a_checkout_says_so_and_writes_nothing(self):
        self.assertIsNone(emx.ROOT)
        for args in ([], ["off"], ["on"]):
            with self.subTest(args=args):
                code, out, err = self.run_main("update", *args)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("runs from a checkout", err)
                self.assertIn("never updates itself", err)
        self.assertFalse(os.path.exists(self.root))
        self.assertFalse(os.path.exists(self.bin))

    def test_update_off_then_on_writes_and_removes_the_marker(self):
        self.installed()
        marker = os.path.join(self.root, "updates-off")
        code, out, _ = self.run_main("update", "off")
        self.assertEqual((code, out), (0, "Automatic updates are off; emx update still updates when you run it.\n"))
        self.assertTrue(os.path.exists(marker))
        code, out, _ = self.run_main("update", "on")
        self.assertEqual((code, out), (0, "Automatic updates are on.\n"))
        self.assertFalse(os.path.exists(marker))

    def test_update_says_up_to_date_or_updated(self):
        self.installed()
        self.assertEqual(self.run_main("update"), (0, "emx is up to date (2.3.1).\n", ""))
        add_version(self.source, "3.0.0")
        code, out, _ = self.run_main("update")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("emx updated to 3.0.0. The running panel switches"), out)
        self.assertEqual(cli.install.current(self.root), "3.0.0")

    def test_update_keeps_one_previous_version_on_disk(self):
        self.installed(("2.3.0",))
        add_version(self.source, "2.3.1")
        self.assertEqual(self.run_main("update")[0], 0)
        # Each `emx update` is a new process running through `current`.
        running = mock.patch.object(emx, "RUNNING", "2.3.1")
        running.start()
        self.addCleanup(running.stop)
        add_version(self.source, "2.3.2")
        self.assertEqual(self.run_main("update")[0], 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "versions"))), ["2.3.0", "2.3.1", "2.3.2"])
        self.assertEqual(cli.install.current(self.root), "2.3.2")

    def test_update_keeps_the_folder_a_lagging_panel_may_still_run_from(self):
        self.installed(("2.3.0",))
        for version in ("2.3.1", "2.3.2"):
            add_version(self.source, version)
            self.assertEqual(self.run_main("update")[0], 0)
            running = mock.patch.object(emx, "RUNNING", version)
            running.start()
            self.addCleanup(running.stop)
        add_version(self.source, "2.3.3")
        self.assertEqual(self.run_main("update")[0], 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "versions"))), ["2.3.1", "2.3.2", "2.3.3"])

    def test_update_report_prints_one_parseable_line(self):
        self.assertEqual(self.report()["status"], "failed")
        self.installed()
        got = self.report()
        self.assertEqual((got["status"], got["before"], got["after"]), ("current", "2.3.1", "2.3.1"))
        add_version(self.source, "2.3.2")
        got = self.report()
        self.assertEqual((got["status"], got["before"], got["after"]), ("updated", "2.3.1", "2.3.2"))
        cli.install.set_updates(self.root, False)
        add_version(self.source, "2.3.3")
        got = self.report()
        self.assertEqual((got["status"], got["after"]), ("off", "2.3.2"))
        cli.install.set_updates(self.root, True)
        cli.install.git(self.root, "remote", "set-url", "origin", os.path.join(self.tmp, "missing"))
        got = self.report()
        self.assertEqual((got["status"], got["before"]), ("failed", "2.3.2"))
        self.assertIn("couldn't fetch", got["reason"])

    def test_update_report_carries_the_cause_of_an_ssh_failure(self):
        self.installed()
        stderr = (
            "ssh: Could not resolve hostname no-such-host.invalid: nodename nor servname provided, or not known\n"
            "fatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\nand the repository exists.\n"
        )
        done = subprocess.CompletedProcess([], 128, stdout="", stderr=stderr)
        with mock.patch.object(cli.install.subprocess, "run", return_value=done):
            got = self.report()
        self.assertEqual(got["status"], "failed")
        self.assertIn("couldn't fetch updates", got["reason"])
        self.assertIn("Could not resolve hostname", got["reason"])
        self.assertNotIn("and the repository exists", got["reason"])

    def test_update_report_never_applies_a_major(self):
        self.installed()
        add_version(self.source, "3.0.0")
        got = self.report()
        self.assertEqual((got["status"], got["after"], got["major"]), ("current", "2.3.1", "3.0.0"))
        self.assertEqual(cli.install.current(self.root), "2.3.1")

    def test_uninstall_from_a_checkout_refuses_and_installed_removes_the_install(self):
        code, _, err = self.run_main("uninstall")
        self.assertEqual(code, 1)
        self.assertIn("checkout", err)
        self.installed()
        code, out, _ = self.run_main("uninstall")
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("Your prefs and emx's state are kept.\n"))
        self.assertFalse(os.path.exists(self.root))
        self.assertFalse(os.path.lexists(os.path.join(self.bin, "emx")))

    def test_relaunch_refuses_default_and_empty_socket(self):
        with mock.patch.object(cli, "Tmux", side_effect=AssertionError("tmux")):
            for socket in ("", "default"):
                with self.subTest(socket=socket):
                    code, out, err = self.run_main("relaunch", socket)
                    self.assertEqual((code, out, err), (2, "", cli.SOCKET_REFUSAL + "\n"))

    def test_relaunch_respawns_the_sidebar_only_and_stamps(self):
        made = []

        def make(*args, **kwargs):
            made.append(RelaunchTmux(*args, **kwargs))
            return made[-1]

        with mock.patch.object(cli, "Tmux", make):
            self.assertEqual(self.run_main("relaunch", "emx-test-unit"), (0, "", ""))
        (tmux,) = made
        self.assertEqual([pane for pane, _ in tmux.respawned], ["%1"])
        self.assertEqual(tmux.respawned[0][1], cli.self_command("sidebar", "emx-test-unit"))
        self.assertEqual(tmux.sourced, 1)
        self.assertEqual(tmux.options[VERSION_OPT], cli.code_version())

    def test_relaunch_without_a_workspace_exits_1(self):
        class Gone(RelaunchTmux):
            def has_session(self):
                return False

        with mock.patch.object(cli, "Tmux", Gone):
            self.assertEqual(self.run_main("relaunch", "emx-test-unit"), (1, "", ""))

    def test_install_sh_runs_from_a_clone_into_temp_root(self):
        here = os.path.dirname(cli.HERE)
        fixture = os.path.join(self.tmp, "fixture")
        os.makedirs(fixture)
        shutil.copy2(os.path.join(here, "install.sh"), fixture)
        shutil.copy2(os.path.join(here, "CHANGELOG.md"), fixture)
        for name in ("bin", "emx", "user-docs"):
            shutil.copytree(os.path.join(here, name), os.path.join(fixture, name), ignore=shutil.ignore_patterns("__pycache__"))
        run_git(fixture, "init", "-q")
        run_git(fixture, "add", "-A")
        run_git(fixture, "commit", "-q", "-m", "fixture")
        run_git(fixture, "tag", "v9.0.0")
        clone = os.path.join(self.tmp, "clone")
        run_git(self.tmp, "clone", "-q", fixture, clone)
        path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.normpath(p) != self.bin)
        env = {**os.environ, **GIT_ENV, "EMX_HOME": self.root, "EMX_BIN_HOME": self.bin, "PATH": path}
        done = subprocess.run(["sh", os.path.join(clone, "install.sh")], capture_output=True, text=True, timeout=120, env=env,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f'export PATH="{self.bin}:$PATH"', done.stdout)
        self.assertEqual(os.readlink(os.path.join(self.root, "current")), os.path.join("versions", "9.0.0"))
        emx_link = os.path.join(self.bin, "emx")
        done = subprocess.run([emx_link, "--version"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(done.stdout, f"emx {__version__}\n")
        done = subprocess.run([emx_link, "docs"], capture_output=True, text=True, timeout=30, env=env)
        with open(os.path.join(here, "user-docs", "index.md"), encoding="utf-8") as fh:
            self.assertEqual((done.returncode, done.stdout), (0, fh.read()))
        current = os.path.join(self.root, "current")
        done = subprocess.run(
            [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import emx; print(emx.BIN)", current],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(done.stdout.strip(), os.path.join(self.root, "current", "bin", "emx"))

    def test_usage_names_update_uninstall_and_the_internal_commands(self):
        for words in ("update [off | on]", "uninstall", "emx install <url> is internal", "emx update --report is internal",
                      "emx relaunch <socket> is internal", "docs [<page>]", "docs <page> prints one page", "EMX_HOME", "EMX_BIN_HOME", "EMX_UPDATE_SECONDS"):
            with self.subTest(words=words):
                self.assertIn(words, cli.USAGE)


def docs_text(page):
    with open(os.path.join(cli.DOCS, page + ".md"), encoding="utf-8") as fh:
        return fh.read()


class DocsCommand(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_docs_prints_the_index(self):
        self.assertEqual(self.run_main("docs"), (0, docs_text("index"), ""))

    def test_docs_prints_every_page(self):
        pages = cli.docs_pages()
        self.assertIn("index", pages)
        self.assertIn("reference", pages)
        for page in pages:
            with self.subTest(page=page):
                self.assertEqual(self.run_main("docs", page), (0, docs_text(page), ""))

    def test_docs_prints_without_a_traceback_when_stdout_cannot_encode_it(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["docs", "sessions"])
        out.flush()
        self.assertEqual(code, 0)
        self.assertEqual(raw.getvalue().decode("ascii").count("\n"), docs_text("sessions").count("\n"))

    def test_docs_accepts_the_md_suffix(self):
        self.assertEqual(self.run_main("docs", "reference.md"), self.run_main("docs", "reference"))

    def test_docs_unknown_page_exits_2(self):
        code, out, err = self.run_main("docs", "nosuch")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("nosuch", err)
        self.assertIn("reference", err)

    def test_docs_does_not_leave_the_docs_folder(self):
        for name in ("../CHANGELOG", "../README", "/etc/hosts", "../emx/cli.py"):
            with self.subTest(name=name):
                code, out, _ = self.run_main("docs", name)
                self.assertEqual((code, out), (2, ""))

    def test_docs_too_many_args_exits_2(self):
        code, out, _ = self.run_main("docs", "a", "b")
        self.assertEqual((code, out), (2, ""))

    def test_the_real_launcher_prints_docs_with_no_tmux_on_path(self):
        for args, page in (([], "index"), (["reference"], "reference")):
            with self.subTest(args=args):
                done = subprocess.run(
                    [sys.executable, cli.BIN, "docs", *args], capture_output=True, text=True, timeout=30,
                    env={**os.environ, "PATH": "/nonexistent"}, stdin=subprocess.DEVNULL,
                )
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, docs_text(page), ""))

    def test_a_closed_pipe_exits_quietly(self):
        done = subprocess.run(
            f"{sys.executable} {cli.BIN} docs reference | head -c 10 >/dev/null", shell=True, capture_output=True, text=True,
            timeout=30, env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(done.stderr, "")


class HookCommand(unittest.TestCase):
    KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.folder = os.path.join(self.root, "sessions", self.KEY)
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "session.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")

    def run_hook(self, stdin, key=None, root=None, env=None):
        done = subprocess.run(
            [sys.executable, "-I", cli.BIN, "hook", root or self.root, self.KEY if key is None else key], input=stdin,
            capture_output=True, text=True, timeout=30, env=env,
        )
        return done

    def written(self):
        found = []
        for base, _, names in os.walk(self.root):
            found += [os.path.relpath(os.path.join(base, n), self.root) for n in names]
        return sorted(found)

    def payload(self, **over):
        base = {"hook_event_name": "Stop", "session_id": self.KEY, "transcript_path": "/c/p/x.jsonl", "cwd": "/w"}
        base.update(over)
        return json.dumps(base)

    def test_a_valid_payload_appends_one_line_and_prints_nothing(self):
        for env in (
            {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"},
            {**os.environ, "XDG_STATE_HOME": os.path.join(self.root, "elsewhere")},
        ):
            done = self.run_hook(self.payload(), env=env)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        with open(os.path.join(self.folder, "events.jsonl"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual((record["event"], record["session_id"]), ("Stop", self.KEY))
        self.assertIsInstance(record["at"], int)
        self.assertFalse(os.path.exists(os.path.join(self.root, "elsewhere")))

    def test_a_huge_payload_is_still_recorded(self):
        big = "x" * (1024 * 1024 + 1)
        done = self.run_hook(self.payload(hook_event_name="PostToolUse", tool_name="Read", tool_response={"content": big}))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        with open(os.path.join(self.folder, "events.jsonl"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual([json.loads(line)["event"] for line in lines], ["PostToolUse"])

    def test_bad_json_appends_nothing_and_prints_nothing(self):
        for text in ("", "not json", "[1]", json.dumps({"session_id": self.KEY})):
            done = self.run_hook(text)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), text)
        self.assertEqual(self.written(), [os.path.join("sessions", self.KEY, "session.json")])

    def test_a_key_that_is_not_a_session_id_writes_nothing_anywhere(self):
        before = self.written()
        for key in ("../x", "x", "", "..", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/../.."):
            done = self.run_hook(self.payload(), key=key)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), key)
        self.assertEqual(self.written(), before)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root), "x")))

    def test_a_key_with_no_record_writes_nothing(self):
        other = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        done = self.run_hook(self.payload(), key=other)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.root, "sessions", other)))

    def test_a_relative_state_folder_writes_nothing(self):
        done = self.run_hook(self.payload(), root="relative/dir")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_wrong_argument_counts_still_exit_0_quietly(self):
        for args in ([], ["only-one"], ["a", "b", "c"]):
            done = subprocess.run(
                [sys.executable, "-I", cli.BIN, "hook", *args], input="{}", capture_output=True, text=True, timeout=30
            )
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), args)

    def test_payload_fields_outside_the_allowlist_never_reach_the_file(self):
        secret = "hunter2-do-not-keep"
        done = self.run_hook(self.payload(tool_input={"command": f"echo {secret}"}, prompt=secret))
        self.assertEqual(done.returncode, 0)
        with open(os.path.join(self.folder, "events.jsonl"), encoding="utf-8") as fh:
            self.assertNotIn(secret, fh.read())

    def test_the_file_is_private(self):
        self.run_hook(self.payload())
        mode = os.stat(os.path.join(self.folder, "events.jsonl")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_usage_names_it_as_internal(self):
        self.assertIn("emx hook", cli.USAGE)
        self.assertIn("internal", cli.USAGE)


MIG_A, MIG_B = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class MigratingTmux:
    """A 2.9.0 workspace on this version's code (no upgrade due): session MIG_A's pane in the home view, MIG_B's hidden, the placeholder hidden, and `attached` clients on `emx`. Records what changes it."""

    made = []

    def __init__(self, socket, config=None, view=SESSION):
        self.socket = socket
        self.view = view
        self.options = {VERSION_OPT: cli.code_version()}
        self.ops = []
        self.attached = 0
        self.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 200),
            Pane("%5", "@1", False, "", MIG_A, 160, 200, agent="claude"),
            Pane("%2", "@2", False, "placeholder", "", 160, 160),
            Pane("%6", "@3", False, "", MIG_B, 160, 160, agent="claude"),
        ]
        MigratingTmux.made.append(self)

    def argv(self, *args):
        return ["tmux", "-L", self.socket, *args]

    def has_session(self):
        return True

    def session_option(self, name):
        return self.options.get(name, "")

    def set_session_option(self, name, value):
        self.ops.append(("set", name, value))
        self.options[name] = value

    def panes(self, session=SESSION):
        return list(self.list) if session == SESSION else []

    def agent_keys(self):
        return {key for op, *rest in self.ops if op == "link" for key in rest[1:]}

    def view_names(self):
        return [SESSION]

    def views(self):
        return [View(SESSION, self.attached, "", None, 200, "", False, "%2")]

    def window_of(self, pane):
        return next(p.window for p in self.list if p.id == pane)

    def swap(self, a, b):
        self.ops.append(("swap", a, b))
        where = {p.id: p.window for p in self.list}
        self.list = [replace(p, window=where[b] if p.id == a else where[a] if p.id == b else p.window) for p in self.list]

    def link_agent(self, window, key, inert):
        self.ops.append(("link", window, key))

    def kill_pane(self, pane):
        self.ops.append(("kill", pane))

    def respawn(self, pane, command):
        self.ops.append(("respawn", pane))

    def run(self, *args):
        self.ops.append(("run",) + args[:3])
        return "emx\n" if args[0] == "list-sessions" else "%9\n"

    def set_pane_option(self, pane, name, value):
        pass

    def source_config(self):
        self.ops.append(("source",))


class Migration(unittest.TestCase):
    """`emx` and the auto update's `emx relaunch` move a running 2.9.0 workspace onto views the same way."""

    def setUp(self):
        MigratingTmux.made = []
        for patch in (
            mock.patch.object(cli, "Tmux", MigratingTmux),
            mock.patch.object(cli.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.dict(cli.os.environ, {"EMX_SOCKET": "emx-test-unit"}),
            mock.patch.object(cli, "record_terminal", lambda tmux: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def launched(self, cols=200, attached=0):
        original = MigratingTmux.views
        MigratingTmux.views = lambda self: [View(SESSION, attached, "", None, 200, "", False, "%2")]
        self.addCleanup(setattr, MigratingTmux, "views", original)
        with mock.patch.object(cli.shutil, "get_terminal_size", return_value=os.terminal_size((cols, 22))), \
                mock.patch.object(cli.os, "execvpe", side_effect=Attached) as execvpe:
            with self.assertRaises(Attached):
                cli.launch()
        return MigratingTmux.made[0].ops, execvpe.call_args.args[1]

    def migration(self, ops):
        return [op for op in ops if op[0] in ("swap", "link", "kill", "set")]

    def test_relaunch_runs_the_same_migration_as_launch(self):
        ops, argv = self.launched()
        self.assertEqual(cli.relaunch("emx-test-unit"), 0)
        relaunched = MigratingTmux.made[-1].ops
        expected = [("swap", "%5", "%2"), ("set", "@emx_reopen", MIG_A), ("link", "@2", MIG_A), ("link", "@3", MIG_B)]
        self.assertEqual(self.migration(ops), expected)
        self.assertEqual(self.migration(relaunched), expected)
        self.assertEqual(argv[-2:], ["-t", "=emx:"])

    def test_a_second_run_finds_nothing_to_do(self):
        tmux = MigratingTmux("emx-test-unit")
        cli.refresh_workspace(tmux)
        first = len(tmux.ops)
        cli.refresh_workspace(tmux)
        self.assertEqual(self.migration(tmux.ops[first:]), [])

    def test_a_narrow_terminal_gets_a_new_view_of_its_own(self):
        ops, argv = self.launched(cols=48, attached=1)
        made = [op for op in ops if op[:3] == ("run", "new-session", "-d")]
        self.assertEqual(len(made), 1)
        self.assertEqual(argv[-2:], ["-t", "=emx-view-1:"])

    def test_a_second_wide_terminal_gets_a_view_of_its_own(self):
        _, argv = self.launched(cols=200, attached=1)
        self.assertEqual(argv[-2:], ["-t", "=emx-view-1:"])


if __name__ == "__main__":
    unittest.main()
