import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import emx
from emx import core, remote
from emx.tmux import RELAY_HOST_OPT, RELAY_OPT, RELAY_PASTE_OPT


def setUpModule():
    tmp = os.path.realpath(tempfile.mkdtemp())
    unittest.addModuleCleanup(shutil.rmtree, tmp, True)
    patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(tmp, "config"), "XDG_STATE_HOME": os.path.join(tmp, "state")})
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    os.environ.pop("EMX_CONFIG_HOME", None)


SID = "0b6f4f0c-6b0a-4c4e-9a53-2f0e3c1d7a11"
FAKE_SSH = str(Path(__file__).resolve().parent / "fake_ssh.py")


def use_remote_command(path):
    """Writes `remote_command` into the module's throwaway config, the laptop side's, so no test reaches the emx on PATH."""
    files = core.prefs_files()
    core.ensure_config(files)
    core.change_prefs(files, lambda prefs: {**prefs, "remote_command": str(path)})


def notice(text="Done.", sid=SID):
    return core.relay_notice("t · p", text, text, sid, core.VOICE_MODE, 1.0)


class AppendRelay(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "state" / "relay.jsonl"

    def test_two_appends_make_two_parseable_lines(self):
        remote.append_relay(self.path, notice("One."))
        remote.append_relay(self.path, notice("Two."))
        lines = self.path.read_text().splitlines()
        self.assertEqual([json.loads(l)["text"] for l in lines], ["One.", "Two."])

    def test_the_file_is_private(self):
        remote.append_relay(self.path, notice())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_crossing_the_limit_rotates_once(self):
        with mock.patch.object(core, "RELAY_MAX_BYTES", 100):
            for text in ("First one here.", "Second one here.", "Third one here."):
                remote.append_relay(self.path, notice(text))
            old = (self.path.parent / "relay.jsonl.1").read_text()
            self.assertIn("Second one here.", old)
            self.assertEqual([json.loads(l)["text"] for l in self.path.read_text().splitlines()], ["Third one here."])

    def test_an_unwritable_folder_raises_nothing(self):
        blocker = self.tmp / "file"
        blocker.write_text("x")
        remote.append_relay(blocker / "relay.jsonl", notice())


class FakeTmux:
    def __init__(self, has=True):
        self.has = has
        self.set = []
        self.global_set = []

    def has_session(self):
        return self.has

    def set_session_option(self, name, value):
        self.set.append((name, value))

    def set_global_option(self, name, value):
        self.global_set.append((name, value))


class Out:
    """A file-like out whose lines a test can read."""

    def __init__(self):
        self.lines = []
        self.buffer = ""
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.buffer += text
            *done, self.buffer = self.buffer.split("\n")
            self.lines.extend(done)

    def flush(self):
        pass

    def records(self):
        with self.lock:
            return [json.loads(l) for l in self.lines if l]

    def notices(self):
        return [r for r in self.records() if r["kind"] != "beat"]


class Follow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "relay.jsonl"
        self.out = Out()
        self.tmux = FakeTmux()
        self.stop = threading.Event()
        self.thread = None
        self.addCleanup(self.finish)

    def start(self, host=None, paste=False):
        self.thread = threading.Thread(
            target=remote.follow, args=(self.path, self.out, self.tmux),
            kwargs={"tick": 0.01, "beat": 0.05, "stop": self.stop, "host": host, "paste": paste}, daemon=True,
        )
        self.thread.start()
        time.sleep(0.1)

    def finish(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)

    def wait(self, count):
        deadline = time.time() + 3
        while time.time() < deadline and len(self.out.notices()) < count:
            time.sleep(0.01)
        time.sleep(0.1)
        return [r.get("text") for r in self.out.notices()]

    def put(self, *records, raw=""):
        with open(self.path, "ab") as fh:
            for r in records:
                fh.write((json.dumps(r) + "\n").encode())
            fh.write(raw.encode())

    def test_what_was_already_there_is_not_sent(self):
        self.put(notice("Old."))
        self.start()
        self.put(notice("New."))
        self.assertEqual(self.wait(1), ["New."])

    def test_a_malformed_line_is_skipped_and_order_kept(self):
        self.start()
        self.put(notice("A."), raw="not json\n")
        self.put(notice("B."))
        self.assertEqual(self.wait(2), ["A.", "B."])

    def test_a_half_line_arrives_once_and_whole(self):
        self.start()
        whole = json.dumps(notice("Whole."))
        self.put(raw=whole[:20])
        time.sleep(0.1)
        self.assertEqual(self.out.notices(), [])
        self.put(raw=whole[20:] + "\n")
        self.assertEqual(self.wait(1), ["Whole."])

    def test_rotation_loses_and_repeats_nothing(self):
        self.put(notice("Seed."))
        self.start()
        self.put(notice("Before."))
        time.sleep(0.1)
        with mock.patch.object(core, "RELAY_MAX_BYTES", 10):
            remote.append_relay(self.path, notice("Rotated."))
        time.sleep(0.1)
        remote.append_relay(self.path, notice("After."))
        self.assertEqual(self.wait(3), ["Before.", "Rotated.", "After."])

    def test_a_file_that_appears_later_is_read_from_the_start(self):
        self.start()
        self.put(notice("First."))
        self.assertEqual(self.wait(1), ["First."])

    def test_beats_are_written_and_stamp_the_option(self):
        self.start()
        time.sleep(0.2)
        self.assertTrue([r for r in self.out.records() if r["kind"] == "beat"])
        self.assertTrue(self.tmux.set)
        name, value = self.tmux.set[0]
        self.assertEqual(name, RELAY_OPT)
        float(value)

    def test_beats_stamp_the_host_when_given(self):
        self.start(host="emx-test-host")
        time.sleep(0.2)
        self.assertEqual(self.tmux.set[0][0], RELAY_OPT)
        float(self.tmux.set[0][1])
        values = [v for n, v in self.tmux.set if n == RELAY_HOST_OPT]
        self.assertTrue(values)
        self.assertEqual(core.relay_host(values[0], time.time()), "emx-test-host")

    def test_no_host_no_host_stamp(self):
        self.start()
        time.sleep(0.2)
        self.assertTrue(self.tmux.set)
        self.assertNotIn(RELAY_HOST_OPT, [n for n, _ in self.tmux.set])

    def test_beats_stamp_the_paste_option_when_asked(self):
        self.start(paste=True)
        time.sleep(0.2)
        values = [v for n, v in self.tmux.global_set if n == RELAY_PASTE_OPT]
        self.assertTrue(values)
        float(values[0])
        self.assertNotIn(RELAY_PASTE_OPT, [n for n, _ in self.tmux.set])

    def test_no_paste_no_paste_stamp(self):
        self.start()
        time.sleep(0.2)
        self.assertTrue(self.tmux.set)
        self.assertNotIn(RELAY_PASTE_OPT, [n for n, _ in self.tmux.set + self.tmux.global_set])

    def test_a_paste_record_is_streamed(self):
        self.start(paste=True)
        self.put(core.relay_paste("%3", SID, "20261018-120000-0a1b2c3d", 1.0))
        time.sleep(0.3)
        self.assertEqual(
            [r for r in self.out.records() if r["kind"] == "paste"],
            [{"kind": "paste", "pane": "%3", "sid": SID, "id": "20261018-120000-0a1b2c3d"}],
        )

    def test_ending_clears_the_paste_stamp(self):
        self.start(paste=True)
        time.sleep(0.2)
        self.finish()
        stamps = [v for n, v in self.tmux.global_set if n == RELAY_PASTE_OPT]
        self.assertEqual(stamps[-1], "")
        self.assertNotEqual(stamps[0], "")

    def test_no_session_no_stamp(self):
        self.tmux.has = False
        self.start()
        time.sleep(0.2)
        self.assertEqual(self.tmux.set, [])

    def test_a_broken_pipe_ends_it(self):
        class Broken:
            def write(self, text):
                raise BrokenPipeError

            def flush(self):
                pass

        result = []
        thread = threading.Thread(
            target=lambda: result.append(remote.follow(self.path, Broken(), self.tmux, beat=0.05, tick=0.01)), daemon=True
        )
        started = time.time()
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertLess(time.time() - started, 1)


class StubVoice:
    def __init__(self):
        self.spoken, self.silenced = [], 0

    def speak(self, text, at=None, fallback=None):
        self.spoken.append((text, at, fallback))

    def silence(self):
        self.silenced += 1


class StubNotifier:
    def __init__(self):
        self.posts = []

    def post(self, title, body, sid, at=None):
        self.posts.append((title, body, sid))


class DeliveryHandle(unittest.TestCase):
    def setUp(self):
        self.voice, self.notifier = StubVoice(), StubNotifier()
        self.delivery = remote.Delivery(self.voice, self.notifier)

    def record(self, mode):
        return core.parse_relay(json.dumps(core.relay_notice("t · p", "Said.", "Plain.", SID, mode, 1.0)))

    def test_a_voice_mode_notice_is_spoken_with_the_banner_as_its_fallback(self):
        self.delivery.handle(self.record(core.VOICE_MODE))
        self.assertEqual(self.voice.spoken, [("Said.", None, ("t · p", "Plain.", SID))])
        self.assertEqual(self.notifier.posts, [])

    def test_a_notification_mode_notice_is_posted(self):
        self.delivery.handle(self.record(core.NOTIFICATION_MODE))
        self.assertEqual(self.notifier.posts, [("t · p", "Said.", SID)])
        self.assertEqual(self.voice.spoken, [])

    def test_a_paste_record_goes_to_the_paster(self):
        class Paste:
            def __init__(self):
                self.requests = []

            def request(self, *args):
                self.requests.append(args)

        paste = Paste()
        delivery = remote.Delivery(self.voice, self.notifier, paste)
        delivery.handle({"kind": "paste", "pane": "%3", "sid": SID, "id": "20261018-120000-0a1b2c3d"})
        self.assertEqual(paste.requests, [("%3", SID, "20261018-120000-0a1b2c3d")])

    def test_a_paste_record_without_a_paster_does_nothing(self):
        self.delivery.handle({"kind": "paste", "pane": "%3", "sid": SID, "id": "20261018-120000-0a1b2c3d"})
        self.assertEqual((self.voice.spoken, self.notifier.posts), ([], []))

    def test_a_hush_silences_once_and_a_beat_does_nothing(self):
        self.delivery.handle({"kind": "hush"})
        self.delivery.handle({"kind": "beat"})
        self.assertEqual(self.voice.silenced, 1)
        self.assertEqual((self.voice.spoken, self.notifier.posts), ([], []))


class Recorded(remote.Delivery):
    """A delivery that keeps what it was given instead of speaking or posting."""

    def __init__(self):
        super().__init__(StubVoice(), StubNotifier())
        self.records = []

    def handle(self, record):
        self.records.append(record)


class ConnectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "ssh.log"
        use_remote_command("/nonexistent/emx-not-set")
        self.delivery = Recorded()
        self.followers = []
        outer = self

        class Seen(remote.Follower):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.followers.append(self)

        for patch in (
            mock.patch.dict(os.environ, {"EMX_SSH": FAKE_SSH, "FAKE_SSH_LOG": str(self.log), "EMX_SOCKET": "emx-test-lap",
                                         "EMX_SHOW_TEST_SESSIONS": "1"}),
            mock.patch.object(remote.Delivery, "started", classmethod(lambda cls, socket, paste=None: self.delivery)),
            mock.patch.object(remote, "Follower", Seen),
            mock.patch.object(remote, "control_dir", lambda: "/tmp/emx-test-ctl/1"),
            mock.patch.object(remote, "NOTE_SECONDS", 0),
            # A test run from a terminal must never stop at the major question.
            mock.patch.object(remote.sys, "stdin", io.StringIO()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def server(self, version="9.9.9", code=0, follow=True, report=None, update="emx updated to 10.0.0."):
        notice = json.dumps(core.relay_notice("t · p", "Say [[slnc 9]] it.", "Plain.", SID, core.NOTIFICATION_MODE, 1.0))
        path = self.tmp / "fake-emx"
        shown = f'echo "emx {version}"' if version else "true"
        report = f"emx-update current {version} {version} -" if report is None else report
        path.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  --version) {shown};;\n"
            f"  update) if [ \"$2\" = --report ]; then printf '%s\\n' {shlex.quote(report)}; else echo {shlex.quote(update)}; fi;;\n"
            f"  follow) {'printf \'%s\\n\' \'' + notice + '\'' if follow else 'true'}; exec sleep 5;;\n"
            f"  *) sleep 0.5; exit {code};;\n"
            "esac\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        use_remote_command(path)
        return path

    def run_connect(self, host="emx-test-host"):
        err = io.StringIO()
        with redirect_stderr(err):
            code = remote.connect(host)
        return code, err.getvalue()

    def argvs(self):
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines()]


class Connect(ConnectCase):
    def test_the_exit_code_is_the_attaches_and_the_calls_come_in_order(self):
        self.server(code=0)
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        calls = self.argvs()
        self.assertEqual(len(calls), 6)
        master, version, report, middle, exit_call = calls[0], calls[1], calls[2], calls[3:5], calls[5]
        self.assertTrue({"ControlMaster=yes", "-N", "-f"} <= set(master))
        self.assertTrue({"-n", "ControlMaster=no"} <= set(version))
        self.assertTrue(version[-1].endswith("--version'"))
        self.assertTrue({"-n", "ControlMaster=no"} <= set(report))
        self.assertTrue(report[-1].endswith(" update --report'"))
        follower = [c for c in middle if c[-1].endswith("follow'")]
        attach = [c for c in middle if "-t" in c]
        self.assertEqual((len(follower), len(attach)), (1, 1))
        self.assertIn("BatchMode=yes", follower[0])
        self.assertTrue(attach[0][-1].endswith(" -lc " + str(self.tmp / "fake-emx")))
        self.assertEqual(exit_call[exit_call.index("-O"):exit_call.index("-O") + 2], ["-O", "exit"])
        self.assertNotIn("lost the connection", err)

    def test_the_notice_the_follower_read_reaches_the_delivery_cleaned_and_the_follower_is_gone(self):
        self.server()
        self.run_connect()
        notices = [r for r in self.delivery.records if r["kind"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertNotIn("[[", notices[0]["text"])
        self.assertIsNotNone(self.followers[0].proc.poll())

    def test_a_lost_connection_says_so_once_and_returns_255(self):
        self.server(code=255)
        code, err = self.run_connect()
        self.assertEqual(code, 255)
        self.assertEqual(err.count("lost the connection to emx-test-host"), 1)

    def run_on_tty(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        out = Tty()
        with mock.patch.object(remote.sys, "stdout", out):
            self.run_connect()
        return out.getvalue()

    def test_a_lost_connection_puts_the_terminal_back_before_the_message(self):
        self.server(code=255)
        written = self.run_on_tty()
        for seq in ("\x1b[?1049l", "\x1b[?1000l", "\x1b[?1002l", "\x1b[?1003l", "\x1b[?1006l", "\x1b[?1l\x1b>", "\x1b[?25h"):
            self.assertIn(seq, written)

    def test_a_clean_exit_sets_the_title_and_puts_it_back_without_restore_sequences(self):
        self.server(code=0)
        written = self.run_on_tty()
        self.assertNotIn("\x1b[?1049l", written)
        self.assertTrue(written.startswith("\x1b[22;0t\x1b]0;emx · emx-test-host\x07"))
        self.assertTrue(written.endswith("\x1b]0;\x07\x1b[23;0t"))

    def test_a_lost_connection_restores_the_title_before_the_terminal(self):
        self.server(code=255)
        written = self.run_on_tty()
        self.assertLess(written.index("\x1b[23;0t"), written.index("\x1b[?1049l"))

    def test_the_follower_carries_the_host(self):
        self.server()
        self.run_connect()
        follower = [c for c in self.argvs() if c[-1].endswith("follow'")]
        self.assertEqual(len(follower), 1)
        self.assertIn("env EMX_RELAY_HOST=emx-test-host ", follower[0][-1])

    def test_a_lost_connection_writes_nothing_when_stdout_is_not_a_tty(self):
        self.server(code=255)
        out = io.StringIO()
        with mock.patch.object(remote.sys, "stdout", out):
            self.run_connect()
        self.assertEqual(out.getvalue(), "")

    def test_a_different_version_is_told_before_and_after_and_it_still_attaches(self):
        self.server(version="9.9.9")
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        self.assertEqual(err.count("emx on emx-test-host is 9.9.9; this laptop has"), 2)
        self.assertTrue([c for c in self.argvs() if "-t" in c])

    def test_a_server_with_no_emx_is_not_attached_to(self):
        self.server(version=None)
        code, err = self.run_connect()
        self.assertEqual(code, 1)
        self.assertIn("remote_command", err)
        self.assertNotIn("EMX_REMOTE_COMMAND", err)
        calls = self.argvs()
        self.assertFalse([c for c in calls if "-t" in c])
        self.assertFalse([c for c in calls if c[-1].endswith("follow'")])
        self.assertEqual(self.followers, [])

    def test_a_host_that_could_be_an_option_runs_nothing(self):
        self.server()
        code, err = self.run_connect("-oProxyCommand=x")
        self.assertEqual(code, 2)
        self.assertEqual(self.argvs(), [])

    def test_with_no_private_control_folder_there_is_no_master_and_the_follower_stays_batch(self):
        self.server()
        with mock.patch.object(remote, "control_dir", lambda: None):
            code, _ = self.run_connect()
        self.assertEqual(code, 0)
        calls = self.argvs()
        self.assertFalse([c for c in calls if "-N" in c or "exit" in c])
        follower = [c for c in calls if c[-1].endswith("follow'")]
        self.assertIn("BatchMode=yes", follower[0])

    def test_ctrl_c_returns_130_stops_the_follower_and_exits_the_master(self):
        self.server()
        real = subprocess.run

        def run(argv, *args, **kwargs):
            if "-t" in argv:
                time.sleep(0.3)
                raise KeyboardInterrupt
            return real(argv, *args, **kwargs)

        with mock.patch.object(remote.subprocess, "run", run):
            code, err = self.run_connect()
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", err)
        self.assertIsNotNone(self.followers[0].proc.poll())
        last = self.argvs()[-1]
        self.assertEqual(last[last.index("-O"):last.index("-O") + 2], ["-O", "exit"])

    def test_ctrl_c_puts_the_title_back_on_a_tty(self):
        self.server()
        real = subprocess.run

        def run(argv, *args, **kwargs):
            if "-t" in argv:
                raise KeyboardInterrupt
            return real(argv, *args, **kwargs)

        out = Tty()
        with mock.patch.object(remote.subprocess, "run", run), mock.patch.object(remote.sys, "stdout", out):
            code, _ = self.run_connect()
        self.assertEqual(code, 130)
        self.assertTrue(out.getvalue().endswith("\x1b]0;\x07\x1b[23;0t"))


class RemoteCommandSetting(ConnectCase):
    def test_remote_command_comes_from_the_config_and_not_the_environment(self):
        path = self.server()
        with mock.patch.dict(os.environ, {"EMX_REMOTE_COMMAND": "/nonexistent/emx-env"}):
            self.assertEqual(self.run_connect()[0], 0)
        self.assertTrue(any(str(path) in c[-1] for c in self.argvs()))
        self.assertFalse(any("/nonexistent/emx-env" in c[-1] for c in self.argvs()))


class Tty(io.StringIO):
    def isatty(self):
        return True


class ConnectUpdates(ConnectCase):
    """`emx remote` asks the server to catch up within its major before attaching, and asks once about a major for both machines."""

    def commands(self):
        return [c[-1] for c in self.argvs()]

    def answering(self, answer):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answer

        for patch in (mock.patch.object(remote.sys, "stdin", Tty()), mock.patch("builtins.input", ask)):
            patch.start()
            self.addCleanup(patch.stop)
        return asked

    def major_waits(self):
        version = remote.__version__
        self.server(version=version, report=f"emx-update current {version} {version} 99.0.0")

    def test_connect_asks_the_server_to_update_before_attaching(self):
        self.server()
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        commands = self.commands()
        version = next(i for i, c in enumerate(commands) if c.endswith(" --version'"))
        report = next(i for i, c in enumerate(commands) if c.endswith(" update --report'"))
        follow = next(i for i, c in enumerate(commands) if c.endswith(" follow'"))
        attach = next(i for i, c in enumerate(self.argvs()) if "-t" in c)
        self.assertLess(version, report)
        self.assertLess(report, min(follow, attach))
        self.assertIn("Checking emx on emx-test-host for updates", err)
        self.assertNotIn("couldn't update", err)

    def test_a_server_update_is_said(self):
        self.server(version="9.9.8", report="emx-update updated 9.9.8 9.9.9 -")
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        self.assertIn("Updated emx on emx-test-host to 9.9.9.", err)
        # The note compares the laptop with the version the server runs now.
        self.assertIn("emx on emx-test-host is 9.9.9;", err)
        self.assertNotIn("is 9.9.8;", err)

    def test_updates_off_on_the_server_is_said(self):
        self.server(report="emx-update off 9.9.9 9.9.9 -")
        _, err = self.run_connect()
        self.assertIn("Automatic updates are off on emx-test-host.", err)

    def test_a_server_that_cannot_update_gets_the_note_and_why(self):
        self.server(report="emx-update failed 9.9.9 couldn't fetch updates: Permission denied (publickey)")
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        self.assertIn("emx on emx-test-host is 9.9.9; this laptop has", err)
        self.assertIn("emx on emx-test-host couldn't update: couldn't fetch updates: Permission denied (publickey)", err)

    def test_a_server_without_emx_update_is_explained_and_still_attached(self):
        self.server(report="usage: emx [--version | hush | remote <host>]")
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        self.assertIn("its emx (9.9.9) has no emx update; install it there with install.sh", err)
        self.assertTrue([c for c in self.argvs() if "-t" in c])

    def test_server_output_is_untrusted(self):
        self.server(report="emx-update failed 9.9.9 bad\x1b[2Jthing\x07 here")
        _, err = self.run_connect()
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\x07", err)
        self.assertIn("couldn't update: bad", err)

    def test_a_major_is_asked_once_and_no_runs_nothing(self):
        self.major_waits()
        asked = self.answering("n")
        code, _ = self.run_connect()
        self.assertEqual(code, 0)
        self.assertEqual(asked, ["Update emx on emx-test-host and this laptop to 99.0.0? [y/N] "])
        self.assertFalse([c for c in self.commands() if c.endswith(" update'")])
        self.assertTrue([c for c in self.argvs() if "-t" in c])

    def test_yes_updates_server_and_laptop(self):
        self.major_waits()
        self.answering("y")
        root = str(self.tmp / "root")
        update = mock.Mock(return_value=((remote.__version__, "99.0.0", None), None))
        for patch in (
            mock.patch.object(emx, "ROOT", root), mock.patch.object(emx, "RUNNING", remote.__version__),
            mock.patch.object(remote.install, "git", return_value=(None, "no git in tests")),
            mock.patch.object(remote.install, "versions", return_value=([], None)),
            mock.patch.object(remote.install, "update", update),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        self.assertEqual(len([c for c in self.commands() if c.endswith(" update'")]), 1)
        self.assertIn("emx-test-host: emx updated to 10.0.0.", err)
        update.assert_called_once_with(root, remote.__version__, allow_major=True)
        self.assertIn("Updated this laptop's emx to 99.0.0.", err)

    def test_yes_on_a_checkout_laptop_says_it_is_not_updated(self):
        self.major_waits()
        self.answering("yes")
        _, err = self.run_connect()
        self.assertIn("This laptop's emx runs from a checkout", err)

    def test_the_laptop_s_own_major_is_asked_about(self):
        self.server(version=remote.__version__)
        asked = self.answering("n")
        for patch in (
            mock.patch.object(emx, "ROOT", str(self.tmp / "root")), mock.patch.object(emx, "RUNNING", remote.__version__),
            mock.patch.object(remote.install, "git", return_value=(None, "offline")),
            mock.patch.object(remote.install, "versions", return_value=(["98.0.0"], None)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.run_connect()
        self.assertEqual(asked, ["Update emx on emx-test-host and this laptop to 98.0.0? [y/N] "])

    def test_without_a_tty_nothing_is_asked(self):
        self.major_waits()
        with mock.patch("builtins.input", side_effect=AssertionError("asked without a tty")):
            code, err = self.run_connect()
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err)
        self.assertFalse([c for c in self.commands() if c.endswith(" update'")])


class StaleSockets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = Path(self.tmp)

    def sock(self, name):
        import socket as pysocket
        s = pysocket.socket(pysocket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(str(self.folder / name))

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_a_leftover_at_the_chosen_path_is_removed(self):
        self.sock("77")
        remote.clear_stale_sockets(str(self.folder / "77"))
        self.assertFalse((self.folder / "77").exists())

    def test_a_socket_of_a_pid_that_no_longer_runs_is_swept_and_a_running_ones_stays(self):
        dead = str(self.dead_pid())
        live = str(os.getpid())
        self.sock(dead)
        self.sock(live)
        remote.clear_stale_sockets(str(self.folder / "1"))
        self.assertFalse((self.folder / dead).exists())
        self.assertTrue((self.folder / live).exists())

    def test_files_that_are_not_sockets_or_pid_names_are_left_alone(self):
        (self.folder / "notes").write_text("x")
        dead = str(self.dead_pid())
        (self.folder / dead).write_text("x")
        remote.clear_stale_sockets(str(self.folder / "1"))
        self.assertTrue((self.folder / "notes").exists())
        self.assertTrue((self.folder / dead).exists())

    def test_a_missing_folder_is_fine(self):
        remote.clear_stale_sockets(str(self.folder / "gone" / "1"))


class OpenRemote(unittest.TestCase):
    def test_a_refused_host_gets_its_own_message(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = remote.open_remote(SID, {"EMX_REMOTE_HOST": "-oProxyCommand=x"})
        self.assertEqual(code, 2)
        self.assertIn("EMX_REMOTE_HOST", err.getvalue())
        self.assertNotIn("session id", err.getvalue())

    def test_a_bad_session_id_still_says_so(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = remote.open_remote("nope", {"EMX_REMOTE_HOST": "emx-test-host"})
        self.assertEqual(code, 2)
        self.assertIn("session id", err.getvalue())


class ControlDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_private_folder_gives_the_per_process_path(self):
        self.assertEqual(remote.control_dir(os.getuid(), 77, self.tmp), f"{self.tmp}/emx-ssh-{os.getuid()}/77")
        mode = os.stat(f"{self.tmp}/emx-ssh-{os.getuid()}").st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_a_folder_others_can_enter_is_refused(self):
        os.makedirs(f"{self.tmp}/emx-ssh-{os.getuid()}", mode=0o755)
        os.chmod(f"{self.tmp}/emx-ssh-{os.getuid()}", 0o755)
        self.assertIsNone(remote.control_dir(os.getuid(), 77, self.tmp))

    def test_a_path_too_long_for_a_socket_is_refused(self):
        self.assertIsNone(remote.control_dir(os.getuid(), 77, self.tmp + "/" + "x" * 80))


if __name__ == "__main__":
    unittest.main()


PASTE_ID = "20261018-120000-0a1b2c3d"
FIXTURE_PNG = core.PNG_SIGNATURE + b"\x00throwaway picture\xff"


class ConnectPaste(ConnectCase):
    def setUp(self):
        super().setUp()
        self.paster_made = []
        outer = self

        class Made(remote.Paster):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.paster_made.append(self)

        patch = mock.patch.object(remote, "Paster", Made)
        patch.start()
        self.addCleanup(patch.stop)
        self.picture = self.tmp / "picture.png"
        self.picture.write_bytes(FIXTURE_PNG)

    def paste_server(self):
        record = json.dumps(core.relay_paste("%3", SID, PASTE_ID, 1.0))
        path = self.tmp / "fake-emx"
        path.write_text(
            "#!/bin/sh\n"
            f"DIR={shlex.quote(str(self.tmp))}\n"
            'case "$1" in\n'
            "  --version) echo 'emx 9.9.9';;\n"
            "  update) printf '%s\\n' 'emx-update current 9.9.9 9.9.9 -';;\n"
            f"  follow) printf '%s\\n' {shlex.quote(record)}; exec sleep 5;;\n"
            '  paste-image) shift; cat > "$DIR/got"; printf \'%s\\n\' "$@" > "$DIR/args";;\n'
            "  *) sleep 1; exit 0;;\n"
            "esac\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        use_remote_command(path)

    def connect_with(self, reader):
        self.paste_server()
        os.environ["EMX_CLIPBOARD_IMAGE"] = reader
        self.addCleanup(os.environ.pop, "EMX_CLIPBOARD_IMAGE", None)
        def started(cls, socket, paste=None):
            self.delivery = remote.Delivery(StubVoice(), StubNotifier(), paste)
            return self.delivery

        with mock.patch.object(remote.Delivery, "started", classmethod(started)):
            self.run_connect()

    def test_the_laptops_image_is_uploaded_on_stdin_before_the_connection_closes(self):
        self.connect_with(f"cp {shlex.quote(str(self.picture))}")
        self.assertEqual((self.tmp / "got").read_bytes(), FIXTURE_PNG)
        self.assertEqual((self.tmp / "args").read_text().splitlines(), ["%3", SID, PASTE_ID])
        calls = self.argvs()
        (upload,) = [c for c in calls if c[-1].endswith(f"paste-image %3 {SID} {PASTE_ID}'")]
        self.assertTrue({"-T", "ControlPath=/tmp/emx-test-ctl/1", "BatchMode=yes"} <= set(upload))
        self.assertNotIn("-n", upload)
        self.assertLess(calls.index(upload), len(calls) - 1)
        self.assertEqual(calls[-1][calls[-1].index("-O"):calls[-1].index("-O") + 2], ["-O", "exit"])

    def test_a_reader_that_fails_uploads_nothing_to_paste(self):
        self.connect_with("false")
        self.assertEqual((self.tmp / "got").read_bytes(), b"")


class PasterJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.picture = self.tmp / "picture.png"
        self.picture.write_bytes(FIXTURE_PNG)
        ssh = self.tmp / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            f"DIR={shlex.quote(str(self.tmp))}\n"
            'N=$(ls "$DIR" | grep -c "^start-")\n'
            'date +%s.%N > "$DIR/start-$N"\n'
            "cat > \"$DIR/in-$N\"\n"
            "sleep 0.3\n"
            'date +%s.%N > "$DIR/end-$N"\n',
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        self.ssh = str(ssh)

    def paster(self, reader):
        paster = remote.Paster(self.ssh, "emx-test-host", None, "emx", {"EMX_CLIPBOARD_IMAGE": reader})
        self.addCleanup(paster.close)
        return paster

    def test_two_requests_upload_in_order(self):
        paster = self.paster(f"cp {shlex.quote(str(self.picture))}")
        paster.request("%1", SID, PASTE_ID)
        paster.request("%2", SID, "20261018-120001-0a1b2c3d")
        paster.wait(10)
        stamp = lambda name: float((self.tmp / name).read_text())
        self.assertGreaterEqual(stamp("start-1"), stamp("end-0"))
        self.assertEqual((self.tmp / "in-0").read_bytes(), FIXTURE_PNG)

    def test_a_reader_that_hangs_is_cut_at_the_timeout(self):
        script = self.tmp / "hang.sh"
        script.write_text("#!/bin/sh\nsleep 5\n")
        script.chmod(0o755)
        paster = self.paster(str(script))
        with mock.patch.object(remote, "CLIPBOARD_TIMEOUT", 0.2):
            paster.request("%1", SID, PASTE_ID)
            paster.wait(10)
        self.assertEqual((self.tmp / "in-0").read_bytes(), b"")

    def test_wait_gives_up_at_its_timeout(self):
        script = self.tmp / "hang.sh"
        script.write_text("#!/bin/sh\nsleep 3\n")
        script.chmod(0o755)
        paster = self.paster(str(script))
        paster.request("%1", SID, PASTE_ID)
        started = time.time()
        paster.wait(0.2)
        self.assertLess(time.time() - started, 2)


class PasterCleanup(unittest.TestCase):
    def paster(self):
        return remote.Paster("true", "emx-test-host", None, "emx", {"EMX_CLIPBOARD_IMAGE": "true"})

    def test_close_removes_the_scratch_folder_even_with_a_job_still_running(self):
        paster = self.paster()
        folder = paster._scratch()
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertTrue(os.path.isdir(folder))
        paster.close()
        self.assertFalse(os.path.exists(folder))
        self.assertIsNone(paster._scratch())

    def test_a_finished_request_leaves_nothing_in_the_folder(self):
        paster = self.paster()
        paster.request("%1", SID, PASTE_ID)
        paster.wait(10)
        folder = paster.folder
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(os.listdir(folder), [])
        paster.close()
        self.assertFalse(os.path.exists(folder))
