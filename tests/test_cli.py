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
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from emx import __version__, cli, core
from emx.tmux import OPEN_OPT, SESSION, TERMINAL_OPT, VERSION_OPT, Pane, Tmux, TmuxError


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
    """`emx left` answers tmux's if-shell with an exit code, 0 only for ← to reach Claude Code; anything wrong means ← goes to the panel, silently, since Claude Code forks the conversation on a ← at an empty prompt."""

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

    def test_an_empty_prompt_answers_1_and_typed_text_0(self):
        lines = ["❯\xa0", "─" * 30]
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "2", "5", "1", "30"], lines), 1)
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "2", "5", "1", "30"], ["❯ typed", "─" * 30]), 0)

    def test_an_empty_prompt_a_column_over_answers_1(self):
        self.assertEqual(self.run_check(["emx-test-unit", "%2", "3", "5", "1", "30"], [" ❯\xa0", "─" * 30]), 1)

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

    def test_open_writes_the_option_and_brings_the_terminal_forward(self):
        code, out, err = self.run_open()
        self.assertEqual((code, out, err), (0, "", ""))
        (setting,) = [argv for argv in self.calls if "set-option" in argv]
        self.assertEqual(setting, ["tmux", "-L", "emx-test-x", "set-option", "-t", "=emx:", OPEN_OPT, SID])
        self.assertEqual(self.calls[-1], ["/x/fake-open", "-b", "com.example.term"])
        self.assertLess(self.calls.index(setting), len(self.calls) - 1)

    def test_open_without_a_known_terminal_only_writes_the_option(self):
        code, _, _ = self.run_open(terminal="")
        self.assertEqual(code, 0)
        self.assertTrue([argv for argv in self.calls if OPEN_OPT in argv])
        self.assertFalse([argv for argv in self.calls if argv[0] != "tmux"])

    def opened(self):
        return [argv for argv in self.calls if argv[0] != "tmux"]

    def command_files(self):
        return sorted(Path(self.state, "emx", "open").glob("*.command"))

    def test_detached_open_writes_the_option_and_opens_a_new_window_in_the_recorded_terminal(self):
        code, out, err = self.run_open(attached=0, terminal="com.googlecode.iterm2")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue([argv for argv in self.calls if "set-option" in argv and OPEN_OPT in argv and SID in argv])
        (path,) = self.command_files()
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.googlecode.iterm2", str(path)]])
        self.assertTrue(str(path).endswith(".command"))

    def test_detached_open_without_a_recorded_terminal_uses_the_fallback(self):
        self.run_open(attached=0, terminal="")
        (path,) = self.command_files()
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.apple.Terminal", str(path)]])

    def test_the_command_file_execs_emx_with_only_allowlisted_env_and_no_session_text(self):
        with mock.patch.dict(os.environ, {"SECRET_TOKEN": "s3cret", "EMX_SAY": "/x/say", "EMX_OPEN_SESSION": SID}):
            self.run_open(attached=0)
        (path,) = self.command_files()
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
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

    def test_old_command_files_are_pruned(self):
        folder = Path(self.state, "emx", "open")
        folder.mkdir(parents=True)
        old, recent = folder / "emx-1.command", folder / "emx-2.command"
        for path in (old, recent):
            path.write_text("#!/bin/sh\n", encoding="utf-8")
        hour_ago = time.time() - cli.OPEN_FILES_KEPT_SECONDS - 5
        os.utime(old, (hour_ago, hour_ago))
        self.run_open(attached=0)
        names = [p.name for p in self.command_files()]
        self.assertNotIn("emx-1.command", names)
        self.assertIn("emx-2.command", names)
        self.assertEqual(len(names), 2)

    def test_a_terminal_that_is_not_a_known_runner_gets_its_new_window_in_terminal_app(self):
        code, _, _ = self.run_open(attached=0, terminal="com.microsoft.VSCode")
        self.assertEqual(code, 0)
        (path,) = self.command_files()
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.apple.Terminal", str(path)]])

    def test_an_attached_open_brings_any_recorded_terminal_forward(self):
        self.run_open(attached=1, terminal="com.microsoft.VSCode")
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.microsoft.VSCode"]])

    def test_an_existing_open_folder_is_tightened_to_0700(self):
        folder = Path(self.state, "emx", "open")
        folder.mkdir(parents=True)
        os.chmod(folder, 0o755)
        self.run_open(attached=0)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_a_command_file_that_vanishes_while_pruning_does_not_stop_the_window(self):
        folder = Path(self.state, "emx", "open")
        folder.mkdir(parents=True)
        old = folder / "emx-1.command"
        old.write_text("#!/bin/sh\n", encoding="utf-8")
        hour_ago = time.time() - cli.OPEN_FILES_KEPT_SECONDS - 5
        os.utime(old, (hour_ago, hour_ago))
        real_unlink = Path.unlink

        def racing(path, *args, **kwargs):
            real_unlink(path)
            raise FileNotFoundError(str(path))

        with mock.patch.object(Path, "unlink", racing):
            code, _, err = self.run_open(attached=0)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.opened()), 1)

    def test_with_no_server_the_window_carries_the_session_and_create_workspace_writes_the_option(self):
        code, out, err = self.run_open(server=False)
        self.assertEqual((code, out, err), (0, "", ""))
        (path,) = self.command_files()
        self.assertIn(f"export EMX_OPEN_SESSION={SID}", path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(self.opened(), [["/x/fake-open", "-b", "com.apple.Terminal", str(path)]])
        self.assertFalse([argv for argv in self.calls if "set-option" in argv])
        made = RecordingWorkspace("emx-test-x")
        with mock.patch.dict(os.environ, {"EMX_OPEN_SESSION": SID, "XDG_CONFIG_HOME": self.state}):
            os.environ.pop("EMX_CONFIG_HOME", None)
            cli.create_workspace(made)
        self.assertEqual(made.options.get(OPEN_OPT), SID)

    def test_create_workspace_ignores_a_non_session_id_in_emx_open_session(self):
        for bad in ("", "garbage", "-x", SID + "; rm"):
            with self.subTest(value=bad):
                made = RecordingWorkspace("emx-test-x")
                with mock.patch.dict(os.environ, {"EMX_OPEN_SESSION": bad, "XDG_CONFIG_HOME": self.state}):
                    os.environ.pop("EMX_CONFIG_HOME", None)
                    cli.create_workspace(made)
                self.assertNotIn(OPEN_OPT, made.options)

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


if __name__ == "__main__":
    unittest.main()
