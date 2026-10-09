import curses
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from emx import cli, core, sidebar
from emx.tmux import Pane, TmuxError


def git(*args):
    subprocess.run(
        ["git", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    )


class GitPlacement(unittest.TestCase):
    """Runs the real git against throwaway repos, reached through a symlink so git's real paths differ from the cwd's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.real = os.path.realpath(cls.tmp.name)
        os.makedirs(os.path.join(cls.real, "in"))
        os.symlink(os.path.join(cls.real, "in"), os.path.join(cls.real, "link"))
        cls.base = os.path.join(cls.real, "link")
        cls.repo = os.path.join(cls.base, "proj-alpha")
        cls.worktree = os.path.join(cls.base, "alpha-feat-x")
        cls.empty = os.path.join(cls.base, "fresh")
        cls.plain = os.path.join(cls.base, "notes")
        cls.home = os.path.join(cls.base, "homey")
        cls.solo = os.path.join(cls.real, "solo", "alpha-feat-x")
        os.makedirs(os.path.join(cls.real, "solo"))
        os.symlink(os.path.join(cls.real, "in", "alpha-feat-x"), cls.solo)
        os.makedirs(os.path.join(cls.repo, "sub"))
        os.makedirs(cls.plain)
        os.makedirs(os.path.join(cls.home, "scratch"))
        git("init", "-q", "-b", "main", cls.repo)
        git("-C", cls.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git("-C", cls.repo, "worktree", "add", "-q", cls.worktree, "-b", "feat-x")
        git("init", "-q", "-b", "main", cls.empty)
        git("init", "-q", "-b", "main", cls.home)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_main_checkout_and_its_subfolder_in_the_cwd_form(self):
        self.assertEqual(sidebar.git_placement(self.repo), (core.Placement(self.repo), None))
        self.assertEqual(sidebar.git_placement(os.path.join(self.repo, "sub")), (core.Placement(self.repo), None))

    def test_the_real_path_still_groups_by_its_own_form(self):
        real_repo = os.path.join(self.real, "in", "proj-alpha")
        self.assertEqual(sidebar.git_placement(real_repo), (core.Placement(real_repo), None))

    def test_worktree_groups_under_the_main_repo(self):
        self.assertEqual(sidebar.git_placement(self.worktree), (core.Placement(self.repo, "feat-x"), None))

    def test_a_symlinked_worktree_never_groups_under_a_folder_that_does_not_exist(self):
        placement, _ = sidebar.git_placement(self.solo)
        self.assertEqual(placement, core.Placement(os.path.join(self.real, "in", "proj-alpha"), "feat-x"))
        self.assertTrue(os.path.isdir(placement.repo))

    def test_repo_with_no_commits_yet(self):
        self.assertEqual(sidebar.git_placement(self.empty), (core.Placement(self.empty), None))

    def test_a_repo_at_home_does_not_swallow_its_folders(self):
        home_real = os.path.realpath(self.home)
        scratch = os.path.join(self.home, "scratch")
        self.assertEqual(sidebar.git_placement(scratch, home_real), (core.Placement(scratch), None))
        self.assertEqual(sidebar.git_placement(scratch), (core.Placement(self.home), None))
        self.assertEqual(sidebar.git_placement(self.repo, home_real), (core.Placement(self.repo), None))

    def test_non_git_and_missing_folders_are_failures_to_retry(self):
        gone = os.path.join(self.plain, "deleted")
        for cwd in (self.plain, gone, ""):
            with self.subTest(cwd=cwd):
                placement, error = sidebar.git_placement(cwd)
                self.assertIsNone(placement)
                self.assertTrue(error)


class RunGitNeverRaises(unittest.TestCase):
    def test_launch_failures_become_an_error_line(self):
        for exc in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5), PermissionError("no")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                    out, error = sidebar.run_git(tempfile.gettempdir(), "status")
                self.assertIsNone(out)
                self.assertTrue(error)
                with mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                    placement, error = sidebar.git_placement(tempfile.gettempdir())
                self.assertIsNone(placement)
                self.assertTrue(error)

    def test_the_folder_is_never_checked_outside_git(self):
        with mock.patch.object(sidebar.os.path, "isdir", side_effect=AssertionError("stat outside git")), \
                mock.patch.object(sidebar.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(sidebar.run_git("/nowhere", "status")[0], None)


def listed(rows):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")


class Listing(unittest.TestCase):
    def test_lists_with_all_so_stopped_sessions_stay(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=listed([])) as run:
            self.assertEqual(sidebar.list_rows("claude"), ([], None))
        self.assertEqual(run.call_args.args[0], ["claude", "agents", "--json", "--all"])

    def test_a_poll_hands_folders_to_the_resolver_without_running_git(self):
        rows = [{"id": i, "kind": "background", "cwd": "/a", "pid": 1} for i in ("x", "y")]
        poller = sidebar.Poller("claude")
        with mock.patch.object(sidebar.subprocess, "run", return_value=listed(rows)), \
                mock.patch.object(sidebar, "git_placement", side_effect=AssertionError("git in the poll")):
            sessions, error = poller.poll()
        self.assertEqual((len(sessions), error), (2, None))
        self.assertEqual(poller.resolver.wanted, {"/a"})


class Resolving(unittest.TestCase):
    def test_each_folder_resolves_once(self):
        resolver = sidebar.Resolver("/h")
        resolver.want(["/a", "/a", "/b"])
        with mock.patch.object(sidebar, "git_placement", return_value=(core.Placement("/r"), None)) as placement:
            for cwd in resolver.due(time.monotonic()):
                resolver.resolve(cwd)
            self.assertEqual(resolver.due(time.monotonic()), [])
        self.assertEqual(placement.call_count, 2)
        self.assertEqual(resolver.take(), {"/a": core.Placement("/r"), "/b": core.Placement("/r")})
        self.assertIsNone(resolver.take())

    def test_a_failure_is_retried_later_with_backoff_not_remembered(self):
        resolver = sidebar.Resolver("/h")
        resolver.want(["/a"])
        now = time.monotonic()
        with mock.patch.object(sidebar, "git_placement", return_value=(None, "git timed out")):
            resolver.resolve("/a")
            self.assertEqual(resolver.due(now), [])
            self.assertEqual(resolver.due(now + core.retry_after(1) + 1), ["/a"])
            resolver.resolve("/a")
            self.assertEqual(resolver.due(now + core.retry_after(1) + 1), [])
            self.assertEqual(resolver.due(now + core.retry_after(2) + 1), ["/a"])
        self.assertIsNone(resolver.take())
        with mock.patch.object(sidebar, "git_placement", return_value=(core.Placement("/r"), None)):
            resolver.resolve("/a")
        self.assertEqual(resolver.take(), {"/a": core.Placement("/r")})
        self.assertNotIn("/a", resolver.failures)

    def test_folders_of_sessions_no_longer_listed_are_not_asked_about(self):
        resolver = sidebar.Resolver("/h")
        resolver.want(["/a", "/b"])
        resolver.want(["/a"])
        self.assertEqual(resolver.due(time.monotonic()), ["/a"])
        self.assertEqual(resolver.wanted, {"/a"})

    def test_a_crash_in_git_counts_as_a_failure(self):
        resolver = sidebar.Resolver("/h")
        with mock.patch.object(sidebar, "git_placement", side_effect=RuntimeError("boom")):
            resolver.resolve("/a")
        self.assertEqual(resolver.failures["/a"][0], 1)


def session(sid, cwd, state="working", pid=1, started=0.0):
    return core.Session(sid, f"emx-test-{sid}", cwd, state, "", "", started, pid is not None and state != "stopped")


class FakeScr:
    def __init__(self, keys=()):
        self.keys = list(keys)

    def getmaxyx(self):
        return 20, 40

    def erase(self):
        pass

    def addstr(self, *args):
        pass

    def noutrefresh(self):
        pass

    def timeout(self, ms):
        pass

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        key = self.keys.pop(0)
        return key() if callable(key) else key


class FakePalette:
    def attr(self, tone, selected=False):
        return 0


class ClientTmux:
    """The two tmux calls a listing makes for notices; anything else is an error, as it is with no tmux at all."""

    def __init__(self, state=(0, "", "", None)):
        self.state = state
        self.passthrough = []

    def client_state(self):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    def allow_passthrough(self, pane):
        self.passthrough.append(pane)


class AppCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name, "TMUX_PANE": "%1"}),
            mock.patch.object(sidebar, "Palette", FakePalette),
            mock.patch.object(sidebar.curses, "doupdate", lambda: None),
            # A worker a test didn't mean to start must never run his real claude.
            mock.patch.object(sidebar, "fetch_sessions", lambda claude: (None, "no claude in tests")),
            # No test may speak, show a banner, ask Haiku or write to the real terminal.
            mock.patch.object(sidebar, "announce", self.recorder("announced", method=False, result=(None, "no haiku in tests"))),
            mock.patch.object(sidebar, "list_voices", lambda say: []),
            mock.patch.object(sidebar.Voice, "speak", self.recorder("spoken")),
            mock.patch.object(sidebar.Voice, "banner", self.recorder("banners")),
            mock.patch.object(sidebar.Voice, "silence", self.recorder("silenced")),
            mock.patch.object(sidebar.App, "write_out", self.recorder("written")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.scr = FakeScr()
        self.app = sidebar.App(self.scr, tmux=ClientTmux(), claude="claude")
        self.app.housekeep = lambda fresh_list: None
        self.opened = []
        self.app.switch_to = lambda sid, trigger, checked=None: self.opened.append(
            (sid, trigger) if checked is None else (sid, trigger, checked)
        )

    def recorder(self, name, method=True, result=None):
        calls = []
        setattr(self, name, calls)

        def record(*args, **kwargs):
            calls.append(args[1:] if method else args)
            return result

        return record

    def poll(self, *sessions):
        self.app.poller.result = (list(sessions), None)


class QueuedClicks(AppCase):
    """A click waits in the input queue while emx is busy; it must open the row he clicked, not the row there now."""

    def test_a_click_queued_behind_a_slow_key_maps_through_the_frame_he_saw(self):
        alpha = session("a", "/p/alpha")
        self.poll(alpha, session("b", "/p/beta"))
        self.app.refresh()
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "a"))

        def slow_key():
            self.poll(session("z", "/p/aaa"), alpha, session("b", "/p/beta"))
            return "x"

        self.scr.keys = [slow_key, curses.KEY_MOUSE]
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.app.read_input()
            self.app.refresh()
            self.app.read_input()
        self.assertEqual(self.opened, [("a", core.OPEN)])
        self.assertEqual(self.app.row_map[y], ("session", "z"))


class AttachChecks(AppCase):
    """This thread never lists: OPEN judges the row he sees, every other trigger the (session, fresh) a worker handed over."""

    def setUp(self):
        super().setUp()
        self.poll(session("s1", "/p/alpha"))
        self.app.refresh()
        no_listing = mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("listed on the main thread"))
        no_listing.start()
        self.addCleanup(no_listing.stop)

    def test_open_uses_the_row_he_sees(self):
        self.assertTrue(self.app.confirm_attach("s1", core.OPEN))

    def test_other_triggers_attach_only_on_a_checked_running_row(self):
        running, stopped = session("s1", "/p/alpha"), session("s1", "/p/alpha", state="stopped", pid=None)
        for trigger in (core.RESTART, core.NEW):
            with self.subTest(trigger=trigger):
                self.assertTrue(self.app.confirm_attach("s1", trigger, (running, True)))
                self.assertFalse(self.app.confirm_attach("s1", trigger, (stopped, True)))
                self.assertIn("isn't running", self.app.notice)
                self.assertFalse(self.app.confirm_attach("s1", trigger, (None, False)))
                self.assertIn("Couldn't list sessions", self.app.notice)
                self.assertFalse(self.app.confirm_attach("s1", trigger))

    def test_refused_while_the_panels_own_polling_is_failing(self):
        self.app.list_error = "listing broken"
        self.assertFalse(self.app.confirm_attach("s1", core.NEW, (session("s1", "/p/alpha"), True)))
        self.assertIn("Couldn't list sessions", self.app.notice)

    def test_the_refusal_names_the_checked_row_when_the_panel_has_none(self):
        exited = core.Session("new1", "emx-test-new1", "/p", "done", "", "", None, False)
        self.assertFalse(self.app.confirm_attach("new1", core.NEW, (exited, True)))
        self.assertTrue(self.app.notice.startswith("emx-test-new1 isn't running"), self.app.notice)


class FakeTmux:
    def __init__(self):
        self.side = Pane("%1", "@1", False, "sidebar", "", 40, 121)
        self.main = Pane("%2", "@1", False, "", "s1", 80, 121)
        self.respawned = []

    def panes(self):
        return [self.side, self.main]

    def respawn(self, pane, command):
        self.respawned.append(pane)

    def select(self, pane):
        pass


class RestartOffThread(AppCase):
    """`r` lists on a worker thread; the panel keeps drawing, and the answer acts only if it is still about what's in main."""

    def setUp(self):
        super().setUp()
        self.app.tmux = FakeTmux()
        self.app.main_pane = self.app.tmux.main
        self.poll(session("s1", "/p/alpha"), session("s2", "/p/alpha"))
        self.app.refresh()
        self.gate = threading.Event()
        self.calls = []
        self.answer = ([session("s1", "/p/alpha")], None)

        def fetch(claude):
            self.calls.append(threading.current_thread() is not threading.main_thread())
            self.gate.wait(5)
            return self.answer

        p = mock.patch.object(sidebar, "fetch_sessions", fetch)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.gate.set)

    def press_r(self):
        self.app.handle_key("r")

    def answer_arrives(self):
        checker = self.app.checker
        self.gate.set()
        checker.join(5)
        self.app.refresh()

    def test_repeated_r_runs_one_check_and_the_panel_keeps_drawing(self):
        for _ in range(3):
            self.press_r()
        deadline = time.monotonic() + 5
        while not self.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.app.notice.startswith("Checking emx-test-s1"), self.app.notice)
        started = time.monotonic()
        self.app.refresh()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.app.tmux.respawned, [])
        self.answer_arrives()
        self.assertEqual(self.calls, [True])
        self.assertEqual(self.app.tmux.respawned, ["%2"])
        self.assertEqual(self.app.notice, "Viewer restarted.")
        self.assertIsNone(self.app.checker)

    def test_a_session_being_closed_is_not_restarted(self):
        self.press_r()
        self.app.closing["s1"] = None
        self.answer_arrives()
        self.assertEqual(self.app.tmux.respawned, [])
        self.assertEqual(self.app.notice, "Still closing emx-test-s1…")

    def test_a_session_stopped_since_the_poll_is_left_alone(self):
        self.answer = ([session("s1", "/p/alpha", state="stopped", pid=None)], None)
        self.press_r()
        self.answer_arrives()
        self.assertEqual(self.app.tmux.respawned, [])
        self.assertIn("isn't running", self.app.notice)

    def test_a_failed_or_timed_out_listing_refuses(self):
        self.answer = (None, "claude agents --json timed out")
        self.press_r()
        self.answer_arrives()
        self.assertEqual(self.app.tmux.respawned, [])
        self.assertIn("Couldn't list sessions", self.app.notice)

    def test_an_answer_about_what_main_no_longer_shows_is_dropped(self):
        for changed in (Pane("%2", "@1", False, "", "s2", 80, 121), Pane("%3", "@1", False, "", "s1", 80, 121)):
            with self.subTest(main=changed):
                self.app.tmux.main = self.app.main_pane = FakeTmux().main
                self.gate.clear()
                self.press_r()
                self.app.tmux.main = changed
                self.answer_arrives()
                self.assertEqual(self.app.tmux.respawned, [])
                self.assertIn("main pane changed", self.app.notice)

    def test_no_check_while_the_panels_own_polling_is_failing(self):
        self.app.list_error = "listing broken"
        self.press_r()
        self.assertIsNone(self.app.checker)
        self.assertEqual(self.calls, [])
        self.assertIn("Couldn't list sessions", self.app.notice)


class StartWaitsUntilRunning(AppCase):
    def rows(self, *pids):
        return [([{"id": "new1", "kind": "background", "state": "working", **({"pid": p} if p else {})}], None)
                for p in pids]

    def test_the_first_listing_without_a_pid_is_waited_out(self):
        with mock.patch.object(sidebar, "list_rows", side_effect=self.rows(None, None, 4343)), \
                mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0):
            found, error = sidebar.wait_until_running("claude", "new1")
        self.assertEqual((found.id, found.running, error), ("new1", True, None))

    def test_listed_but_never_running_gives_up_saying_so(self):
        with mock.patch.object(sidebar, "list_rows", side_effect=self.rows(None) * 3), \
                mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0):
            found, error = sidebar.wait_until_running("claude", "new1")
        self.assertIsNone(found)
        self.assertIn("isn't running", error)
        with mock.patch.object(sidebar, "list_rows", return_value=([], None)), \
                mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0):
            self.assertIn("doesn't list it", sidebar.wait_until_running("claude", "new1")[1])

    def test_the_starters_listing_is_the_fresh_one_the_viewer_opens_on(self):
        new = session("new1", "/p/alpha")
        done = sidebar.Starter("claude", "/p/alpha", "")
        done.result = (new, None)
        self.app.starter = done
        self.app.finish_start()
        self.assertEqual(self.opened, [("new1", core.NEW, (new, True))])


class CursorAfterStart(AppCase):
    def test_cursor_stays_put_until_the_new_session_is_listed(self):
        a, b = session("a", "/p/alpha"), session("b", "/p/alpha", started=1.0)
        self.poll(a, b)
        self.app.refresh()
        self.app.cursor = ("session", "b")
        self.app.wanted_cursor = ("session", "new")
        self.poll(a, b)
        self.app.refresh()
        self.assertEqual(self.app.cursor, ("session", "b"))
        self.poll(a, b, session("new", "/p/zeta", started=2.0))
        self.app.refresh()
        self.assertEqual((self.app.cursor, self.app.wanted_cursor), (("session", "new"), None))


FOCUS_IN, FOCUS_OUT = ["\x1b", "[", "I"], ["\x1b", "[", "O"]


class FocusTmux:
    """The sidebar (%1) and main (%2); `panel_active` is which of them tmux says is active."""

    def __init__(self):
        self.panel_active = True
        self.back = False
        self.cleared = []

    def panes(self):
        return [
            Pane("%1", "@1", False, "sidebar", "", 40, 121, self.panel_active, self.back),
            Pane("%2", "@1", False, "", "b", 80, 121, not self.panel_active),
        ]

    def clear_back(self, pane):
        self.cleared.append(pane)
        self.back = False


class Focus(AppCase):
    """tmux reports focus as three keys; they never reach handle_key, and focus itself is read from tmux's active pane."""

    def setUp(self):
        super().setUp()
        self.poll(session("a", "/p/alpha"), session("b", "/p/alpha", started=1.0), session("c", "/p/beta"))
        self.app.refresh()
        self.app.tmux = FocusTmux()
        self.app.main_pane = self.app.tmux.panes()[1]
        self.app.cursor = ("session", "c")

    def read(self, *keys):
        self.scr.keys = list(keys)
        self.app.read_input()

    def leave_panel(self):
        self.app.tmux.panel_active = False
        self.read(*FOCUS_OUT)

    def come_back(self, *then, by_left=False):
        self.app.tmux.panel_active = True
        self.app.tmux.back = by_left
        self.read(*FOCUS_IN, *then)

    def test_coming_back_with_left_moves_the_cursor_to_the_session_in_main(self):
        self.leave_panel()
        self.assertIs(self.app.focused, False)
        self.assertEqual(self.app.cursor, ("session", "c"))
        self.come_back(by_left=True)
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "b"))
        self.assertEqual(self.app.tmux.cleared, ["%1"])
        self.assertEqual(self.opened, [])

    def test_coming_back_with_ctrl_backslash_or_a_click_leaves_the_cursor_where_he_left_it(self):
        self.leave_panel()
        self.come_back()
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "c"))
        self.assertEqual(self.app.tmux.cleared, [])

    def test_a_left_mark_seen_while_the_panel_is_inactive_is_dropped_and_never_moves_the_cursor(self):
        # ← marked the panel, then Ctrl+\ took focus back to main before the panel read it.
        self.leave_panel()
        self.app.tmux.back = True
        self.app.read_focus()
        self.assertEqual((self.app.focused, self.app.tmux.cleared), (False, ["%1"]))
        self.app.tmux.panel_active = True
        self.app.read_focus()
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "c"))
        self.assertEqual(self.app.tmux.cleared, ["%1"])

    def test_a_mark_tmux_fails_to_clear_still_records_focus(self):
        def refuse(pane):
            raise sidebar.TmuxError("no such pane")

        self.leave_panel()
        self.app.tmux.clear_back = refuse
        self.come_back(by_left=True)
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "b"))
        self.assertIn("tmux: no such pane", self.app.notice)

    def test_leaving_the_panel_cancels_an_armed_ctrl_x_but_his_terminal_losing_focus_does_not(self):
        self.app.handle_key(sidebar.CTRL_X)
        # c is working, so the press also starts an interrupt; it is let finish before the test ends.
        self.app.interrupters["c"].join(5)
        self.read(*FOCUS_OUT)
        self.assertEqual(self.app.confirm.id, "c")
        self.leave_panel()
        self.assertIsNone(self.app.confirm)

    def test_his_terminal_losing_and_regaining_focus_changes_nothing(self):
        self.read(*FOCUS_IN)
        self.read(*FOCUS_OUT)
        self.assertIs(self.app.focused, True)
        self.read(*FOCUS_IN)
        self.assertEqual(self.app.cursor, ("session", "c"))

    def test_a_click_right_after_focus_in_opens_the_row_clicked(self):
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "a"))
        self.leave_panel()
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.come_back(curses.KEY_MOUSE)
        self.assertEqual(self.opened, [("a", core.OPEN)])
        self.assertEqual(self.app.cursor, ("session", "a"))

    def test_a_report_never_reaches_the_prompt_or_closes_help(self):
        self.app.open_prompt()
        text = self.app.prompt["text"]
        self.leave_panel()
        self.come_back()
        self.assertEqual(self.app.prompt["text"], text)
        self.app.prompt = None
        self.app.help = True
        self.leave_panel()
        self.assertTrue(self.app.help)

    def test_esc_and_keys_after_it_still_arrive(self):
        self.app.open_prompt()
        self.read("\x1b")
        self.assertIsNone(self.app.prompt)
        self.app.open_prompt()
        self.read("\x1b", "[", "x")
        self.assertIsNone(self.app.prompt)
        self.assertEqual(self.app.pending_keys, [])

    def test_a_key_typed_here_means_the_panel_has_focus_without_moving_the_cursor(self):
        self.app.cursor = ("session", "a")
        self.leave_panel()
        self.read("j")
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "b"))


class ArrowKeys(AppCase):
    def setUp(self):
        super().setUp()
        self.poll(session("a", "/p/alpha"))
        self.app.refresh()

    def test_right_opens_the_session_under_the_cursor(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(curses.KEY_RIGHT)
        self.assertEqual(self.opened, [("a", core.OPEN)])

    def test_right_on_a_group_header_does_nothing(self):
        self.app.cursor = ("group", "/p/alpha")
        self.app.handle_key(curses.KEY_RIGHT)
        self.assertEqual(self.opened, [])
        self.assertNotIn("/p/alpha", self.app.collapsed())

    def test_footer_and_help_show_the_arrows(self):
        help_keys = " ".join(key for key, _ in sidebar.HELP)
        for arrow in ("←", "→"):
            with self.subTest(arrow=arrow):
                self.assertIn(arrow, sidebar.FOOTERS[0])
                self.assertIn(arrow, help_keys)


class AutoRestoreFakeTmux(FakeTmux):
    def __init__(self):
        super().__init__()
        self.selected = []

    def select(self, pane):
        self.selected.append(pane)

    def capture(self, pane):
        return self.screen


class AutoRestore(AppCase):
    """Main caught showing Claude Code's agent view is brought back after a fresh listing, and focus goes to the panel."""

    def setUp(self):
        super().setUp()
        self.app.tmux = AutoRestoreFakeTmux()
        self.app.main_pane = self.app.tmux.main
        self.poll(session("s1", "/p/alpha"))
        self.app.refresh()
        self.answer = ([session("s1", "/p/alpha")], None)
        p = mock.patch.object(sidebar, "fetch_sessions", lambda claude: self.answer)
        p.start()
        self.addCleanup(p.stop)

    def show(self, screen_name):
        (path,) = (Path(__file__).parent / "fixtures" / "screens").glob(f"{screen_name}.w*.txt")
        text, width = path.read_text(encoding="utf-8"), int(path.stem.rsplit(".w", 1)[1])
        self.app.tmux.screen = text
        self.app.tmux.main = Pane("%2", "@1", False, "", "s1", width, 121)
        self.app.main_pane = self.app.tmux.main

    def settle(self):
        self.app.checker.join(5)
        self.app.finish_check()

    def test_agent_view_is_restored_and_the_panel_focused_without_the_r_warning(self):
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.assertTrue(self.app.checker.auto)
        self.assertNotIn("press r", " ".join(t for t, _ in self.app.bottom_lines(60)))
        self.settle()
        self.assertEqual(self.app.tmux.respawned, ["%2"])
        self.assertEqual(self.app.tmux.selected, ["%1"])
        self.assertIn("brought the session back", self.app.notice)

    def test_the_restore_is_a_way_back_so_the_cursor_goes_to_mains_session(self):
        self.app.focused, self.app.cursor = False, ("group", "/p/alpha")
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.settle()
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "s1"))

    def test_a_refusal_is_said_once_and_the_warning_stays(self):
        self.answer = ([session("s1", "/p/alpha", state="stopped", pid=None)], None)
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.settle()
        self.assertEqual(self.app.tmux.respawned, [])
        self.assertIn("isn't running", self.app.notice)
        self.app.check_main(self.app.main_pane)
        self.assertIsNone(self.app.checker)
        self.assertIn("press r", " ".join(t for t, _ in self.app.bottom_lines(60)))

    def test_a_session_being_closed_is_not_restored(self):
        self.app.closing["s1"] = None
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.assertIsNone(self.app.checker)
        self.assertEqual(self.app.tmux.respawned, [])

    def test_a_close_that_starts_while_the_restore_lists_stops_the_restore(self):
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.assertTrue(self.app.checker.auto)
        self.app.closing["s1"] = None
        self.settle()
        self.assertEqual(self.app.tmux.respawned, [])
        self.assertEqual(self.app.tmux.selected, [])
        self.assertEqual(self.app.notice, "Still closing emx-test-s1…")

    def test_another_session_showing_is_left_to_him(self):
        self.app.sessions = [core.Session("s1", "emx-test-named", "/p/alpha", "working", "", "", 0.0, True)]
        self.show("cross-wired")
        self.app.check_main(self.app.main_pane)
        self.assertIsNone(self.app.checker)
        self.assertIn("press r", " ".join(t for t, _ in self.app.bottom_lines(60)))

    def test_r_while_it_is_restoring_focuses_main_as_r_always_does(self):
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.app.handle_key("r")
        self.settle()
        self.assertEqual(self.app.tmux.selected, ["%2"])

    def test_a_restore_is_dropped_when_main_has_left_the_agent_view_by_the_answer(self):
        # He picked another session in Claude Code's agent view, or kept working, while the listing ran.
        for since in ("named-viewer", "working-streaming", "permission-dialog"):
            with self.subTest(main_now_shows=since):
                self.app.tmux.respawned, self.app.tmux.selected = [], []
                self.app.flagged_viewers = self.app.auto_tried = frozenset()
                self.app.last_restore = None
                self.show("agent-view")
                self.app.check_main(self.app.main_pane)
                self.assertIsNotNone(self.app.checker)
                self.show(since)
                self.settle()
                self.assertEqual(self.app.tmux.respawned, [])
                self.assertEqual(self.app.tmux.selected, [])
                self.assertNotIn("bringing back", self.app.notice)

    def restore_cycles(self, screens):
        """Each screen is a fixture name, or "" for a viewer still starting, which reads "unknown"."""
        for screen in screens:
            if screen:
                self.show(screen)
            else:
                self.app.tmux.screen = ""
            self.app.check_main(self.app.main_pane)
            if self.app.checker is not None:
                self.settle()

    def test_the_agent_view_back_right_after_a_restore_stops_restoring_and_warns(self):
        self.restore_cycles(["agent-view", ""] * 4)
        self.assertEqual(self.app.tmux.respawned, ["%2"])
        self.assertEqual(self.app.tmux.selected, ["%1"])
        shown = " ".join(t for t, _ in self.app.bottom_lines(60))
        self.assertIn("press r", shown)
        self.assertNotIn("brought the session back", shown)

    def test_an_ok_read_after_a_restore_lets_the_next_agent_view_restore_again(self):
        self.app.sessions = [core.Session("s1", "emx-test-named", "/p/alpha", "working", "", "", 0.0, True)]
        self.restore_cycles(["agent-view", "named-viewer", "agent-view"])
        self.assertEqual(self.app.tmux.respawned, ["%2", "%2"])

    def age_last_restore(self):
        pane, _, count = self.app.last_restore
        self.app.last_restore = (pane, time.monotonic() - core.RESTORE_BACKOFF_SECONDS - 1, count)

    def test_after_the_backoff_window_the_agent_view_is_restored_again(self):
        self.restore_cycles(["agent-view"])
        self.age_last_restore()
        self.restore_cycles(["", "agent-view"])
        self.assertEqual(self.app.tmux.respawned, ["%2", "%2"])

    def test_the_agent_view_back_after_a_second_restore_stops_restoring_whatever_the_time(self):
        self.restore_cycles(["agent-view"])
        self.age_last_restore()
        self.restore_cycles(["", "agent-view"])
        self.age_last_restore()
        self.restore_cycles(["", "agent-view"])
        self.assertEqual(self.app.tmux.respawned, ["%2", "%2"])
        self.assertIn("press r", " ".join(t for t, _ in self.app.bottom_lines(60)))

    def test_the_restored_notice_goes_by_itself(self):
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.settle()
        self.assertIn("brought the session back", self.app.notice)
        self.app.refresh()
        self.assertIn("brought the session back", self.app.notice)
        self.app.notice_until = time.monotonic() - 1
        self.app.refresh()
        self.assertEqual(self.app.notice, "")

    def test_a_later_notice_is_not_cut_short_by_the_restored_notices_timer(self):
        self.show("agent-view")
        self.app.check_main(self.app.main_pane)
        self.settle()
        self.app.say("Session started.")
        self.app.refresh()
        self.assertEqual(self.app.notice, "Session started.")
        self.assertIsNone(self.app.notice_until)


class CloseTmux:
    """The sidebar (%1), main (%2) showing `a`, a hidden viewer (%3) of `b` and the placeholder (%4) in its own window."""

    def __init__(self):
        self.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 121),
            Pane("%2", "@1", False, "", "a", 80, 121),
            Pane("%3", "@2", False, "", "b", 80, 80),
            Pane("%4", "@3", False, "placeholder", "", 80, 80),
        ]
        self.calls = []

    def panes(self):
        return list(self.list)

    def swap(self, source, target):
        self.calls.append(("swap", source, target))

    def kill_pane(self, pane):
        self.calls.append(("kill", pane))

    def client_state(self):
        return 0, "", "", None


class CloseStaleOnListings(AppCase):
    def setUp(self):
        super().setUp()
        self.tmux = CloseTmux()
        self.app.tmux = self.tmux
        self.app.sessions = []

    def run_close(self, times):
        for _ in range(times):
            panes = self.tmux.panes()
            main = next(p for p in panes if p.id == "%2")
            self.app.close_stale(panes, main)

    def make_dead(self, pane_id):
        self.tmux.list = [replace(p, dead=True) if p.id == pane_id else p for p in self.tmux.list]

    def test_empty_listings_never_close_live_viewers(self):
        self.run_close(core.STALE_AFTER_MISSES + 1)
        self.assertEqual(self.tmux.calls, [])

    def test_empty_listings_close_a_dead_viewer(self):
        self.make_dead("%3")
        self.run_close(core.STALE_AFTER_MISSES)
        self.assertIn(("kill", "%3"), self.tmux.calls)
        self.assertNotIn(("kill", "%2"), self.tmux.calls)

    def test_a_dead_viewer_in_main_is_swapped_for_the_placeholder(self):
        self.make_dead("%2")
        self.run_close(core.STALE_AFTER_MISSES)
        self.assertEqual(self.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2")])

    def test_a_listing_without_the_session_still_closes_its_viewer(self):
        self.app.sessions = [session("b", "/tmp/b")]
        self.run_close(2)
        self.assertIn(("swap", "%4", "%2"), self.tmux.calls)
        self.assertIn(("kill", "%2"), self.tmux.calls)
        self.assertNotIn(("kill", "%3"), self.tmux.calls)


class EmptyListing(AppCase):
    def test_the_cursor_returns_to_its_row_after_an_empty_listing(self):
        rows = [session(x, "/tmp/x") for x in "abc"]
        self.poll(*rows)
        self.app.take_poll()
        self.app.cursor = ("session", "b")
        self.poll()
        self.app.take_poll()
        self.assertIsNone(self.app.cursor)
        self.poll(*rows)
        self.app.take_poll()
        self.assertEqual(self.app.cursor, ("session", "b"))

    def test_after_an_empty_listing_a_gone_row_leaves_the_cursor_on_its_neighbour(self):
        rows = [session(x, "/tmp/x") for x in "abc"]
        self.poll(*rows)
        self.app.take_poll()
        self.app.cursor = ("session", "b")
        self.poll()
        self.app.take_poll()
        self.poll(rows[0], rows[2])
        self.app.take_poll()
        self.assertEqual(self.app.cursor, ("session", "c"))

    def test_a_pending_rename_survives_an_empty_listing(self):
        self.poll(session("a", "/tmp/x"))
        self.app.take_poll()
        self.app.renaming = {"a": ("old", "new", time.monotonic() + 30)}
        self.poll()
        self.app.take_poll()
        self.assertIn("a", self.app.renaming)


class CloseSession(AppCase):
    """Ctrl+X twice on one row closes that session (stop if running, then remove), on a worker thread, after a fresh listing agrees with the row.

    The rows wait for input, so the first press only arms: interrupting a working row is InterruptSession's.
    """

    def setUp(self):
        super().setUp()
        self.a, self.b = session("a", "/p/alpha", state="blocked"), session("b", "/p/alpha", state="blocked", started=1.0)
        self.poll(self.a, self.b)
        self.app.refresh()
        self.app.tmux = CloseTmux()
        self.app.cursor = ("session", "a")
        self.listing = ([self.a, self.b], None)
        self.ran = []
        self.done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        self.failing = {}

        def run(args, **kw):
            self.ran.append((args, threading.current_thread() is not threading.main_thread()))
            return self.failing.get(args[1], self.done)

        def fetch(claude):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            return self.listing

        for p in (mock.patch.object(sidebar.subprocess, "run", run), mock.patch.object(sidebar, "fetch_sessions", fetch)):
            p.start()
            self.addCleanup(p.stop)

    def press(self, *keys):
        for key in keys:
            self.app.handle_key(key)

    def settle(self):
        if self.app.closer is not None:
            self.app.closer.join(5)
        self.app.refresh()

    def drawn(self):
        drawn = []
        self.app.put = lambda y, x, text, attr=0: drawn.append(text)
        self.app.draw()
        del self.app.put
        return drawn

    def stopped(self, sid, started=0.0):
        return session(sid, "/p/alpha", state="stopped", pid=None, started=started)

    def test_double_ctrl_x_on_a_running_row_stops_then_removes_it_off_the_curses_thread(self):
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.confirm.id, "a")
        self.assertEqual(self.app.interrupters, {})
        self.assertIn(sidebar.CONFIRM_FOOTER, [t for t, _ in self.app.bottom_lines(39)])
        self.assertEqual(self.ran, [])
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Closing emx-test-a…")
        self.assertIsNone(self.app.confirm)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "stop", "a"], True), (["claude", "rm", "a"], True)])
        self.assertEqual((self.app.notice, self.app.notice_error), ("Closed emx-test-a.", False))
        self.assertTrue(self.app.poller.wake.is_set())
        self.assertEqual(self.app.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2")])

    def test_double_ctrl_x_on_a_stopped_session_removes_it_and_closes_its_viewer_in_main(self):
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.listing = ([self.stopped("a"), self.b], None)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "rm", "a"], True)])
        self.assertEqual(self.app.notice, "Closed emx-test-a.")
        self.assertEqual(self.app.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2")])

    def test_a_worktree_rm_left_behind_shows_after_closed_on_one_line(self):
        self.done = subprocess.CompletedProcess(
            [], 0, stdout="removed a\nworktree directory left at /x/wt (git no longer recognized it)\n", stderr="",
        )
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(
            (self.app.notice, self.app.notice_error),
            ("Closed emx-test-a. worktree directory left at /x/wt (git no longer recognized it)", False),
        )
        self.assertEqual(self.app.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2")])

    def test_removing_a_session_kills_its_hidden_viewer(self):
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.app.tmux.calls, [("kill", "%3")])

    def test_a_refused_rm_shows_its_message_on_one_line_and_leaves_the_row(self):
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.listing = ([self.stopped("a"), self.b], None)
        self.done = subprocess.CompletedProcess([], 1, stdout="", stderr="worktree has 2 unpushed commits\nrerun with a flag\n")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "rm", "a"], True)])
        self.assertEqual((self.app.notice, self.app.notice_error), ("worktree has 2 unpushed commits rerun with a flag", True))
        self.assertEqual(self.app.tmux.calls, [])
        self.assertIsNotNone(self.app.session("a"))
        self.assertEqual(self.app.closing, {})

    def test_a_failed_rm_drops_closing_and_shows_the_message_on_one_line(self):
        self.failing["rm"] = subprocess.CompletedProcess([], 1, stdout="", stderr="worktree has 2 unpushed commits\nrerun with a flag\n")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertIn("a", self.app.closing)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "stop", "a"], True), (["claude", "rm", "a"], True)])
        self.assertEqual(
            (self.app.notice, self.app.notice_error),
            ("Stopped emx-test-a, but didn't remove it: worktree has 2 unpushed commits rerun with a flag", True),
        )
        self.assertEqual(self.app.closing, {})
        self.assertEqual(self.app.tmux.calls, [])
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.assertNotIn("closing…", self.drawn())

    def test_a_failed_stop_never_removes(self):
        self.failing["stop"] = subprocess.CompletedProcess([], 1, stdout="", stderr="couldn't stop it\n")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "stop", "a"], True)])
        self.assertEqual((self.app.notice, self.app.notice_error), ("couldn't stop it", True))
        self.assertEqual(self.app.closing, {})

    def test_a_refusal_on_stdout_alone_shows_on_one_line_and_leaves_the_row(self):
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.listing = ([self.stopped("a"), self.b], None)
        self.done = subprocess.CompletedProcess([], 1, stdout="kept a — its worktree is still at /x/wt", stderr="")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual((self.app.notice, self.app.notice_error), ("kept a — its worktree is still at /x/wt", True))
        self.assertEqual(self.app.tmux.calls, [])
        self.assertIsNotNone(self.app.session("a"))

    def test_a_stopped_row_resumed_elsewhere_is_never_removed(self):
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [])
        self.assertIn("running again", self.app.notice)
        self.assertEqual(self.app.closing, {})

    def test_moving_between_presses_acts_on_neither_row(self):
        self.press(sidebar.CTRL_X, curses.KEY_DOWN, sidebar.CTRL_X)
        self.assertIsNone(self.app.closer)
        self.assertEqual(self.app.confirm.id, "b")
        self.assertEqual(self.ran, [])

    def test_any_other_key_cancels(self):
        for key in ("x", "?", curses.KEY_MOUSE, "\x1b"):
            with self.subTest(key=key):
                self.app.help = False
                self.press(sidebar.CTRL_X, key)
                self.assertIsNone(self.app.confirm)

    def test_a_resize_keeps_the_confirm(self):
        self.press(sidebar.CTRL_X, curses.KEY_RESIZE, sidebar.CTRL_X)
        self.assertIsNotNone(self.app.closer)
        self.settle()

    def test_the_confirm_runs_out_and_the_next_press_only_rearms(self):
        self.press(sidebar.CTRL_X)
        armed = self.app.confirm
        with mock.patch.object(sidebar.time, "monotonic", return_value=armed.at + core.CONFIRM_SECONDS + 1):
            self.app.refresh()
            self.assertIsNone(self.app.confirm)
            self.press(sidebar.CTRL_X)
        self.assertIsNone(self.app.closer)
        self.assertIsNotNone(self.app.confirm)

    def test_the_poll_changing_the_rows_running_state_cancels(self):
        self.press(sidebar.CTRL_X)
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.assertIsNone(self.app.confirm)

    def test_the_cursor_moved_by_a_poll_cancels(self):
        self.press(sidebar.CTRL_X)
        self.poll(self.b)
        self.app.refresh()
        self.assertNotEqual(self.app.cursor, ("session", "a"))
        self.assertIsNone(self.app.confirm)

    def test_no_press_arms_while_listing_fails_or_a_close_runs(self):
        self.app.list_error = "listing broken"
        self.press(sidebar.CTRL_X)
        self.assertIsNone(self.app.confirm)
        self.assertIn("Can't list sessions", self.app.notice)
        self.app.list_error = ""
        busy, _ = core.press_close(None, ("session", "b"), self.b, 0.0)
        self.app.closer = sidebar.Closer("claude", (busy,))
        self.app.closer.is_alive = lambda: True
        self.press(sidebar.CTRL_X)
        self.assertIsNone(self.app.confirm)
        self.assertEqual(self.app.notice, "Still closing emx-test-b…")
        self.app.cursor = ("group", "/p/alpha")
        self.press(sidebar.CTRL_X)
        self.assertIsNone(self.app.confirm)
        self.assertEqual(self.app.notice, "Still closing emx-test-b…")

    def test_the_armed_row_says_what_the_second_press_does(self):
        self.press(sidebar.CTRL_X)
        drawn = self.drawn()
        self.assertIn("ctrl+x again to close", drawn)
        self.assertNotIn("emx-test-a", drawn)

    def test_the_row_reads_closing_from_the_second_press_until_a_listing_lacks_it(self):
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertIn("closing…", self.drawn())
        self.assertNotIn("emx-test-a", self.drawn())
        self.settle()
        self.assertEqual(self.app.notice, "Closed emx-test-a.")
        self.assertIn("closing…", self.drawn())
        self.poll()
        self.app.refresh()
        self.assertEqual(self.app.closing, {"a": True})
        self.poll(self.a, self.b)
        self.app.refresh()
        self.assertIn("closing…", self.drawn())
        self.poll(self.b)
        self.app.refresh()
        self.assertNotIn("closing…", self.drawn())
        self.assertEqual(self.app.closing, {})

    def test_the_closing_row_is_bold_in_the_confirm_tone(self):
        self.app.palette.attr = lambda tone, selected=None: {"error": 1 << 20}.get(tone, 0)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        drawn = []
        self.app.put = lambda y, x, text, attr=0: drawn.append((text, attr))
        self.app.draw()
        self.assertIn(("closing…", (1 << 20) | curses.A_BOLD), drawn)
        self.settle()

    def test_enter_click_and_ctrl_r_on_a_closing_row_open_nothing(self):
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.listing = ([self.stopped("a"), self.b], None)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        for key in ("\r", curses.KEY_RIGHT):
            with self.subTest(key=key):
                self.app.say("")
                self.press(key)
                self.assertEqual(self.opened, [])
                self.assertEqual(self.app.notice, "Still closing emx-test-a…")
        self.app.say("")
        self.app.draw()
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "a"))
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.press(curses.KEY_MOUSE)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.notice, "Still closing emx-test-a…")
        self.app.say("")
        self.press(sidebar.CTRL_R)
        self.assertIsNone(self.app.rename_edit)
        self.assertEqual(self.app.notice, "Still closing emx-test-a…")
        self.settle()

    def test_after_removal_the_cursor_lands_on_the_row_below(self):
        c = session("c", "/p/alpha", state="blocked", started=2.0)
        self.poll(self.a, self.b, c)
        self.app.refresh()
        self.listing = ([self.a, self.b, c], None)
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.poll(self.a, c)
        self.app.refresh()
        self.assertEqual(self.app.cursor, ("session", "c"))

    def test_after_removal_the_cursor_lands_above_when_it_was_last(self):
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.poll(self.a)
        self.app.refresh()
        self.assertEqual(self.app.cursor, ("session", "a"))

    def test_double_ctrl_x_on_a_project_header_closes_every_session_in_it(self):
        other = session("z", "/p/zeta", state="blocked")
        self.poll(self.a, self.stopped("b", 1.0), other)
        self.app.refresh()
        self.listing = ([self.a, self.stopped("b", 1.0), other], None)
        self.app.cursor = ("group", "/p/alpha")
        self.press(sidebar.CTRL_X)
        self.assertIsInstance(self.app.confirm, core.GroupConfirm)
        self.assertIn("ctrl+x again to close 2 sessions", self.drawn())
        self.assertEqual(self.ran, [])
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Closing 2 sessions…")
        drawn = self.drawn()
        self.assertEqual(drawn.count("closing…"), 3)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "stop", "a"], True), (["claude", "rm", "a"], True),
                                    (["claude", "rm", "b"], True)])
        self.assertEqual((self.app.notice, self.app.notice_error), ("Closed emx-test-a. Closed emx-test-b.", False))
        self.assertEqual(self.app.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2"), ("kill", "%3")])
        self.poll(other)
        self.app.refresh()
        self.assertEqual(self.app.cursor, ("group", "/p/zeta"))
        self.assertEqual(self.app.closing, {})

    def test_a_group_close_rechecks_each_session_and_closes_the_rest(self):
        self.app.cursor = ("group", "/p/alpha")
        self.press(sidebar.CTRL_X)
        self.listing = ([self.a], None)
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "stop", "a"], True), (["claude", "rm", "a"], True)])
        self.assertEqual(self.app.notice, "Closed emx-test-a. emx-test-b isn't listed any more, so emx didn't close it.")
        self.assertTrue(self.app.notice_error)
        self.assertEqual(self.app.closing, {"a": True})

    def test_ctrl_x_on_a_pinned_or_state_header_closes_nothing_and_says_why(self):
        self.app.prefs = core.toggle_pin(self.app.prefs, "a")
        self.app.regroup()
        self.app.cursor = ("group", core.PINNED)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer), (None, None))
        self.assertEqual(self.app.notice, "Ctrl+X can't close the Pinned group; close its sessions one at a time.")
        self.app.say("")
        self.app.prefs = core.toggle_view(self.app.prefs)
        self.app.regroup()
        self.app.cursor = ("group", core.PINNED)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer), (None, None))
        self.assertEqual(self.app.notice, "Ctrl+X can't close the Pinned group; close its sessions one at a time.")
        self.app.say("")
        self.app.cursor = ("group", core.NEEDS_INPUT)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer), (None, None))
        self.assertEqual(self.app.notice, "Ctrl+X closes a project's group from the project view.")
        self.assertEqual(self.ran, [])

    def test_first_ctrl_x_on_a_folded_header_unfolds_and_arms_and_the_second_closes_all(self):
        self.app.cursor = ("group", "/p/alpha")
        self.app.toggle_group("/p/alpha")
        self.assertIn("/p/alpha", self.app.collapsed())
        self.press(sidebar.CTRL_X)
        self.assertNotIn("/p/alpha", self.app.collapsed())
        self.assertEqual(self.app.cursor, ("group", "/p/alpha"))
        self.assertEqual(self.app.confirm.key, "/p/alpha")
        self.assertIn("ctrl+x again to close 2 sessions", self.drawn())
        self.assertEqual(self.ran, [])
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual([args for args, _ in self.ran],
                         [["claude", "stop", "a"], ["claude", "rm", "a"], ["claude", "stop", "b"], ["claude", "rm", "b"]])

    def test_a_footer_and_the_help_show_ctrl_x(self):
        self.assertTrue(any("^x" in footer for footer in sidebar.FOOTERS))
        self.assertIn("^x", " ".join(key for key, _ in sidebar.HELP))

    def test_help_names_interrupt_and_close(self):
        self.assertEqual(dict(sidebar.HELP)["^x"], "interrupt a working session; twice closes it (stop, then remove)")
        at = [key for key, _ in sidebar.HELP].index("^x")
        self.assertEqual(sidebar.HELP[at + 1], ("", "on a project header, twice closes the group"))
        self.assertIn("^x close", sidebar.FOOTERS[0])
        scr = RecordingScr(30, 60)
        self.app.scr = scr
        self.press("?")
        self.app.draw()
        shown = "\n".join(scr.cells.values())
        self.assertIn("interrupt a working session; twice closes it", shown)
        self.assertIn("on a project header, twice closes the group", shown)


class FakeListings:
    def __init__(self, cache=None):
        self.cache = dict(cache or {})
        self.asked = []
        self.readable_cache = {}

    def get(self, folder):
        self.asked.append(folder)
        return self.cache.get(folder)

    def readable(self, folder):
        self.asked.append(folder)
        return self.readable_cache.get(folder)


class NewSession(AppCase):
    """`n` starts where the cursor is; Tab picks another folder from suggestions, never touching the filesystem on this thread."""

    def setUp(self):
        super().setUp()
        self.started = []
        test = self

        class FakeStarter:
            def __init__(self, claude, folder, message):
                test.started.append((folder, message))

            def start(self):
                pass

        patch = mock.patch.object(sidebar, "Starter", FakeStarter)
        patch.start()
        self.addCleanup(patch.stop)
        self.app.home = "/h/me"
        self.app.listings = FakeListings()
        self.poll(session("a", "/p/alpha/sub"), session("b", "/p/beta"))
        self.app.refresh()

    def keys(self, *keys):
        for key in keys:
            self.app.handle_key(key)

    def type(self, text):
        self.keys(*text)

    def test_n_then_enter_starts_an_empty_session_in_the_cursors_session_folder(self):
        self.app.cursor = ("session", "a")
        self.keys("n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", "/p/alpha/sub"))
        self.keys("\n")
        self.assertEqual(self.started, [("/p/alpha/sub", "")])
        self.assertIsNone(self.app.prompt)

    def test_on_a_group_header_the_groups_folder_and_the_message_goes_with_it(self):
        self.app.cursor = ("group", "/p/beta")
        self.keys("n")
        self.type("hi there")
        self.keys("\r")
        self.assertEqual(self.started, [("/p/beta", "hi there")])

    def test_the_prompt_names_the_folder_and_shows_tab(self):
        self.app.cursor = ("session", "a")
        self.keys("n")
        drawn = [text for text, _ in self.app.bottom_lines(39)]
        self.assertIn("New session in /p/alpha/sub", drawn)
        self.assertIn("Tab folder", drawn[-1])
        self.assertFalse(any(line in sidebar.FOOTERS for line in drawn), drawn)

    def test_a_by_state_header_uses_the_last_folder_else_opens_the_picker(self):
        self.app.toggle_view()
        self.app.cursor = ("group", core.WORKING)
        self.keys("n")
        self.assertEqual(self.app.prompt["stage"], "folder")
        self.keys("\x1b")
        self.assertIsNone(self.app.prompt)
        self.app.prefs["last_folder"] = "/p/last"
        self.keys("n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", "/p/last"))

    def test_with_no_sessions_n_opens_the_picker_and_esc_cancels(self):
        self.poll()
        self.app.refresh()
        self.keys("n")
        self.assertEqual(self.app.prompt["stage"], "folder")
        self.keys("\x1b")
        self.assertIsNone(self.app.prompt)
        self.assertEqual(self.started, [])

    def test_tab_picks_a_suggestion_with_the_arrows_and_keeps_the_message(self):
        self.app.listings.cache["/h/me"] = ["Emx-Lab", "notes"]
        self.app.cursor = ("session", "b")
        self.keys("n")
        self.type("draft")
        self.keys("\t")
        self.assertEqual(self.app.prompt["stage"], "folder")
        self.type("EmX")
        self.keys(curses.KEY_DOWN)
        self.assertEqual(self.app.prompt["picked"], "/h/me/Emx-Lab")
        self.keys("\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"], self.app.prompt["text"]),
                         ("message", "/h/me/Emx-Lab", "draft"))
        self.keys("\n")
        self.assertEqual(self.started, [("/h/me/Emx-Lab", "draft")])

    def test_esc_in_the_picker_goes_back_to_the_message_and_a_second_esc_cancels(self):
        self.app.cursor = ("session", "b")
        self.keys("n")
        self.type("draft")
        self.keys("\t", "x", "\x1b")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"], self.app.prompt["text"]),
                         ("message", "/p/beta", "draft"))
        self.keys("\x1b")
        self.assertIsNone(self.app.prompt)
        self.assertEqual(self.started, [])

    def test_the_highlight_stays_on_the_folder_he_chose_when_the_rows_move(self):
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("p")
        self.keys(curses.KEY_DOWN, curses.KEY_DOWN)
        chosen = self.app.prompt["picked"]
        self.assertEqual([r.path for r in self.app.suggestions()].index(chosen), 1)
        self.poll(session("a", "/p/alpha/sub"), session("b", "/p/beta"), session("c", "/p/a"))
        self.app.refresh()
        self.assertEqual([r.path for r in self.app.suggestions()].index(chosen), 2)
        self.keys("\n")
        self.assertEqual(self.app.prompt["folder"], chosen)

    def test_tab_completes_the_highlighted_or_first_suggestion(self):
        self.app.listings.cache["/h/me"] = ["Work"]
        self.app.listings.cache["/h/me/Work"] = ["inner"]
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("wo")
        self.keys("\t")
        self.assertEqual(self.app.prompt["text"], "~/Work/")
        rows = self.app.suggestions()
        self.assertEqual([r.path for r in rows if r.source == core.LISTED], ["/h/me/Work/inner"])

    def test_a_typed_path_is_accepted_only_once_a_listing_shows_it_is_a_folder(self):
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("/q/new")
        self.keys("\n")
        self.assertEqual(self.app.prompt["stage"], "folder")
        self.assertIn("Still reading", self.app.prompt["hint"])
        self.app.listings.cache["/q"] = ["other"]
        self.keys("\n")
        self.assertIn("Not a folder", self.app.prompt["hint"])
        self.app.listings.cache["/q"] = ["new"]
        self.keys("\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", "/q/new"))

    def test_the_picker_never_touches_the_filesystem_on_this_thread(self):
        self.app.cursor = ("session", "b")
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("scanned")), \
                mock.patch.object(sidebar.os.path, "isdir", side_effect=AssertionError("stat")):
            self.keys("n", "\t")
            self.type("~/some/path")
            self.app.draw()
            self.keys(curses.KEY_DOWN, "\t", "\n")

    def test_n_enter_enter_never_reopens_the_stopped_row_under_the_cursor(self):
        self.poll(session("s", "/p/stopped", state="stopped", pid=None), session("b", "/p/beta"))
        self.app.refresh()
        self.app.cursor = ("session", "s")
        self.keys("n", "\n", "\n", curses.KEY_RIGHT)
        self.assertEqual(self.started, [("/p/stopped", "")])
        self.assertFalse([o for o in self.opened if o[1] == core.OPEN], self.opened)
        self.assertEqual(self.app.notice, "Still starting the last session…")
        self.assertEqual(self.app.cursor, ("session", "s"))

    def test_a_late_enter_after_the_start_waits_for_the_new_row(self):
        self.poll(session("s", "/p/stopped", state="stopped", pid=None), session("b", "/p/beta"))
        self.app.refresh()
        self.app.cursor = ("session", "s")
        self.keys("n", "\n")
        self.app.starter = mock.Mock(folder="/p/stopped", result=(session("new1", "/p/stopped"), None))
        self.app.starter.is_alive.return_value = False
        self.app.finish_start()
        self.assertIsNone(self.app.starter)
        self.keys("\n", curses.KEY_RIGHT)
        self.assertFalse([o for o in self.opened if o[1] == core.OPEN], self.opened)
        self.assertEqual(self.app.notice, "Still starting the last session…")
        self.poll(session("s", "/p/stopped", state="stopped", pid=None), session("b", "/p/beta"), session("new1", "/p/stopped"))
        self.app.refresh()
        self.assertEqual(self.app.cursor, ("session", "new1"))
        self.keys("\n")
        self.assertIn("new1", [o[0] for o in self.opened if o[1] == core.OPEN])

    def start_from_the_stopped_row(self, folder):
        self.poll(session("s", "/p/stopped", state="stopped", pid=None), session("b", "/p/beta"))
        self.app.refresh()
        self.app.cursor = ("session", "s")
        self.app.starter = mock.Mock(folder=folder, result=(session("new1", folder), None))
        self.app.starter.is_alive.return_value = False
        self.app.finish_start()

    def test_a_late_enter_stays_blocked_while_the_new_row_is_folded_out_of_reach(self):
        self.app.toggle_group("/p/beta")
        self.start_from_the_stopped_row("/p/beta")
        self.poll(session("s", "/p/stopped", state="stopped", pid=None), session("b", "/p/beta"), session("new1", "/p/beta"))
        self.app.refresh()
        self.assertEqual(self.app.cursor, ("session", "s"))
        self.keys("\n")
        self.assertFalse([o for o in self.opened if o[1] == core.OPEN], self.opened)
        self.assertEqual(self.app.notice, "Still starting the last session…")

    def test_j_or_k_lifts_the_block_while_the_listing_is_out(self):
        self.start_from_the_stopped_row("/p/stopped")
        self.keys("\n")
        self.assertFalse([o for o in self.opened if o[1] == core.OPEN], self.opened)
        self.keys("k", "j")
        self.assertEqual(self.app.cursor, ("session", "s"))
        self.keys("\n")
        self.assertIn("s", [o[0] for o in self.opened if o[1] == core.OPEN])

    def test_a_late_enter_is_freed_once_the_wait_gives_up(self):
        self.start_from_the_stopped_row("/p/stopped")
        later = sidebar.time.monotonic() + sidebar.FRESH_GRACE_SECONDS + 1
        with mock.patch.object(sidebar.time, "monotonic", return_value=later):
            self.keys("\n")
        self.assertIn("s", [o[0] for o in self.opened if o[1] == core.OPEN])

    def test_a_failed_start_frees_enter_once_its_notice_shows(self):
        self.app.cursor = ("session", "b")
        self.app.starter = mock.Mock(folder="/p/beta", result=(None, "claude said no"))
        self.app.starter.is_alive.return_value = False
        self.app.finish_start()
        self.assertEqual(self.app.notice, "claude said no")
        self.keys("\n")
        self.assertIn("b", [o[0] for o in self.opened if o[1] == core.OPEN])

    def test_enter_on_a_group_header_still_folds_while_a_session_starts(self):
        self.app.cursor = ("group", "/p/beta")
        self.keys("n", "\n")
        self.assertIsNotNone(self.app.starter)
        self.keys("\n")
        self.assertIn("/p/beta", self.app.collapsed())

    def test_a_suggestion_that_raises_shows_an_error_line_not_a_frozen_draw(self):
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        with mock.patch.object(core, "folder_suggestions", side_effect=IndexError("boom")):
            drawn = [text for text, _ in self.app.bottom_lines(39, 20)]
        self.assertIn("! boom", drawn)

    def test_enter_with_nothing_highlighted_takes_a_confirmed_typed_folder_else_the_top_suggestion(self):
        self.app.listings.cache["/q"] = ["xy"]
        self.app.listings.readable_cache["/q/x"] = True
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("/q/x")
        self.assertEqual(self.app.suggestions()[0].path, "/q/xy")
        self.keys("\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", "/q/x"))
        self.keys("\t")
        self.type("/q/x-")
        self.app.listings.cache["/q"] = ["x-ray", "x-wing-long"]
        self.keys("\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", "/q/x-ray"))
        self.keys("\t")
        self.type("/q/zzz")
        self.keys("\n")
        self.assertEqual(self.app.prompt["stage"], "folder")
        self.assertIn("Not a folder", self.app.prompt["hint"])

    def test_a_bare_word_finds_a_folder_one_level_inside_a_folder_he_uses(self):
        self.app.listings.cache["/p"] = ["beta", "quokka"]
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("quok")
        self.assertEqual([r.path for r in self.app.suggestions()], ["/p/quokka"])
        self.assertIn("/p", self.app.listings.asked)

    def test_a_bare_word_searches_beside_a_worktrees_repo_in_either_view(self):
        worktree = "/w/kiwi/.claude/worktrees/feat"
        self.poll(session("a", worktree))
        self.app.refresh()
        self.app.listings.cache["/w"] = ["kiwi", "quokka"]
        self.app.listings.cache["/w/kiwi/.claude/worktrees"] = ["quokka-wt"]
        self.app.placements = {worktree: core.Placement("/w/kiwi", "feat")}
        self.app.prefs["recent_folders"] = [worktree]
        for view in (core.PROJECT, core.BY_STATE):
            with self.subTest(view=view):
                self.assertEqual(self.app.view(), view)
                self.app.listings.asked.clear()
                self.app.cursor = ("session", "a")
                self.keys("n", "\t")
                self.type("quok")
                self.assertEqual([r.path for r in self.app.suggestions()], ["/w/quokka"])
                self.assertIn("/w", self.app.listings.asked)
                self.assertNotIn("/w/kiwi/.claude/worktrees", self.app.listings.asked)
                self.keys("\x1b", "\x1b")
                self.app.toggle_view()
        self.assertEqual(self.app.view(), core.PROJECT)

    def test_enter_on_an_empty_picker_takes_the_top_suggestion(self):
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        top = self.app.suggestions()[0].path
        self.assertNotEqual(top, "/h/me")
        self.keys("\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", top))
        self.keys("\t")
        self.type("x")
        self.keys("\x15", "\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", top))

    def test_enter_on_an_empty_picker_with_no_suggestions_falls_back_to_home(self):
        self.poll()
        self.app.refresh()
        self.keys("n")
        self.assertEqual(self.app.suggestions(), [])
        self.keys("\n")
        self.assertIn("Still reading", self.app.prompt["hint"])
        self.app.listings.readable_cache["/h/me"] = True
        self.keys("\n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["folder"]), ("message", "/h/me"))

    def test_no_match_is_said_once(self):
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("/q/zzz")
        self.keys("\t")
        drawn = [text for text, _ in self.app.bottom_lines(39, 20)]
        self.assertEqual(sum("no match" in line for line in drawn), 1, drawn)

    def test_on_a_short_pane_the_rows_give_way_and_the_highlight_survives_past_them(self):
        self.app.listings.cache["/h/me"] = [f"dir{i}" for i in range(12)]
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        self.type("~/")
        for _ in range(6):
            self.keys(curses.KEY_DOWN)
        picked = self.app.prompt["picked"]
        self.assertEqual(picked, "/h/me/dir3")
        drawn = [text for text, _ in self.app.bottom_lines(39, 9)]
        self.assertLessEqual(len(drawn), 9 - 2)
        self.assertEqual(drawn[0], "New session · pick a folder:")
        self.assertTrue(drawn[1].startswith("> ~/"), drawn)
        self.assertTrue(any("more" in line for line in drawn), drawn)
        self.assertFalse(any("dir3" in line for line in drawn), drawn)
        self.assertEqual(self.app.prompt["picked"], picked)
        self.keys("\n")
        self.assertEqual(self.app.prompt["folder"], picked)

    def test_the_title_and_input_stay_on_a_pane_too_short_for_any_row(self):
        self.app.listings.cache["/h/me"] = ["dir"]
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")
        drawn = [text for text, _ in self.app.bottom_lines(39, 5)]
        self.assertEqual(drawn[0], "New session · pick a folder:")
        self.assertFalse(any("dir" in line for line in drawn), drawn)

    def test_a_started_folder_becomes_recent_and_last(self):
        self.app.starter = mock.Mock(folder="/p/gamma", result=(session("new1", "/p/gamma"), None))
        self.app.starter.is_alive.return_value = False
        self.app.finish_start()
        self.assertEqual(self.app.prefs["recent_folders"][0], "/p/gamma")
        self.assertEqual(core.load_prefs(self.app.prefs_file)["last_folder"], "/p/gamma")

    def test_the_help_says_where_n_starts(self):
        self.assertIn("Tab", dict(sidebar.HELP)["n"])


class FindSessions(AppCase):
    """Ctrl+F or / filters the panel to sessions whose name or folder contains the text; Esc clears it, Enter opens the top match."""

    def setUp(self):
        super().setUp()
        self.app.home = "/h/me"
        self.poll(session("a", "/p/alpha"), session("b", "/p/beta"), session("c", "/p/alpha/docs", started=1.0))
        self.app.refresh()
        self.app.cursor = ("session", "a")

    def keys(self, *keys):
        for key in keys:
            self.app.handle_key(key)

    def shown(self):
        return [item[1] for item in self.app.items() if item[0] == "session"]

    def test_typing_filters_by_name_or_folder_and_puts_the_cursor_on_the_top_match(self):
        self.keys(sidebar.CTRL_F, *"BETA")
        self.assertEqual(self.shown(), ["b"])
        self.assertEqual(self.app.cursor, ("session", "b"))
        self.keys(*["\x7f"] * 4, *"docs")
        self.assertEqual(self.shown(), ["c"])

    def test_slash_opens_find_too_and_its_line_replaces_the_footer(self):
        self.keys("/", *"alp")
        texts = [t for t, _ in self.app.bottom_lines(39)]
        self.assertIn("> alp▏", texts)
        self.assertIn(sidebar.FIND_HINT, texts)
        self.assertFalse(any(f in texts for f in sidebar.FOOTERS))

    def test_esc_clears_the_filter(self):
        self.keys(sidebar.CTRL_F, *"beta", "\x1b")
        self.assertIsNone(self.app.find)
        self.assertEqual(sorted(self.shown()), ["a", "b", "c"])
        self.assertEqual(self.app.cursor, ("session", "b"))

    def test_enter_opens_the_top_match_and_ends_the_find(self):
        self.keys(sidebar.CTRL_F, *"alpha", "\r")
        self.assertEqual(self.opened, [("a", core.OPEN)])
        self.assertIsNone(self.app.find)

    def test_arrows_move_among_the_matches_and_enter_opens_the_one_chosen(self):
        self.keys(sidebar.CTRL_F, *"alpha", curses.KEY_DOWN, "\r")
        self.assertEqual(self.opened, [("c", core.OPEN)])

    def test_no_match_opens_nothing(self):
        self.keys(sidebar.CTRL_F, *"zzz", "\r")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.groups, [])
        self.assertEqual(self.app.notice, "No session matches.")

    def test_keys_typed_into_find_are_text_not_commands(self):
        self.keys(sidebar.CTRL_F, *"nq<")
        self.assertEqual(self.app.find, "nq<")
        self.assertIsNone(self.app.prompt)
        self.assertEqual(self.app.prefs["panel_width"], core.DEFAULT_PREFS["panel_width"])

    def test_a_folded_group_shows_its_matches_while_finding(self):
        self.app.toggle_group("/p/alpha")
        self.assertNotIn("a", self.shown())
        self.keys(sidebar.CTRL_F, *"alpha")
        self.assertEqual(self.shown(), ["a", "c"])
        self.keys("\x1b")
        self.assertNotIn("a", self.shown())

    def test_enter_waits_while_a_session_starts(self):
        self.app.starter = mock.Mock()
        self.app.starter.is_alive.return_value = True
        self.keys(sidebar.CTRL_F, *"alpha", "\r")
        self.assertEqual(self.opened, [])
        self.assertIn("Still starting", self.app.notice)

    def click(self, item):
        self.app.draw()
        y = next(y for y, at in self.app.row_map.items() if at == item)
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.app.handle_key(curses.KEY_MOUSE)

    def test_a_click_on_a_header_while_finding_does_nothing(self):
        self.keys(sidebar.CTRL_F, *"alpha")
        saved = core.load_prefs(self.app.prefs_file)
        self.click(("group", "/p/alpha"))
        self.assertEqual(self.app.find, "alpha")
        self.assertNotIn("/p/alpha", core.collapsed_keys(self.app.prefs, self.app.view()))
        self.assertEqual(core.load_prefs(self.app.prefs_file), saved)
        self.assertEqual(self.opened, [])

    def test_a_click_on_a_session_while_finding_ends_the_find_and_opens_it(self):
        self.keys(sidebar.CTRL_F, *"alpha")
        self.click(("session", "c"))
        self.assertEqual(self.opened, [("c", core.OPEN)])
        self.assertIsNone(self.app.find)
        self.assertEqual(self.app.cursor, ("session", "c"))

    def test_a_poll_while_finding_keeps_the_filter(self):
        self.keys(sidebar.CTRL_F, *"beta")
        self.poll(session("a", "/p/alpha"), session("b", "/p/beta"), session("d", "/p/beta-two"))
        self.app.refresh()
        self.assertEqual(self.shown(), ["b", "d"])


class PinSessions(AppCase):
    """Ctrl+T pins a session into a Pinned group at the top, kept in emx's prefs and dropped once the session is gone."""

    def setUp(self):
        super().setUp()
        self.poll(session("a", "/p/alpha"), session("b", "/p/beta"))
        self.app.refresh()

    def test_ctrl_t_pins_and_unpins_the_session_under_the_cursor(self):
        self.app.cursor = ("session", "b")
        self.app.handle_key(sidebar.CTRL_T)
        self.assertEqual(self.app.groups[0], core.Group(core.PINNED, (session("b", "/p/beta"),)))
        self.assertEqual(self.app.cursor, ("session", "b"))
        self.app.handle_key(sidebar.CTRL_T)
        self.assertNotIn(core.PINNED, [g.key for g in self.app.groups])

    def test_ctrl_t_on_a_header_does_nothing(self):
        self.app.cursor = ("group", "/p/alpha")
        self.app.handle_key(sidebar.CTRL_T)
        self.assertNotIn("pinned", self.app.prefs)

    def test_a_pin_survives_a_restart_of_the_panel(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(sidebar.CTRL_T)
        again = sidebar.App(FakeScr(), tmux=None, claude="claude")
        again.poller.result = ([session("a", "/p/alpha"), session("b", "/p/beta")], None)
        again.housekeep = lambda fresh_list: None
        again.refresh()
        self.assertEqual(again.groups[0].key, core.PINNED)

    def test_a_pin_is_dropped_after_two_listings_without_its_session(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(sidebar.CTRL_T)
        self.poll(session("b", "/p/beta"))
        self.app.refresh()
        self.assertEqual(core.pinned_ids(core.load_prefs(self.app.prefs_file)), {"a"})
        self.poll(session("b", "/p/beta"))
        self.app.refresh()
        self.assertEqual(core.pinned_ids(core.load_prefs(self.app.prefs_file)), set())

    def test_an_empty_listing_prunes_nothing(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(sidebar.CTRL_T)
        for _ in range(3):
            self.poll()
            self.app.refresh()
        self.assertEqual(core.pinned_ids(core.load_prefs(self.app.prefs_file)), {"a"})

    def test_a_failed_listing_prunes_nothing(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(sidebar.CTRL_T)
        for _ in range(3):
            self.app.poller.result = (None, "listing broken")
            self.app.refresh()
        self.assertEqual(core.pinned_ids(self.app.prefs), {"a"})

    def test_the_by_state_view_shows_a_pinned_rows_state(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(sidebar.CTRL_T)
        self.app.toggle_view()
        drawn = []
        self.app.put = lambda y, x, text, attr=0: drawn.append(text)
        self.app.draw()
        self.assertIn("Pinned", drawn)
        self.assertIn("Working", drawn)
        self.assertEqual(drawn.count("Working"), 2)

    def test_the_help_says_the_pin_is_emxs_own(self):
        self.assertIn("in emx", dict(sidebar.HELP)["^t"])


def fixture_screen(name):
    (path,) = (Path(__file__).parent / "fixtures" / "screens").glob(f"{name}.w*.txt")
    return path.read_text(encoding="utf-8"), int(path.stem.rsplit(".w", 1)[1])


PROMPT_CURSORS = json.loads((Path(__file__).parent / "fixtures" / "screens" / "prompt-cursors.json").read_text("utf-8"))


class RenameTmux:
    """The panel (%1) has focus; main (%2) shows `named`; a hidden viewer (%3) shows `b`. Its screen and prompt come from real captures, and tmux's last-moment check answers `gate_says`."""

    def __init__(self):
        self.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 141, active=True),
            Pane("%2", "@1", False, "", "named", 100, 141),
            Pane("%3", "@2", False, "", "b", 100, 100),
        ]
        self.view("named-viewer", "prompt-empty")
        self.in_mode = False
        self.gate_says = core.TYPED
        self.prompt_after = None
        self.typed = []
        self.gates = []
        self.threads = []

    def view(self, screen, prompt):
        self.screen, _ = fixture_screen(screen)
        text, width = fixture_screen(prompt)
        cursor = PROMPT_CURSORS["cursors"][prompt]
        lines = text.split("\n")
        self.prompt = (lines[cursor["y"]], lines[cursor["y"] + 1])
        self.cursor_at = (cursor["x"], cursor["y"], cursor["shown"], width)

    def note(self):
        self.threads.append(threading.current_thread() is threading.main_thread())

    def panes(self):
        return list(self.list)

    def capture(self, pane):
        self.note()
        return self.screen

    def prompt_probe(self, pane):
        self.note()
        return (*self.cursor_at, self.in_mode)

    def cursor_lines(self, pane, y):
        self.note()
        return list(self.prompt)

    def type_if(self, pane, gate, text, typed, report):
        self.note()
        self.gates.append(gate)
        if self.gate_says == typed:
            self.typed.append((pane, text))
            if self.prompt_after == "gone":
                self.prompt_probe = mock.Mock(side_effect=TmuxError("can't find pane"))
            elif self.prompt_after is not None:
                self.view("named-viewer", self.prompt_after)
        return self.gate_says


class RenameSession(AppCase):
    """Ctrl+R edits the name inline; Enter types `/rename <name>` into the open viewer only when every check passes, off the curses thread."""

    def setUp(self):
        super().setUp()
        self.named = session("named", "/p/alpha", state="blocked")
        self.poll(self.named, session("b", "/p/beta"), session("c", "/p/gamma"))
        self.app.refresh()
        self.app.tmux = RenameTmux()
        self.app.cursor = ("session", "named")
        self.listings = [[self.named], [session("named", "/p/alpha", state="blocked")]]
        self.after = core.Session("named", "fresh name", "/p/alpha", "blocked", "", "", 0.0, True)
        self.fetched = []
        test = self

        def fetch(claude):
            test.fetched.append(threading.current_thread() is threading.main_thread())
            rows = test.listings.pop(0) if test.listings else [test.after]
            return rows, None

        for p in (
            mock.patch.object(sidebar, "fetch_sessions", fetch),
            mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0.01),
            mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0.2),
        ):
            p.start()
            self.addCleanup(p.stop)

    def keys(self, *keys):
        for key in keys:
            self.app.handle_key(key)

    def rename_to(self, text):
        self.keys(sidebar.CTRL_R, "\x15", *text, "\r")

    def settle(self):
        if self.app.renamer is not None:
            self.app.renamer.join(5)
        self.app.refresh()

    def refused(self, want):
        self.assertEqual(self.app.tmux.typed, [])
        self.assertIn(want, self.app.notice)
        self.assertTrue(self.app.notice_error)
        self.assertNotIn("named", self.app.renaming)

    def test_ctrl_r_opens_an_inline_edit_holding_the_name(self):
        self.keys(sidebar.CTRL_R)
        self.assertEqual(self.app.rename_edit["text"], "emx-test-named")
        self.assertIn(sidebar.RENAME_FOOTER, [t for t, _ in self.app.bottom_lines(39)])
        drawn = []
        self.app.put = lambda y, x, text, attr=0: drawn.append(text)
        self.app.draw()
        self.assertIn("emx-test-named▏", drawn)

    def test_enter_types_the_rename_through_tmuxs_last_moment_check_off_the_curses_thread(self):
        self.rename_to("fresh name")
        self.assertEqual(self.app.notice, "Renaming emx-test-named…")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])
        self.assertEqual(self.app.tmux.gates, [core.type_gate("%2", "%1", "named", 26, sidebar.SESSION_OPT)])
        self.assertNotIn(True, self.app.tmux.threads)
        self.assertNotIn(True, self.fetched)
        self.assertEqual((self.app.notice, self.app.notice_error), ("Renamed emx-test-named to fresh name.", False))

    def test_an_emoji_held_together_by_joiners_can_be_typed_into_the_edit(self):
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        self.keys(sidebar.CTRL_R, "\x15", *family)
        self.keys("\x01", "\u2028")
        self.assertEqual(self.app.rename_edit["text"], family)

    def test_an_edit_whose_session_drops_out_of_the_listing_is_dropped_with_a_reason(self):
        self.keys(sidebar.CTRL_R, *"half")
        for _ in range(core.STALE_AFTER_MISSES):
            self.poll(session("b", "/p/beta"), session("c", "/p/gamma"))
            self.app.refresh()
        self.assertIsNone(self.app.rename_edit)
        self.assertEqual(self.app.notice, "emx-test-named isn't listed any more, so emx dropped the rename.")
        self.assertTrue(self.app.notice_error)

    def test_one_listing_without_the_session_keeps_the_edit_and_its_text(self):
        self.keys(sidebar.CTRL_R, "\x15", *"half")
        self.poll(session("b", "/p/beta"), session("c", "/p/gamma"))
        self.app.refresh()
        self.assertEqual(self.app.rename_edit["text"], "half")

    def test_an_empty_listing_never_counts_against_the_edit(self):
        self.keys(sidebar.CTRL_R, "\x15", *"half")
        for _ in range(core.STALE_AFTER_MISSES + 1):
            self.poll()
            self.app.refresh()
        self.assertEqual(self.app.rename_edit["text"], "half")

    def test_a_listing_that_still_has_the_session_resets_the_misses(self):
        self.keys(sidebar.CTRL_R, "\x15", *"half")
        for listing in ([session("b", "/p/beta")], [self.named], [session("b", "/p/beta")]):
            self.poll(*listing)
            self.app.refresh()
        self.assertEqual(self.app.rename_edit["text"], "half")

    def test_a_viewer_scrolled_back_is_never_typed_into(self):
        self.app.tmux.in_mode = True
        self.rename_to("fresh name")
        self.settle()
        self.refused("scrolled back")
        self.assertEqual(self.app.tmux.gates, [])

    def test_a_change_tmux_catches_at_the_last_moment_types_nothing(self):
        cases = {
            "0 0 1 2 26 named": "Focus left the panel",
            "1 0 1 2 26 named": "scrolled back",
            "0 0 1 9 26 named": "prompt changed",
            "0 0 1 2 26 b": "viewer changed",
        }
        for report, want in cases.items():
            with self.subTest(report=report):
                self.listings = [[self.named]]
                self.app.tmux.gate_says = report
                self.rename_to("fresh name")
                self.settle()
                self.refused(want)

    def test_a_prompt_holding_his_text_is_never_typed_into(self):
        for prompt in ("prompt-text", "prompt-text-at-start", "prompt-multiline", "prompt-slash-menu", "prompt-permission"):
            with self.subTest(prompt=prompt):
                self.listings = [[self.named]]
                self.app.tmux.view("named-viewer", prompt)
                self.rename_to("fresh name")
                self.settle()
                self.refused("emx-test-named's prompt isn't empty, so emx didn't type into it.")

    def test_a_viewer_showing_the_agent_view_or_another_session_is_never_typed_into(self):
        for screen in ("agent-view", "cross-wired"):
            with self.subTest(screen=screen):
                self.listings = [[self.named]]
                self.app.tmux.view(screen, "prompt-empty")
                self.rename_to("fresh name")
                self.settle()
                self.refused("isn't showing its prompt")

    def test_a_viewer_another_listed_session_would_look_the_same_in_is_never_typed_into(self):
        twin = core.Session("d", "emx-test-named", "/p/delta", "blocked", "", "", 0.0, True)
        self.listings = [[self.named, twin]]
        self.rename_to("fresh name")
        self.settle()
        self.refused("looks the same as another session's")

    def test_an_unnamed_sessions_own_empty_prompt_is_typed_into(self):
        unnamed = core.Session("named", "named", "/p/alpha", "blocked", "", "", 0.0, True)
        self.poll(unnamed)
        self.app.refresh()
        self.listings = [[unnamed]]
        self.app.tmux.view("unnamed-empty-viewer", "prompt-empty")
        self.keys(sidebar.CTRL_R)
        self.assertEqual(self.app.rename_edit["text"], "")
        self.keys(*"fresh name", "\r")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])
        self.assertEqual(self.app.notice, "Renamed named to fresh name.")

    def test_an_unnamed_sessions_viewer_showing_another_session_is_never_typed_into(self):
        unnamed = core.Session("named", "named", "/p/alpha", "blocked", "", "", 0.0, True)
        self.poll(unnamed)
        self.app.refresh()
        self.listings = [[unnamed]]
        self.app.tmux.view("cross-wired", "prompt-empty")
        self.rename_to("fresh name")
        self.settle()
        self.refused("isn't showing its prompt")

    def test_a_viewer_emx_caught_showing_something_else_is_never_typed_into(self):
        self.app.flagged_viewers = frozenset({"%2"})
        self.rename_to("fresh name")
        self.settle()
        self.refused("isn't showing its prompt")

    def test_a_working_or_stopped_session_is_never_typed_into(self):
        for state, pid in (("working", 1), ("stopped", None)):
            with self.subTest(state=state):
                self.listings = [[session("named", "/p/alpha", state=state, pid=pid)]]
                self.rename_to("fresh name")
                self.settle()
                self.assertEqual(self.app.tmux.typed, [])
                self.assertTrue(self.app.notice_error)

    def test_a_failed_listing_types_nothing(self):
        self.listings = [None]
        self.rename_to("fresh name")
        self.settle()
        self.refused("Couldn't list sessions")

    def test_the_session_is_the_one_ctrl_r_was_pressed_on(self):
        self.keys(sidebar.CTRL_R, "\x15", *"fresh name")
        self.app.cursor = ("session", "b")
        self.keys("\r")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])

    def test_esc_cancels_and_types_nothing(self):
        self.keys(sidebar.CTRL_R, *"x", "\x1b")
        self.assertIsNone(self.app.rename_edit)
        self.assertIsNone(self.app.renamer)

    def test_an_empty_or_unchanged_name_types_nothing(self):
        self.keys(sidebar.CTRL_R, "\x15", "\r")
        self.assertIn("Type a name", self.app.rename_edit["hint"])
        self.keys("\x1b", sidebar.CTRL_R, "\r")
        self.assertIsNone(self.app.rename_edit)
        self.assertIsNone(self.app.renamer)

    def test_no_open_viewer_means_no_rename(self):
        self.app.cursor = ("session", "c")
        self.keys(sidebar.CTRL_R)
        self.assertIsNone(self.app.rename_edit)
        self.assertEqual(self.app.notice, "Open emx-test-c once, then press ctrl+r to rename it.")

    def test_a_name_the_listing_never_shows_is_reported_and_stops_being_pending(self):
        self.after = self.named
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(len(self.app.tmux.typed), 1)
        self.assertIn("doesn't show a new name yet", self.app.notice)
        self.assertIn("named", self.app.renaming)
        later = time.monotonic() + core.RENAME_PENDING_SECONDS
        self.poll(self.named)
        with mock.patch.object(sidebar.time, "monotonic", lambda: later):
            self.app.refresh()
        self.assertNotIn("named", self.app.renaming)

    def test_no_new_name_and_text_left_in_the_prompt_is_said_plainly(self):
        self.after = self.named
        self.app.tmux.prompt_after = "prompt-multiline"
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(len(self.app.tmux.typed), 1)
        self.assertIn("prompt isn't empty", self.app.notice)
        self.assertTrue(self.app.notice_error)
        self.assertIn("named", self.app.renaming)

    def test_a_tmux_error_rechecking_the_prompt_still_counts_the_rename_as_typed(self):
        self.after = self.named
        self.app.tmux.prompt_after = "gone"
        self.rename_to("fresh name")
        self.settle()
        self.assertIn("doesn't show a new name yet", self.app.notice)
        self.assertIn("named", self.app.renaming)

    def test_the_new_name_on_main_is_not_flagged_while_the_listing_catches_up(self):
        self.rename_to("emx-test-renamed")
        self.app.renamer.join(5)
        text, width = fixture_screen("named-viewer")
        rule = next(line for line in text.split("\n") if line.endswith(" emx-test-named ─"))
        self.app.tmux.screen = text.replace(rule, "─" * (width - 19) + " emx-test-renamed ─")
        self.assertEqual(core.main_view_state(self.app.tmux.screen, "emx-test-named", width), "mismatch")
        main = self.app.tmux.list[1]
        self.assertEqual(self.app.view_state(main), "ok")

    def test_the_footer_and_help_offer_rename(self):
        self.assertIn("^r rename", sidebar.FOOTERS[0])
        self.assertIn("␣ peek", sidebar.FOOTERS[0])
        self.assertIn("/rename", dict(sidebar.HELP)["^r"])


def named_call(text):
    return core.SummaryCall(text, "claude-haiku-5-5", 1.0, 1.0, 0.0, 0, 0, 0, 0)


class RenameSuggest(AppCase):
    """Tab in the rename box asks Haiku for a name off the curses thread; the name only fills the box, and only Enter renames."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.named = session("named", "/p/alpha", state="blocked")
        self.poll(self.named, session("b", "/p/beta"))
        self.app.refresh()
        self.app.tmux = RenameTmux()
        self.app.cursor = ("session", "named")
        path = requests_file(tmp.name, "write a poem about tea", "now a csv parser")
        self.app.poller.transcripts.path = lambda sid: path if sid == "named" else None
        self.answers = [(named_call("fix-login-flow"), None), (named_call("tea poem and csv parser"), None)]
        self.asked = []
        self.release = threading.Event()
        self.release.set()
        self.addCleanup(self.release.set)
        test = self

        def suggest(claude, excerpt, cwd):
            test.asked.append((excerpt, threading.current_thread() is threading.main_thread()))
            test.release.wait(5)
            return test.answers.pop(0) if test.answers else (None, "no more answers")

        def fetch(claude):
            return [test.named], None

        for p in (
            mock.patch.object(sidebar, "suggest_name", suggest),
            mock.patch.object(sidebar, "summary_scratch", return_value=tmp.name),
            mock.patch.object(sidebar, "fetch_sessions", fetch),
            mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0.01),
            mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0.2),
        ):
            p.start()
            self.addCleanup(p.stop)

    def keys(self, *keys):
        for key in keys:
            self.app.handle_key(key)

    def answered(self):
        self.release.set()
        if self.app.namer is not None:
            self.app.namer.join(5)
        self.app.refresh()

    def settle(self):
        if self.app.renamer is not None:
            self.app.renamer.join(5)
        self.app.refresh()

    def test_tab_puts_a_name_in_the_box_and_types_nothing(self):
        self.keys(sidebar.CTRL_R, "\t")
        self.answered()
        self.assertEqual(self.app.rename_edit["text"], "fix-login-flow")
        self.assertEqual(self.app.tmux.typed, [])
        self.assertIsNone(self.app.renamer)
        self.assertEqual([on_main for _, on_main in self.asked], [False])
        self.assertIn("write a poem about tea", self.asked[0][0])

    def test_the_box_says_it_is_asking_while_the_call_runs(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "\t")
        self.assertIn(sidebar.NAME_ASKING, [t for t, _ in self.app.bottom_lines(39)])
        self.answered()
        self.assertNotIn(sidebar.NAME_ASKING, [t for t, _ in self.app.bottom_lines(39)])

    def test_tab_again_sends_the_first_name_and_gets_another(self):
        self.keys(sidebar.CTRL_R, "\t")
        self.answered()
        self.keys("\t")
        self.answered()
        self.assertIn("fix-login-flow", self.asked[1][0])
        self.assertEqual(self.app.rename_edit["text"], "tea poem and csv parser")

    def test_tab_while_asking_starts_no_second_call(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "\t", "\t", "\t")
        self.answered()
        self.assertEqual(len(self.asked), 1)

    def test_enter_takes_it_through_the_rename_path(self):
        self.keys(sidebar.CTRL_R, "\t")
        self.answered()
        self.keys("\r")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fix-login-flow")])

    def test_enter_while_asking_submits_the_box_and_drops_the_answer(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "-", "2", "\t")
        self.assertEqual(self.app.rename_edit["asking"][1], "emx-test-named-2")
        self.keys("\r")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename emx-test-named-2")])
        self.answered()
        self.assertIsNone(self.app.rename_edit)
        self.assertEqual(len(self.app.tmux.typed), 1)

    def test_esc_cancels_and_a_late_answer_is_dropped(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "\t", "\x1b")
        self.answered()
        self.assertIsNone(self.app.rename_edit)
        self.assertEqual(self.app.tmux.typed, [])

    def test_his_typing_is_never_overwritten(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "\t", "x")
        self.answered()
        self.assertEqual(self.app.rename_edit["text"], "emx-test-namedx")
        self.assertNotIn("asking", self.app.rename_edit)

    def test_a_reopened_box_ignores_the_old_answer(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "\t", "\x1b", sidebar.CTRL_R)
        self.answered()
        self.assertEqual(self.app.rename_edit["text"], "emx-test-named")
        self.assertEqual(self.app.rename_edit["offered"], [])

    def test_a_failure_keeps_the_text_and_shows_the_error(self):
        self.answers = [(None, "claude -p timed out")]
        self.keys(sidebar.CTRL_R, "\t")
        self.answered()
        self.assertEqual(self.app.rename_edit["text"], "emx-test-named")
        self.assertEqual(self.app.rename_edit["hint"], "claude -p timed out")

    def test_a_retry_after_a_failure_says_it_is_asking_again(self):
        self.answers = [(None, "claude -p timed out")]
        self.keys(sidebar.CTRL_R, "\t")
        self.answered()
        self.assertIn("claude -p timed out", [t for t, _ in self.app.bottom_lines(39)])
        self.release.clear()
        self.keys("\t")
        shown = [t for t, _ in self.app.bottom_lines(39)]
        self.assertIn(sidebar.NAME_ASKING, shown)
        self.assertNotIn("claude -p timed out", shown)
        self.answered()

    def test_an_answer_that_arrived_since_the_last_frame_is_shown_and_tab_starts_nothing(self):
        self.keys(sidebar.CTRL_R, "\t")
        self.app.namer.join(5)
        self.keys("\t")
        self.assertEqual(self.app.rename_edit["text"], "fix-login-flow")
        self.assertEqual(len(self.asked), 1)
        self.assertIsNone(self.app.namer)

    def test_tab_in_a_new_box_while_an_old_call_finishes_says_so(self):
        self.release.clear()
        self.keys(sidebar.CTRL_R, "\t", "\x1b", sidebar.CTRL_R, "\t")
        self.assertEqual(self.app.rename_edit["hint"], sidebar.NAME_STILL_FINISHING)
        self.answered()
        self.assertEqual(len(self.asked), 1)


class FolderListingsOffThread(unittest.TestCase):
    def test_a_hung_folder_never_blocks_and_never_piles_up_threads(self):
        release = threading.Event()
        all_started = threading.Barrier(sidebar.MAX_LISTINGS + 1, timeout=5)
        calls = []

        def hang(folder):
            calls.append(folder)
            all_started.wait()
            release.wait(5)
            return ["late"]

        listings = sidebar.Listings()
        with mock.patch.object(sidebar, "list_subdirs", side_effect=hang):
            began = time.monotonic()
            for _ in range(20):
                self.assertIsNone(listings.get("/hung"))
            self.assertLess(time.monotonic() - began, 0.5)
            for i in range(sidebar.MAX_LISTINGS + 3):
                listings.get(f"/other{i}")
            all_started.wait()
            self.assertEqual(calls.count("/hung"), 1)
            self.assertEqual(len(calls), sidebar.MAX_LISTINGS)
            release.set()
            deadline = time.monotonic() + 2
            while listings.get("/hung") is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(listings.get("/hung"), ["late"])

    def test_list_subdirs_lists_folders_only_and_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("Alpha", ".hidden"):
                os.mkdir(os.path.join(d, n))
            Path(d, "file").write_text("")
            self.assertEqual(sorted(sidebar.list_subdirs(d)), [".hidden", "Alpha"])
            self.assertIsNone(sidebar.list_subdirs(os.path.join(d, "missing")))
            self.assertIsNone(sidebar.list_subdirs(os.path.join(d, "file")))

    def test_a_folder_read_says_whether_it_is_one(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "file").write_text("")
            listings = sidebar.Listings()
            deadline = time.monotonic() + 2
            while (listings.readable(d) is None or listings.readable(os.path.join(d, "file")) is None) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(listings.readable(d))
            self.assertFalse(listings.readable(os.path.join(d, "file")))
            self.assertEqual(listings.get(os.path.join(d, "file")), [])


def transcript_line(kind, content, at="2026-10-08T09:00:00Z"):
    return json.dumps({"type": kind, "timestamp": at, "message": {"role": kind, "content": content}})


U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U9 = "99999999-9999-4999-8999-999999999999"


class ReadTranscript(unittest.TestCase):
    def test_finds_a_reply_behind_a_huge_line_reading_only_the_tail_and_never_writing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "s.jsonl")
            early = transcript_line("assistant", [{"type": "text", "text": "Too early."}])
            reply = transcript_line("assistant", [{"type": "text", "text": "Done here."}])
            padding = json.dumps({"type": "attachment", "x": "p" * core.TRANSCRIPT_FIRST_BYTES})
            filler = "\n".join([early] + [padding] * 3)
            path.write_text(filler + "\n" + reply + "\n" + padding + "\n")
            before = os.stat(path)
            with mock.patch.object(sidebar, "open", wraps=open, create=True) as opened:
                found, ok = sidebar.read_transcript(str(path), before.st_size)
            self.assertEqual((found.reply, ok), ("Done here.", True))
            self.assertEqual([c.args[1] for c in opened.call_args_list], ["rb"])
            self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)

    def test_a_missing_file_fails_and_a_garbled_one_reads_as_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sidebar.read_transcript(os.path.join(d, "missing.jsonl"), 10), (None, False))
            Path(d, "bad.jsonl").write_bytes(b"\xff\xfe{not json\n")
            self.assertEqual(sidebar.read_transcript(os.path.join(d, "bad.jsonl"), 13), (None, True))


class TranscriptReader(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.reader = sidebar.Transcripts(self.root)

    def write(self, folder, uuid, *lines):
        os.makedirs(os.path.join(self.root, "projects", folder), exist_ok=True)
        path = os.path.join(self.root, "projects", folder, f"{uuid}.jsonl")
        with open(path, "a") as f:
            f.write("".join(line + "\n" for line in lines))
        return path

    def row(self, sid, cwd, uuid):
        return core.Session(sid, sid, cwd, "done", "", "", None, True, uuid)

    def test_reads_the_transcript_where_claude_code_keeps_it_for_the_folder(self):
        self.write("-tmp-a-proj", U1, transcript_line("assistant", [{"type": "text", "text": "Hi."}]))
        self.reader.want([self.row("s1", "/tmp/a.proj", U1), self.row("s2", "/tmp/b", "")])
        self.reader.read_all()
        self.assertEqual({sid: t.reply for sid, t in self.reader.take().items()}, {"s1": "Hi."})
        self.assertIsNone(self.reader.take())

    def test_a_transcript_elsewhere_is_found_by_its_file_name_and_a_miss_backs_off(self):
        self.write("-some-shortened-name-1234", U1, transcript_line("user", "go"))
        self.write("-other", U9, transcript_line("user", "not this one"))
        self.assertTrue(self.reader.locate("/tmp/x", U1).endswith(f"-some-shortened-name-1234/{U1}.jsonl"))
        self.assertIsNone(self.reader.locate("/tmp/x", U2))
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("searched again")):
            self.assertIsNone(self.reader.locate("/tmp/x", U2))

    def test_a_session_id_not_shaped_like_one_is_never_looked_up(self):
        self.write("-tmp-a", "notes", transcript_line("user", "go"))
        for bad in ("notes", "../" + U1[3:], U1 + "/x", U1[:-1], ""):
            with self.subTest(bad=bad), \
                    mock.patch.object(sidebar.os.path, "isfile", side_effect=AssertionError("looked up")), \
                    mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("searched")):
                self.assertIsNone(self.reader.locate("/tmp/a", bad))

    def test_a_missing_projects_folder_backs_off_too(self):
        with self.assertRaises(OSError):
            self.reader.locate("/tmp/x", U1)
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("searched again")):
            self.assertIsNone(self.reader.locate("/tmp/x", U1))

    def test_an_unchanged_file_is_not_read_again(self):
        path = self.write("-tmp-a", U1, transcript_line("user", "go"))
        self.reader.want([self.row("s1", "/tmp/a", U1)])
        with mock.patch.object(sidebar, "read_transcript", wraps=sidebar.read_transcript) as reads:
            self.reader.read_all()
            self.reader.read_all()
            self.assertEqual(reads.call_count, 1)
            self.write("-tmp-a", U1, transcript_line("assistant", [{"type": "text", "text": "Went."}]))
            self.reader.read_all()
            self.assertEqual(reads.call_count, 2)
        self.assertEqual(self.reader.take()["s1"].reply, "Went.")
        self.assertTrue(os.path.exists(path))

    def test_a_failed_read_is_tried_again_while_an_empty_one_is_not(self):
        self.write("-tmp-a", U1, transcript_line("user", "go"))
        self.write("-tmp-b", U2, "{not json")
        self.reader.want([self.row("s1", "/tmp/a", U1), self.row("s2", "/tmp/b", U2)])
        real = sidebar.read_transcript
        failed = []

        def flaky(path, size):
            if U1 in path and not failed:
                failed.append(path)
                return None, False
            return real(path, size)

        with mock.patch.object(sidebar, "read_transcript", side_effect=flaky) as reads:
            self.reader.read_all()
            self.reader.read_all()
            self.reader.read_all()
        self.assertEqual(sorted(U1 in c.args[0] for c in reads.call_args_list), [False, True, True])
        self.assertEqual(self.reader.take()["s1"].prompt, "go")

    def test_a_session_no_longer_listed_is_forgotten(self):
        self.write("-elsewhere", U1, transcript_line("user", "go"))
        self.reader.want([self.row("s1", "/tmp/a", U1), self.row("s2", "/tmp/b", U2)])
        self.reader.read_all()
        self.reader.take()
        self.assertEqual((set(self.reader.paths), set(self.reader.misses)), ({U1}, {U2}))
        self.reader.want([])
        self.reader.read_all()
        self.assertEqual(self.reader.take(), {})
        self.assertEqual((self.reader.paths, self.reader.misses, self.reader.stamps), ({}, {}, {}))

    def test_the_poller_hands_its_sessions_to_the_reader(self):
        poller = sidebar.Poller("claude")
        rows = [self.row("s1", "/tmp/a", U1)]
        with mock.patch.object(sidebar, "fetch_sessions", return_value=(rows, None)):
            poller.poll()
        self.assertEqual(poller.transcripts.wanted, {"s1": ("/tmp/a", U1)})
        self.assertEqual(set(poller.summaries.wanted), {"s1"})

    def test_an_empty_listing_keeps_what_the_workers_want(self):
        poller = sidebar.Poller("claude")
        rows = [self.row("s1", "/tmp/a", U1)]
        with mock.patch.object(sidebar, "fetch_sessions", return_value=(rows, None)):
            poller.poll()
        wanted = (dict(poller.transcripts.wanted), set(poller.summaries.wanted), set(poller.resolver.wanted))
        with mock.patch.object(sidebar, "fetch_sessions", return_value=([], None)):
            self.assertEqual(poller.poll(), ([], None))
        self.assertEqual(
            (dict(poller.transcripts.wanted), set(poller.summaries.wanted), set(poller.resolver.wanted)), wanted,
        )

    def test_path_is_the_file_read_for_a_listed_session(self):
        self.write("-tmp-a", U1, transcript_line("user", "go"))
        self.reader.want([self.row("s1", "/tmp/a", U1)])
        self.reader.read_all()
        self.assertEqual(self.reader.path("s1"), self.reader.stamps["s1"][0])
        self.assertTrue(self.reader.path("s1").endswith(f"{U1}.jsonl"))
        self.assertIsNone(self.reader.path("unknown"))


def requests_file(folder, *requests, name="s.jsonl"):
    path = Path(folder, name)
    lines = []
    for text in requests:
        lines.append(transcript_line("user", text))
        lines.append(transcript_line("assistant", [{"type": "text", "text": f"Reply to {text}."}]))
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return str(path)


class ReadRequests(unittest.TestCase):
    def test_reads_read_only_and_never_writes(self):
        with tempfile.TemporaryDirectory() as d:
            path = requests_file(d, "write a poem", "now a csv parser")
            before = os.stat(path)
            with mock.patch.object(sidebar, "open", wraps=open, create=True) as opened:
                found = sidebar.read_requests(path)
            self.assertEqual(found, (["write a poem", "now a csv parser"], None, True))
            self.assertEqual([c.args[1] for c in opened.call_args_list], ["rb"])
            self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)

    def test_a_big_file_keeps_first_and_latest_and_marks_the_gap(self):
        padding = json.dumps({"type": "attachment", "x": "p" * 100})
        lines = [transcript_line("user", "early 1"), transcript_line("user", "early 2")]
        lines += [padding] * 30
        lines += [transcript_line("user", f"middle {i}") for i in range(3)]
        lines += [padding] * 30
        lines += [transcript_line("user", "late 1"), transcript_line("user", "late 2")]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, "NAME_READ_MAX_BYTES", 4096):
            path = Path(d, "big.jsonl")
            path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
            self.assertGreater(os.stat(path).st_size, 4096)
            requests, gap_at, ok = sidebar.read_requests(str(path))
        self.assertTrue(ok)
        self.assertEqual(requests, ["early 1", "early 2", "late 1", "late 2"])
        self.assertEqual(gap_at, 2)

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sidebar.read_requests(os.path.join(d, "missing.jsonl")), (None, None, False))


NAME_JSON = json.dumps({"result": "tea poem and csv parser", "is_error": False, "duration_ms": 700,
                        "total_cost_usd": 0.001, "usage": {"input_tokens": 50, "output_tokens": 8},
                        "modelUsage": {"claude-haiku-5-5": {}}})


class SuggestName(unittest.TestCase):
    def test_every_failure_is_a_line_never_a_raise(self):
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("claude", 30), OSError("boom")):
            with self.subTest(exc=exc), mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                call, error = sidebar.suggest_name("claude", "x", "/tmp")
                self.assertIsNone(call)
                self.assertIsInstance(error, str)
        for done, want in ((completed(1, json.dumps({"is_error": True, "result": "Not logged in"})), "Not logged in"),
                           (completed(2, "", "\n  bad flag\n"), "bad flag"),
                           (completed(3), "claude -p exited with 3"),
                           (completed(1, BUDGET_STOP_JSON), "Reached maximum budget ($0.00001)")):
            with self.subTest(want=want), mock.patch.object(sidebar.subprocess, "run", return_value=done):
                self.assertEqual(sidebar.suggest_name("claude", "x", "/tmp"), (None, want))

    def test_a_refusal_is_the_name_line(self):
        refused = json.dumps({"is_error": True, "stop_reason": "refusal", "result": "API Error: can't help. Start a new session."})
        for code in (0, 1):
            with self.subTest(code=code), mock.patch.object(sidebar.subprocess, "run", return_value=completed(code, refused)):
                self.assertEqual(sidebar.suggest_name("claude", "x", "/tmp"), (None, sidebar.NAME_REFUSED))
        self.assertEqual(sidebar.NAME_REFUSED, "Haiku wouldn't suggest a name for this session; type one instead.")

    def test_the_excerpt_goes_on_stdin_and_the_call_runs_in_the_folder_given(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, NAME_JSON)) as run:
            call, error = sidebar.suggest_name("claude", "--help me", "/tmp/scratch")
        self.assertIsNone(error)
        self.assertEqual(run.call_args.kwargs["input"], "--help me")
        self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/scratch")
        self.assertEqual(run.call_args.args[0], core.name_args("claude"))
        self.assertNotIn("--help me", run.call_args.args[0])
        self.assertEqual(call.text, "tea poem and csv parser")


class NamerWorker(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        scratch = mock.patch.object(sidebar, "summary_scratch", return_value=self.dir)
        scratch.start()
        self.addCleanup(scratch.stop)

    def run_namer(self, path, offered=()):
        namer = sidebar.Namer("claude", path, "a", "emx-test-a", False, offered)
        namer.start()
        namer.join(5)
        return namer.result

    def test_no_transcript_or_no_requests_asks_nothing(self):
        Path(self.dir, "replies.jsonl").write_text(transcript_line("assistant", [{"type": "text", "text": "Hi."}]) + "\n")
        with mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("called claude")) as run:
            self.assertEqual(self.run_namer(None), (None, sidebar.NAME_NO_TRANSCRIPT, None))
            self.assertEqual(self.run_namer(os.path.join(self.dir, "replies.jsonl")), (None, sidebar.NAME_NO_REQUESTS, None))
            self.assertEqual(self.run_namer(self.dir), (None, sidebar.NAME_UNREADABLE, None))
        run.assert_not_called()

    def test_a_name_already_offered_did_not_fit(self):
        path = requests_file(self.dir, "write a poem")
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, NAME_JSON)):
            name, error, call = self.run_namer(path, offered=("tea poem and csv parser",))
        self.assertEqual((name, error), (None, sidebar.NAME_UNFIT))
        self.assertEqual(call.model, "claude-haiku-5-5")

    def test_a_good_answer_is_the_name(self):
        path = requests_file(self.dir, "write a poem")
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, NAME_JSON)) as run:
            name, error, _ = self.run_namer(path)
        self.assertEqual((name, error), ("tea poem and csv parser", None))
        self.assertIn("write a poem", run.call_args.kwargs["input"])


GOOD_JSON = json.dumps({"result": "\"Fixing the tests.\"\nmore", "is_error": False, "duration_ms": 600,
                        "total_cost_usd": 0.0001, "usage": {"input_tokens": 2, "output_tokens": 9},
                        "modelUsage": {"claude-haiku-5-5": {}}})


BUDGET_STOP_JSON = json.dumps({
    "type": "result", "subtype": "error_max_budget_usd", "is_error": True, "num_turns": 1, "stop_reason": "end_turn",
    "terminal_reason": "budget_exhausted", "total_cost_usd": 0.0001639, "duration_ms": 596,
    "usage": {"input_tokens": 0, "output_tokens": 0}, "modelUsage": {"claude-haiku-5-5": {"costUSD": 0.0001639}},
    "errors": ["Reached maximum budget ($0.00001)"],
})


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Summarise(unittest.TestCase):
    def test_every_failure_is_a_line_never_a_raise(self):
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("claude", 30), OSError("boom")):
            with self.subTest(exc=exc), mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                call, error = sidebar.summarise("claude", "x", "/tmp")
                self.assertIsNone(call)
                self.assertIsInstance(error, str)

    def test_a_failing_exit_gives_the_reported_error(self):
        out = json.dumps({"is_error": True, "result": "Not logged in"})
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(1, out, "")):
            self.assertEqual(sidebar.summarise("claude", "x", "/tmp"), (None, "Not logged in"))
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(2, "", "\n  bad flag\n")):
            self.assertEqual(sidebar.summarise("claude", "x", "/tmp"), (None, "bad flag"))
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(3)):
            self.assertEqual(sidebar.summarise("claude", "x", "/tmp"), (None, "claude -p exited with 3"))

    def test_a_call_stopped_by_the_budget_is_a_failure_with_its_own_message(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(1, BUDGET_STOP_JSON, "")):
            self.assertEqual(sidebar.summarise("claude", "x", "/tmp"), (None, "Reached maximum budget ($0.00001)"))

    def test_the_excerpt_goes_on_stdin_and_the_call_runs_in_the_folder_given(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, GOOD_JSON)) as run:
            call, error = sidebar.summarise("claude", "--help me", "/tmp/scratch")
        self.assertIsNone(error)
        self.assertEqual(run.call_args.kwargs["input"], "--help me")
        self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/scratch")
        self.assertNotEqual(run.call_args.kwargs["cwd"], os.getcwd())
        self.assertEqual(run.call_args.args[0], core.summary_args("claude"))
        self.assertNotIn("--help me", run.call_args.args[0])
        self.assertEqual((call.text, call.model), ("Fixing the tests", "claude-haiku-5-5"))


class SummaryScratch(unittest.TestCase):
    def test_a_folder_open_to_group_or_others_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sidebar.tempfile, "gettempdir", return_value=tmp):
            target = os.path.join(tmp, f"emx-summary-{os.getuid()}")
            os.mkdir(target)
            os.chmod(target, 0o755)
            with self.assertRaises(OSError):
                sidebar.summary_scratch()

    def test_a_folder_of_another_user_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sidebar.tempfile, "gettempdir", return_value=tmp):
            with mock.patch.object(sidebar.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(OSError, "not a private folder"):
                    sidebar.summary_scratch()

    def test_it_is_one_private_empty_folder_reused_across_calls(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sidebar.tempfile, "gettempdir", return_value=tmp):
            first = sidebar.summary_scratch()
            self.assertEqual(sidebar.summary_scratch(), first)
            self.assertEqual(os.listdir(first), [])
            self.assertEqual(os.stat(first).st_mode & 0o777, 0o700)
            self.assertTrue(os.path.dirname(first) == tmp and "emx-summary-" in first)

    def test_a_path_that_is_not_a_folder_of_ours_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sidebar.tempfile, "gettempdir", return_value=tmp):
            target = os.path.join(tmp, f"emx-summary-{os.getuid()}")
            os.symlink(tmp, target)
            with self.assertRaises(OSError):
                sidebar.summary_scratch()


class SummariesWorker(unittest.TestCase):
    def setUp(self):
        self.transcripts = sidebar.Transcripts("/nonexistent")
        self.worker = sidebar.Summaries("claude", self.transcripts)
        self.worker.want([session("a", "/p/alpha")])
        self.transcripts.found = {"a": core.Transcript(prompt="Fix it", latest="prompt")}
        self.excerpts = []
        self.fail = False

        def fake(claude, excerpt, cwd):
            self.excerpts.append(excerpt)
            if self.fail:
                return None, "boom"
            return core.SummaryCall(f"Line {len(self.excerpts)}", "claude-haiku-5-5", 1, 1, 0.0, 1, 1, 0, 0), None

        patch = mock.patch.object(sidebar, "summarise", fake)
        patch.start()
        self.addCleanup(patch.stop)
        scratch = mock.patch.object(sidebar, "summary_scratch", return_value="/tmp/emx-test-scratch")
        scratch.start()
        self.addCleanup(scratch.stop)

    def test_an_unchanged_transcript_is_summarised_once(self):
        self.worker.step()
        self.worker.step()
        self.assertEqual(len(self.excerpts), 1)
        self.assertEqual(self.worker.take()["a"].text, "Line 1")

    def test_a_changed_transcript_is_summarised_again_once_the_interval_passed(self):
        self.worker.step()
        self.transcripts.found = {"a": core.Transcript(prompt="Fix it", reply="Done now", latest="reply")}
        self.worker.step()
        self.assertEqual(len(self.excerpts), 1)
        with mock.patch.object(core, "SUMMARY_EVERY_SECONDS", 0):
            self.worker.step()
        self.assertEqual(len(self.excerpts), 2)
        self.assertEqual(len(self.worker.calls), 2)

    def test_a_failed_call_keeps_the_earlier_line(self):
        self.worker.step()
        self.fail = True
        self.transcripts.found = {"a": core.Transcript(prompt="Other", latest="prompt")}
        with mock.patch.object(core, "SUMMARY_EVERY_SECONDS", 0):
            self.worker.step()
        self.assertEqual(len(self.excerpts), 2)
        self.assertEqual(self.worker.found["a"].text, "Line 1")
        self.assertEqual(self.worker.take()["a"].text, "Line 1")

    def test_a_budget_stopped_call_backs_off_and_is_not_asked_again_at_once(self):
        def stopped(claude, excerpt, cwd):
            self.excerpts.append(excerpt)
            return sidebar.core.parse_summary_output(BUDGET_STOP_JSON, 10)

        with mock.patch.object(sidebar, "summarise", stopped):
            for _ in range(5):
                self.worker.step()
        self.assertEqual(len(self.excerpts), 1)
        self.assertEqual(self.worker.failures["a"][0], 1)
        self.assertIsNone(self.worker.take())
        self.assertNotIn("a", self.worker.found)

    def test_a_dropped_session_is_gone_from_the_next_take(self):
        self.worker.step()
        self.worker.take()
        self.worker.want([])
        self.worker.step()
        self.assertEqual(self.worker.take(), {})

    def test_take_is_none_when_nothing_changed(self):
        self.assertIsNone(self.worker.take())
        self.worker.step()
        self.assertIsNotNone(self.worker.take())
        self.assertIsNone(self.worker.take())

    def test_an_excerpt_starting_with_a_flag_reaches_the_call_only_as_the_excerpt(self):
        self.transcripts.found = {"a": core.Transcript(prompt="--help", latest="prompt")}
        self.worker.step()
        self.assertIn("--help", self.excerpts[0])
        self.assertFalse(self.excerpts[0].startswith("-"))

    def test_an_answer_that_cleans_to_nothing_is_not_asked_for_again(self):
        empty = core.SummaryCall("", "m", 1, 1, 0.0, 1, 1, 0, 0)
        with mock.patch.object(sidebar, "summarise", lambda c, e, w: (empty, None)):
            for _ in range(3):
                self.worker.step()
        self.assertEqual(len(self.worker.calls), 1)

    def test_a_refused_scratch_folder_makes_no_call_backs_off_and_keeps_the_line(self):
        self.worker.step()
        self.worker.take()
        self.transcripts.found = {"a": core.Transcript(prompt="Other", latest="prompt")}
        self.worker.scratch = None
        with mock.patch.object(sidebar, "summary_scratch", side_effect=OSError("refused")):
            with mock.patch.object(core, "SUMMARY_EVERY_SECONDS", 0):
                for _ in range(4):
                    self.worker.step()
        self.assertEqual(len(self.excerpts), 1)
        self.assertEqual(self.worker.failures["a"][0], 1)
        self.assertEqual(self.worker.found["a"].text, "Line 1")

    def test_the_session_with_nothing_to_say_is_never_called(self):
        self.transcripts.found = {}
        self.worker.step()
        self.assertEqual(self.excerpts, [])


class RecordingScr(FakeScr):
    def __init__(self, height=20, width=40):
        super().__init__()
        self.size = (height, width)
        self.cells = {}

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        self.cells[y] = self.cells.get(y, "").ljust(x)[:x] + text

    def erase(self):
        self.cells = {}


def blocked(sid, cwd, waiting="permission prompt"):
    return core.Session(sid, f"emx-test-{sid}", cwd, "blocked", "waiting", waiting, None, True, f"u-{sid}")


PERMISSION = core.parse_transcript("\n".join([
    transcript_line("user", "run it"),
    transcript_line("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                   "input": {"command": "npm test", "description": "Run the tests"}}]),
]))


class HaikuRows(AppCase):
    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(width=120)
        self.app.scr = self.scr

    def screen(self):
        return "\n".join(self.scr.cells.get(y, "") for y in range(self.scr.size[0]))

    def test_haikus_line_shows_on_its_row(self):
        self.poll(session("a", "/p/alpha", state="done"))
        self.app.poller.summaries.found = {"a": core.Summary("Wrote the parser", core.DONE, "x", 0.0)}
        self.app.poller.summaries.changed = True
        self.app.refresh()
        self.app.draw()
        self.assertIn("Wrote the parser", self.screen())

    def test_a_line_for_another_state_gives_way_to_the_transcript_line(self):
        self.poll(session("a", "/p/alpha", state="working"))
        self.app.poller.summaries.found = {"a": core.Summary("Wrote the parser", core.DONE, "x", 0.0)}
        self.app.poller.summaries.changed = True
        self.app.poller.transcripts.found = {"a": core.parse_transcript(
            (Path(__file__).parent / "fixtures" / "transcripts" / "working.jsonl").read_text(encoding="utf-8"))}
        self.app.poller.transcripts.changed = True
        self.app.refresh()
        self.app.draw()
        self.assertIn("Sleep for 9 seconds", self.screen())
        self.assertNotIn("Wrote the parser", self.screen())

    def test_an_interrupted_row_drops_the_line_written_before_the_interrupt(self):
        self.poll(core.Session("a", "emx-test-a", "/p/alpha", "working", "idle", "", None, True, "u-a"))
        self.app.poller.summaries.found = {"a": core.Summary("Reading the config files", core.WORKING, "x", 0.0)}
        self.app.poller.summaries.changed = True
        self.app.poller.transcripts.found = {"a": core.Transcript(prompt="Fix it", latest="interrupted")}
        self.app.poller.transcripts.changed = True
        self.app.refresh()
        self.app.draw()
        self.assertIn("interrupted", self.screen())
        self.assertNotIn("Reading the config files", self.screen())

    def test_the_closing_and_armed_labels_win_over_a_line_on_screen(self):
        self.poll(session("a", "/p/alpha", state="done"))
        self.app.poller.summaries.found = {"a": core.Summary("Wrote the parser", core.DONE, "x", 0.0)}
        self.app.poller.summaries.changed = True
        self.app.refresh()
        self.app.confirm = core.Confirm("a", "emx-test-a", True, time.monotonic())
        self.app.draw()
        self.assertIn("ctrl+x again to close", self.screen())
        self.assertNotIn("Wrote the parser", self.screen())
        self.app.confirm = None
        self.app.closing["a"] = None
        self.app.draw()
        self.assertIn("closing…", self.screen())
        self.assertNotIn("Wrote the parser", self.screen())

    def test_without_summaries_today_s_line_shows(self):
        self.poll(blocked("a", "/p/alpha"))
        self.app.poller.transcripts.found = {"a": PERMISSION}
        self.app.poller.transcripts.changed = True
        self.app.refresh()
        self.app.draw()
        self.assertIn("approve", self.screen())

    def test_the_curses_thread_never_runs_a_subprocess_for_it(self):
        self.poll(session("a", "/p/alpha", state="done"))
        self.app.poller.summaries.found = {"a": core.Summary("Wrote the parser", core.DONE, "x", 0.0)}
        self.app.poller.summaries.changed = True
        with mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("ran on the main thread")):
            self.app.refresh()
            self.app.draw()
        self.assertEqual(self.app.notice, "")


class Peek(AppCase):
    """Space peeks at the session under the cursor; ↑↓ move the peek to the next session; Space or Esc close it."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr()
        self.app.scr = self.scr
        self.poll(blocked("a", "/p/alpha"), session("b", "/p/alpha"), session("c", "/p/beta", state="done"))
        self.app.poller.transcripts.found = {"a": PERMISSION}
        self.app.poller.transcripts.changed = True
        self.app.refresh()

    def screen(self):
        return [self.scr.cells.get(y, "") for y in range(self.scr.size[0])]

    def test_the_row_shows_the_request_after_the_state_word(self):
        self.app.draw()
        row = next(line for line in self.screen() if "emx-test-a" in line)
        self.assertIn("Needs input · approve", row)

    def test_space_on_a_session_opens_the_peek_and_it_is_drawn_boxed_and_read_only(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)
        self.app.draw()
        text = "\n".join(self.screen())
        for want in ("╭─ emx-test-a", "Needs input · for", "Run the tests", "$ npm test", "permission prompt", "╰", "␣ Esc close"):
            self.assertIn(want, text)
        self.assertTrue(any(line.startswith(" ✻ emx-test-a") for line in self.screen()))

    def test_space_on_a_header_does_nothing(self):
        self.app.cursor = ("group", "/p/alpha")
        self.app.handle_key(" ")
        self.assertFalse(self.app.peeking)

    def test_arrows_move_the_peek_to_the_next_session_past_headers(self):
        self.assertEqual(self.app.items()[1:3], [("session", "b"), ("session", "a")])
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.app.handle_key(curses.KEY_DOWN)
        self.assertEqual((self.app.cursor, self.app.peeking), (("session", "c"), True))
        self.app.handle_key("k")
        self.assertEqual(self.app.cursor, ("session", "a"))

    def test_space_or_esc_closes_it_and_any_other_key_closes_it_and_still_acts(self):
        for close in (" ", "\x1b"):
            self.app.cursor = ("session", "a")
            self.app.handle_key(" ")
            self.app.handle_key(close)
            self.assertFalse(self.app.peeking)
        self.app.handle_key(" ")
        self.app.handle_key(curses.KEY_RIGHT)
        self.assertFalse(self.app.peeking)
        self.assertEqual(self.opened, [("a", core.OPEN)])

    def test_the_peek_closes_when_its_session_goes(self):
        self.app.cursor = ("session", "c")
        self.app.handle_key(" ")
        self.poll(blocked("a", "/p/alpha"))
        self.app.refresh()
        self.assertFalse(self.app.peeking)

    def test_it_fits_a_short_pane_and_keeps_the_cursor_row_in_view(self):
        self.app.cursor = ("session", "c")
        self.app.handle_key(" ")
        for height in (6, 9, 12, 20):
            self.scr.size = (height, 40)
            self.app.draw()
            screen = self.screen()
            self.assertEqual(len([y for y in self.scr.cells if y >= height]), 0)
            self.assertTrue(any("emx-test-c" in line for line in screen[2:]), (height, screen))
            boxed = [line for line in screen if line.startswith("╭")]
            if boxed:
                bottom = screen.index(boxed[0])
                self.assertTrue(screen[bottom + sum(1 for line in screen[bottom:] if line[:1] in "╭│")].startswith("╰"))

    def test_space_in_a_pane_too_short_for_the_box_says_so_instead_of_peeking(self):
        self.app.cursor = ("session", "a")
        self.scr.size = (8, 40)
        self.app.handle_key(" ")
        self.assertFalse(self.app.peeking)
        self.assertEqual(self.app.notice, sidebar.PEEK_NO_ROOM)
        self.assertEqual(len(core.wrap_words(sidebar.PEEK_NO_ROOM, 39)), 1)
        self.scr.size = (20, 40)
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)

    def test_shrinking_the_pane_closes_the_peek_with_the_notice(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.scr.size = (8, 40)
        self.app.handle_key(curses.KEY_RESIZE)
        self.assertFalse(self.app.peeking)
        self.assertEqual(self.app.notice, sidebar.PEEK_NO_ROOM)
        self.app.draw()
        self.assertFalse(any(line.startswith("╭") for line in self.screen()))

    def box_rows(self):
        return sum(1 for line in self.screen() if line[:1] in ("╭", "│", "╰"))

    def test_an_error_line_draws_the_peek_shorter_and_leaves_it_open(self):
        self.app.cursor = ("session", "a")
        self.scr.size = (11, 40)
        self.app.handle_key(" ")
        self.app.draw()
        before = self.box_rows()
        self.app.list_error = "listing broken"
        self.app.refresh()
        self.assertTrue(self.app.peeking)
        self.assertTrue(0 < self.box_rows() < before, (before, self.screen()))

    def test_an_error_squeezing_the_box_to_nothing_closes_it_without_calling_the_pane_short(self):
        self.app.cursor = ("session", "a")
        self.scr.size = (11, 40)
        self.app.handle_key(" ")
        self.app.list_error = "listing broken " * 6
        self.app.refresh()
        self.assertFalse(self.app.peeking)
        self.assertNotEqual(self.app.notice, sidebar.PEEK_NO_ROOM)
        screen = self.screen()
        self.assertFalse(any(line.startswith("╭") for line in screen))
        self.assertFalse(any("␣ Esc close" in line for line in screen))

    def test_a_long_body_is_wrapped_once_per_change_not_per_draw(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        with mock.patch.object(core, "peek_lines", wraps=core.peek_lines) as wraps:
            for _ in range(3):
                self.app.draw()
            self.assertEqual(wraps.call_count, 1)
            self.scr.size = (20, 50)
            self.app.draw()
            self.assertEqual(wraps.call_count, 2)

    def test_the_footer_and_help_mention_it(self):
        self.assertIn("␣ peek", sidebar.FOOTERS[0])
        self.assertIn("space", dict(sidebar.HELP))


LONG_WAIT = "approve Bash: python3 -m unittest discover -s tests -v"


class WrappedRows(AppCase):
    """On a narrow panel a long row takes several lines; clicks, the cursor and scrolling count every one of them."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(20, 40)
        self.app.scr = self.scr
        self.poll(blocked("a", "/p/alpha", waiting=LONG_WAIT), session("b", "/p/alpha"))
        self.app.refresh()

    def screen(self):
        return [self.scr.cells.get(y, "") for y in range(self.scr.size[0])]

    def rows_of(self, sid):
        return sorted(y for y, item in self.app.row_map.items() if item == ("session", sid))

    def test_the_whole_request_is_on_screen_under_the_row(self):
        self.assertEqual(self.app.notice, "")
        ys = self.rows_of("a")
        self.assertGreater(len(ys), 1)
        self.assertEqual(ys, list(range(ys[0], ys[0] + len(ys))))
        shown = " ".join(self.screen()[y] for y in ys)
        self.assertEqual(" ".join(shown.split()).split("Needs input · ")[1], LONG_WAIT)
        self.assertNotIn("…", shown)
        self.assertTrue(self.screen()[ys[0]].startswith(" ✻ emx-test-a"))
        for y in ys[1:]:
            self.assertTrue(self.screen()[y].startswith(" " * core.ROW_INDENT), self.screen()[y])

    def test_a_click_on_any_line_of_a_wrapped_row_opens_it(self):
        for y in self.rows_of("a"):
            self.opened.clear()
            with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 10, y, 0, curses.BUTTON1_CLICKED)):
                self.app.handle_mouse()
            self.assertEqual(self.opened, [("a", core.OPEN)], y)
        above = self.rows_of("a")[0] - 1
        self.assertEqual(self.app.row_map[above], ("session", "b"))
        self.assertNotIn(self.rows_of("a")[-1] + 1, self.app.row_map)

    def test_the_selection_bar_covers_every_line_of_the_row(self):
        bars = []
        self.app.palette.attr = lambda tone, selected=None: 1 if selected else 0
        self.scr.addstr = lambda y, x, text, attr=0: bars.append(y) if attr and text.strip() == "" else None
        self.app.cursor = ("session", "a")
        self.app.draw()
        self.assertEqual(sorted(set(bars)), self.rows_of("a"))

    def test_scrolling_keeps_the_cursor_row_whole_and_clicks_follow_the_scroll(self):
        self.scr.size = (9, 40)
        self.poll(*[blocked(c, "/p/alpha", waiting=LONG_WAIT) for c in "abcd"])
        self.app.refresh()
        for sid in "dcba":
            self.app.cursor = ("session", sid)
            self.app.draw()
            ys = self.rows_of(sid)
            self.assertEqual(len(ys), len(core.row_lines(
                f"emx-test-{sid}", "", "Needs input", LONG_WAIT, core.session_age(self.app.session(sid), time.time() * 1000), 40,
            )), sid)
            self.assertTrue(self.screen()[ys[0]].startswith(" ✻ emx-test-" + sid), self.screen())
            with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 10, ys[-1], 0, curses.BUTTON1_CLICKED)):
                self.opened.clear()
                self.app.handle_mouse()
            self.assertEqual(self.opened, [(sid, core.OPEN)])

    def test_the_arrows_move_one_session_however_many_lines_it_takes(self):
        self.assertEqual(self.app.items()[1:], [("session", "b"), ("session", "a")])
        self.app.cursor = ("session", "a")
        self.app.handle_key(curses.KEY_UP)
        self.assertEqual(self.app.cursor, ("session", "b"))
        self.app.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.app.cursor, ("session", "a"))

    def test_arming_ctrl_x_keeps_the_rows_height(self):
        self.app.cursor = ("session", "b")
        self.app.draw()
        before = self.rows_of("b"), self.rows_of("a")
        self.app.handle_key(sidebar.CTRL_X)
        # b is working, so the press also starts an interrupt; it is let finish before the test ends.
        self.app.interrupters["b"].join(5)
        self.app.draw()
        self.assertIsNotNone(self.app.confirm)
        self.assertEqual((self.rows_of("b"), self.rows_of("a")), before)
        self.assertIn("ctrl+x again", self.screen()[before[0][0]])

    def test_the_closing_label_keeps_the_rows_height(self):
        self.app.cursor = ("session", "a")
        self.app.draw()
        before = self.rows_of("a"), self.rows_of("b")
        self.assertGreater(len(before[0]), 1)
        self.app.closing = {"a": None}
        self.app.draw()
        self.assertEqual((self.rows_of("a"), self.rows_of("b")), before)
        self.assertIn("closing…", self.screen()[before[0][0]])

    def test_a_rename_edit_on_a_wrapped_row_keeps_the_rows_height(self):
        self.app.cursor = ("session", "b")
        self.app.draw()
        before = self.rows_of("b"), self.rows_of("a")
        self.app.rename_edit = {"id": "b", "old": "emx-test-b", "text": "emx-test-b", "hint": ""}
        self.app.draw()
        self.assertEqual((self.rows_of("b"), self.rows_of("a")), before)
        self.assertIn("emx-test-b▏", self.screen()[before[0][0]])
        for y in before[0][1:]:
            self.assertEqual(self.screen()[y].strip(), "", y)

    def test_the_peek_leaves_the_cursor_row_whole(self):
        self.scr.size = (11, 24)
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.app.draw()
        self.assertTrue(self.app.peeking)
        tall = len(self.rows_of("a"))
        self.assertGreater(tall, core.PEEK_MIN_LIST_ROWS)
        box = next(y for y, line in enumerate(self.screen()) if line.startswith("╭─"))
        self.assertLess(self.rows_of("a")[-1], box)

    def test_a_wide_panel_keeps_the_row_on_one_line(self):
        self.scr.size = (20, 100)
        self.app.draw()
        self.assertEqual(len(self.rows_of("a")), 1)
        self.assertIn("Needs input · " + LONG_WAIT, self.screen()[self.rows_of("a")[0]])


class WhatsNew(AppCase):
    """The first panel to run a newer version shows its changelog lines once, then records the version."""

    LOG = "# Changelog\n\n## 1.1.0\n\n- Shiny thing.\n\n## 1.0.0\n\n- First.\n"

    def start(self, recorded=None, version="1.1.0", log=LOG):
        if recorded is not None:
            core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "last_version": recorded})
        with mock.patch.object(sidebar, "__version__", version), mock.patch.object(sidebar, "read_changelog", return_value=log):
            app = sidebar.App(FakeScr(), tmux=None, claude="claude")
        app.housekeep = lambda fresh_list: None
        return app

    def recorded(self):
        return core.load_prefs(core.prefs_path()).get("last_version")

    def test_a_newer_version_shows_its_lines_once_and_records_itself(self):
        app = self.start("1.0.0")
        self.assertEqual(app.notice, "New in emx 1.1.0:\n· Shiny thing.")
        self.assertEqual(self.recorded(), "1.1.0")
        self.assertEqual(self.start().notice, "")

    def test_the_first_run_records_quietly(self):
        core.prefs_path().unlink(missing_ok=True)
        app = self.start()
        self.assertEqual(app.notice, "")
        self.assertEqual(self.recorded(), "1.1.0")

    def test_the_same_or_an_older_version_shows_nothing_and_keeps_the_record(self):
        for version in ("1.1.0", "1.0.5"):
            with self.subTest(version=version):
                app = self.start("1.1.0", version=version)
                self.assertEqual(app.notice, "")
                self.assertEqual(self.recorded(), "1.1.0")

    def test_no_changelog_lines_still_records(self):
        app = self.start("1.0.0", log="")
        self.assertEqual(app.notice, "")
        self.assertEqual(self.recorded(), "1.1.0")

    def test_other_prefs_are_kept(self):
        core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "side": "left", "pinned": ["a"], "last_version": "1.0.0"})
        self.start()
        self.assertEqual(
            core.load_prefs(core.prefs_path()),
            {**core.DEFAULT_PREFS, "side": "left", "pinned": ["a"], "last_version": "1.1.0"},
        )

    def test_any_key_dismisses_it_and_still_does_its_job(self):
        app = self.start("1.0.0")
        app.poller.result = ([session("a", "/p/alpha"), session("b", "/p/beta")], None)
        app.refresh()
        app.handle_key(curses.KEY_RESIZE)
        self.assertTrue(app.notice.startswith("New in emx"))
        cursor = app.cursor
        app.handle_key("j")
        self.assertEqual(app.notice, "")
        self.assertNotEqual(app.cursor, cursor)

    def test_a_key_after_another_notice_replaced_it_leaves_that_notice(self):
        app = self.start("1.0.0")
        app.say("Pinned x.")
        app.handle_key("j")
        self.assertEqual(app.notice, "Pinned x.")

    def test_it_goes_by_itself_after_a_while(self):
        app = self.start("1.0.0")
        self.assertAlmostEqual(app.notice_until - time.monotonic(), sidebar.WHATS_NEW_SECONDS, delta=5)
        app.notice_until = time.monotonic() - 1
        app.refresh()
        self.assertEqual(app.notice, "")

    def test_it_shows_under_the_list(self):
        app = self.start("1.0.0")
        self.assertEqual([t for t, _ in app.bottom_lines(38)][:2], ["New in emx 1.1.0:", "· Shiny thing."])

    def test_prefs_without_a_record_stay_quiet_and_record(self):
        core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "side": "left"})
        app = self.start()
        self.assertEqual(app.notice, "")
        self.assertEqual(self.recorded(), "1.1.0")

    def test_a_skipped_version_s_lines_show_too(self):
        log = "# Changelog\n\n## 1.2.0\n\n- Newest.\n\n## 1.1.0\n\n- Middle.\n\n## 1.0.0\n\n- First.\n"
        app = self.start("1.0.0", version="1.2.0", log=log)
        self.assertEqual(app.notice, "New in emx 1.2.0:\n· Newest.\nNew in emx 1.1.0:\n· Middle.")

    def test_the_shipped_1_0_0_note_is_whole_or_says_what_is_cut(self):
        log = cli.read_changelog()
        app = self.start("0.9.0", version="1.0.0", log=log)
        note = [t for t, _ in app.bottom_lines(39, 40)][:-1]
        self.assertLessEqual(len(note), max(6, 40 // 2))
        joined = " ".join(note)
        whole = all(line in joined for line in core.changelog_entry(log, "1.0.0"))
        self.assertTrue(whole or re.fullmatch(r"… \d+ more in CHANGELOG\.md", note[-1]), note)

    def test_a_cut_note_says_how_much_is_missing(self):
        log = "# Changelog\n\n## 1.1.0\n\n" + "".join(f"- Line number {n}.\n" for n in range(30))
        app = self.start("1.0.0", log=log)
        lines = [t for t, _ in app.bottom_lines(39, 20)]
        self.assertEqual(lines[:2], ["New in emx 1.1.0:", "· Line number 0."])
        self.assertEqual(lines[9], "… 22 more in CHANGELOG.md")

    def test_other_notices_keep_six_lines(self):
        app = self.start()
        app.say("word " * 200)
        notes = [t for t, _ in app.bottom_lines(39, 40) if t.startswith("word")]
        self.assertEqual(len(notes), 6)

    def test_a_failed_save_shows_the_error_instead(self):
        core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "last_version": "1.0.0"})
        with mock.patch.object(core, "save_prefs", side_effect=OSError("read-only")):
            app = self.start()
        self.assertTrue(app.notice_error)
        self.assertIn("read-only", app.notice)


class AttrScr(RecordingScr):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drawn = []

    def addstr(self, y, x, text, attr=0):
        super().addstr(y, x, text, attr)
        self.drawn.append((y, x, text, attr))


class TonePalette:
    def attr(self, tone, selected=False):
        return 1000 + sum(map(ord, tone))


def nearest_256(r, g, b):
    levels = (0, 95, 135, 175, 215, 255)
    colours = {16 + 36 * i + 6 * j + k: (levels[i], levels[j], levels[k]) for i in range(6) for j in range(6) for k in range(6)}
    colours |= {232 + n: (8 + 10 * n,) * 3 for n in range(24)}
    return min(colours, key=lambda c: sum((a - b) ** 2 for a, b in zip(colours[c], (r, g, b))))


class TopLine(AppCase):
    def drawn_top_line(self, focused):
        scr = AttrScr()
        app = sidebar.App(scr, tmux=None, claude="claude")
        app.palette = TonePalette()
        app.focused = focused
        app.draw_top_line(38)
        return {text: attr for y, x, text, attr in scr.drawn if y == 0}

    def test_the_logo_is_copper_the_name_bold_the_version_dim(self):
        drawn = self.drawn_top_line(True)
        self.assertEqual(drawn["⚒"], TonePalette().attr("copper"))
        self.assertEqual(drawn["emalatxana"], curses.A_BOLD)
        self.assertEqual(drawn[sidebar.__version__], curses.A_DIM)

    def test_the_name_dims_when_the_panel_loses_focus(self):
        self.assertEqual(self.drawn_top_line(False)["emalatxana"], curses.A_DIM)

    def test_copper_is_the_nearest_256_colour_to_d08a4a(self):
        self.assertEqual(sidebar.TONES["copper"][0], nearest_256(0xD0, 0x8A, 0x4A))

    def test_the_top_line_draws_the_logo_then_the_counts(self):
        scr = RecordingScr()
        app = sidebar.App(scr, tmux=None, claude="claude")
        app.housekeep = lambda fresh_list: None
        app.poller.result = ([session("a", "/p/alpha")], None)
        app.refresh()
        self.assertEqual(scr.cells[0], f" ⚒ emalatxana {sidebar.__version__}".ljust(30) + "1 working")


class InterruptTmux(RenameTmux):
    """The panel (%1) has focus; main (%2) shows `working` mid-turn. Records every call in order, with the screen a capture returned."""

    def __init__(self):
        super().__init__()
        self.list[1] = Pane("%2", "@1", False, "", "working", 100, 141)
        self.view("working-streaming", "prompt-empty")
        self.screen_name = "working-streaming"
        self.screens = None
        self.events = []
        self.keys_sent = []

    def view(self, screen, prompt):
        super().view(screen, prompt)
        self.screen_name = screen

    def capture(self, pane):
        if self.screens:
            name = self.screens.pop(0)
            self.screen, _ = fixture_screen(name)
            self.screen_name = name
        self.events.append(("capture", pane, self.screen_name))
        return super().capture(pane)

    def key_if(self, pane, gate, key, typed, report):
        self.note()
        self.gates.append(gate)
        self.events.append(("key_if", pane, key))
        if self.gate_says == typed:
            self.keys_sent.append((pane, key))
        return self.gate_says

    def new_hidden_window(self, command):
        self.note()
        self.events.append(("new_hidden_window", command))
        self.list.append(Pane("%5", "@3", False, "", "", 100, 100))
        return "%5"

    def set_pane_option(self, pane, name, value):
        self.events.append(("set_pane_option", pane, name, value))
        self.list = [Pane(p.id, p.window, p.dead, p.role, value, p.width, p.window_width) if p.id == pane else p
                     for p in self.list]

    def swap(self, source, target):
        self.events.append(("swap", source, target))

    def select(self, pane):
        self.events.append(("select", pane))

    def kill_pane(self, pane):
        self.events.append(("kill", pane))


def idle_row(sid, cwd):
    return core.Session(sid, f"emx-test-{sid}", cwd, "working", "idle", "", 0.0, True)


class InterruptSession(AppCase):
    """One Ctrl+X on a working row sends Esc into its viewer, off the curses thread, after a fresh listing and the screen checks, and arms the close."""

    def setUp(self):
        super().setUp()
        self.working = session("working", "/p/alpha")
        self.poll(self.working, session("b", "/p/beta"))
        self.app.refresh()
        self.app.tmux = InterruptTmux()
        self.app.cursor = ("session", "working")
        self.listings = [[self.working]]
        self.after = idle_row("working", "/p/alpha")
        self.fetched = []
        test = self

        def fetch(claude):
            test.fetched.append(threading.current_thread() is threading.main_thread())
            if test.listings:
                rows = test.listings.pop(0)
                return (None, "claude agents timed out") if rows is None else (rows, None)
            return [test.after], None

        for p in (
            mock.patch.object(sidebar, "fetch_sessions", fetch),
            mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0.01),
            mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0.2),
            mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("ran a command in a test")),
        ):
            p.start()
            self.addCleanup(p.stop)

    def press(self, *keys):
        for key in keys:
            self.app.handle_key(key)

    def settle(self):
        for worker in (*self.app.interrupters.values(), self.app.closer):
            if worker is not None:
                worker.join(5)
        self.app.refresh()

    def kinds(self):
        return [e[0] for e in self.app.tmux.events]

    def test_one_ctrl_x_on_a_working_row_sends_escape_through_key_if_off_the_curses_thread(self):
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Interrupting emx-test-working…")
        self.settle()
        self.assertEqual(self.app.tmux.keys_sent, [("%2", "Escape")])
        self.assertEqual(self.app.tmux.gates, [core.type_gate("%2", "%1", "working", 26, sidebar.SESSION_OPT, cursor_x=2)])
        self.assertNotIn(True, self.app.tmux.threads)
        self.assertNotIn(True, self.fetched)
        self.assertEqual((self.app.notice, self.app.notice_error), ("Interrupted emx-test-working.", False))
        self.assertNotIn("new_hidden_window", self.kinds())
        self.assertNotIn("swap", self.kinds())
        self.assertNotIn("select", self.kinds())

    def test_it_also_arms_the_close(self):
        self.press(sidebar.CTRL_X)
        self.assertIsInstance(self.app.confirm, core.Confirm)
        self.assertEqual(self.app.confirm.id, "working")
        self.assertIn(sidebar.CONFIRM_FOOTER, [t for t, _ in self.app.bottom_lines(39)])
        self.settle()

    def test_a_draft_in_the_prompt_still_interrupts(self):
        self.app.tmux.view("working-streaming", "prompt-text")
        x, y = PROMPT_CURSORS["cursors"]["prompt-text"]["x"], PROMPT_CURSORS["cursors"]["prompt-text"]["y"]
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.app.tmux.keys_sent, [("%2", "Escape")])
        self.assertEqual(self.app.tmux.gates, [core.type_gate("%2", "%1", "working", y, sidebar.SESSION_OPT, cursor_x=x)])

    def test_an_idle_or_waiting_row_gets_no_escape_and_only_arms(self):
        for row in (idle_row("working", "/p/alpha"), session("working", "/p/alpha", state="blocked"),
                    session("working", "/p/alpha", state="done")):
            with self.subTest(state=row.state, status=row.status):
                self.app.confirm = None
                self.poll(row, session("b", "/p/beta"))
                self.app.refresh()
                self.press(sidebar.CTRL_X)
                self.assertEqual(self.app.interrupters, {})
                self.assertEqual(self.app.confirm.id, "working")
        self.assertEqual(self.app.tmux.events, [])
        self.assertEqual(self.fetched, [])

    def test_a_scrolled_back_or_other_session_viewer_gets_nothing_and_says_why(self):
        cases = {
            "scrolled back": lambda tmux: setattr(tmux, "in_mode", True),
            "isn't showing its prompt": lambda tmux: tmux.view("named-viewer", "prompt-empty"),
        }
        for want, arrange in cases.items():
            with self.subTest(want=want):
                self.app.tmux = InterruptTmux()
                arrange(self.app.tmux)
                self.listings = [[self.working]]
                self.app.confirm = None
                self.press(sidebar.CTRL_X)
                self.settle()
                self.assertEqual(self.app.tmux.keys_sent, [])
                self.assertNotIn("key_if", self.kinds())
                self.assertIn(want, self.app.notice)
                self.assertTrue(self.app.notice_error)

    def test_a_second_press_inside_the_window_starts_the_close_not_a_second_escape(self):
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run):
            self.press(sidebar.CTRL_X)
            first = self.app.interrupters["working"]
            first.join(5)
            self.press(sidebar.CTRL_X)
            self.assertIs(self.app.interrupters["working"], first)
            self.assertIsNotNone(self.app.closer)
            self.settle()
        self.assertEqual(self.kinds().count("key_if"), 1)
        self.assertEqual(ran, [["claude", "stop", "working"], ["claude", "rm", "working"]])

    def test_the_interrupted_row_reads_idle_with_a_still_glyph(self):
        scr = RecordingScr(20, 60)
        self.app.scr = scr
        self.poll(idle_row("working", "/p/alpha"), session("b", "/p/beta"))
        self.app.refresh()
        row = next(line for line in scr.cells.values() if "emx-test-working" in line)
        self.assertTrue(row.startswith(" ✻ emx-test-working"), row)
        self.assertIn("Idle", row)
        self.assertNotIn("Working", row)
        with mock.patch.object(sidebar.time, "monotonic", return_value=12345.6):
            self.app.draw()
        self.assertTrue(next(line for line in scr.cells.values() if "emx-test-working" in line).startswith(" ✻ "))

    def no_viewer(self):
        tmux = self.app.tmux
        tmux.list = [p for p in tmux.list if p.session != "working"]

    def test_with_no_viewer_a_hidden_viewer_is_opened_and_gets_the_escape(self):
        self.no_viewer()
        self.app.tmux.screens = ["agent-view", "agent-view", "working-streaming"]
        self.press(sidebar.CTRL_X)
        self.settle()
        tmux = self.app.tmux
        opened = [e for e in tmux.events if e[0] == "new_hidden_window"]
        self.assertEqual(opened, [("new_hidden_window", core.viewer_command("claude", "working"))])
        self.assertIn(("set_pane_option", "%5", sidebar.SESSION_OPT, "working"), tmux.events)
        self.assertNotIn("swap", self.kinds())
        self.assertNotIn("select", self.kinds())
        at = self.kinds().index("key_if")
        captures = [e for e in tmux.events[:at] if e[0] == "capture"]
        self.assertEqual(captures[-1], ("capture", "%5", "working-streaming"))
        self.assertEqual([e for e in captures if e[2] != "working-streaming"],
                         [("capture", "%5", "agent-view"), ("capture", "%5", "agent-view")])
        self.assertEqual(tmux.keys_sent, [("%5", "Escape")])
        self.assertNotIn(True, tmux.threads)
        self.assertNotIn("kill", self.kinds())
        self.assertEqual(self.app.notice, "Interrupted emx-test-working.")

    def test_with_no_viewer_a_stopped_or_unlisted_session_or_a_failing_listing_opens_nothing(self):
        cases = {
            "isn't running": [session("working", "/p/alpha", state="stopped", pid=None)],
            "isn't listed": [session("b", "/p/beta")],
            "Couldn't list": None,
        }
        for want, listing in cases.items():
            with self.subTest(want=want):
                self.app.tmux = InterruptTmux()
                self.no_viewer()
                self.listings = [listing]
                self.app.confirm = None
                self.press(sidebar.CTRL_X)
                self.settle()
                self.assertNotIn("new_hidden_window", self.kinds())
                self.assertNotIn("key_if", self.kinds())
                self.assertIn(want, self.app.notice)
                self.assertIn("emx-test-working", self.app.notice)

    def test_a_hidden_viewer_that_never_shows_the_prompt_gets_no_escape(self):
        self.no_viewer()
        self.app.tmux.screens = ["agent-view"] * 1000
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertIn("new_hidden_window", self.kinds())
        self.assertNotIn("key_if", self.kinds())
        self.assertEqual(self.app.notice, "emx-test-working's viewer didn't come up in time, so emx didn't interrupt it.")
        self.assertTrue(self.app.notice_error)

    def interrupt_hidden(self, *screens):
        """Ctrl+X with no viewer open, the hidden viewer showing `screens` in turn; returns the Interrupter once it has ended."""
        self.no_viewer()
        self.app.tmux.screens = list(screens)
        self.press(sidebar.CTRL_X)
        interrupter = self.app.interrupters["working"]
        self.settle()
        return interrupter

    def test_a_hidden_viewer_that_times_out_before_its_prompt_is_killed(self):
        interrupter = self.interrupt_hidden(*["agent-view"] * 1000)
        self.assertIn(("kill", "%5"), self.app.tmux.events)
        self.assertNotIn("key_if", self.kinds())
        self.assertTrue(interrupter.ended)
        self.assertEqual(self.app.notice, "emx-test-working's viewer didn't come up in time, so emx didn't interrupt it.")

    def test_an_error_after_the_hidden_viewer_opens_kills_it(self):
        failures = {
            "tmux: server busy": sidebar.TmuxError("server busy"),
            "interrupt failed: boom": RuntimeError("boom"),
        }
        for want, failure in failures.items():
            with self.subTest(want=want):
                self.app.tmux = InterruptTmux()

                def capture(pane, failure=failure):
                    raise failure

                self.app.tmux.capture = capture
                self.listings = [[self.working]]
                self.app.confirm = None
                interrupter = self.interrupt_hidden()
                self.assertIn(("new_hidden_window", core.viewer_command("claude", "working")), self.app.tmux.events)
                self.assertEqual(self.app.tmux.events[-1], ("kill", "%5"))
                self.assertTrue(interrupter.ended)
                self.assertIn(want, self.app.notice)

    def test_a_refusal_after_the_hidden_viewer_comes_up_kills_it(self):
        self.app.tmux.gate_says = "0 0 1 2 26 working"
        interrupter = self.interrupt_hidden("working-streaming")
        self.assertEqual(self.app.tmux.keys_sent, [])
        self.assertIn("Focus left the panel", self.app.notice)
        self.assertEqual(self.app.tmux.events[-1], ("kill", "%5"))
        self.assertTrue(interrupter.ended)

    def test_a_successful_interrupt_keeps_its_hidden_viewer(self):
        interrupter = self.interrupt_hidden("agent-view", "working-streaming")
        self.assertEqual(self.app.tmux.keys_sent, [("%5", "Escape")])
        self.assertNotIn("kill", self.kinds())
        self.assertTrue(interrupter.ended)
        self.assertEqual(self.app.notice, "Interrupted emx-test-working.")

    def test_a_hidden_viewer_he_opened_in_main_while_it_came_up_is_left_to_him(self):
        tmux = self.app.tmux
        captured = tmux.capture

        def capture(pane):
            tmux.list = [Pane(p.id, "@1", p.dead, p.role, p.session, p.width, p.window_width) if p.id == "%5" else p
                         for p in tmux.list]
            return captured(pane)

        tmux.capture = capture
        interrupter = self.interrupt_hidden(*["agent-view"] * 1000)
        self.assertNotIn("kill", self.kinds())
        self.assertTrue(interrupter.ended)

    def test_a_hidden_viewer_whose_kill_failed_after_a_timeout_is_killed_by_the_next_close_or_blocks_it(self):
        tmux = self.app.tmux
        kills = []

        def refuse(pane):
            kills.append(pane)
            raise sidebar.TmuxError("server busy")

        tmux.kill_pane = refuse
        interrupter = self.interrupt_hidden(*["agent-view"] * 1000)
        self.assertEqual(kills, ["%5"])
        self.assertFalse(interrupter.ended)
        self.assertEqual(self.app.interrupters, {})
        confirm = core.Confirm("working", "emx-test-working", True, 0.0)
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run):
            self.app.start_close(confirm)
            self.assertEqual(self.app.closer.wait_for, (interrupter,))
            self.app.closer.join(5)
            self.app.refresh()
            self.assertEqual(kills, ["%5", "%5"])
            self.assertEqual(ran, [])
            self.assertEqual(self.app.notice,
                             "emx couldn't close the viewer it opened for emx-test-working, so it didn't close it.")
            self.assertEqual(self.app.unended, {"working": [interrupter]})

            def kill(pane):
                kills.append(pane)
                tmux.list = [p for p in tmux.list if p.id != pane]

            tmux.kill_pane = kill
            self.app.start_close(confirm)
            self.app.closer.join(5)
            self.app.refresh()
        self.assertEqual(kills, ["%5", "%5", "%5"])
        self.assertTrue(interrupter.ended)
        self.assertEqual(ran, [["claude", "stop", "working"], ["claude", "rm", "working"]])
        self.assertEqual(self.app.unended, {})

    def test_a_hidden_viewer_tmux_cant_place_at_the_end_is_left_for_the_next_close_to_kill(self):
        tmux = self.app.tmux
        listed = tmux.panes

        def panes():
            if "new_hidden_window" in [e[0] for e in tmux.events]:
                raise sidebar.TmuxError("server busy")
            return listed()

        tmux.panes = panes
        interrupter = self.interrupt_hidden(*["agent-view"] * 1000)
        self.assertNotIn("kill", self.kinds())
        self.assertFalse(interrupter.ended)
        self.assertEqual(self.app.unended, {"working": [interrupter]})
        tmux.panes = listed
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run):
            self.app.start_close(core.Confirm("working", "emx-test-working", True, 0.0))
            self.app.closer.join(5)
            self.app.refresh()
        self.assertIn(("kill", "%5"), tmux.events)
        self.assertTrue(interrupter.ended)
        self.assertEqual(ran, [["claude", "stop", "working"], ["claude", "rm", "working"]])

    def test_a_close_started_before_the_escape_cancels_it_and_waits_for_it(self):
        self.no_viewer()
        ran = []
        held, release = threading.Event(), threading.Event()
        calls = []

        def fetch(claude):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                held.set()
                release.wait(5)
            return [self.working], None

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar, "fetch_sessions", fetch), mock.patch.object(sidebar.subprocess, "run", run):
            self.press(sidebar.CTRL_X)
            interrupter = self.app.interrupters["working"]
            self.assertTrue(held.wait(5))
            self.press(sidebar.CTRL_X)
            self.assertTrue(interrupter.cancel.is_set())
            self.assertEqual(self.app.closer.wait_for, (interrupter,))
            time.sleep(0.1)
            self.assertEqual(ran, [])
            release.set()
            self.app.closer.join(5)
            interrupter.join(5)
            self.app.refresh()
        self.assertNotIn("new_hidden_window", self.kinds())
        self.assertNotIn("key_if", self.kinds())
        self.assertEqual(ran, [["claude", "stop", "working"], ["claude", "rm", "working"]])
        self.assertEqual(self.app.notice, "Closed emx-test-working.")

    def close_while_the_hidden_viewer_comes_up(self):
        """Ctrl+X twice with no viewer open, the second press landing while the hidden viewer is still not at its prompt; the stop and rm go into tmux.events, so the kill and them share one order."""
        self.no_viewer()
        tmux = self.app.tmux
        tmux.screens = ["agent-view"] * 100000

        def run(args, **kw):
            tmux.events.append(("run", args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 5), mock.patch.object(sidebar.subprocess, "run", run):
            self.press(sidebar.CTRL_X)
            interrupter = self.app.interrupters["working"]
            deadline = time.monotonic() + 5
            while ("capture", "%5", "agent-view") not in list(tmux.events) and time.monotonic() < deadline:
                time.sleep(0.005)
            self.press(sidebar.CTRL_X)
            self.assertTrue(interrupter.cancel.is_set())
            self.app.closer.join(5)
            interrupter.join(5)
        return interrupter

    def test_a_close_while_the_hidden_viewer_comes_up_kills_it_before_the_stop(self):
        interrupter = self.close_while_the_hidden_viewer_comes_up()
        events = self.app.tmux.events
        self.assertIn(("capture", "%5", "agent-view"), events)
        kill = events.index(("kill", "%5"))
        stop = events.index(("run", ["claude", "stop", "working"]))
        self.assertLess(kill, stop)
        self.assertNotIn("key_if", self.kinds())
        self.assertTrue(interrupter.ended)
        self.app.refresh()
        self.assertEqual(self.app.notice, "Closed emx-test-working.")

    def test_a_hidden_viewer_that_wont_die_leaves_the_session_unclosed(self):
        tmux = self.app.tmux

        def refuse(pane):
            tmux.events.append(("kill", pane))
            raise sidebar.TmuxError("server busy")

        tmux.kill_pane = refuse
        interrupter = self.close_while_the_hidden_viewer_comes_up()
        self.assertFalse(interrupter.ended)
        self.assertNotIn("run", self.kinds())
        self.app.refresh()
        self.assertEqual(self.app.notice,
                         "emx couldn't close the viewer it opened for emx-test-working, so it didn't close it.")
        self.assertTrue(self.app.notice_error)

    def test_a_hidden_viewer_already_gone_when_the_kill_fails_still_lets_the_close_run(self):
        tmux = self.app.tmux

        def gone(pane):
            tmux.list = [p for p in tmux.list if p.id != pane]
            raise sidebar.TmuxError("can't find pane")

        tmux.kill_pane = gone
        interrupter = self.close_while_the_hidden_viewer_comes_up()
        self.assertTrue(interrupter.ended)
        self.assertIn(("run", ["claude", "stop", "working"]), self.app.tmux.events)

    def hidden_viewer_he_swaps_into_main(self, placeholder=True):
        """The placeholder (%4) waits in its own window, the first capture of the hidden viewer finds it swapped into main, and swap and kill move and drop panes as tmux does."""
        tmux = self.app.tmux
        if placeholder:
            tmux.list.append(Pane("%4", "@4", False, "placeholder", "", 100, 100))
        captured = tmux.capture

        def capture(pane):
            if "swap" not in self.kinds():
                tmux.list = [replace(p, window="@1") if p.id == "%5" else p for p in tmux.list]
            return captured(pane)

        def swap(source, target):
            tmux.events.append(("swap", source, target))
            where = {p.id: p.window for p in tmux.list}
            moved = {source: where[target], target: where[source]}
            tmux.list = [replace(p, window=moved.get(p.id, p.window)) for p in tmux.list]

        def kill(pane):
            tmux.events.append(("kill", pane))
            tmux.list = [p for p in tmux.list if p.id != pane]

        tmux.capture, tmux.swap, tmux.kill_pane = capture, swap, kill

    def test_a_close_while_he_has_the_hidden_viewer_in_main_swaps_the_placeholder_in_then_kills_it_before_the_stop(self):
        self.hidden_viewer_he_swaps_into_main()
        interrupter = self.close_while_the_hidden_viewer_comes_up()
        events = self.app.tmux.events
        swap = events.index(("swap", "%4", "%5"))
        kill = events.index(("kill", "%5"))
        stop = events.index(("run", ["claude", "stop", "working"]))
        self.assertLess(swap, kill)
        self.assertLess(kill, stop)
        self.assertTrue(interrupter.ended)
        _, main = self.app.layout_panes(self.app.tmux.panes())
        self.assertEqual(main.id, "%4")
        self.app.refresh()
        self.assertEqual(self.app.notice, "Closed emx-test-working.")

    def test_a_close_with_the_hidden_viewer_in_main_and_no_placeholder_kills_nothing_and_closes_nothing(self):
        self.hidden_viewer_he_swaps_into_main(placeholder=False)
        interrupter = self.close_while_the_hidden_viewer_comes_up()
        self.assertNotIn("kill", self.kinds())
        self.assertNotIn("run", self.kinds())
        self.assertFalse(interrupter.ended)
        _, main = self.app.layout_panes(self.app.tmux.panes())
        self.assertEqual(main.id, "%5")
        self.app.refresh()
        self.assertEqual(self.app.notice,
                         "emx couldn't close the viewer it opened for emx-test-working, so it didn't close it.")

    def test_the_closers_retry_of_a_leftover_in_main_swaps_the_placeholder_in_then_kills_it_before_the_stop(self):
        tmux = self.app.tmux
        listed = tmux.panes

        def panes():
            if "new_hidden_window" in self.kinds():
                raise sidebar.TmuxError("server busy")
            return listed()

        tmux.panes = panes
        interrupter = self.interrupt_hidden(*["agent-view"] * 1000)
        self.assertFalse(interrupter.ended)
        tmux.panes = listed
        tmux.list = [replace(p, window="@1") if p.id == "%5" else p for p in tmux.list]
        self.hidden_viewer_he_swaps_into_main()

        def run(args, **kw):
            tmux.events.append(("run", args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run):
            self.app.start_close(core.Confirm("working", "emx-test-working", True, 0.0))
            self.app.closer.join(5)
            self.app.refresh()
        swap = tmux.events.index(("swap", "%4", "%5"))
        kill = tmux.events.index(("kill", "%5"))
        stop = tmux.events.index(("run", ["claude", "stop", "working"]))
        self.assertLess(swap, kill)
        self.assertLess(kill, stop)
        self.assertTrue(interrupter.ended)
        _, main = self.app.layout_panes(tmux.panes())
        self.assertEqual(main.id, "%4")

    def group_close(self):
        return core.GroupConfirm("/p", (core.Confirm("working", "emx-test-working", True, 0.0),
                                        core.Confirm("b", "emx-test-b", True, 0.0)), 0.0)

    def test_a_group_close_held_back_by_a_running_interrupt_says_none_of_the_group_was_closed(self):
        _, release = self.held_interrupts()
        self.press(sidebar.CTRL_X)
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run), mock.patch.object(sidebar, "INTERRUPT_JOIN_SECONDS", 0):
            self.app.start_close(self.group_close())
            self.app.closer.join(5)
            self.app.refresh()
        release.set()
        self.assertEqual(ran, [])
        self.assertEqual(self.app.notice, "emx is still interrupting emx-test-working, so it didn't close any of them.")

    def test_a_group_close_held_back_by_a_hidden_viewer_that_wont_die_says_none_of_the_group_was_closed(self):
        tmux = self.app.tmux

        def refuse(pane):
            tmux.events.append(("kill", pane))
            raise sidebar.TmuxError("server busy")

        tmux.kill_pane = refuse
        interrupter = self.interrupt_hidden(*["agent-view"] * 1000)
        self.assertFalse(interrupter.ended)
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run):
            self.app.start_close(self.group_close())
            self.app.closer.join(5)
            self.app.refresh()
        self.assertEqual(ran, [])
        self.assertEqual(self.app.notice,
                         "emx couldn't close the viewer it opened for emx-test-working, so it didn't close any of them.")

    def held_interrupts(self):
        """Interrupter.attempt replaced by one that records whose it is and waits for `release`, so several can be alive at once."""
        attempts, release = [], threading.Event()

        def attempt(interrupter):
            attempts.append(interrupter.interrupt.id)
            release.wait(5)
            return True, f"Interrupted {interrupter.interrupt.name}."

        p = mock.patch.object(sidebar.Interrupter, "attempt", attempt)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(release.set)
        return attempts, release

    def test_a_second_working_row_is_interrupted_while_the_first_still_is_and_each_gets_one(self):
        attempts, release = self.held_interrupts()
        self.press(sidebar.CTRL_X)
        a = self.app.interrupters["working"]
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Interrupting emx-test-b…")
        self.assertEqual(set(self.app.interrupters), {"working", "b"})
        self.assertTrue(a.is_alive())
        self.app.cursor, self.app.confirm = ("session", "working"), None
        self.press(sidebar.CTRL_X)
        self.assertIs(self.app.interrupters["working"], a)
        release.set()
        self.settle()
        self.assertEqual(sorted(attempts), ["b", "working"])
        self.assertEqual(self.app.interrupters, {})

    def test_a_group_close_cancels_and_waits_for_every_interrupt_in_it(self):
        attempts, release = self.held_interrupts()
        self.press(sidebar.CTRL_X)
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X)
        both = (self.app.interrupters["b"], self.app.interrupters["working"])
        act = core.GroupConfirm("/p", (core.Confirm("working", "emx-test-working", True, 0.0),
                                       core.Confirm("b", "emx-test-b", True, 0.0)), 0.0)
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(sidebar.subprocess, "run", run):
            self.app.start_close(act)
            self.assertEqual(self.app.closer.wait_for, both)
            self.assertTrue(all(i.cancel.is_set() for i in both))
            time.sleep(0.1)
            self.assertEqual(ran, [])
            release.set()
            self.app.closer.join(5)
        self.assertTrue(all(not i.is_alive() for i in both))
        self.assertEqual(ran[0], ["claude", "stop", "working"])

    def test_a_session_that_leaves_the_listing_after_the_escape_ends_the_wait_at_once_without_an_error(self):
        self.after = session("b", "/p/beta")
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.app.tmux.keys_sent, [("%2", "Escape")])
        self.assertEqual(len(self.fetched), 2)
        self.assertEqual((self.app.notice, self.app.notice_error),
                         ("emx sent Esc to emx-test-working, which isn't listed any more.", False))

    def test_a_failing_listing_while_waiting_is_not_taken_for_the_session_leaving(self):
        self.listings = [[self.working], None, None]
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual(len(self.fetched), 4)
        self.assertEqual((self.app.notice, self.app.notice_error), ("Interrupted emx-test-working.", False))

    def test_an_escape_that_does_not_stop_it_says_so(self):
        self.after = self.working
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.app.tmux.keys_sent, [("%2", "Escape")])
        self.assertEqual(self.app.notice, "emx-test-working is still working; emx sent Esc but it didn't stop.")
        self.assertTrue(self.app.notice_error)



LONG_ANSWER = (
    "The invoicing app, sir: the tax rounding bug is fixed, three tests added. "
    "Pull request forty-one is ready for you, and the whole suite is green."
)
LONG_JSON = json.dumps({"result": LONG_ANSWER, "is_error": False, "duration_ms": 900, "total_cost_usd": 0.0004,
                        "usage": {"input_tokens": 3, "output_tokens": 30}, "modelUsage": {"claude-haiku-5-5": {}}})


class Announce(unittest.TestCase):
    def test_every_failure_is_a_line_never_a_raise(self):
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("claude", 15), OSError("boom")):
            with self.subTest(exc=exc), mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                call, error = sidebar.announce("claude", "x", "/tmp")
                self.assertIsNone(call)
                self.assertIsInstance(error, str)

    def test_the_call_is_the_notice_command_with_the_excerpt_on_stdin_in_the_folder_given(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, LONG_JSON)) as run:
            call, error = sidebar.announce("claude", "--Project: app", "/tmp/scratch")
        self.assertIsNone(error)
        self.assertEqual(run.call_args.args[0], core.notice_args("claude"))
        self.assertEqual(run.call_args.kwargs["input"], "--Project: app")
        self.assertEqual(run.call_args.kwargs["cwd"], "/tmp/scratch")
        self.assertEqual(run.call_args.kwargs["timeout"], sidebar.NOTICE_TIMEOUT)

    def test_a_two_sentence_answer_arrives_whole_not_cut_to_a_summary_line(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, LONG_JSON)):
            call, _ = sidebar.announce("claude", "x", "/tmp")
        self.assertEqual(call.text, LONG_ANSWER)
        self.assertGreater(len(LONG_ANSWER), core.SUMMARY_MAX_CHARS)

    def test_a_refusal_is_reported_as_declined(self):
        refused = json.dumps({"is_error": True, "result": "", "stop_reason": "refusal"})
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, refused)):
            self.assertEqual(sidebar.announce("claude", "x", "/tmp"), (None, core.HAIKU_DECLINED))

    def test_summaries_still_use_their_own_timeout_and_cleaning(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, LONG_JSON)) as run:
            call, _ = sidebar.summarise("claude", "x", "/tmp")
        self.assertEqual(run.call_args.kwargs["timeout"], sidebar.SUMMARY_TIMEOUT)
        self.assertLessEqual(len(call.text), core.SUMMARY_MAX_CHARS)


class StubVoice:
    def __init__(self):
        self.spoken, self.banners = [], []

    def speak(self, text):
        self.spoken.append(text)

    def banner(self, text):
        self.banners.append(text)


class NoticesWorker(unittest.TestCase):
    def setUp(self):
        self.transcripts = sidebar.Transcripts("/nonexistent")
        self.speaker = StubVoice()
        self.worker = sidebar.Notices("claude", self.transcripts, sidebar.Resolver("/h"), self.speaker)
        self.alpha = session("a", "/p/alpha", "done")
        self.worker.want([self.alpha])
        self.transcripts.found = {"a": core.Transcript(prompt="Fix it", reply="Fixed.", reply_line="Fixed.", answered=True, latest="reply")}
        self.asked = []
        self.answer = (core.SummaryCall("Alpha, sir: all fixed.", "claude-haiku-5-5", 1, 1, 0.0, 1, 1, 0, 0), None)

        def fake(claude, excerpt, cwd):
            self.asked.append(excerpt)
            return self.answer

        for name, value in (("announce", fake), ("summary_scratch", lambda: "/tmp/emx-test-scratch")):
            patch = mock.patch.object(sidebar, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def notice(self, kind=core.NOTICE_DONE, age=0.0, sid="a"):
        notice = core.Notice(sid, kind, f"emx-test-{sid}", "/p/alpha", time.monotonic() - age)
        self.worker.add(notice)
        return notice

    def test_a_good_answer_reaches_the_outbox_the_voice_and_the_calls(self):
        self.notice()
        self.assertTrue(self.worker.step())
        self.assertEqual(self.worker.take(), ["Alpha, sir: all fixed."])
        self.assertEqual(self.speaker.spoken, ["Alpha, sir: all fixed."])
        self.assertEqual([c.text for c in self.worker.calls], ["Alpha, sir: all fixed."])
        self.assertEqual(self.worker.pending, [])
        self.assertTrue(self.asked[0].startswith("Project: alpha\nSession: emx-test-a\nWhat happened: it has finished"))
        self.assertEqual(self.worker.take(), [])

    def test_an_error_a_refusal_or_an_empty_answer_gives_the_plain_line(self):
        empty = core.SummaryCall("", "m", 1, 1, 0.0, 1, 1, 0, 0)
        for answer in ((None, "boom"), (None, core.HAIKU_DECLINED), (empty, None)):
            with self.subTest(answer=answer):
                self.answer = answer
                self.notice()
                self.worker.step()
                self.assertEqual(self.worker.take(), ["emx-test-a, in alpha, is done."])
        self.assertEqual(len(self.worker.calls), 1)

    def test_an_empty_excerpt_makes_no_call_and_gives_the_plain_line(self):
        self.transcripts.found = {}
        self.notice(age=core.NOTICE_SETTLE_SECONDS + 1)
        self.worker.step()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.worker.take(), ["emx-test-a, in alpha, is done."])
        self.assertEqual(self.worker.calls, [])

    def test_a_needs_input_fallback_says_needs_you(self):
        self.worker.want([session("a", "/p/alpha", "blocked")])
        self.transcripts.found = {}
        self.answer = (None, "boom")
        self.notice(core.NOTICE_NEEDS, age=core.NOTICE_SETTLE_SECONDS + 1)
        self.worker.step()
        self.assertEqual(self.worker.take(), ["emx-test-a, in alpha, needs you."])

    def test_not_ready_sends_nothing_until_the_settle_time_has_passed(self):
        self.transcripts.found = {"a": core.Transcript(prompt="Fix it", latest="prompt")}
        self.notice(age=1)
        self.assertFalse(self.worker.step())
        self.assertEqual((self.worker.take(), self.asked), ([], []))
        self.worker.pending.clear()
        self.notice(age=core.NOTICE_SETTLE_SECONDS)
        self.assertTrue(self.worker.step())
        self.assertEqual(len(self.worker.take()), 1)

    def test_a_notice_for_a_session_now_in_another_state_is_dropped(self):
        self.worker.want([session("a", "/p/alpha", "working")])
        self.notice()
        self.worker.step()
        self.assertEqual((self.worker.take(), self.worker.pending, self.asked, self.speaker.spoken), ([], [], [], []))

    def test_a_session_that_changed_state_during_the_call_is_dropped_too(self):
        def slow(claude, excerpt, cwd):
            self.worker.want([session("a", "/p/alpha", "working")])
            return self.answer

        with mock.patch.object(sidebar, "announce", slow):
            self.notice()
            self.worker.step()
        self.assertEqual((self.worker.take(), self.worker.pending, self.speaker.spoken), ([], [], []))

    def test_a_session_absent_from_the_listing_keeps_its_notice(self):
        self.worker.want([session("b", "/p/beta")])
        self.transcripts.found = {}
        self.notice(age=1)
        self.assertFalse(self.worker.step())
        self.assertEqual(len(self.worker.pending), 1)
        self.worker.pending.clear()
        self.notice(age=core.NOTICE_SETTLE_SECONDS + 1)
        self.worker.step()
        self.assertEqual(self.worker.take(), ["emx-test-a, in alpha, is done."])

    def test_with_the_voice_off_the_outbox_still_gets_it_and_nothing_is_spoken(self):
        self.worker.voice = False
        self.notice()
        self.worker.step()
        self.assertEqual(self.worker.take(), ["Alpha, sir: all fixed."])
        self.assertEqual(self.speaker.spoken, [])

    def test_a_refused_scratch_folder_still_gives_the_plain_line(self):
        with mock.patch.object(sidebar, "summary_scratch", side_effect=OSError("refused")):
            self.notice()
            self.worker.step()
        self.assertEqual((self.asked, self.worker.take()), ([], ["emx-test-a, in alpha, is done."]))

    def test_hostile_words_are_cleaned_before_they_are_shown_or_spoken(self):
        self.answer = (core.SummaryCall("Done [[slnc 9000]] \x1b]9;x\x07 now.", "m", 1, 1, 0.0, 1, 1, 0, 0), None)
        self.notice()
        self.worker.step()
        (text,) = self.worker.take()
        self.assertNotIn("[[", text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(self.speaker.spoken, [text])

    def test_notices_go_out_oldest_first(self):
        self.worker.want([self.alpha, session("b", "/p/beta", "done")])
        self.transcripts.found["b"] = self.transcripts.found["a"]
        self.notice(sid="a", age=3)
        self.notice(sid="b", age=2)
        self.worker.step()
        self.worker.step()
        self.assertEqual(len(self.asked), 2)
        self.assertIn("Project: alpha", self.asked[0])
        self.assertIn("Project: beta", self.asked[1])


class FakeSay:
    """Stands in for a `say` process: communicate takes `hold` seconds unless terminated, and the peak of concurrent runs is counted."""
    lock = threading.Lock()
    running = peak = 0
    hold = 0.05
    made = []

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs, self.text = argv, kwargs, None
        self.ended, self.entered = threading.Event(), threading.Event()
        self.returncode = None
        with FakeSay.lock:
            FakeSay.made.append(self)

    @classmethod
    def reset(cls, hold=0.05):
        cls.running = cls.peak = 0
        cls.made = []
        cls.hold = hold

    def communicate(self, text=None, timeout=None):
        with FakeSay.lock:
            FakeSay.running += 1
            FakeSay.peak = max(FakeSay.peak, FakeSay.running)
        self.text = text
        self.entered.set()
        self.ended.wait(FakeSay.hold)
        with FakeSay.lock:
            FakeSay.running -= 1
        self.returncode = 0
        return "", ""

    def terminate(self):
        self.ended.set()

    def kill(self):
        self.ended.set()

    def poll(self):
        return self.returncode

    def wait(self):
        return 0


def wait_until(test, ready, what, seconds=5):
    deadline = time.monotonic() + seconds
    while not ready():
        if time.monotonic() > deadline:
            test.fail(f"timed out waiting for {what}")
        time.sleep(0.01)


class ListVoices(unittest.TestCase):
    def test_it_never_raises(self):
        for exc in (FileNotFoundError(), OSError("x"), subprocess.TimeoutExpired("say", 1)):
            with mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                self.assertEqual(sidebar.list_voices("say"), [])
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(1, "Daniel en_GB # x")):
            self.assertEqual(sidebar.list_voices("say"), [])
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, "Daniel   en_GB    # x")):
            self.assertEqual(sidebar.list_voices("say"), ["Daniel"])


class VoiceQueue(unittest.TestCase):
    def setUp(self):
        FakeSay.reset()
        for target, name, value in ((sidebar.subprocess, "Popen", FakeSay), (sidebar, "list_voices", lambda say: ["Daniel"])):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.voice = sidebar.Voice()

    def test_a_sentence_put_after_a_mute_is_dropped_but_a_banner_is_kept(self):
        self.voice.muted = True
        self.voice.put(("say", "late"))
        self.voice.put(("banner", "shown"))
        self.assertEqual(self.voice.queue, [("banner", "shown")])

    def test_five_texts_from_two_threads_play_one_at_a_time_in_order_with_the_text_on_stdin(self):
        self.voice.start()
        def queue(texts):
            for text in texts:
                self.voice.speak(text)
        first = threading.Thread(target=queue, args=(["a1", "a2", "a3"],))
        second = threading.Thread(target=queue, args=(["b1", "b2"],))
        first.start(), second.start(), first.join(), second.join()
        wait_until(self, lambda: len([p for p in FakeSay.made if p.returncode == 0]) == 5, "five texts")
        played = [p.text for p in FakeSay.made]
        self.assertEqual(sorted(played), ["a1", "a2", "a3", "b1", "b2"])
        self.assertEqual([t for t in played if t[0] == "a"], ["a1", "a2", "a3"])
        self.assertEqual([t for t in played if t[0] == "b"], ["b1", "b2"])
        self.assertEqual(FakeSay.peak, 1)
        for proc in FakeSay.made:
            self.assertNotIn(proc.text, proc.argv)
            self.assertEqual(proc.argv[:3], ["say", "-v", "Daniel"])

    def test_silence_stops_the_one_playing_and_the_queued_ones_never_run(self):
        FakeSay.reset(hold=5)
        self.voice.start()
        for text in ("one", "two", "three"):
            self.voice.speak(text)
        wait_until(self, lambda: FakeSay.made and FakeSay.made[0].entered.is_set(), "the first to start")
        self.voice.silence()
        self.assertTrue(FakeSay.made[0].ended.is_set())
        time.sleep(0.3)
        self.assertEqual([p.text for p in FakeSay.made], ["one"])

    def test_a_silence_that_came_while_the_process_was_starting_still_stops_it(self):
        self.voice.speak_aloud("late", self.voice.generation - 1)
        (proc,) = FakeSay.made
        self.assertTrue(proc.ended.is_set())
        self.assertEqual(proc.text, "")

    def test_speaking_while_muted_queues_nothing_and_unmuting_lets_it_through(self):
        self.voice.muted = True
        self.voice.speak("late")
        self.assertEqual(self.voice.queue, [])
        self.voice.muted = False
        self.voice.speak("heard")
        self.assertEqual(self.voice.queue, [("say", "heard")])

    def test_a_banner_goes_ahead_of_the_speech_queued_and_behind_earlier_banners(self):
        self.voice.speak("s1")
        self.voice.speak("s2")
        self.voice.banner("b1")
        self.voice.banner("b2")
        self.assertEqual(self.voice.queue, [("banner", "b1"), ("banner", "b2"), ("say", "s1"), ("say", "s2")])

    def test_a_banner_survives_a_silence(self):
        with mock.patch.object(sidebar.subprocess, "run") as run:
            self.voice.banner("kept")
            self.voice.speak("dropped")
            self.voice.silence()
            self.voice.step()
        self.assertEqual(run.call_args.args[0][-1], "kept")

    def test_the_banner_text_is_the_last_argument_after_a_double_dash_and_never_in_the_script(self):
        for text in ("Alpha is done.", "-e display dialog"):
            with self.subTest(text=text), mock.patch.object(sidebar.subprocess, "run") as run:
                self.voice.show_banner(text)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "osascript")
            self.assertEqual(argv[-2:], ["--", text])
            scripts = [argv[i + 1] for i, a in enumerate(argv[:-2]) if a == "-e"]
            self.assertTrue(scripts)
            self.assertFalse(any(text in script for script in scripts))

    def test_a_banner_goes_through_the_same_queue(self):
        with mock.patch.object(sidebar.subprocess, "run") as run:
            self.voice.start()
            self.voice.banner("queued")
            wait_until(self, lambda: run.called, "the banner")
        self.assertEqual(run.call_args.args[0][-1], "queued")

    def test_under_test_nothing_real_is_spoken_or_shown(self):
        with mock.patch.dict(sidebar.os.environ, {"EMX_SHOW_TEST_SESSIONS": "1"}), \
                mock.patch.object(sidebar.subprocess, "run") as run, \
                mock.patch.object(sidebar, "list_voices") as voices:
            sidebar.os.environ.pop("EMX_SAY", None)
            self.voice.start()
            self.voice.speak("quiet")
            self.voice.banner("silent")
            self.voice.speak_aloud("quiet", self.voice.generation)
            self.voice.show_banner("silent")
            time.sleep(0.3)
        self.assertEqual(FakeSay.made, [])
        run.assert_not_called()
        voices.assert_not_called()

    def test_under_test_with_emx_say_set_only_that_command_runs_and_no_banner(self):
        with mock.patch.dict(sidebar.os.environ, {"EMX_SHOW_TEST_SESSIONS": "1", "EMX_SAY": "fake-say"}), \
                mock.patch.object(sidebar.subprocess, "run") as run:
            voice = sidebar.Voice()
            voice.speak_aloud("heard", voice.generation)
            voice.show_banner("silent")
        (proc,) = FakeSay.made
        self.assertEqual(proc.argv[0], "fake-say")
        run.assert_not_called()

    def test_a_missing_say_does_not_raise_and_the_next_item_still_runs(self):
        calls = []

        def popen(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise FileNotFoundError("say")
            return FakeSay(argv, **kwargs)

        with mock.patch.object(sidebar.subprocess, "Popen", popen):
            self.voice.start()
            self.voice.speak("lost")
            self.voice.speak("heard")
            wait_until(self, lambda: FakeSay.made and FakeSay.made[0].returncode == 0, "the second text")
        self.assertEqual(FakeSay.made[0].text, "heard")

    def test_no_voice_found_means_say_runs_with_its_default(self):
        with mock.patch.object(sidebar, "list_voices", lambda say: []):
            self.voice.start()
            self.voice.speak("hello")
            wait_until(self, lambda: FakeSay.made and FakeSay.made[0].returncode == 0, "the text")
        self.assertEqual(FakeSay.made[0].argv, ["say"])

    def test_the_say_command_can_be_overridden_by_the_environment(self):
        with mock.patch.dict(os.environ, {"EMX_SAY": "/x/shim"}):
            self.assertEqual(sidebar.say_command(), "/x/shim")
        with mock.patch.dict(os.environ, {"EMX_SAY": ""}):
            self.assertEqual(sidebar.say_command(), "say")

    def test_building_a_poller_starts_no_thread_and_runs_no_command(self):
        before = threading.active_count()
        with mock.patch.object(sidebar.subprocess, "Popen", side_effect=AssertionError("Popen")), \
                mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("run")), \
                mock.patch.object(sidebar, "list_voices", side_effect=AssertionError("list_voices")):
            poller = sidebar.Poller("claude")
        self.assertEqual(threading.active_count(), before)
        self.assertIsNone(poller.voice.voice)
        self.assertFalse(poller.notices.is_alive() or poller.voice.is_alive())


REAL_WRITE_OUT = sidebar.App.write_out


class NoticeWiring(AppCase):
    """A listing that moves a session into Needs input or Done queues one notice, unless he is looking at that session."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: self.assertEqual(self.announced, [], "Haiku was asked on the curses thread"))

    def pending(self):
        return list(self.app.poller.notices.pending)

    def step(self, *sessions):
        self.poll(*sessions)
        self.app.refresh()

    def main_shows(self, sid, state=(1, "1", "%2", 3.0)):
        self.app.main_pane = Pane("%2", "@1", False, "", sid, 100, 141)
        self.app.tmux = ClientTmux(state)

    def test_working_then_done_gives_one_notice_and_a_third_identical_poll_none(self):
        self.step(session("a", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "done"))
        (notice,) = self.pending()
        self.assertEqual((notice.id, notice.kind, notice.name, notice.cwd), ("a", core.DONE, "emx-test-a", "/p/alpha"))
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(len(self.pending()), 1)

    def test_needing_input_is_a_notice_of_its_own_kind(self):
        self.step(session("a", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "blocked"))
        self.assertEqual([n.kind for n in self.pending()], [core.NEEDS_INPUT])

    def test_a_first_poll_already_done_gives_none(self):
        self.step(session("a", "/p/alpha", "done"), session("b", "/p/alpha", "blocked"))
        self.assertEqual(self.pending(), [])

    def test_an_error_and_an_empty_listing_in_between_give_none(self):
        self.step(session("a", "/p/alpha", "done"))
        self.app.poller.result = (None, "listing failed")
        self.app.refresh()
        self.step()
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(self.pending(), [])

    def test_a_session_being_closed_gives_none(self):
        self.step(session("a", "/p/alpha", "working"))
        self.app.closing = {"a": True}
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(self.pending(), [])

    def test_a_session_being_interrupted_or_renamed_gives_none(self):
        self.step(session("a", "/p/alpha", "working"), session("b", "/p/alpha", "working"))
        self.app.interrupters = {"a": object()}
        self.app.renamer = mock.Mock(rename=mock.Mock(id="b"))
        self.step(session("a", "/p/alpha", "done"), session("b", "/p/alpha", "done"))
        self.assertEqual(self.pending(), [])

    def test_the_session_in_front_is_quiet_and_another_is_not(self):
        self.main_shows("a")
        self.step(session("a", "/p/alpha", "working"), session("b", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "done"), session("b", "/p/alpha", "done"))
        self.assertEqual([n.id for n in self.pending()], ["b"])

    def test_each_condition_alone_makes_the_open_session_notify(self):
        cases = {
            "focus out": (1, "0", "%2", 3.0),
            "focus unknown": (1, "", "%2", 3.0),
            "no client": (0, "1", "%2", 3.0),
            "panel active": (1, "1", "%1", 3.0),
            "away": (1, "1", "%2", core.AWAY_SECONDS + 1.0),
            "activity unknown": (1, "1", "%2", None),
            "tmux failed": TmuxError("gone"),
        }
        for name, state in cases.items():
            with self.subTest(name):
                self.app.poller.notices.pending.clear()
                self.app.notice_seen = None
                self.main_shows("a", state)
                self.step(session("a", "/p/alpha", "working"))
                self.step(session("a", "/p/alpha", "done"))
                self.assertEqual([n.id for n in self.pending()], ["a"])

    def test_a_main_pane_flagged_off_is_not_in_front(self):
        self.main_shows("a")
        self.app.flagged_viewers = frozenset({"%2"})
        self.step(session("a", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(len(self.pending()), 1)

    def test_flushing_while_attached_writes_the_terminal_notification_and_no_banner(self):
        self.app.tmux = ClientTmux((1, "0", "%1", 3.0))
        self.app.poller.notices.outbox = ["Alpha is done."]
        self.app.flush_notices()
        self.assertEqual(self.written, [(core.osc9("Alpha is done."),)])
        self.assertEqual(self.banners, [])
        self.assertEqual(self.app.poller.notices.outbox, [])

    def test_flushing_while_detached_shows_a_banner_and_writes_nothing(self):
        self.app.tmux = ClientTmux((0, "", "%1", None))
        self.app.poller.notices.outbox = ["Alpha is done."]
        self.app.flush_notices()
        self.assertEqual(self.banners, [("Alpha is done.",)])
        self.assertEqual(self.written, [])

    def test_detaching_between_the_transition_and_delivery_still_shows_the_notice(self):
        self.app.tmux = ClientTmux((1, "0", "%1", 3.0))
        self.step(session("a", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(len(self.pending()), 1)
        self.app.tmux = ClientTmux((0, "", "%1", None))
        self.app.poller.notices.outbox = ["Alpha is done."]
        self.app.refresh()
        self.assertEqual(self.banners, [("Alpha is done.",)])
        self.assertEqual(self.written, [])

    def test_a_tmux_error_at_delivery_falls_back_to_the_banner(self):
        self.app.tmux = ClientTmux(TmuxError("gone"))
        self.app.poller.notices.outbox = ["Alpha is done."]
        self.app.flush_notices()
        self.assertEqual(self.banners, [("Alpha is done.",)])

    def test_a_failed_terminal_write_shows_on_the_panel_and_not_as_a_crash(self):
        with mock.patch.object(sidebar.os, "write", side_effect=OSError("closed")):
            REAL_WRITE_OUT(self.app, b"x")
        self.assertIn("closed", self.app.notice)
        self.assertTrue(self.app.notice_error)

    def test_a_real_housekeep_opens_passthrough_for_the_sidebar_pane_only_once(self):
        class Housekeeping(ClientTmux):
            socket = "emx-test-wiring"

            def panes(self):
                return [Pane("%1", "@1", False, "sidebar", "", 40, 141), Pane("%2", "@1", False, "", "", 100, 141)]

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        self.app.tmux = Housekeeping()
        self.app.me = "%1"
        sidebar.App.housekeep(self.app, False)
        sidebar.App.housekeep(self.app, False)
        self.assertEqual(self.app.tmux.passthrough, ["%1"])
        self.assertNotIn("%2", self.app.tmux.passthrough)


class MuteKey(AppCase):
    def prefs_on_disk(self):
        return json.loads(self.app.prefs_file.read_text(encoding="utf-8"))

    def test_m_mutes_and_saves_and_a_new_panel_starts_muted(self):
        self.assertTrue(self.app.poller.notices.voice)
        self.app.handle_key("m")
        self.assertIs(self.prefs_on_disk()["voice"], False)
        self.assertFalse(self.app.poller.notices.voice)
        self.assertEqual(self.app.notice, "Spoken notices off")
        again = sidebar.App(FakeScr(), tmux=ClientTmux(), claude="claude")
        self.assertFalse(again.poller.notices.voice)

    def test_m_sets_the_voice_muted_flag_both_ways(self):
        self.app.poller.voice = sidebar.Voice()
        self.app.handle_key("m")
        self.assertTrue(self.app.poller.voice.muted)
        self.app.handle_key("m")
        self.assertFalse(self.app.poller.voice.muted)

    def test_m_again_unmutes(self):
        self.app.handle_key("m")
        self.app.handle_key("m")
        self.assertIs(self.prefs_on_disk()["voice"], True)
        self.assertTrue(self.app.poller.notices.voice)
        self.assertEqual(self.app.notice, "Spoken notices on")

    def test_muting_silences_the_voice_and_unmuting_does_not(self):
        self.app.handle_key("m")
        self.assertEqual(len(self.silenced), 1)
        self.app.handle_key("m")
        self.assertEqual(len(self.silenced), 1)

    def test_m_while_typing_in_the_find_box_is_text(self):
        self.app.handle_key("/")
        self.app.handle_key("m")
        self.assertTrue(core.voice_on(self.app.prefs))

    def test_the_help_has_the_m_line(self):
        self.assertIn(("m", "mute or unmute spoken notices; banners still show"), sidebar.HELP)

    def test_the_top_line_shows_muted_only_while_muted(self):
        def top_line():
            drawn = []
            self.app.put = lambda y, x, text, attr=0: drawn.append(text)
            self.app.draw_top_line(60)
            return drawn

        self.assertNotIn("muted", top_line())
        self.app.handle_key("m")
        self.assertIn("muted", top_line())
        self.app.handle_key("m")
        self.assertNotIn("muted", top_line())

if __name__ == "__main__":
    unittest.main()
