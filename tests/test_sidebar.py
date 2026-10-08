import curses
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from emx import core, sidebar
from emx.tmux import Pane


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


if __name__ == "__main__":
    unittest.main()
