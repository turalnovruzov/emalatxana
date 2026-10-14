import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from emx import core, remote
from emx.tmux import RELAY_OPT

SID = "0b6f4f0c-6b0a-4c4e-9a53-2f0e3c1d7a11"


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


if __name__ == "__main__":
    unittest.main()
