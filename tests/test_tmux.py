import base64
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from emx import cli, core, engine
from emx.tmux import (
    AGENT_OPT, BACK_OPT, HUSH_OPT, PASTE_OPT, RELAY_PASTE_OPT, ROLE, SESSION, SESSION_OPT, VIEW_OPT, Pane, Tmux, TmuxError,
)

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

    recorder_source = RECORDER

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
            fh.write(self.recorder_source)
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
        self.tmux.set_pane_option(self.main, AGENT_OPT, engine.AGENT)
        self.tmux.set_pane_option(self.main, SESSION_OPT, "s1")
        self.tmux.set_pane_option(self.sidebar, ROLE, "sidebar")
        # main stands in for a view's slot showing s1; the bindings gate on the slot's view tag.
        self.tmux.set_pane_option(self.main, VIEW_OPT, "s1")
        self.client = self.attach()
        self.tmux.select(self.main)
        self.wait_for(lambda: self.show(self.main, "#{cursor_x}") == "2", "the recorder's prompt")

    def keys(self, pane):
        return os.path.join(self.dir, f"{pane}.keys")

    def attach(self, session=SESSION, size=None):
        """Attach a pty client to `session` and return its name; the first one's pty is `self.client_fd`, each one's is in `self.client_fds`."""
        before = set(self.tmux.run("list-clients", "-F", "#{client_name}").split())
        pid, fd = pty.fork()
        if pid == 0:
            # A failed exec must not leave a forked copy of the test run going on.
            try:
                env = {k: v for k, v in os.environ.items() if k != "TMUX"}
                env["TERM"] = "xterm-256color"
                os.execvpe("tmux", self.tmux.argv("attach-session", "-t", f"={session}:"), env)
            finally:
                os._exit(127)
        self.addCleanup(self.reap, pid, fd)
        if size:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
        if not hasattr(self, "client_fd"):
            self.client_fd = fd

        def new_client():
            return set(self.tmux.run("list-clients", "-F", "#{client_name}").split()) - before

        self.wait_for(new_client, "an attached client")
        (name,) = new_client()
        self.client_fds = {**getattr(self, "client_fds", {}), name: fd}
        return name

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
        self.tmux.bind_left(check)
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")

    def test_the_binding_parses(self):
        self.tmux.bind_left("true")
        (line,) = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if line.split()[:4] == ["bind-key", "-T", "root", "Left"]]
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_a_check_exiting_1_selects_the_panel_and_marks_it(self):
        self.press_left("false")
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")
        self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_a_check_that_cannot_run_selects_the_panel(self):
        self.press_left(os.path.join(self.dir, "no-such-check"))
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_a_pane_without_the_view_tag_never_runs_the_check(self):
        self.tmux.run("set-option", "-p", "-u", "-t", self.main, VIEW_OPT)
        self.press_left("false")
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), LEFT)
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")

    def test_a_check_exiting_0_lets_the_key_reach_the_pane(self):
        self.press_left("true")
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), LEFT)
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")

    def press_toggle(self):
        self.tmux.bind_toggle("C-\\")
        self.tmux.run("send-keys", "-K", "-c", self.client, "C-\\")
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")

    def test_the_toggle_binding_parses(self):
        self.tmux.bind_toggle("C-\\")
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
        self.tmux.bind_left("false")
        self.tmux.kill_pane(self.sidebar)
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), LEFT)


# Draws interactive Claude's input box as captured (the line with the cursor, then a pane-wide rule), the cursor where it was, then records every byte typed.
PROMPT_RECORDER = """
import json, os, shutil, sys, tty
tty.setraw(0)
line, x = json.loads(sys.argv[2])
cols = shutil.get_terminal_size().columns
os.write(1, ("\\x1b[2J\\x1b[H" + line + "\\x1b[0m\\r\\n" + "\\u2500" * cols + f"\\x1b[1;{x + 1}H").encode())
while True:
    data = os.read(0, 64)
    if not data:
        break
    with open(sys.argv[1], "ab") as out:
        out.write(data)
"""

SCREENS = Path(__file__).parent / "fixtures" / "screens"


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class LeftInASessionPane(PrivateServer):
    """← in a pane holding one of emx's own sessions, with the real `emx left` check: Claude Code turns a ← it receives on an empty prompt into a fork of the conversation into its background service, so on an empty prompt it must never get through while the panel is there."""

    def show_prompt(self, line, x):
        script = os.path.join(self.dir, "prompt.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(PROMPT_RECORDER)
        self.tmux.run(
            "respawn-pane", "-k", "-t", self.main,
            f"{sys.executable} -I {script} {self.keys('main')} '{json.dumps([line, x])}'",
        )
        key = str(uuid.uuid4())
        self.tmux.set_pane_option(self.main, SESSION_OPT, key)
        self.tmux.set_pane_option(self.main, AGENT_OPT, engine.AGENT)
        self.tmux.set_pane_option(self.main, VIEW_OPT, key)
        self.tmux.select(self.main)
        self.wait_for(lambda: self.show(self.main, "#{cursor_x}") == str(x), "the drawn prompt")

    def captured_empty_prompt(self, name):
        cursor = json.loads((SCREENS / "interactive-cursors.json").read_text("utf-8"))["cursors"][name]
        (path,) = SCREENS.glob(f"{name}.w*.txt")
        return path.read_text("utf-8").split("\n")[cursor["y"]], cursor["x"]

    def press_left(self, check=None):
        self.tmux.bind_left(check or cli.self_command("left", self.tmux.socket))
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")

    def assert_on_the_panel(self):
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")
        self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_claudes_empty_prompt_puts_the_cursor_two_columns_after_its_mark(self):
        for name in ("interactive-prompt-empty-named", "interactive-prompt-empty-unnamed", "interactive-prompt-working"):
            with self.subTest(name=name):
                self.assertEqual(self.captured_empty_prompt(name)[1], core.PROMPT_TEXT_X)

    def test_left_on_an_empty_prompt_never_reaches_claude_and_lands_on_the_panel(self):
        for name in ("interactive-prompt-empty-named", "interactive-prompt-empty-unnamed"):
            with self.subTest(name=name):
                self.show_prompt(*self.captured_empty_prompt(name))
                self.press_left()
                self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")
                self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "1")
                time.sleep(0.3)
                self.assertEqual(self.typed("main"), b"")
                self.tmux.run("set-option", "-p", "-u", "-t", self.sidebar, BACK_OPT)

    def test_left_with_text_typed_moves_the_text_cursor_in_claude(self):
        for x in (5, 2):
            with self.subTest(x=x):
                self.show_prompt("❯\xa0abc", x)
                self.press_left()
                self.wait_for(lambda: self.typed("main"), "the key in the session")
                self.assertEqual(self.typed("main"), LEFT)
                self.assertEqual(self.show(self.main, "#{pane_active}"), "1")
                os.unlink(self.keys("main"))

    def test_left_on_an_empty_prompt_drawn_a_column_over_still_lands_on_the_panel(self):
        line, x = self.captured_empty_prompt("interactive-prompt-empty-named")
        self.show_prompt(" " + line, x + 1)
        self.press_left()
        self.assert_on_the_panel()

    def test_left_lands_on_the_panel_when_the_check_errors(self):
        # The check reads the screen from a server that isn't there, so it errors even where text is typed.
        broken = f"env TMUX_TMPDIR={os.path.join(self.dir, 'no-server-here')} {cli.self_command('left', self.tmux.socket)}"
        for prompt in (self.captured_empty_prompt("interactive-prompt-empty-named"), ("❯\xa0abc", 5)):
            with self.subTest(prompt=prompt):
                self.show_prompt(*prompt)
                self.press_left(broken)
                self.assert_on_the_panel()
                self.tmux.run("set-option", "-p", "-u", "-t", self.sidebar, BACK_OPT)

    def test_left_with_text_typed_reaches_claude_when_the_prompt_is_a_column_over(self):
        self.show_prompt(" ❯\xa0abc", 6)
        self.press_left()
        self.wait_for(lambda: self.typed("main"), "the key in the session")
        self.assertEqual(self.typed("main"), LEFT)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class CollapseBinding(PrivateServer):
    """The collapse key as installed on a private tmux server, pressed through an attached client so tmux's key table runs."""

    KEY = "C-q"

    def zoomed(self):
        return self.show(self.main, "#{window_zoomed_flag}")

    def press(self, key=KEY):
        self.tmux.run("send-keys", "-K", "-c", self.client, key)

    def test_the_collapse_binding_parses(self):
        self.tmux.bind_collapse(self.KEY)
        (line,) = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if " C-q " in line]
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_pressed_in_the_session_hides_and_again_shows(self):
        self.tmux.bind_collapse(self.KEY)
        self.press()
        self.wait_for(lambda: self.zoomed() == "1", "the panel to hide")
        self.assertEqual(self.show(self.main, "#{pane_width}"), self.show(self.main, "#{window_width}"))
        self.press()
        self.wait_for(lambda: self.zoomed() == "0", "the panel to show")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_pressed_in_the_panel_hides_and_lands_in_the_session(self):
        self.tmux.bind_collapse(self.KEY)
        self.tmux.select(self.sidebar)
        self.press()
        self.wait_for(lambda: self.zoomed() == "1", "the panel to hide")
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("sidebar"), b"")

    def test_the_toggle_key_brings_the_panel_back(self):
        self.tmux.bind_collapse(self.KEY)
        self.tmux.bind_toggle("C-\\")
        self.press()
        self.wait_for(lambda: self.zoomed() == "1", "the panel to hide")
        self.press("C-\\")
        self.wait_for(lambda: self.zoomed() == "0", "the panel to show")
        self.assertEqual(self.show(self.sidebar, "#{pane_active}"), "1")

    def test_the_raw_ctrl_q_byte_from_the_terminal_hides(self):
        self.tmux.run("set-option", "-s", "extended-keys", "always")
        self.tmux.run("set-option", "-s", "extended-keys-format", "csi-u")
        self.tmux.bind_collapse(self.KEY)
        os.write(self.client_fd, b"\x11")
        self.wait_for(lambda: self.zoomed() == "1", "the raw byte to hide the panel")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        os.write(self.client_fd, b"\x1b[113;5u")
        self.wait_for(lambda: self.zoomed() == "0", "the CSI-u form to show the panel")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_with_main_gone_the_key_reaches_the_pane(self):
        self.tmux.bind_collapse(self.KEY)
        self.tmux.kill_pane(self.main)
        self.press()
        self.wait_for(lambda: self.typed("sidebar"), "the key in the panel")
        self.assertEqual(self.typed("sidebar"), b"\x11")
        self.assertEqual(self.show(self.sidebar, "#{window_zoomed_flag}"), "0")

    def test_panes_reads_the_zoom_flag(self):
        self.assertEqual([p.zoomed for p in self.tmux.panes()], [False, False])
        self.tmux.toggle_zoom(self.main)
        self.assertEqual([p.zoomed for p in self.tmux.panes()], [True, True])

    def test_the_hidden_panel_keeps_its_width(self):
        self.tmux.resize_width(self.sidebar, 40)
        self.tmux.toggle_zoom(self.main)
        (panel,) = [p for p in self.tmux.panes() if p.id == self.sidebar]
        self.assertEqual(panel.width, 40)

    def test_respawning_the_panel_and_re_sourcing_the_config_keep_it_hidden(self):
        self.tmux.toggle_zoom(self.main)
        self.tmux.respawn(self.sidebar, "sleep 60")
        self.assertEqual(self.zoomed(), "1")
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")
        config = os.path.join(os.path.dirname(os.path.abspath(cli.__file__)), "emx.tmux.conf")
        self.tmux.run("source-file", config)
        self.assertEqual(self.zoomed(), "1")

    def test_left_at_an_empty_prompt_brings_the_panel_back(self):
        self.tmux.toggle_zoom(self.main)
        self.tmux.bind_left("false")
        self.press("Left")
        self.wait_for(lambda: self.zoomed() == "0", "the panel to show")
        self.assertEqual(self.show(self.sidebar, "#{pane_active}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_swapping_into_main_shows_the_panel(self):
        fresh = self.tmux.new_hidden_window("sleep 60")
        self.tmux.toggle_zoom(self.main)
        self.tmux.swap(fresh, self.main)
        self.assertEqual(self.show(self.sidebar, "#{window_zoomed_flag}"), "0")

    def test_selecting_the_panel_shows_it(self):
        self.tmux.toggle_zoom(self.main)
        self.tmux.select(self.sidebar)
        self.assertEqual(self.zoomed(), "0")
        self.assertEqual(self.show(self.sidebar, "#{pane_active}"), "1")


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

    def test_it_types_the_text_and_no_enter_when_asked_not_to(self):
        gate = core.type_gate(self.main, self.sidebar, "s1", 0, SESSION_OPT)
        report = self.tmux.type_if(self.main, gate, self.TEXT, core.TYPED, core.gate_report(SESSION_OPT), enter=False)
        self.assertEqual(report, core.TYPED)
        self.wait_for(lambda: len(self.typed("main")) >= len(self.TEXT.encode("utf-8")), "the line in main")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), self.TEXT.encode("utf-8"))

    def test_enter_and_ctrl_u_go_only_through_their_own_gated_methods(self):
        gate = core.type_gate(self.main, self.sidebar, "s1", 0, SESSION_OPT)
        refused = core.type_gate(self.main, self.sidebar, "s1", 5, SESSION_OPT)
        report = core.gate_report(SESSION_OPT)
        self.assertNotEqual(self.tmux.submit_if(self.main, refused, core.TYPED, report), core.TYPED)
        self.assertNotEqual(self.tmux.clear_if(self.main, refused, core.TYPED, report), core.TYPED)
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        self.assertEqual(self.tmux.submit_if(self.main, gate, core.TYPED, report), core.TYPED)
        self.assertEqual(self.tmux.clear_if(self.main, gate, core.TYPED, report), core.TYPED)
        self.wait_for(lambda: len(self.typed("main")) >= 2, "both keys in main")
        self.assertEqual(self.typed("main"), b"\r\x15")
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
        self.refused(self.type(), "session changed")

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
            self.assertIn("session changed", core.gate_refusal(core.Rename("s1", "old", "new"), report, 0))
        time.sleep(0.3)
        self.assertEqual(self.typed("sidebar"), b"")

    def test_the_probe_reads_where_the_prompt_put_the_cursor_and_the_mode(self):
        width = int(self.show(self.main, "#{pane_width}"))
        self.assertEqual(self.tmux.prompt_probe(self.main), (2, 0, True, width, False))
        self.tmux.run("copy-mode", "-t", self.main)
        self.assertTrue(self.tmux.prompt_probe(self.main)[4])


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class KeyIf(PrivateServer):
    """One named key into a viewer through `type_gate`, judged and sent by tmux in one command, never followed by Enter."""

    def setUp(self):
        super().setUp()
        self.tmux.select(self.sidebar)

    def send(self, gate=None):
        gate = gate or core.type_gate(self.main, self.sidebar, "s1", 0, SESSION_OPT)
        return self.tmux.key_if(self.main, gate, "Escape", core.TYPED, core.gate_report(SESSION_OPT))

    def test_it_sends_escape_and_no_enter_while_the_gate_holds(self):
        self.assertEqual(self.send(), core.TYPED)
        self.wait_for(lambda: self.typed("main"), "the key in main")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"\x1b")
        self.assertEqual(self.typed("sidebar"), b"")

    def test_a_false_gate_sends_nothing_and_reports(self):
        report = self.send("#{==:1,0}")
        self.assertEqual(report, "0 0 1 2 0 s1")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_a_viewer_in_copy_mode_gets_nothing(self):
        self.tmux.run("copy-mode", "-t", self.main)
        report = self.send()
        self.assertNotEqual(report, core.TYPED)
        self.assertEqual(report.split()[0], "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        self.assertEqual(self.typed("sidebar"), b"")


class KeyIfTarget(unittest.TestCase):
    def test_a_key_off_the_allowlist_or_a_bad_pane_runs_no_tmux_command(self):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run") as run:
            for pane, key in (("%3", "Enter"), ("%3", "C-c"), ("%3", "Escape ; kill-server"), ("%3", "escape"),
                              ("emx:", "Escape"), ("%3;", "Escape"), ("%3 ", "Escape")):
                with self.subTest(pane=pane, key=key):
                    with self.assertRaises(TmuxError):
                        tmux.key_if(pane, "#{==:1,1}", key, core.TYPED, "")
        run.assert_not_called()


class TypeIfTarget(unittest.TestCase):
    def test_a_target_that_is_not_a_pane_id_runs_no_tmux_command(self):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run") as run:
            for pane in ("emx:", "%3;", "%", "=emx:0.1", "%3 "):
                with self.subTest(pane=pane):
                    with self.assertRaises(TmuxError):
                        tmux.type_if(pane, "#{==:1,1}", "/rename x", core.TYPED, "")
        run.assert_not_called()



CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "emx", "emx.tmux.conf")
SHELL_CONF = open(CONF, encoding="utf-8").read()
STATUS_KEYS = (
    "MouseDown1Status", "C-MouseDown1Status", "MouseDown3Status", "M-MouseDown3Status",
    "MouseDown3StatusLeft", "M-MouseDown3StatusLeft", "WheelUpStatus", "WheelDownStatus",
)


class ClientStateParse(unittest.TestCase):
    def state(self, out, now=1000.0):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run", return_value=out) as run:
            got = tmux.client_state(now)
        return got, run.call_args.args

    def test_it_asks_the_emx_session_exactly(self):
        _, args = self.state("1\t1\t%2\t990\n")
        self.assertEqual(args[:3], ("display-message", "-p", "-t"))
        self.assertEqual(args[3], "=emx:")
        self.assertIn("#{session_attached}", args[4])
        self.assertIn("#{@emx_focus}", args[4])
        self.assertIn("#{pane_id}", args[4])
        self.assertIn("#{client_activity}", args[4])

    def test_an_attached_focused_client(self):
        self.assertEqual(self.state("1\t1\t%2\t990\n")[0], (1, "1", "%2", 10.0))

    def test_no_client_and_nothing_known(self):
        self.assertEqual(self.state("0\t\t%0\t\n")[0], (0, "", "%0", None))
        self.assertEqual(self.state("0\t\t\t\n")[0], (0, "", "", None))
        self.assertEqual(self.state("")[0], (0, "", "", None))

    def test_an_unreadable_answer_is_a_tmux_error_not_a_crash(self):
        for out in ("x\t1\t%1\t5", "1.5\t1\t%1\t5"):
            with self.subTest(out=out), self.assertRaises(TmuxError):
                self.state(out)
        self.assertEqual(self.state("1\t1\t%1\tnot-a-time")[0], (1, "1", "%1", None))

    def test_a_focus_out_report_and_an_activity_in_the_future(self):
        self.assertEqual(self.state("1\t0\t%1\t1005\n")[0], (1, "0", "%1", 0.0))


class ClientTtys(unittest.TestCase):
    def test_it_lists_the_emx_sessions_clients_by_tty(self):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run", return_value="/dev/ttys004\n\n/dev/ttys009\n") as run:
            self.assertEqual(tmux.client_ttys(), ["/dev/ttys004", "/dev/ttys009"])
        self.assertEqual(run.call_args.args, ("list-clients", "-t", "=emx:", "-F", "#{client_tty}"))


class ShippedConf(unittest.TestCase):
    def test_both_focus_hooks_are_there_and_passthrough_is_not_set_globally(self):
        self.assertIn("set-hook -g client-focus-in 'set-option @emx_focus 1'", SHELL_CONF)
        self.assertIn("set-hook -g client-focus-out 'set-option @emx_focus 0'", SHELL_CONF)
        self.assertNotIn("allow-passthrough", SHELL_CONF)

    def test_the_status_line_is_on_at_the_bottom(self):
        for line in ("set -g status on", "set -g status-position bottom", "set -g status-style 'bg=colour236", "set -g status-format[0] ''"):
            self.assertIn(line, SHELL_CONF)
        self.assertNotIn("status off", SHELL_CONF)

    def test_the_status_line_mouse_keys_are_unbound(self):
        for key in STATUS_KEYS:
            self.assertIn(f"unbind -q -n {key}\n", SHELL_CONF)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class Focus(PrivateServer):
    """The shipped conf sourced onto a private server, with focus reports written through an attached pty client."""

    def setUp(self):
        super().setUp()
        self.tmux.config = CONF
        self.tmux.source_config()

    def test_sourcing_the_conf_twice_succeeds(self):
        self.tmux.source_config()

    def test_focus_in_and_out_reach_the_session_option(self):
        os.write(self.client_fd, b"\x1b[I")
        self.wait_for(lambda: self.tmux.client_state()[1] == "1", "focus in")
        attached, focus, active, idle = self.tmux.client_state()
        self.assertEqual((attached, focus, active), (1, "1", self.main))
        self.assertLess(idle, 5)
        os.write(self.client_fd, b"\x1b[O")
        self.wait_for(lambda: self.tmux.client_state()[1] == "0", "focus out")

    def test_a_focus_report_lands_on_the_reporting_clients_own_view(self):
        self.tmux.run("new-session", "-d", "-s", "emx-view-1", "-x", "80", "-y", "24", "sleep 60")
        phone = self.attach("emx-view-1")
        self.tmux.run("set-option", "-t", "=emx:", "@emx_focus", "0")
        os.write(self.client_fds[phone], b"\x1b[I")
        view = Tmux(self.tmux.socket, view="emx-view-1")
        self.wait_for(lambda: view.client_state()[1] == "1", "focus in on the second view")
        self.assertEqual(self.tmux.client_state()[1], "0")

    def test_nested_clients_get_the_laptops_terminal_features(self):
        features = self.tmux.run("show-options", "-s", "terminal-features")
        self.assertIn("tmux*:extkeys:RGB:clipboard:ccolour:cstyle:focus:title", features)
        self.assertNotIn("hyperlinks", features)
        self.assertEqual(self.tmux.run("show-options", "-gv", "window-size").strip(), "latest")

    def test_the_attached_client_is_listed_by_its_tty(self):
        (tty,) = self.tmux.client_ttys()
        self.assertTrue(tty.startswith(("/dev/tty", "/dev/pts/")), tty)

@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class Bar(PrivateServer):
    def setUp(self):
        super().setUp()
        self.tmux.config = CONF
        self.tmux.source_config()

    def test_the_bar_text_reaches_the_attached_client(self):
        self.tmux.set_bar("emx-bar-probe")
        self.read_client_until(b"emx-bar-probe")

    def test_unsetting_the_2_6_0_panel_style_leaves_the_terminals_own(self):
        self.tmux.set_pane_option(self.sidebar, "window-style", "bg=colour235")
        self.tmux.unset_pane_option(self.sidebar, "window-style")
        self.assertEqual(self.tmux.run("show-options", "-p", "-t", self.sidebar, "window-style").strip(), "")

    def test_set_bar_targets_the_emx_session(self):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run") as run:
            tmux.set_bar("x")
        self.assertEqual(run.call_args.args, ("set-option", "-t", "=emx:", "status-format[0]", "x"))

    def test_no_status_mouse_key_is_bound_after_sourcing_twice(self):
        self.tmux.source_config()
        keys = self.tmux.run("list-keys", "-T", "root")
        self.assertNotIn("Status", keys)

    def read_client_until(self, needle):
        seen = bytearray()

        def has_it():
            ready, _, _ = select.select([self.client_fd], [], [], 0)
            if ready:
                seen.extend(os.read(self.client_fd, 65536))
            return needle in seen

        self.wait_for(has_it, repr(needle))


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class ClearSessionOptionIf(PrivateServer):
    OPT = "@emx_test_opt"

    def test_a_matching_value_clears(self):
        self.tmux.set_session_option(self.OPT, "abc")
        self.tmux.clear_session_option_if(self.OPT, "abc")
        self.assertEqual(self.tmux.session_option(self.OPT), "")

    def test_a_different_value_is_left_alone(self):
        self.tmux.set_session_option(self.OPT, "abc")
        self.tmux.clear_session_option_if(self.OPT, "xyz")
        self.assertEqual(self.tmux.session_option(self.OPT), "abc")

    def test_a_matching_value_clears_with_a_second_session_on_the_server(self):
        self.tmux.run("new-session", "-d", "-s", "other-session", "-x", "80", "-y", "24")
        self.tmux.run("set-option", "-t", "=other-session:", self.OPT, "other")
        self.tmux.set_session_option(self.OPT, "abc")
        self.tmux.clear_session_option_if(self.OPT, "abc")
        self.assertEqual(self.tmux.session_option(self.OPT), "")
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", "=other-session:", self.OPT).strip(), "other")


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class HushAcrossServers(unittest.TestCase):
    """The real launcher's `hush` against two private servers: only the one named by EMX_SOCKET is touched."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.servers = {}
        for name in ("a", "b"):
            tmux = Tmux(f"emx-test-{name}-{uuid.uuid4().hex[:8]}", os.devnull)
            self.addCleanup(self.kill, tmux)
            tmux.run("new-session", "-d", "-s", SESSION, "sleep 30")
            self.servers[name] = tmux

    @staticmethod
    def kill(tmux):
        try:
            tmux.run("kill-server")
        except TmuxError:
            pass

    def hush(self, name):
        env = {**os.environ, "EMX_SOCKET": self.servers[name].socket}
        self.assertEqual(env["TMUX_TMPDIR"], self.tmpdir)
        self.assertTrue(env["EMX_SOCKET"].startswith("emx-test-"))
        done = subprocess.run(
            [sys.executable, cli.BIN, "hush"], capture_output=True, text=True, timeout=30, env=env,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def token(self, name):
        return self.servers[name].run("show-options", "-qv", "-t", f"={SESSION}:", HUSH_OPT).strip()

    def test_it_sets_a_token_on_the_named_server_and_never_on_the_other(self):
        self.hush("a")
        first = self.token("a")
        self.assertTrue(first.isdigit())
        self.assertEqual(self.token("b"), "")
        self.hush("a")
        self.assertNotEqual(self.token("a"), first)
        self.assertEqual(self.token("b"), "")


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class WindowParams(PrivateServer):
    """The working folder and agent tag a session's pane is given."""

    def pwd_command(self, out):
        return f"{sys.executable} -c \"import os; open({out!r}, 'w').write(os.getcwd()); import time; time.sleep(30)\""

    def read(self, path):
        self.wait_for(lambda: os.path.exists(path) and os.path.getsize(path), "the pane to report its folder")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_a_new_window_starts_in_the_folder_it_is_given(self):
        work = os.path.realpath(tempfile.mkdtemp(dir=self.dir))
        out = os.path.join(self.dir, "pwd-new.txt")
        pane = self.tmux.new_hidden_window(self.pwd_command(out), cwd=work)
        self.assertRegex(pane, r"^%\d+$")
        self.assertEqual(os.path.realpath(self.read(out)), work)

    def test_a_new_window_without_a_folder_still_works(self):
        out = os.path.join(self.dir, "pwd-default.txt")
        self.tmux.new_hidden_window(self.pwd_command(out))
        self.assertTrue(self.read(out))

    def test_respawn_starts_in_the_folder_it_is_given(self):
        work = os.path.realpath(tempfile.mkdtemp(dir=self.dir))
        out = os.path.join(self.dir, "pwd-respawn.txt")
        pane = self.tmux.new_hidden_window("sleep 30")
        self.tmux.respawn(pane, self.pwd_command(out), cwd=work)
        self.assertEqual(os.path.realpath(self.read(out)), work)

    def test_panes_report_the_agent_tag(self):
        pane = self.tmux.new_hidden_window("sleep 30")
        self.assertEqual({p.id: p.agent for p in self.tmux.panes()}[pane], "")
        self.tmux.set_pane_option(pane, AGENT_OPT, "claude")
        self.tmux.set_pane_option(pane, SESSION_OPT, "k1")
        found = {p.id: p for p in self.tmux.panes()}[pane]
        self.assertEqual((found.agent, found.session), ("claude", "k1"))
        self.assertEqual({p.agent for p in self.tmux.panes() if p.id not in (pane, self.main)}, {""})


FAKE_CLAUDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_claude.py")


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class EngineLive(unittest.TestCase):
    """engine.start against a private tmux server, with a stand-in for claude that runs the hook commands emx wrote."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.dir, "EMX_SHOW_TEST_SESSIONS": ""})
        env.start()
        self.addCleanup(env.stop)
        self.state = os.path.join(self.dir, "state", "emx")
        self.folder = os.path.join(self.dir, "work")
        os.mkdir(self.folder)
        self.tmux = Tmux(f"emx-test-{uuid.uuid4().hex[:8]}", os.devnull)
        self.addCleanup(self.kill_server)
        self.tmux.run("new-session", "-d", "-s", SESSION, "-x", "100", "-y", "30", "sleep 120")

    def kill_server(self):
        try:
            self.tmux.run("kill-server")
        except TmuxError:
            pass

    def listed(self, key):
        sessions, error = engine.list_sessions(self.tmux, self.state)
        self.assertIsNone(error)
        return [s for s in sessions if s.id == key]

    def wait_for_state(self, key, state, running=True):
        deadline = time.monotonic() + 10
        while True:
            found = self.listed(key)
            if found and (found[0].state, found[0].running) == (state, running):
                return found[0]
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {state}; last saw {found}")
            time.sleep(0.05)

    def pane_of(self, key):
        (pane,) = [p.id for p in self.tmux.panes() if p.session == key]
        return pane

    def send(self, key, event):
        self.tmux.run("send-keys", "-t", self.pane_of(key), "-l", event)
        self.tmux.run("send-keys", "-t", self.pane_of(key), "Enter")

    def test_a_session_goes_starting_then_working_then_done_and_stopped_when_its_pane_is_killed(self):
        Path(self.folder, "hold").write_text("")
        session, error = engine.start(self.tmux, FAKE_CLAUDE, self.folder, "hello", state=self.state)
        self.assertIsNone(error)
        starting = self.wait_for_state(session.id, "starting")
        self.assertEqual((core.category(starting), starting.turned), (core.NEEDS_INPUT, False))
        os.unlink(os.path.join(self.folder, "hold"))
        self.assertEqual(self.wait_for_state(session.id, "blocked").turned, False)
        self.send(session.id, "UserPromptSubmit")
        self.assertEqual(self.wait_for_state(session.id, "working").turned, True)
        self.send(session.id, "Stop")
        done = self.wait_for_state(session.id, "done")
        self.assertEqual((core.category(done), done.session_id, done.cwd), (core.DONE, session.id, self.folder))
        self.tmux.kill_pane(self.pane_of(session.id))
        stopped = self.wait_for_state(session.id, "stopped", running=False)
        self.assertEqual(core.category(stopped), core.STOPPED)

    def test_the_pane_runs_in_the_folder_and_carries_the_tags(self):
        session, _ = engine.start(self.tmux, FAKE_CLAUDE, self.folder, "", state=self.state)
        (pane,) = [p for p in self.tmux.panes() if p.session == session.id]
        self.assertEqual(pane.agent, "claude")
        self.wait_for_state(session.id, "blocked")
        with open(os.path.join(self.state, "sessions", session.id, "events.jsonl"), encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        self.assertEqual((first["event"], first["source"], os.path.realpath(first["cwd"])), ("SessionStart", "startup", self.folder))

    def test_a_resumed_session_reads_idle_and_keeps_its_conversation(self):
        session, _ = engine.start(self.tmux, FAKE_CLAUDE, self.folder, "", state=self.state)
        self.wait_for_state(session.id, "blocked")
        self.send(session.id, "SessionEnd")
        self.wait_for_state(session.id, "stopped", running=False)
        command, error = engine.resume_command(session.id, FAKE_CLAUDE, state=self.state)
        self.assertIsNone(error)
        pane = self.tmux.new_hidden_window(command, cwd=self.folder)
        self.tmux.set_pane_option(pane, SESSION_OPT, session.id)
        self.tmux.set_pane_option(pane, AGENT_OPT, "claude")
        resumed = self.wait_for_state(session.id, "working")
        self.assertEqual((resumed.status, resumed.turned, resumed.session_id), ("idle", False, session.id))
        self.assertTrue(core.idle(resumed))


BRACKETED_RECORDER = RECORDER.replace('os.write(1, "\\u276f ".encode())', 'os.write(1, b"\\x1b[?2004h" + "\\u276f ".encode())')


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class PasteBinding(PrivateServer):
    """Ctrl+V as bound on a private tmux server, pressed through an attached client so tmux's key table runs."""

    KEY = "C-v"
    CTRL_V = b"\x16"

    def setUp(self):
        super().setUp()
        self.tmux.set_pane_option(self.main, SESSION_OPT, str(uuid.uuid4()))
        self.tmux.run("set-option", "-g", RELAY_PASTE_OPT, str(time.time()))

    def press(self):
        self.tmux.run("send-keys", "-K", "-c", self.client, self.KEY)

    def marker_check(self):
        marker = os.path.join(self.dir, "ran")
        script = os.path.join(self.dir, "check.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
        os.chmod(script, 0o755)
        return script, marker

    def test_the_paste_binding_parses(self):
        self.tmux.bind_paste(self.KEY, "true")
        (line,) = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if " C-v " in line]
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_a_check_exiting_0_sends_nothing(self):
        self.tmux.bind_paste(self.KEY, "true")
        self.press()
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_a_check_exiting_1_passes_the_key(self):
        self.tmux.bind_paste(self.KEY, "false")
        self.press()
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), self.CTRL_V)

    def test_a_check_that_cannot_run_passes_the_key(self):
        self.tmux.bind_paste(self.KEY, os.path.join(self.dir, "no-such-check"))
        self.press()
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), self.CTRL_V)

    def test_no_stamp_never_runs_the_check(self):
        script, marker = self.marker_check()
        self.tmux.run("set-option", "-g", "-u", RELAY_PASTE_OPT)
        self.tmux.bind_paste(self.KEY, script)
        self.press()
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), self.CTRL_V)
        self.assertFalse(os.path.exists(marker))

    def test_a_pane_without_the_agent_tag_passes_the_key(self):
        script, marker = self.marker_check()
        self.tmux.run("set-option", "-p", "-u", "-t", self.main, AGENT_OPT)
        self.tmux.bind_paste(self.KEY, script)
        self.press()
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), self.CTRL_V)
        self.assertFalse(os.path.exists(marker))

    def test_the_raw_ctrl_v_byte_is_caught(self):
        self.tmux.run("set-option", "-s", "extended-keys", "always")
        self.tmux.run("set-option", "-s", "extended-keys-format", "csi-u")
        self.tmux.bind_paste(self.KEY, "true")
        os.write(self.client_fd, self.CTRL_V)
        time.sleep(0.5)
        self.assertEqual(self.typed("main"), b"")
        self.tmux.bind_paste(self.KEY, "false")
        os.write(self.client_fd, self.CTRL_V)
        self.wait_for(lambda: self.typed("main"), "the raw byte in main")
        self.assertEqual(self.typed("main"), self.CTRL_V)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class PasteIf(PrivateServer):
    recorder_source = BRACKETED_RECORDER

    def setUp(self):
        super().setUp()
        self.sid = str(uuid.uuid4())
        self.tmux.set_pane_option(self.main, SESSION_OPT, self.sid)
        self.wait_for(lambda: self.show(self.main, "#{cursor_x}") == "2", "the recorder's prompt")

    def buffers(self):
        return self.tmux.run("list-buffers", "-F", "#{buffer_name}").split()

    def test_it_pastes_bracketed_into_the_matching_session(self):
        self.assertTrue(self.tmux.paste_if(self.main, self.sid, "emx-paste-a", "/tmp/a b.png"))
        self.wait_for(lambda: self.typed("main"), "the paste in main")
        self.assertEqual(self.typed("main"), b"\x1b[200~/tmp/a b.png\x1b[201~")
        self.assertEqual(self.buffers(), [])

    def test_holds_session_is_true_only_for_the_pane_and_session_that_match(self):
        self.assertTrue(self.tmux.holds_session(self.main, self.sid))
        self.assertFalse(self.tmux.holds_session(self.main, str(uuid.uuid4())))
        self.assertFalse(self.tmux.holds_session("%9999", self.sid))

    def test_another_session_gets_nothing(self):
        self.assertFalse(self.tmux.paste_if(self.main, str(uuid.uuid4()), "emx-paste-a", "/tmp/a.png"))
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        self.assertNotIn("emx-paste-a", self.buffers())

    def test_copy_mode_gets_nothing(self):
        self.tmux.run("copy-mode", "-t", self.main)
        self.assertFalse(self.tmux.paste_if(self.main, self.sid, "emx-paste-a", "/tmp/a.png"))
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        self.assertNotIn("emx-paste-a", self.buffers())

    def test_show_message_reaches_the_client(self):
        self.tmux.run("set-option", "-g", "status", "off")
        self.tmux.show_message(self.main, "no image in clipboard")
        seen = b""
        deadline = time.monotonic() + 5
        while b"no image in clipboard" not in seen and time.monotonic() < deadline:
            try:
                seen += os.read(self.client_fd, 65536)
            except OSError:
                break
        self.assertIn(b"no image in clipboard", seen)


class PasteIfTarget(unittest.TestCase):
    def test_a_bad_pane_or_name_runs_no_tmux_command(self):
        tmux = Tmux("emx-test-none", os.devnull)
        with mock.patch.object(Tmux, "run") as run:
            for pane, name in [("12", "ok"), ("%1;x", "ok"), ("%1", "a b"), ("%1", "a;b"), ("%1", "")]:
                with self.assertRaises(TmuxError):
                    tmux.paste_if(pane, "s1", name, "x")
        run.assert_not_called()


class PaneParse(unittest.TestCase):
    def test_the_pane_line_reads_the_pid_and_the_view(self):
        tmux = Tmux("emx-test-unused")
        line = "\t".join(["%3", "@2", "0", "viewer", "", "80", "120", "1", "", "0", "", "4242", "k1"])
        with mock.patch.object(tmux, "run", return_value=line + "\n"):
            (pane,) = tmux.panes()
        self.assertEqual((pane.id, pane.role, pane.pid, pane.view), ("%3", "viewer", 4242, "k1"))

    def test_a_line_with_the_old_eleven_fields_is_skipped(self):
        tmux = Tmux("emx-test-unused")
        with mock.patch.object(tmux, "run", return_value="\t".join(["%3", "@2", "0", "", "", "80", "120", "1", "", "0", ""]) + "\n"):
            self.assertEqual(tmux.panes(), [])

    def test_the_view_targets_the_bar_the_client_state_and_the_view_options(self):
        tmux = Tmux("emx-test-unused", view="emx-view-2")
        with mock.patch.object(tmux, "run", return_value="0\t\t\t\n") as run:
            tmux.set_bar("x")
            tmux.client_state()
            tmux.client_ttys()
            tmux.view_option("@emx_reopen")
            tmux.session_option("@emx_hush")
        targets = [call.args[call.args.index("-t") + 1] for call in run.call_args_list]
        self.assertEqual(targets, ["=emx-view-2:"] * 4 + ["=emx:"])

    def test_an_agent_session_name_needs_a_session_id(self):
        key = str(uuid.uuid4())
        self.assertEqual(Tmux("emx-test-unused").viewer_command(key), f"exec env -u TMUX tmux -L emx-test-unused attach-session -t =emx-a-{key}:")
        for bad in ("s1", "", "x;y", key + " "):
            with self.subTest(bad=bad), self.assertRaises(TmuxError):
                Tmux("emx-test-unused").viewer_command(bad)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class Views(PrivateServer):
    """Agent panes linked into their own sessions and shown in view slots by nested clients of the same server."""

    def setUp(self):
        super().setUp()
        self.tmux.run("set-option", "-g", "remain-on-exit", "on")
        self.key = str(uuid.uuid4())
        recorder = os.path.join(self.dir, "recorder.py")
        self.agent = self.tmux.new_hidden_window(f"{sys.executable} -I {recorder} {self.keys('agent')}")
        self.tmux.set_pane_option(self.agent, AGENT_OPT, engine.AGENT)
        self.tmux.set_pane_option(self.agent, SESSION_OPT, self.key)
        self.window = self.show(self.agent, "#{window_id}")
        self.wait_for(lambda: self.show(self.agent, "#{cursor_x}") == "2", "the agent's prompt")
        # main becomes a view's slot, not a session's pane.
        for opt in (AGENT_OPT, SESSION_OPT, VIEW_OPT):
            self.tmux.unset_pane_option(self.main, opt)

    def clients(self):
        return self.tmux.run("list-clients", "-F", "#{client_name}\t#{client_session}").splitlines()

    def show_in(self, slot, key):
        self.tmux.link_agent(self.window, key, "cat")
        before = len(self.clients())
        self.tmux.respawn(slot, self.tmux.viewer_command(key))
        self.tmux.set_pane_option(slot, ROLE, "viewer")
        self.tmux.set_pane_option(slot, VIEW_OPT, key)
        self.wait_for(lambda: len(self.clients()) > before, "the viewer to attach")
        self.wait_for(lambda: "❯" in self.tmux.capture(slot), "the agent's screen in the slot")

    def second_view(self):
        slot = self.tmux.run(
            "new-session", "-d", "-s", "emx-view-1", "-x", "100", "-y", "30", "-P", "-F", "#{pane_id}",
            f"{sys.executable} -I {os.path.join(self.dir, 'recorder.py')} {self.keys('slot2')}",
        ).strip()
        panel = self.tmux.run(
            "split-window", "-h", "-t", slot, "-P", "-F", "#{pane_id}",
            f"{sys.executable} -I {os.path.join(self.dir, 'recorder.py')} {self.keys('panel2')}",
        ).strip()
        self.tmux.set_pane_option(panel, ROLE, "sidebar")
        return slot, panel, self.attach("emx-view-1")

    def test_linking_keeps_the_pane_and_its_process_and_a_second_link_changes_nothing(self):
        before = {p.id: p.pid for p in self.tmux.panes()}[self.agent]
        self.tmux.link_agent(self.window, self.key, "cat")
        linked = self.tmux.panes(f"emx-a-{self.key}")
        self.assertEqual([(p.id, p.pid, p.dead) for p in linked], [(self.agent, before, False)])
        self.assertIn(self.agent, [p.id for p in self.tmux.panes()])
        options = self.tmux.run("show-options", "-t", f"=emx-a-{self.key}:", "status").strip()
        self.assertEqual(options, "status off")
        sessions = self.tmux.run("list-sessions", "-F", "#{session_name}")
        self.tmux.link_agent(self.window, self.key, "cat")
        self.assertEqual(self.tmux.run("list-sessions", "-F", "#{session_name}"), sessions)
        self.assertEqual([(p.id, p.pid) for p in self.tmux.panes(f"emx-a-{self.key}")], [(self.agent, before)])

    def test_a_viewer_whose_session_pane_is_killed_dies_and_is_never_moved_onto_a_view(self):
        self.show_in(self.main, self.key)
        self.tmux.kill_pane(self.agent)
        self.wait_for(lambda: self.show(self.main, "#{pane_dead}") == "1", "the viewer to exit")
        self.assertEqual([line.split("\t")[1] for line in self.clients()], [SESSION])
        self.assertFalse(self.tmux.has_agent_session(self.key))

    def test_the_toggle_key_acts_only_in_the_view_it_is_pressed_in(self):
        self.tmux.bind_toggle("C-\\")
        slot2, panel2, client2 = self.second_view()
        self.tmux.select(self.main)
        self.tmux.select(slot2)
        self.tmux.run("send-keys", "-K", "-c", client2, "C-\\")
        self.wait_for(lambda: self.show(panel2, "#{pane_active}") == "1", "view B's panel to be selected")
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")
        self.tmux.run("send-keys", "-K", "-c", client2, "C-\\")
        self.wait_for(lambda: self.show(slot2, "#{pane_active}") == "1", "view B's slot to be selected")
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")

    def test_the_collapse_key_zooms_only_the_pressing_views_window(self):
        self.tmux.bind_collapse("C-q")
        slot2, panel2, client2 = self.second_view()
        self.tmux.select(panel2)
        self.tmux.run("send-keys", "-K", "-c", client2, "C-q")
        self.wait_for(lambda: self.show(slot2, "#{window_zoomed_flag}") == "1", "view B to hide its panel")
        self.assertEqual(self.show(slot2, "#{pane_active}"), "1")
        self.assertEqual(self.show(self.main, "#{window_zoomed_flag}"), "0")
        time.sleep(0.3)
        self.assertEqual((self.typed("panel2"), self.typed("slot2")), (b"", b""))

    def test_left_on_a_slot_selects_that_views_panel_or_reaches_the_session_through_the_viewer(self):
        self.show_in(self.main, self.key)
        self.tmux.select(self.main)
        self.tmux.bind_left("false")
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")
        self.wait_for(lambda: self.show(self.sidebar, "#{pane_active}") == "1", "the panel to be selected")
        self.assertEqual(self.show(self.sidebar, "#{" + BACK_OPT + "}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("agent"), b"")
        self.tmux.select(self.main)
        self.tmux.bind_left("true")
        self.tmux.run("send-keys", "-K", "-c", self.client, "Left")
        self.wait_for(lambda: self.typed("agent"), "the key in the session")
        self.assertEqual(self.typed("agent"), LEFT)
        self.assertEqual(self.typed("main"), b"")

    def test_ctrl_v_in_a_viewer_runs_the_paste_check_with_the_sessions_own_pane(self):
        args = os.path.join(self.dir, "paste-args")
        script = os.path.join(self.dir, "paste.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\necho "$@" > {args}\nexit 0\n')
        os.chmod(script, 0o755)
        self.show_in(self.main, self.key)
        self.tmux.select(self.main)
        self.tmux.run("set-option", "-g", RELAY_PASTE_OPT, str(time.time()))
        self.tmux.bind_paste("C-v", script)
        self.tmux.run("send-keys", "-K", "-c", self.client, "C-v")
        self.wait_for(lambda: os.path.exists(args) and os.path.getsize(args), "the paste check to run")
        with open(args, encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), [self.agent, self.key])
        time.sleep(0.3)
        self.assertEqual(self.typed("agent"), b"")

    def test_every_binding_parses_back(self):
        self.tmux.bind_toggle("C-\\")
        self.tmux.bind_collapse("C-q")
        self.tmux.bind_left("true")
        self.tmux.bind_paste("C-v", "true")
        lines = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if line.split()[3] in ("C-\\\\", "C-q", "Left", "C-v")]
        self.assertEqual(len(lines), 4)
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_typing_into_a_linked_session_sees_the_focus_of_a_panel_in_any_view(self):
        self.tmux.link_agent(self.window, self.key, "cat")
        _, panel2, _ = self.second_view()
        for panel in (self.sidebar, panel2):
            with self.subTest(panel=panel):
                self.tmux.select(panel)
                gate = core.type_gate(self.agent, panel, self.key, 0, SESSION_OPT)
                report = self.tmux.type_if(self.agent, gate, "hi", core.TYPED, core.gate_report(SESSION_OPT), enter=False)
                self.assertEqual(report, core.TYPED)
        self.tmux.select(self.main)
        gate = core.type_gate(self.agent, self.sidebar, self.key, 0, SESSION_OPT)
        self.assertNotEqual(self.tmux.type_if(self.agent, gate, "no", core.TYPED, core.gate_report(SESSION_OPT), enter=False), core.TYPED)
        self.wait_for(lambda: self.typed("agent") == b"hihi", "both typings")

    def test_a_mouse_copy_in_a_viewer_reaches_the_terminal_showing_it(self):
        self.tmux.run("set-option", "-g", "mouse", "on")
        self.tmux.bind_copy(cli.self_command("copied", self.tmux.socket))
        self.show_in(self.main, self.key)
        self.tmux.select(self.main)
        seen = bytearray()

        def drain():
            while select.select([self.client_fd], [], [], 0)[0]:
                seen.extend(os.read(self.client_fd, 65536))

        time.sleep(0.3)
        drain()
        seen.clear()
        for event in (b"\x1b[<0;1;1M", b"\x1b[<32;2;1M", b"\x1b[<32;3;1M", b"\x1b[<0;3;1m"):
            os.write(self.client_fd, event)
            time.sleep(0.1)

        def clipboard():
            drain()
            return re.search(rb"\x1b\]52;[a-z]*;([A-Za-z0-9+/=]+)(\x07|\x1b\\\\)", bytes(seen))

        self.wait_for(clipboard, "the clipboard escape on the laptop's terminal")
        copied = base64.b64decode(clipboard().group(1))
        time.sleep(0.5)
        self.assertEqual(copied, self.tmux.run("show-buffer").encode())
        self.assertTrue(copied.startswith("❯".encode()), copied)
        self.assertEqual(len(self.tmux.run("list-buffers", "-F", "#{buffer_name}").split()), 1)

    def test_views_reads_each_views_clients_and_what_its_slot_shows(self):
        self.show_in(self.main, self.key)
        slot2, _, _ = self.second_view()
        views = {v.name: v for v in self.tmux.views()}
        self.assertEqual(sorted(views), [SESSION, "emx-view-1"])
        self.assertEqual((views[SESSION].attached, views[SESSION].shows, views[SESSION].slot), (1, self.key, self.main))
        self.assertEqual((views["emx-view-1"].attached, views["emx-view-1"].shows, views["emx-view-1"].slot), (1, "", slot2))
        self.assertIsNotNone(views[SESSION].activity)


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class MigratesA290Workspace(PrivateServer):
    """`refresh_workspace` on a real server holding 2.9.0's shape: session A's pane swapped into the home view, the placeholder and session B's pane hidden."""

    def setUp(self):
        super().setUp()
        self.tmux.run("set-option", "-g", "remain-on-exit", "on")
        self.a, self.b = str(uuid.uuid4()), str(uuid.uuid4())
        self.tmux.unset_pane_option(self.main, VIEW_OPT)
        self.tmux.set_pane_option(self.main, SESSION_OPT, self.a)
        self.placeholder = self.tmux.new_hidden_window("cat")
        self.tmux.set_pane_option(self.placeholder, ROLE, "placeholder")
        self.other = self.tmux.new_hidden_window("cat")
        self.tmux.set_pane_option(self.other, AGENT_OPT, engine.AGENT)
        self.tmux.set_pane_option(self.other, SESSION_OPT, self.b)
        self.tmux.set_session_option(cli.VERSION_OPT, cli.code_version())
        self.before = self.processes()

    def processes(self):
        return {p.id: (p.pid, p.dead) for p in self.tmux.panes() if p.agent}

    def slot(self):
        window = self.show(self.sidebar, "#{window_id}")
        return next(p for p in self.tmux.panes() if p.window == window and p.id != self.sidebar)

    def test_every_sessions_pane_keeps_its_id_and_process_and_gets_its_own_session(self):
        cli.refresh_workspace(self.tmux)
        self.assertEqual(self.processes(), self.before)
        self.assertEqual(sorted(self.before), sorted([self.main, self.other]))
        for pane, key in ((self.main, self.a), (self.other, self.b)):
            with self.subTest(key=key):
                (linked,) = self.tmux.panes(f"emx-a-{key}")
                self.assertEqual((linked.id, linked.pid, linked.dead), (pane, self.before[pane][0], False))
        slot = self.slot()
        self.assertEqual((slot.id, slot.role), (self.placeholder, "placeholder"))
        self.assertEqual(self.tmux.session_option(cli.REOPEN_OPT), self.a)

    def test_a_second_run_changes_nothing(self):
        cli.refresh_workspace(self.tmux)
        layout = self.tmux.run("list-panes", "-a", "-F", "#{session_name} #{window_id} #{pane_id} #{pane_pid}")
        sessions = self.tmux.run("list-sessions", "-F", "#{session_name}")
        cli.refresh_workspace(self.tmux)
        self.assertEqual(self.tmux.run("list-panes", "-a", "-F", "#{session_name} #{window_id} #{pane_id} #{pane_pid}"), layout)
        self.assertEqual(self.tmux.run("list-sessions", "-F", "#{session_name}"), sessions)

    def test_the_session_that_was_in_the_home_view_shows_there_again_through_a_viewer(self):
        cli.refresh_workspace(self.tmux)
        slot = self.slot()
        self.tmux.respawn(slot.id, self.tmux.viewer_command(self.a))
        self.wait_for(lambda: "❯" in self.tmux.capture(slot.id), "the session's screen in the slot")
        self.assertEqual(self.processes(), self.before)


if __name__ == "__main__":
    unittest.main()
