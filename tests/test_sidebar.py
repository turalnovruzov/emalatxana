import curses
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from emx import core, sidebar
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


class AppCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name, "TMUX_PANE": "%1"}),
            mock.patch.object(sidebar, "Palette", FakePalette),
            mock.patch.object(sidebar.curses, "doupdate", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.scr = FakeScr()
        self.app = sidebar.App(self.scr, tmux=None, claude="claude")
        self.app.housekeep = lambda fresh_list: None
        self.opened = []
        self.app.switch_to = lambda sid, trigger, checked=None: self.opened.append(
            (sid, trigger) if checked is None else (sid, trigger, checked)
        )

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


class CloseSession(AppCase):
    """Ctrl+X twice on one row stops or removes that session, on a worker thread, after a fresh listing agrees with the row."""

    def setUp(self):
        super().setUp()
        self.a, self.b = session("a", "/p/alpha"), session("b", "/p/alpha", started=1.0)
        self.poll(self.a, self.b)
        self.app.refresh()
        self.app.tmux = CloseTmux()
        self.app.cursor = ("session", "a")
        self.listing = ([self.a, self.b], None)
        self.ran = []
        self.done = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        def run(args, **kw):
            self.ran.append((args, threading.current_thread() is not threading.main_thread()))
            return self.done

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

    def test_double_ctrl_x_stops_a_running_session_off_the_curses_thread(self):
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.confirm.id, "a")
        self.assertIn(sidebar.CONFIRM_FOOTER, [t for t, _ in self.app.bottom_lines(39)])
        self.assertEqual(self.ran, [])
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Stopping emx-test-a…")
        self.assertIsNone(self.app.confirm)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "stop", "a"], True)])
        self.assertEqual((self.app.notice, self.app.notice_error), ("Stopped emx-test-a.", False))
        self.assertTrue(self.app.poller.wake.is_set())
        self.assertEqual(self.app.tmux.calls, [])

    def test_double_ctrl_x_on_a_stopped_session_removes_it_and_closes_its_viewer_in_main(self):
        stopped = session("a", "/p/alpha", state="stopped", pid=None)
        self.poll(stopped, self.b)
        self.app.refresh()
        self.listing = ([stopped, self.b], None)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "rm", "a"], True)])
        self.assertEqual(self.app.notice, "Removed emx-test-a.")
        self.assertEqual(self.app.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2")])

    def test_a_worktree_rm_left_behind_shows_after_removed_on_one_line(self):
        stopped = session("a", "/p/alpha", state="stopped", pid=None)
        self.poll(stopped, self.b)
        self.app.refresh()
        self.listing = ([stopped, self.b], None)
        self.done = subprocess.CompletedProcess(
            [], 0, stdout="removed a\nworktree directory left at /x/wt (git no longer recognized it)\n", stderr="",
        )
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(
            (self.app.notice, self.app.notice_error),
            ("Removed emx-test-a. worktree directory left at /x/wt (git no longer recognized it)", False),
        )
        self.assertEqual(self.app.tmux.calls, [("swap", "%4", "%2"), ("kill", "%2")])

    def test_removing_a_session_kills_its_hidden_viewer(self):
        stopped = session("b", "/p/alpha", state="stopped", pid=None, started=1.0)
        self.poll(self.a, stopped)
        self.app.refresh()
        self.listing = ([self.a, stopped], None)
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.app.tmux.calls, [("kill", "%3")])

    def test_a_refused_rm_shows_its_message_on_one_line_and_leaves_the_row(self):
        stopped = session("a", "/p/alpha", state="stopped", pid=None)
        self.poll(stopped, self.b)
        self.app.refresh()
        self.listing = ([stopped, self.b], None)
        self.done = subprocess.CompletedProcess([], 1, stdout="", stderr="worktree has 2 unpushed commits\nrerun with a flag\n")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [(["claude", "rm", "a"], True)])
        self.assertEqual((self.app.notice, self.app.notice_error), ("worktree has 2 unpushed commits rerun with a flag", True))
        self.assertEqual(self.app.tmux.calls, [])
        self.assertIsNotNone(self.app.session("a"))

    def test_a_refusal_on_stdout_alone_shows_on_one_line_and_leaves_the_row(self):
        stopped = session("a", "/p/alpha", state="stopped", pid=None)
        self.poll(stopped, self.b)
        self.app.refresh()
        self.listing = ([stopped, self.b], None)
        self.done = subprocess.CompletedProcess([], 1, stdout="kept a — its worktree is still at /x/wt", stderr="")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual((self.app.notice, self.app.notice_error), ("kept a — its worktree is still at /x/wt", True))
        self.assertEqual(self.app.tmux.calls, [])
        self.assertIsNotNone(self.app.session("a"))

    def test_a_stopped_row_resumed_elsewhere_is_never_removed(self):
        stopped = session("a", "/p/alpha", state="stopped", pid=None)
        self.poll(stopped, self.b)
        self.app.refresh()
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.ran, [])
        self.assertIn("running again", self.app.notice)

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
        self.poll(session("a", "/p/alpha", state="stopped", pid=None), self.b)
        self.app.refresh()
        self.assertIsNone(self.app.confirm)

    def test_the_cursor_moved_by_a_poll_cancels(self):
        self.press(sidebar.CTRL_X)
        self.poll(self.b)
        self.app.refresh()
        self.assertNotEqual(self.app.cursor, ("session", "a"))
        self.assertIsNone(self.app.confirm)

    def test_ctrl_x_on_a_group_header_does_nothing(self):
        self.app.cursor = ("group", "/p/alpha")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer, self.app.notice), (None, None, ""))

    def test_no_press_arms_while_listing_fails_or_a_close_runs(self):
        self.app.list_error = "listing broken"
        self.press(sidebar.CTRL_X)
        self.assertIsNone(self.app.confirm)
        self.assertIn("Can't list sessions", self.app.notice)
        self.app.list_error = ""
        self.app.closer = sidebar.Closer("claude", core.press_close(None, ("session", "b"), self.b, 0.0)[0])
        self.app.closer.is_alive = lambda: True
        self.press(sidebar.CTRL_X)
        self.assertIsNone(self.app.confirm)
        self.assertEqual(self.app.notice, "Still stopping emx-test-b…")

    def test_the_armed_row_says_what_the_second_press_does(self):
        self.press(sidebar.CTRL_X)
        drawn = []
        self.app.put = lambda y, x, text, attr=0: drawn.append(text)
        self.app.draw()
        self.assertIn("ctrl+x again to stop", drawn)
        self.assertNotIn("emx-test-a", drawn)

    def test_a_footer_and_the_help_show_ctrl_x(self):
        self.assertTrue(any("^x" in footer for footer in sidebar.FOOTERS))
        self.assertIn("^x", " ".join(key for key, _ in sidebar.HELP))

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


if __name__ == "__main__":
    unittest.main()
