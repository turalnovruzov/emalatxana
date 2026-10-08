import os
import pty
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from emx.tmux import BACK_OPT, SESSION, SESSION_OPT, Tmux, TmuxError

# Draws a prompt mark so the cursor sits where an empty Claude Code prompt puts it, then records every byte typed.
RECORDER = """
import os, sys, tty
tty.setraw(0)
os.write(1, "\\u276f ".encode())
while True:
    data = os.read(0, 64)
    if not data:
        break
    with open(sys.argv[1], "ab") as out:
        out.write(data)
"""

LEFT = b"\x1b[D"


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class LeftBinding(unittest.TestCase):
    """The ← binding as installed on a private tmux server, pressed through an attached client so tmux's key table runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        # The server's socket lives in the temporary folder too; tmux leaves the file behind after kill-server.
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.dir})
        env.start()
        self.addCleanup(env.stop)
        recorder = os.path.join(self.dir, "recorder.py")
        with open(recorder, "w", encoding="utf-8") as fh:
            fh.write(RECORDER)
        self.tmux = Tmux(f"emx-test-{uuid.uuid4().hex[:8]}", os.devnull)
        self.addCleanup(self.kill_server)
        self.main = self.tmux.run(
            "new-session", "-d", "-s", SESSION, "-x", "120", "-y", "30", "-P", "-F", "#{pane_id}",
            f"{sys.executable} -I {recorder} {self.keys('main')}",
        ).strip()
        self.sidebar = self.tmux.run(
            "split-window", "-h", "-t", self.main, "-P", "-F", "#{pane_id}",
            f"{sys.executable} -I {recorder} {self.keys('sidebar')}",
        ).strip()
        self.tmux.set_pane_option(self.main, SESSION_OPT, "s1")
        self.client = self.attach()
        self.tmux.select(self.main)
        self.wait_for(lambda: self.show(self.main, "#{cursor_x}") == "2", "the recorder's prompt")

    def keys(self, pane):
        return os.path.join(self.dir, f"{pane}.keys")

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:
            env = {k: v for k, v in os.environ.items() if k != "TMUX"}
            env["TERM"] = "xterm-256color"
            os.execvpe("tmux", self.tmux.argv("attach-session", "-t", f"={SESSION}:"), env)
        self.addCleanup(self.reap, pid, fd)
        self.wait_for(lambda: self.tmux.run("list-clients", "-F", "#{client_name}").strip(), "an attached client")
        return self.tmux.run("list-clients", "-F", "#{client_name}").strip()

    def kill_server(self):
        try:
            self.tmux.run("kill-server")
        except TmuxError:
            pass

    def reap(self, pid, fd):
        os.close(fd)
        for _ in range(50):
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            time.sleep(0.05)
        os.kill(pid, 9)
        os.waitpid(pid, 0)

    def show(self, pane, fmt):
        return self.tmux.run("display-message", "-p", "-t", pane, fmt).strip()

    def wait_for(self, ready, what):
        deadline = time.monotonic() + 5
        while not ready():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)

    def typed(self, pane):
        try:
            with open(self.keys(pane), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return b""

    def press_left(self, check):
        self.tmux.bind_left(self.sidebar, check, 2)
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")

    def test_the_binding_parses(self):
        self.tmux.bind_left(self.sidebar, "true", 2)
        (line,) = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if " Left " in line]
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_a_check_exiting_0_selects_the_panel_and_marks_it(self):
        self.press_left("true")
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")
        self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_a_check_exiting_1_lets_the_key_reach_the_pane(self):
        self.press_left("false")
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), LEFT)
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")

    def test_with_the_panel_gone_the_key_reaches_the_pane(self):
        self.tmux.bind_left(self.sidebar, "true", 2)
        self.tmux.kill_pane(self.sidebar)
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), LEFT)


if __name__ == "__main__":
    unittest.main()
