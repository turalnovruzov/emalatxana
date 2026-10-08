import os
import pty
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from emx import core
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


class PrivateServer(unittest.TestCase):
    """A private tmux server with main and the panel side by side, each running the recorder, and a client attached."""

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
            # A failed exec must not leave a forked copy of the test run going on.
            try:
                env = {k: v for k, v in os.environ.items() if k != "TMUX"}
                env["TERM"] = "xterm-256color"
                os.execvpe("tmux", self.tmux.argv("attach-session", "-t", f"={SESSION}:"), env)
            finally:
                os._exit(127)
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



@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class LeftBinding(PrivateServer):
    """The ← binding as installed on a private tmux server, pressed through an attached client so tmux's key table runs."""

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

    def press_toggle(self):
        self.tmux.bind_toggle("C-\\", self.sidebar, self.main)
        self.tmux.run("send-keys", "-K", "-c", self.client, "C-\\")
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")

    def test_the_toggle_binding_parses(self):
        self.tmux.bind_toggle("C-\\", self.sidebar, self.main)
        (line,) = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if " C-\\\\ " in line]
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_the_toggle_to_the_panel_drops_a_left_mark_left_behind(self):
        self.tmux.set_pane_option(self.sidebar, BACK_OPT, "1")
        self.press_toggle()
        self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "")

    def test_the_toggle_to_the_panel_works_with_no_mark_to_drop(self):
        self.press_toggle()
        self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "")

    def test_with_the_panel_gone_the_key_reaches_the_pane(self):
        self.tmux.bind_left(self.sidebar, "true", 2)
        self.tmux.kill_pane(self.sidebar)
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), LEFT)



@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class TypeIf(PrivateServer):
    """Typing into a viewer through `type_gate`, judged and typed by tmux in one command, with focus on the panel."""

    TEXT = "/rename näme; with -l \\; and ;"

    def setUp(self):
        super().setUp()
        self.tmux.select(self.sidebar)

    def type(self, pane=None, sid="s1", y=0):
        pane = pane or self.main
        gate = core.type_gate(pane, self.sidebar, sid, y, SESSION_OPT)
        return self.tmux.type_if(pane, gate, self.TEXT, core.TYPED, core.gate_report(SESSION_OPT))

    def refused(self, report, want, y=0):
        self.assertIn(want, core.gate_refusal(core.Rename("s1", "old", "new"), report, y))
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        self.assertEqual(self.typed("sidebar"), b"")

    def test_it_types_the_text_exactly_then_enter_while_nothing_changed(self):
        self.assertEqual(self.type(), core.TYPED)
        want = self.TEXT.encode("utf-8") + b"\r"
        self.wait_for(lambda: len(self.typed("main")) >= len(want), "the line in main")
        self.assertEqual(self.typed("main"), want)
        self.assertEqual(self.typed("sidebar"), b"")

    def test_it_types_into_a_viewer_in_a_hidden_window(self):
        hidden = self.tmux.new_hidden_window(f"{sys.executable} -I {os.path.join(self.dir, 'recorder.py')} {self.keys('hidden')}")
        self.tmux.set_pane_option(hidden, SESSION_OPT, "s2")
        self.wait_for(lambda: self.show(hidden, "#{cursor_x}") == "2", "the recorder's prompt")
        self.assertEqual(self.type(hidden, "s2"), core.TYPED)
        self.wait_for(lambda: self.typed("hidden"), "the line in the hidden viewer")

    def test_a_viewer_in_copy_mode_gets_nothing(self):
        self.tmux.run("copy-mode", "-t", self.main)
        self.refused(self.type(), "scrolled back")

    def test_focus_off_the_panel_types_nothing(self):
        self.tmux.select(self.main)
        self.refused(self.type(), "Focus left the panel")

    def test_a_pane_holding_another_session_gets_nothing(self):
        self.tmux.set_pane_option(self.main, SESSION_OPT, "s2")
        self.refused(self.type(), "viewer changed")

    def test_a_cursor_that_moved_types_nothing(self):
        self.refused(self.type(y=5), "prompt changed", y=5)

    def test_a_viewer_that_is_gone_types_nothing_anywhere(self):
        gate = core.type_gate(self.main, self.sidebar, "s1", 0, SESSION_OPT)
        self.tmux.kill_pane(self.main)
        try:
            report = self.tmux.type_if(self.main, gate, self.TEXT, core.TYPED, core.gate_report(SESSION_OPT))
        except TmuxError:
            report = None
        self.assertNotEqual(report, core.TYPED)
        if report is not None:
            self.assertIn("viewer changed", core.gate_refusal(core.Rename("s1", "old", "new"), report, 0))
        time.sleep(0.3)
        self.assertEqual(self.typed("sidebar"), b"")

    def test_the_probe_reads_where_the_prompt_put_the_cursor_and_the_mode(self):
        width = int(self.show(self.main, "#{pane_width}"))
        self.assertEqual(self.tmux.prompt_probe(self.main), (2, 0, True, width, False))
        self.tmux.run("copy-mode", "-t", self.main)
        self.assertTrue(self.tmux.prompt_probe(self.main)[4])


class TypeIfTarget(unittest.TestCase):
    def test_a_target_that_is_not_a_pane_id_runs_no_tmux_command(self):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run") as run:
            for pane in ("emx:", "%3;", "%", "=emx:0.1", "%3 "):
                with self.subTest(pane=pane):
                    with self.assertRaises(TmuxError):
                        tmux.type_if(pane, "#{==:1,1}", "/rename x", core.TYPED, "")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
