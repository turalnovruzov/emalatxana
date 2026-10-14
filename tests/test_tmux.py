import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from emx import cli, core
from emx.tmux import BACK_OPT, HUSH_OPT, SESSION, SESSION_OPT, Tmux, TmuxError

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
        self.client_fd = fd
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
class CollapseBinding(PrivateServer):
    """The collapse key as installed on a private tmux server, pressed through an attached client so tmux's key table runs."""

    KEY = "C-q"

    def zoomed(self):
        return self.show(self.main, "#{window_zoomed_flag}")

    def press(self, key=KEY):
        self.tmux.run("send-keys", "-K", "-c", self.client, key)

    def test_the_collapse_binding_parses(self):
        self.tmux.bind_collapse(self.KEY, self.main)
        (line,) = [line for line in self.tmux.run("list-keys", "-T", "root").splitlines() if " C-q " in line]
        listed = os.path.join(self.dir, "listed.conf")
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self.tmux.run("source-file", "-n", listed)

    def test_pressed_in_the_session_hides_and_again_shows(self):
        self.tmux.bind_collapse(self.KEY, self.main)
        self.press()
        self.wait_for(lambda: self.zoomed() == "1", "the panel to hide")
        self.assertEqual(self.show(self.main, "#{pane_width}"), self.show(self.main, "#{window_width}"))
        self.press()
        self.wait_for(lambda: self.zoomed() == "0", "the panel to show")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_pressed_in_the_panel_hides_and_lands_in_the_session(self):
        self.tmux.bind_collapse(self.KEY, self.main)
        self.tmux.select(self.sidebar)
        self.press()
        self.wait_for(lambda: self.zoomed() == "1", "the panel to hide")
        self.assertEqual(self.show(self.main, "#{pane_active}"), "1")
        time.sleep(0.3)
        self.assertEqual(self.typed("sidebar"), b"")

    def test_the_toggle_key_brings_the_panel_back(self):
        self.tmux.bind_collapse(self.KEY, self.main)
        self.tmux.bind_toggle("C-\\", self.sidebar, self.main)
        self.press()
        self.wait_for(lambda: self.zoomed() == "1", "the panel to hide")
        self.press("C-\\")
        self.wait_for(lambda: self.zoomed() == "0", "the panel to show")
        self.assertEqual(self.show(self.sidebar, "#{pane_active}"), "1")

    def test_the_raw_ctrl_q_byte_from_the_terminal_hides(self):
        self.tmux.run("set-option", "-s", "extended-keys", "always")
        self.tmux.run("set-option", "-s", "extended-keys-format", "csi-u")
        self.tmux.bind_collapse(self.KEY, self.main)
        os.write(self.client_fd, b"\x11")
        self.wait_for(lambda: self.zoomed() == "1", "the raw byte to hide the panel")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")
        os.write(self.client_fd, b"\x1b[113;5u")
        self.wait_for(lambda: self.zoomed() == "0", "the CSI-u form to show the panel")
        time.sleep(0.3)
        self.assertEqual(self.typed("main"), b"")

    def test_with_main_gone_the_key_reaches_the_pane(self):
        self.tmux.bind_collapse(self.KEY, self.main)
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

    def test_left_at_an_empty_prompt_brings_the_panel_back(self):
        self.tmux.toggle_zoom(self.main)
        self.tmux.bind_left(self.sidebar, "true", 2)
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


class ShippedConf(unittest.TestCase):
    def test_both_focus_hooks_are_there_and_passthrough_is_not_set_globally(self):
        self.assertIn("set-hook -g client-focus-in 'set-option -t \"=emx:\" @emx_focus 1'", SHELL_CONF)
        self.assertIn("set-hook -g client-focus-out 'set-option -t \"=emx:\" @emx_focus 0'", SHELL_CONF)
        self.assertNotIn("allow-passthrough", SHELL_CONF)


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


if __name__ == "__main__":
    unittest.main()
