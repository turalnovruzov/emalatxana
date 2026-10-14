import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from emx import core, remote
from emx.tmux import RELAY_OPT

SID = "0b6f4f0c-6b0a-4c4e-9a53-2f0e3c1d7a11"
FAKE_SSH = str(Path(__file__).resolve().parent / "fake_ssh.py")


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

    def has_session(self):
        return self.has

    def set_session_option(self, name, value):
        self.set.append((name, value))


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

    def start(self):
        self.thread = threading.Thread(
            target=remote.follow, args=(self.path, self.out, self.tmux),
            kwargs={"tick": 0.01, "beat": 0.05, "stop": self.stop}, daemon=True,
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


class Connect(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "ssh.log"
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
            mock.patch.object(remote.Delivery, "started", classmethod(lambda cls, socket: self.delivery)),
            mock.patch.object(remote, "Follower", Seen),
            mock.patch.object(remote, "control_dir", lambda: "/tmp/emx-test-ctl/1"),
            mock.patch.object(remote, "NOTE_SECONDS", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def server(self, version="9.9.9", code=0, follow=True):
        notice = json.dumps(core.relay_notice("t · p", "Say [[slnc 9]] it.", "Plain.", SID, core.NOTIFICATION_MODE, 1.0))
        path = self.tmp / "fake-emx"
        shown = f'echo "emx {version}"' if version else "true"
        path.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  --version) {shown};;\n"
            f"  follow) {'printf \'%s\\n\' \'' + notice + '\'' if follow else 'true'}; exec sleep 5;;\n"
            f"  *) sleep 0.5; exit {code};;\n"
            "esac\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        os.environ["EMX_REMOTE_COMMAND"] = str(path)
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

    def test_the_exit_code_is_the_attaches_and_the_calls_come_in_order(self):
        self.server(code=0)
        code, err = self.run_connect()
        self.assertEqual(code, 0)
        calls = self.argvs()
        self.assertEqual(len(calls), 5)
        master, version, middle, exit_call = calls[0], calls[1], calls[2:4], calls[4]
        self.assertTrue({"ControlMaster=yes", "-N", "-f"} <= set(master))
        self.assertTrue({"-n", "ControlMaster=no"} <= set(version))
        self.assertTrue(version[-1].endswith("--version'"))
        follower = [c for c in middle if c[-1].endswith("follow'")]
        attach = [c for c in middle if "-t" in c]
        self.assertEqual((len(follower), len(attach)), (1, 1))
        self.assertIn("BatchMode=yes", follower[0])
        self.assertTrue(attach[0][-1].endswith(" -lc " + os.environ["EMX_REMOTE_COMMAND"]))
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
        self.assertIn("EMX_REMOTE_COMMAND", err)
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
