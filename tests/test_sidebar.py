import curses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

import emx
from emx import cli, core, sidebar
from emx.tmux import AGENT_OPT, OPEN_OPT, RELAY_HOST_OPT, RELAY_OPT, SESSION, SESSION_OPT, TERMINAL_OPT, Pane, Tmux, TmuxError, View


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


REAL_FETCH_SESSIONS = sidebar.fetch_sessions
REAL_ANNOUNCE = sidebar.announce


class FakeEngine:
    """An engine whose listing is given: nothing reads a state folder, runs tmux or starts a claude."""

    def __init__(self, listing=(None, "no engine in tests"), claude="claude", tmux=None):
        self.listing, self.claude, self.tmux = listing, claude, tmux
        self.state = Path("/nonexistent-state")
        self.calls = []

    def list_sessions(self):
        self.calls.append(("list",))
        return self.listing


class ListsFromEngine(unittest.TestCase):
    def test_fetch_sessions_is_the_engines_listing_and_runs_nothing(self):
        rows = [session("x", "/a")]
        engine = FakeEngine((rows, None))
        with mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("a command ran")):
            self.assertEqual(sidebar.fetch_sessions(engine), (rows, None))
        self.assertEqual(engine.calls, [("list",)])

    def test_a_poll_hands_both_sessions_to_every_worker_without_running_git(self):
        x, y = (replace(session(k, cwd), session_id=f"{k * 8}-1111-4111-8111-111111111111") for k, cwd in (("a", "/a"), ("b", "/b")))
        poller = sidebar.Poller(FakeEngine(([x, y], None)))
        with mock.patch.object(sidebar, "git_placement", side_effect=AssertionError("git in the poll")), \
                mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("a command ran")):
            sessions, error = poller.poll()
        self.assertEqual((sessions, error), ([x, y], None))
        self.assertEqual(poller.resolver.wanted, {"/a", "/b"})
        self.assertEqual(set(poller.transcripts.wanted), {"a", "b"})
        self.assertEqual(set(poller.summaries.wanted), {"a", "b"})
        self.assertEqual(set(poller.notices.wanted), {"a", "b"})
        self.assertEqual(poller.asks.wanted, {})
        asking = replace(x, state="done", reply="Which one?", stop_key="k1", asking=True)
        with mock.patch.object(sidebar, "fetch_sessions", return_value=([asking, y], None)):
            poller.poll()
        self.assertEqual(poller.asks.wanted, {("a", "k1"): asking})

    def test_an_engine_error_is_the_polls_error(self):
        poller = sidebar.Poller(FakeEngine((None, "Couldn't read emx's sessions folder: denied")))
        self.assertEqual(poller.poll(), (None, "Couldn't read emx's sessions folder: denied"))


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

    def clear(self):
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
    can_shade = True

    def __init__(self, scheme=core.DARK):
        self.schemes = [scheme]
        self.shades = [False]

    def use(self, scheme, shade=False):
        self.schemes.append(scheme)
        self.shades.append(shade)

    def attr(self, tone, selected=False):
        return 0


class FakeNotifier:
    """Stands in for `sidebar.Notifier` as `FakeSay` does for `say`: records posts and permission requests, with a settable `state`."""

    def __init__(self, socket="emx"):
        self.socket = socket
        self.posts = []
        self.asked = 0
        self.state = core.HELPER_OK
        self.mode = core.NOTIFICATION_MODE

    def post(self, title, body, sid, at=None):
        self.posts.append((title, body, sid))

    def authorize(self):
        self.asked += 1

    def start(self):
        pass

    def is_alive(self):
        return False


def window_of(panes, pane):
    """The panes in the window of `pane`, as `Tmux.window_panes` gives them."""
    mine = next((p for p in panes if p.id == pane), None)
    return [p for p in panes if mine is not None and p.window == mine.window]


class WindowPanes:
    def window_panes(self, pane):
        return window_of(self.panes(), pane)


class ClientTmux(WindowPanes):
    """The tmux calls a listing makes for notices; anything else is an error, as it is with no tmux at all.

    `state` is the one view's client state as `Tmux.client_state` gives it;
    `views` reads the same view, its slot `slot` showing `shows`.
    """

    def __init__(self, state=(0, "", "", None), shows="", slot="%2"):
        self.state = state
        self.hush = ""
        self.shows = shows
        self.slot = slot
        self.globals_set = []
        self.calls = []

    def set_global_option(self, name, value):
        self.globals_set.append((name, value))

    def set_view_option(self, name, value):
        return self.set_session_option(name, value)

    def set_session_option(self, name, value):
        self.calls.append(("set_view_option", name, value))

    def unset_view_option(self, name):
        self.calls.append(("unset_view_option", name))

    def views(self):
        if isinstance(self.state, Exception):
            raise self.state
        attached, focus, active, idle = self.state
        activity = None if idle is None else time.time() - idle
        return [View(SESSION, attached, focus, activity, 0, self.shows, active == self.slot, self.slot)]

    def view_option(self, name):
        return self.session_option(name)

    def session_option(self, name):
        if isinstance(self.hush, Exception):
            raise self.hush
        return self.hush

    def client_state(self):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class FakeBar:
    def __init__(self, tmux):
        self.tmux = tmux
        self.pushed = []
        self.line = 0
        self.restarts = []

    def restart(self, line):
        self.line = line
        self.restarts.append(line)

    def push(self, text):
        self.pushed.append(text)
        calls = getattr(self.tmux, "calls", None)
        if calls is not None:
            calls.append(("set_bar", text, self.line))


REST_BAR = "→ open  ␣ peek  n new  ^f find  ^x close  ? keys"


def plain_bar(text):
    return re.sub(r"#\[[^\]]*\]", "", text).replace("##", "#").replace("%%", "%")


TERMINAL_WRITES = []


def setUpModule():
    patch = mock.patch.object(sidebar, "write_terminal", TERMINAL_WRITES.append)
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class AppCase(unittest.TestCase):
    def bar(self, width=100):
        """The bar's text as the panel pushes it for a window `width` wide, without its style tags."""
        self.app.window_width = width
        self.app.update_bar()
        return plain_bar(self.app.bar.pushed[-1])

    def setUp(self):
        TERMINAL_WRITES.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name, "XDG_STATE_HOME": tmp.name, "TMUX_PANE": "%1",
                                         "EMX_NOTIFY_HOME": os.path.join(tmp.name, "notify")}),
            mock.patch.object(sidebar, "Palette", FakePalette),
            mock.patch.object(sidebar, "StatusBar", FakeBar),
            # No test may build the helper or post a real notification.
            mock.patch.object(sidebar, "Notifier", FakeNotifier),
            mock.patch.object(sidebar.curses, "doupdate", lambda: None),
            # A worker a test didn't mean to start must never run his real claude.
            mock.patch.object(sidebar, "fetch_sessions", lambda engine: (None, "no engine in tests")),
            # No test may speak, ask Haiku or write to the real terminal.
            mock.patch.object(sidebar, "announce", self.recorder("announced", method=False, result=(None, "no haiku in tests"))),
            mock.patch.object(sidebar, "classify", self.recorder("classified", method=False, result=(None, "no haiku in tests"))),
            mock.patch.object(sidebar, "list_voices", lambda say: []),
            mock.patch.object(sidebar.Voice, "speak", self.recorder("spoken")),
            mock.patch.object(sidebar.Voice, "silence", self.recorder("silenced")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.scr = FakeScr()
        self.app = sidebar.App(self.scr, tmux=ClientTmux(), claude="claude")
        self.app.housekeep = lambda fresh_list: None
        # The viewers these tests build carry no agent tag; LegacyViewers tests the one-time cleanup of such panes.
        self.app.legacy_cleared = True
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

    def write_scheme(self, name, mtime):
        core.save_prefs(self.app.prefs_file, core.set_scheme(core.load_prefs(self.app.prefs_file), name))
        os.utime(self.app.prefs_file, ns=(mtime, mtime))

    def housekeep_once(self):
        quiet = ("check_relay", "check_open", "time_whats_new", "check_update", "check_phone", "check_hush")
        with mock.patch.object(sidebar.App, "my_panes", lambda self: []), \
                mock.patch.object(sidebar.App, "layout_panes", lambda self, panes: (None, None)):
            patches = [mock.patch.object(sidebar.App, name, lambda self: None) for name in quiet]
            for p in patches:
                p.start()
                self.addCleanup(p.stop)
            sidebar.App.housekeep(self.app, False)

    def no_scheme(self):
        """For a test of something else that runs the real housekeep against a tmux fake that only records its own calls."""
        for name in ("apply_scheme", "ask_background_again"):
            patch = mock.patch.object(sidebar.App, name, lambda self: None)
            patch.start()
            self.addCleanup(patch.stop)


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
        for trigger in (core.NEW,):
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


class StartWaitsUntilRunning(AppCase):
    def listings(self, *running):
        return [([session("new1", "/p/alpha", pid=1 if up else None, state="starting" if up else "stopped")], None)
                for up in running]

    def test_a_listing_before_its_pane_runs_is_waited_out(self):
        engine = FakeEngine()
        with mock.patch.object(sidebar, "fetch_sessions", side_effect=self.listings(False, False, True)), \
                mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0):
            found, error = sidebar.wait_until_running(engine, "new1")
        self.assertEqual((found.id, found.running, error), ("new1", True, None))

    def test_listed_but_never_running_gives_up_saying_so(self):
        with mock.patch.object(sidebar, "fetch_sessions", side_effect=self.listings(False) * 3), \
                mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0):
            found, error = sidebar.wait_until_running(FakeEngine(), "new1")
        self.assertIsNone(found)
        self.assertIn("isn't running", error)
        for listing in (([], None), (None, "Couldn't read emx's sessions folder")):
            with self.subTest(listing=listing), mock.patch.object(sidebar, "fetch_sessions", return_value=listing), \
                    mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0):
                self.assertIn("doesn't list it", sidebar.wait_until_running(FakeEngine(), "new1")[1])

    def test_the_starters_listing_is_the_fresh_one_the_viewer_opens_on(self):
        new = session("new1", "/p/alpha")
        done = sidebar.Starter(FakeEngine(), "/p/alpha", "")
        done.result = (new, None)
        self.app.starter = done
        self.app.finish_start()
        self.assertEqual(self.opened, [("new1", core.NEW, (new, True))])


CLAUDE_SID = "11111111-1111-4111-8111-111111111111"


class SwitchToTmux(ClientTmux):
    """The panel (%1) and the slot (%2, the placeholder unless `slot` says otherwise) in one window, and `held` as the pane of session KEY_A, or none. Records every call that changes something, in order."""

    socket = "emx-test-switch"

    def __init__(self, held=None, slot=None):
        super().__init__()
        self.held = held
        self.made = None
        self.slot_pane = slot or Pane("%2", "@1", False, "placeholder", "", 100, 141)
        self.calls = []

    def panes(self):
        listed = [Pane("%1", "@1", False, "sidebar", "", 40, 141), self.slot_pane]
        return listed + [p for p in (self.held, self.made) if p is not None]

    def respawn(self, pane, command, cwd=None):
        self.calls.append(("respawn", pane, command, cwd))

    def new_hidden_window(self, command, cwd=None):
        self.calls.append(("new_hidden_window", command, cwd))
        self.made = Pane("%9", "@9", False, "", KEY_A, 100, 100, agent="claude")
        return "%9"

    def link_agent(self, window, key, inert):
        self.calls.append(("link_agent", window, key))

    def viewer_command(self, key):
        return f"viewer {key}"

    def clear_history(self, pane):
        pass

    def set_pane_option(self, pane, name, value):
        self.calls.append(("set_pane_option", pane, name, value))

    def swap(self, a, b):
        self.calls.append(("swap", a, b))

    def select(self, pane):
        self.calls.append(("select", pane))

    def kill_pane(self, pane):
        self.calls.append(("kill", pane))

    def capture(self, pane):
        return ""


class SwitchTo(AppCase):
    """Enter, → or a click: a running session's own pane comes into main; a stopped one is resumed, on his open only."""

    def setUp(self):
        super().setUp()
        self.app.me = "%1"
        self.cwd = os.path.join(os.environ["XDG_STATE_HOME"], "work")
        os.makedirs(self.cwd)
        where = os.path.join(self.app.state, "sessions", KEY_A)
        os.makedirs(where)
        record = {"key": KEY_A, "agent": "claude", "name": "alpha", "cwd": self.cwd, "session_id": KEY_A,
                  "started_at": 1, "run_at": 1}
        with open(os.path.join(where, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        with open(os.path.join(where, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "SessionStart", "at": 2, "session_id": CLAUDE_SID, "source": "startup"}) + "\n")

    def open(self, held, row_state, trigger=core.OPEN, checked=None):
        self.app.tmux = SwitchToTmux(held)
        running = row_state != "stopped"
        self.poll(core.Session(KEY_A, "alpha", self.cwd, row_state, "", "", 1.0, running))
        self.app.refresh()
        sidebar.App.switch_to(self.app, KEY_A, trigger, checked)
        return self.app.tmux.calls

    def shown(self, window, slot="%2"):
        """The calls that show KEY_A, whose pane is in `window`, in the slot and go to it."""
        return [
            ("link_agent", window, KEY_A), ("respawn", slot, f"viewer {KEY_A}", None),
            ("set_pane_option", slot, sidebar.ROLE, "viewer"), ("set_pane_option", slot, sidebar.VIEW_OPT, KEY_A),
            ("select", slot),
        ]

    def test_a_running_session_is_shown_in_the_slot_by_a_viewer_and_its_pane_never_moves(self):
        calls = self.open(Pane("%5", "@2", False, "", KEY_A, 100, 100, agent="claude"), "done")
        self.assertEqual(calls, self.shown("@2"))
        self.assertNotIn("swap", [c[0] for c in calls])
        self.assertNotIn("kill", [c[0] for c in calls])

    def test_a_slot_already_showing_the_session_is_only_selected(self):
        self.app.tmux = SwitchToTmux(
            Pane("%5", "@2", False, "", KEY_A, 100, 100, agent="claude"),
            slot=Pane("%2", "@1", False, "viewer", "", 100, 141, view=KEY_A),
        )
        self.poll(core.Session(KEY_A, "alpha", self.cwd, "done", "", "", 1.0, True))
        self.app.refresh()
        sidebar.App.switch_to(self.app, KEY_A, core.OPEN)
        self.assertEqual(self.app.tmux.calls, [("select", "%2")])

    def test_a_stopped_session_with_a_dead_pane_is_resumed_there_on_open(self):
        calls = self.open(Pane("%5", "@2", True, "", KEY_A, 100, 100, agent="claude"), "stopped")
        respawns = [c for c in calls if c[0] == "respawn" and c[1] != "%2"]
        self.assertEqual(len(respawns), 1)
        _, pane, command, cwd = respawns[0]
        words = shlex.split(command)
        self.assertEqual((pane, cwd), ("%5", self.cwd))
        self.assertEqual(words[words.index("--resume") + 1], CLAUDE_SID)
        self.assertIn("--settings", words)
        self.assertNotIn("new_hidden_window", [c[0] for c in calls])
        self.assertEqual(calls[1:], self.shown("@2"))

    def test_a_stopped_session_with_no_pane_gets_a_tagged_window_before_it_is_shown(self):
        calls = self.open(None, "stopped")
        self.assertEqual([c[0] for c in calls[:3]], ["new_hidden_window", "set_pane_option", "set_pane_option"])
        _, command, cwd = calls[0]
        self.assertEqual(cwd, self.cwd)
        self.assertIn(f"--resume {CLAUDE_SID}", command)
        self.assertEqual(calls[1:3], [("set_pane_option", "%9", AGENT_OPT, "claude"), ("set_pane_option", "%9", SESSION_OPT, KEY_A)])
        self.assertEqual(calls[3:], self.shown("@9"))

    def test_a_stopped_session_is_never_resumed_by_any_other_trigger(self):
        stopped = core.Session(KEY_A, "alpha", self.cwd, "stopped", "", "", 1.0, False)
        for trigger in (core.NEW, "restart", "interrupt"):
            for held in (None, Pane("%5", "@2", True, "", KEY_A, 100, 100, agent="claude")):
                with self.subTest(trigger=trigger, held=held):
                    self.app.say("")
                    calls = self.open(held, "stopped", trigger, (stopped, True))
                    self.assertEqual(calls, [])
                    self.assertIn("isn't running", self.app.notice)
                    self.assertTrue(self.app.notice_error)

    def test_a_session_listed_running_whose_pane_has_gone_is_never_resumed_but_by_his_open(self):
        running = core.Session(KEY_A, "alpha", self.cwd, "done", "", "", 1.0, True)
        for held in (None, Pane("%5", "@2", True, "", KEY_A, 100, 100, agent="claude")):
            with self.subTest(held=held):
                self.app.say("")
                calls = self.open(held, "done", core.NEW, (running, True))
                self.assertEqual(calls, [])
                self.assertIn("isn't running", self.app.notice)
                self.assertTrue(self.app.notice_error)

    def test_an_old_viewer_with_no_agent_tag_is_never_taken_for_the_session(self):
        calls = self.open(Pane("%5", "@2", False, "", KEY_A, 100, 100), "stopped")
        self.assertEqual(calls[0][0], "new_hidden_window")
        self.assertNotIn(("link_agent", "@2", KEY_A), calls)

    def test_a_window_that_cannot_be_tagged_is_killed(self):
        class Untaggable(SwitchToTmux):
            def set_pane_option(self, pane, name, value):
                raise TmuxError("gone")

        self.app.tmux = Untaggable()
        self.poll(core.Session(KEY_A, "alpha", self.cwd, "stopped", "", "", 1.0, False))
        self.app.refresh()
        sidebar.App.switch_to(self.app, KEY_A, core.OPEN)
        self.assertIn(("kill", "%9"), self.app.tmux.calls)
        self.assertNotIn("respawn", [c[0] for c in self.app.tmux.calls])
        self.assertNotIn("link_agent", [c[0] for c in self.app.tmux.calls])
        self.assertTrue(self.app.notice_error)


class StartsWithTheEngine(AppCase):
    def setUp(self):
        super().setUp()
        real = mock.patch.object(sidebar, "fetch_sessions", REAL_FETCH_SESSIONS)
        real.start()
        self.addCleanup(real.stop)

    def test_a_pane_never_seen_running_is_an_error_after_the_wait_and_nothing_opens(self):
        started = core.Session(KEY_A, "alpha", "/p", "starting", "", "", 1.0, True)

        class Engine(FakeEngine):
            def start(self, folder, message):
                self.calls.append(("start", folder, message))
                return started, None

        engine = Engine(([replace(started, state="stopped", running=False)], None))
        with mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0), mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0):
            starter = sidebar.Starter(engine, "/p", "hello")
            starter.run()
        self.assertEqual(engine.calls[0], ("start", "/p", "hello"))
        self.assertIsNone(starter.result[0])
        self.assertIn("isn't running", starter.result[1])
        self.app.starter = starter
        self.app.finish_start()
        self.assertEqual(self.opened, [])
        self.assertTrue(self.app.notice_error)

    def test_a_start_error_is_said_and_nothing_is_listed_for(self):
        class Engine(FakeEngine):
            def start(self, folder, message):
                return None, "The folder /gone doesn't exist."

        engine = Engine()
        starter = sidebar.Starter(engine, "/gone", "")
        starter.run()
        self.assertEqual(starter.result, (None, "The folder /gone doesn't exist."))
        self.assertEqual(engine.calls, [])

    def test_once_the_pane_runs_the_new_session_opens_with_new(self):
        started = core.Session(KEY_A, "alpha", "/p", "starting", "", "", 1.0, True)

        class Engine(FakeEngine):
            def start(self, folder, message):
                return started, None

        starter = sidebar.Starter(Engine(([started], None)), "/p", "")
        starter.run()
        self.app.starter = starter
        self.app.finish_start()
        self.assertEqual(self.opened, [(KEY_A, core.NEW, (started, True))])


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


class FocusTmux(WindowPanes):
    """The sidebar (%1) and the slot (%2) showing `b`, or with `main_agent` empty showing nothing; `panel_active` is which of them tmux says is active."""

    def __init__(self):
        self.panel_active = True
        self.back = False
        self.cleared = []
        self.main_agent = "claude"

    def panes(self):
        return [
            Pane("%1", "@1", False, "sidebar", "", 40, 121, self.panel_active, self.back),
            Pane("%2", "@1", False, "viewer" if self.main_agent else "placeholder", "", 80, 121, not self.panel_active,
                 view="b" if self.main_agent else ""),
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

    def test_left_from_a_pane_without_the_agent_tag_leaves_the_cursor_where_he_left_it(self):
        self.app.tmux.main_agent = ""
        self.app.main_pane = self.app.tmux.panes()[1]
        self.leave_panel()
        self.come_back(by_left=True)
        self.assertIs(self.app.focused, True)
        self.assertEqual(self.app.cursor, ("session", "c"))

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

    def test_help_shows_the_arrows(self):
        help_keys = " ".join(key for key, _ in sidebar.HELP)
        for arrow in ("←", "→"):
            with self.subTest(arrow=arrow):
                self.assertIn(arrow, help_keys)


class CloseTmux(WindowPanes):
    """The sidebar (%1), the slot (%2) holding an old viewer of `a`, a hidden old viewer (%3) of `b` and the 2.9.0 placeholder (%4) in its own window."""

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

    def respawn(self, pane, command, cwd=None):
        self.calls.append(("respawn", pane))

    def set_pane_option(self, pane, name, value):
        pass

    def unset_pane_option(self, pane, name):
        pass

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
            main = next(p for p in self.tmux.panes() if p.id == "%2")
            self.app.close_stale(main)

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

    def test_a_dead_viewer_in_the_slot_gives_way_to_the_placeholder(self):
        self.make_dead("%2")
        self.run_close(core.STALE_AFTER_MISSES)
        self.assertEqual(self.tmux.calls, [("respawn", "%2")])

    def test_a_listing_without_the_session_still_closes_its_viewer(self):
        self.app.sessions = [session("b", "/tmp/b")]
        self.run_close(2)
        self.assertIn(("respawn", "%2"), self.tmux.calls)
        self.assertNotIn(("kill", "%2"), self.tmux.calls)
        self.assertNotIn(("kill", "%3"), self.tmux.calls)


KEY_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class LegacyTmux(ClientTmux):
    """The panel (%1); main (%2) showing an old `claude attach` viewer of `old1`; a hidden old viewer (%3) of `old2`; the placeholder (%4); and a session emx holds itself (%5). Swaps and kills change what `panes` lists, as tmux would."""

    socket = "emx-test-legacy"

    def __init__(self, main_agent=""):
        super().__init__()
        self.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 121),
            Pane("%2", "@1", False, "", "old1", 80, 121, agent=main_agent),
            Pane("%3", "@2", False, "", "old2", 80, 80),
            Pane("%4", "@3", False, "placeholder", "", 80, 80),
            Pane("%5", "@4", False, "", KEY_A, 80, 80, agent="claude"),
        ]
        self.calls = []

    def panes(self):
        return list(self.list)

    def swap(self, source, target):
        self.calls.append(("swap", source, target))
        a = next(p for p in self.list if p.id == source)
        b = next(p for p in self.list if p.id == target)
        self.list = [replace(b, window=a.window) if p is a else replace(a, window=b.window) if p is b else p for p in self.list]

    def kill_pane(self, pane):
        self.calls.append(("kill", pane))
        self.list = [p for p in self.list if p.id != pane]

    def capture(self, pane):
        return ""

    def respawn(self, pane, command, cwd=None):
        self.calls.append(("respawn", pane))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class LegacyViewers(AppCase):
    """The first housekeep after the panel starts on 2.0.0 closes every `claude attach` viewer an older emx left, whatever the listing says."""

    def setUp(self):
        super().setUp()
        self.no_scheme()
        self.app.tmux = LegacyTmux()
        self.app.me = "%1"
        self.app.legacy_cleared = False
        self.app.sessions = []

    def housekeep(self):
        sidebar.App.housekeep(self.app, False)

    def test_with_an_empty_listing_the_slot_gets_the_placeholder_and_every_other_old_viewer_is_killed(self):
        self.housekeep()
        self.assertEqual(self.app.tmux.calls, [("respawn", "%2"), ("kill", "%3")])
        self.assertEqual((self.app.main_pane.id, self.app.main_pane.role), ("%2", "placeholder"))
        self.assertTrue(self.app.legacy_cleared)

    def test_it_runs_once(self):
        self.housekeep()
        self.app.tmux.list.append(Pane("%6", "@5", False, "", "old3", 80, 80))
        self.app.tmux.calls = []
        self.housekeep()
        self.assertNotIn(("kill", "%6"), self.app.tmux.calls)

    def test_a_pane_with_an_agent_tag_is_never_killed_or_swapped(self):
        self.app.tmux = LegacyTmux(main_agent="claude")
        self.housekeep()
        self.assertEqual(self.app.tmux.calls, [("kill", "%3")])
        self.assertIn("%5", [p.id for p in self.app.tmux.list])

    def test_with_no_placeholder_pane_the_slot_still_gets_the_placeholder_and_the_rest_go(self):
        self.app.tmux.list = [p for p in self.app.tmux.list if p.role != "placeholder"]
        self.housekeep()
        self.assertEqual(self.app.tmux.calls, [("respawn", "%2"), ("kill", "%3")])

    def test_a_tmux_failure_leaves_it_to_the_next_housekeep(self):
        def broken(pane):
            raise TmuxError("no server")
        self.app.tmux.kill_pane = broken
        self.housekeep()
        self.assertFalse(self.app.legacy_cleared)
        self.assertTrue(self.app.notice_error)


class EngineErrorKeepsRows(AppCase):
    def test_an_engine_error_sets_list_error_and_leaves_the_sessions(self):
        rows = [session("a", "/p"), session("b", "/p")]
        self.poll(*rows)
        self.app.take_poll()
        self.app.poller.result = (None, "Couldn't read emx's sessions folder: denied")
        self.app.take_poll()
        self.assertEqual(self.app.list_error, "Couldn't read emx's sessions folder: denied")
        self.assertEqual(self.app.sessions, rows)
        self.poll(*rows)
        self.app.take_poll()
        self.assertEqual(self.app.list_error, "")


class StaleNeverEndsASession(AppCase):
    def test_a_pane_emx_holds_a_session_in_is_never_closed_as_stale(self):
        self.app.tmux = LegacyTmux()
        self.app.sessions = [session("other", "/p")]
        for _ in range(core.STALE_AFTER_MISSES + 1):
            self.app.close_stale(self.app.tmux.panes()[1])
        self.assertNotIn(("kill", "%5"), self.app.tmux.calls)
        self.assertIn(("kill", "%3"), self.app.tmux.calls)


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

class SessionCloseTmux(ClientTmux):
    """The panel (%1); the slot (%2) showing session `a` through a viewer, as does another view's slot (%8); `a`'s own pane (%5) and `b`'s (%3) hidden. Kills and the slots' tags change what `panes` and `views` list, as tmux would, and kills and the slots' respawns go into the shared `log`."""

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 121),
            Pane("%2", "@1", False, "viewer", "", 80, 121, view="a"),
            Pane("%3", "@2", False, "", "b", 80, 80, agent="claude"),
            Pane("%5", "@4", False, "", "a", 80, 80, agent="claude"),
        ]
        self.kill_fails = False
        self.kill_ends = True
        self.shows = {"%2": "a", "%8": "a"}

    def panes(self):
        return list(self.list)

    def views(self):
        return [View(SESSION, 1, "", None, 121, self.shows.get("%2", ""), False, "%2"),
                View("emx-view-1", 1, "", None, 48, self.shows.get("%8", ""), False, "%8")]

    def respawn(self, pane, command, cwd=None):
        self.log.append(("respawn", pane))

    def set_pane_option(self, pane, name, value):
        if name == sidebar.ROLE:
            self.list = [replace(p, role=value) if p.id == pane else p for p in self.list]

    def unset_pane_option(self, pane, name):
        if name == sidebar.VIEW_OPT:
            self.shows.pop(pane, None)
            self.list = [replace(p, view="") if p.id == pane else p for p in self.list]

    def kill_pane(self, pane):
        self.log.append(("kill", pane))
        if self.kill_fails:
            raise TmuxError("server busy")
        if self.kill_ends:
            self.list = [p for p in self.list if p.id != pane]


class CloseEngine(FakeEngine):
    def __init__(self, tmux, log):
        super().__init__(tmux=tmux)
        self.log = log
        self.forget_result = (True, None)
        self.threads = []

    def forget(self, key):
        self.log.append(("forget", key))
        self.threads.append(threading.current_thread() is not threading.main_thread())
        return self.forget_result


class CloseSession(AppCase):
    """Ctrl+X twice on one row closes that session (its pane is killed if it runs, then emx forgets it), on a worker thread, after a fresh listing agrees with the row.

    The rows wait for input, so the first press only arms: interrupting a working row is InterruptSession's.
    """

    def setUp(self):
        super().setUp()
        self.a, self.b = session("a", "/p/alpha", state="blocked"), session("b", "/p/alpha", state="blocked", started=1.0)
        self.poll(self.a, self.b)
        self.app.refresh()
        self.log = []
        self.app.tmux = SessionCloseTmux(self.log)
        self.engine = CloseEngine(self.app.tmux, self.log)
        self.app.cursor = ("session", "a")
        self.listing = ([self.a, self.b], None)

        def fetch(engine):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            sessions, error = self.listing
            if sessions is None:
                return self.listing
            live = {p.session for p in self.app.tmux.list if p.agent and not p.dead}
            return [s if not s.running or s.id in live else replace(s, state="stopped", running=False)
                    for s in sessions], error

        for p in (mock.patch.object(sidebar, "fetch_sessions", fetch),
                  mock.patch.object(sidebar, "Engine", lambda tmux, claude, state: self.engine),
                  mock.patch.object(sidebar.subprocess, "run", side_effect=AssertionError("a command ran")),
                  mock.patch.object(sidebar, "LISTED_CHECK_SECONDS", 0)):
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

    def test_double_ctrl_x_on_a_running_row_ends_and_forgets_it_off_the_curses_thread(self):
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.confirm.id, "a")
        self.assertEqual(self.app.interrupters, {})
        self.assertEqual(self.app.bar_hints(), (sidebar.CONFIRM_FOOTER,))
        self.assertEqual(self.app.bottom_lines(39), [])
        self.assertIn("^x to confirm", self.bar())
        self.assertEqual(self.log, [])
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Closing emx-test-a…")
        self.assertIsNone(self.app.confirm)
        self.settle()
        # Every view showing it gets the placeholder before the kill, so none shows its viewer closing.
        self.assertEqual(self.log, [("respawn", "%2"), ("respawn", "%8"), ("kill", "%5"), ("forget", "a")])
        self.assertEqual(self.engine.threads, [True])
        self.assertEqual((self.app.notice, self.app.notice_error), ("Closed emx-test-a.", False))
        self.assertTrue(self.app.poller.wake.is_set())
        self.assertEqual([(p.id, p.role, p.view) for p in self.app.tmux.list if p.id == "%2"], [("%2", "placeholder", "")])

    def test_a_stopped_session_is_only_forgotten_and_its_dead_pane_and_the_slot_showing_it_close_after(self):
        self.app.tmux.list = [replace(p, dead=True) if p.id == "%5" else p for p in self.app.tmux.list]
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.listing = ([self.stopped("a"), self.b], None)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.log, [("forget", "a"), ("kill", "%5"), ("respawn", "%2")])
        self.assertEqual(self.app.notice, "Closed emx-test-a.")

    def test_closing_a_session_whose_pane_is_hidden_kills_that_pane_with_no_swap(self):
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.log, [("kill", "%3"), ("forget", "b")])

    def test_a_kill_tmux_refuses_forgets_nothing_and_says_so(self):
        self.app.tmux.kill_fails = True
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertNotIn("forget", [c[0] for c in self.log])
        self.assertEqual((self.app.notice, self.app.notice_error), ("tmux: server busy", True))
        self.assertEqual(self.app.closing, {})
        self.assertIsNotNone(self.app.session("a"))

    def test_a_session_still_listed_running_after_the_kill_is_not_forgotten(self):
        self.app.tmux.kill_ends = False
        with mock.patch.object(sidebar, "LISTED_WAIT_SECONDS", 0):
            self.press(sidebar.CTRL_X, sidebar.CTRL_X)
            self.settle()
        self.assertIn(("kill", "%5"), self.log)
        self.assertNotIn("forget", [c[0] for c in self.log])
        self.assertEqual((self.app.notice, self.app.notice_error),
                         ("emx-test-a is still running, so emx didn't remove it.", True))
        self.assertEqual(self.app.closing, {})

    def test_a_failed_forget_is_tried_again_then_said_and_the_row_stays(self):
        self.engine.forget_result = (False, "Couldn't remove the session: denied")
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertIn("a", self.app.closing)
        self.settle()
        self.assertEqual([c for c in self.log if c[0] == "forget"], [("forget", "a")] * sidebar.FORGET_TRIES)
        self.assertEqual((self.app.notice, self.app.notice_error), ("Couldn't remove the session: denied", True))
        self.assertEqual(self.app.closing, {})
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.assertNotIn("closing…", self.drawn())

    def test_a_stopped_row_resumed_elsewhere_is_never_removed(self):
        self.poll(self.stopped("a"), self.b)
        self.app.refresh()
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.log, [])
        self.assertIn("running again", self.app.notice)
        self.assertEqual(self.app.closing, {})

    def test_moving_between_presses_acts_on_neither_row(self):
        self.press(sidebar.CTRL_X, curses.KEY_DOWN, sidebar.CTRL_X)
        self.assertIsNone(self.app.closer)
        self.assertEqual(self.app.confirm.id, "b")
        self.assertEqual(self.log, [])

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
        self.app.closer = sidebar.Closer(FakeEngine(), (busy,))
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
        self.assertIn("^x again to close", drawn)
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
        self.app.tmux.list = [replace(p, dead=True) if p.id == "%3" else p for p in self.app.tmux.list]
        self.poll(self.a, self.stopped("b", 1.0), other)
        self.app.refresh()
        self.listing = ([self.a, self.stopped("b", 1.0), other], None)
        self.app.cursor = ("group", "/p/alpha")
        self.press(sidebar.CTRL_X)
        self.assertIsInstance(self.app.confirm, core.GroupConfirm)
        self.assertIn("^x again to close 2 sessions", self.drawn())
        self.assertEqual(self.log, [])
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Closing 2 sessions…")
        drawn = self.drawn()
        self.assertEqual(drawn.count("closing…"), 3)
        self.settle()
        self.assertEqual(self.log, [("respawn", "%2"), ("respawn", "%8"), ("kill", "%5"), ("forget", "a"), ("forget", "b"), ("kill", "%3")])
        self.assertEqual((self.app.notice, self.app.notice_error), ("Closed emx-test-a. Closed emx-test-b.", False))
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
        self.assertEqual(self.log, [("respawn", "%2"), ("respawn", "%8"), ("kill", "%5"), ("forget", "a")])
        self.assertEqual(self.app.notice, "Closed emx-test-a. emx-test-b isn't listed any more, so emx didn't close it.")
        self.assertTrue(self.app.notice_error)
        self.assertEqual(self.app.closing, {"a": True})

    def test_ctrl_x_on_a_pinned_or_state_header_closes_nothing_and_says_why(self):
        self.app.prefs = core.toggle_pin(self.app.prefs, "a")
        self.app.regroup()
        self.app.cursor = ("group", core.PINNED)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer), (None, None))
        self.assertEqual(self.app.notice, "^x can't close the Pinned group; close its sessions one at a time.")
        self.app.say("")
        self.app.prefs = core.toggle_view(self.app.prefs)
        self.app.regroup()
        self.app.cursor = ("group", core.PINNED)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer), (None, None))
        self.assertEqual(self.app.notice, "^x can't close the Pinned group; close its sessions one at a time.")
        self.app.say("")
        self.app.cursor = ("group", core.NEEDS_INPUT)
        self.press(sidebar.CTRL_X, sidebar.CTRL_X)
        self.assertEqual((self.app.confirm, self.app.closer), (None, None))
        self.assertEqual(self.app.notice, "^x closes a project's group from the project view.")
        self.assertEqual(self.log, [])

    def test_first_ctrl_x_on_a_folded_header_unfolds_and_arms_and_the_second_closes_all(self):
        self.app.cursor = ("group", "/p/alpha")
        self.app.toggle_group("/p/alpha")
        self.assertIn("/p/alpha", self.app.collapsed())
        self.press(sidebar.CTRL_X)
        self.assertNotIn("/p/alpha", self.app.collapsed())
        self.assertEqual(self.app.cursor, ("group", "/p/alpha"))
        self.assertEqual(self.app.confirm.key, "/p/alpha")
        self.assertIn("^x again to close 2 sessions", self.drawn())
        self.assertEqual(self.log, [])
        self.press(sidebar.CTRL_X)
        self.settle()
        self.assertEqual(self.log, [("respawn", "%2"), ("respawn", "%8"), ("kill", "%5"), ("forget", "a"), ("kill", "%3"), ("forget", "b")])

    def test_the_help_shows_ctrl_x(self):
        self.assertIn("^x", " ".join(key for key, _ in sidebar.HELP))

    def test_help_names_interrupt_and_close(self):
        self.assertEqual(dict(sidebar.HELP)["^x"], "interrupt a working session; twice closes it")
        at = [key for key, _ in sidebar.HELP].index("^x")
        self.assertEqual(sidebar.HELP[at + 1], ("", "on a project header, twice closes the group"))
        scr = RecordingScr(30, 60)
        self.app.scr = scr
        self.press("?")
        self.app.draw()
        shown = "\n".join(scr.cells.values())
        self.assertIn("interrupt a working session; twice closes it", shown)
        self.assertIn("on a project header, twice closes the group", shown)

    def test_the_help_and_bar_hints_name_no_viewer_and_no_r_key(self):
        self.assertNotIn("r", [key for key, _ in sidebar.HELP])
        hints = [v if isinstance(v, str) else core.hint_text(v) for v in self.app.bar_hints()]
        self.assertTrue(hints)
        for text in [*hints, *(f"{key} {what}" for key, what in sidebar.HELP)]:
            with self.subTest(text=text):
                self.assertNotIn("viewer", text)
                self.assertNotIn("stop, then remove", text)

    def test_r_in_the_panel_makes_no_tmux_call_and_starts_no_worker(self):
        tmux = mock.Mock(spec=sidebar.Tmux)
        self.app.tmux = tmux
        with mock.patch.object(threading.Thread, "start", side_effect=AssertionError("a worker started")):
            self.press("r")
        self.assertEqual(tmux.method_calls, [])
        self.assertEqual(self.log, [])
        self.assertEqual(
            (self.app.starter, self.app.closer, self.app.interrupters, self.app.renamer, self.app.namer),
            (None, None, {}, None, None),
        )


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
        self.assertEqual(self.app.bar_hints(), ())

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
        self.app.prefs.update(favourite_folders=["/f/star"], recent_folders=["/f/r1"])
        saves = []
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("scanned")), \
                mock.patch.object(sidebar.os.path, "isdir", side_effect=AssertionError("stat")), \
                mock.patch.object(sidebar.App, "save_prefs", lambda app: saves.append(1)):
            self.keys("n", "\t", "\x14")
            self.type("~/some/path")
            self.app.draw()
            self.keys(curses.KEY_DOWN, "\t", "\n")
        self.assertEqual(len(saves), 1)

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
        drawn = [text for text, _ in self.app.bottom_lines(39, 7)]
        self.assertLessEqual(len(drawn), 7)
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

    def picker_with(self, favourites=(), recent=()):
        if favourites:
            self.app.prefs["favourite_folders"] = list(favourites)
        if recent:
            self.app.prefs["recent_folders"] = list(recent)
        self.app.cursor = ("session", "b")
        self.keys("n", "\t")

    def paths(self):
        return [r.path for r in self.app.suggestions()]

    def saved(self):
        return core.load_prefs(self.app.prefs_file)

    def test_the_picker_opens_with_favourites_then_recents_before_any_typing(self):
        self.picker_with(["/f/star"], ["/f/r1", "/f/r2"])
        paths = self.paths()
        self.assertEqual(paths[:3], ["/f/star", "/f/r1", "/f/r2"])
        self.assertIn("/p/beta", paths[3:])
        lines = [text for text, _ in self.app.bottom_lines(39, 30)]
        self.assertTrue(next(t for t in lines if "/f/star" in t).startswith("★"))
        self.assertTrue(next(t for t in lines if "/f/r1" in t).startswith(" "))

    def test_ctrl_t_stars_the_highlight_saves_it_and_keeps_the_highlight(self):
        self.picker_with(recent=["/f/r1", "/f/r2"])
        self.keys(curses.KEY_DOWN, curses.KEY_DOWN, "\x14")
        self.assertEqual(self.saved()["favourite_folders"], ["/f/r2"])
        self.assertEqual(self.app.suggestions()[0].path, "/f/r2")
        self.assertEqual(self.app.prompt["picked"], "/f/r2")
        self.assertIn("favourite", self.app.prompt["hint"])
        self.keys("\x14")
        self.assertNotIn("favourite_folders", self.saved())

    def test_ctrl_t_with_nothing_highlighted_stars_the_top_row(self):
        self.picker_with(recent=["/f/r1"])
        self.keys("\x14")
        self.assertEqual(self.saved()["favourite_folders"], ["/f/r1"])

    def test_ctrl_t_in_the_message_stage_does_nothing(self):
        self.app.cursor = ("session", "b")
        self.keys("n", "\x14")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["text"]), ("message", ""))
        self.assertNotIn("favourite_folders", self.app.prefs)

    def test_unstarring_a_folder_that_leaves_the_list_clears_the_highlight(self):
        self.picker_with(favourites=["/f/only"])
        self.keys(curses.KEY_DOWN)
        self.assertEqual(self.app.prompt["picked"], "/f/only")
        self.keys("\x14")
        self.assertNotIn("/f/only", self.paths())
        self.assertIsNone(self.app.prompt["picked"])

    def test_a_remembered_folder_found_missing_is_hidden_and_kept(self):
        self.app.listings.readable_cache.update({"/f/gone": False, "/f/gonefav": False})
        self.picker_with(["/f/gonefav"], ["/f/gone", "/f/r1"])
        for typed in ("", "gone"):
            self.type(typed)
            self.assertNotIn("/f/gone", self.paths())
            self.assertNotIn("/f/gonefav", self.paths())
        self.assertIn("/f/gone", self.app.prefs["recent_folders"])
        self.assertIn("/f/gonefav", self.app.prefs["favourite_folders"])

    def test_a_ninth_recent_fills_in_for_a_missing_one_and_is_checked(self):
        self.app.listings.readable_cache["/f/r1"] = False
        self.picker_with(recent=[f"/f/r{i}" for i in range(1, 10)])
        rows = [r.path for r in self.app.suggestions() if r.source == core.RECENT]
        self.assertEqual(rows, [f"/f/r{i}" for i in range(2, 10)])
        self.assertIn("/f/r9", self.app.listings.asked)

    def test_the_picker_hint_names_ctrl_t(self):
        self.picker_with()
        drawn = " ".join(text.strip() for text, _ in self.app.bottom_lines(39, 30))
        self.assertIn("^t ★", drawn)

    def test_the_help_names_the_favourite_key(self):
        self.assertTrue(any("^t" in text and "★" in text for _, text in sidebar.HELP))
        self.assertIn("Tab", dict(sidebar.HELP)["n"])

    def test_opening_a_session_makes_its_folder_recent_but_not_last(self):
        self.app.prefs["last_folder"] = "/p/last"
        self.app.activate(("session", "a"))
        saved = self.saved()
        self.assertEqual(saved["recent_folders"][0], "/p/alpha/sub")
        self.assertEqual(saved["last_folder"], "/p/last")
        self.assertIn(("a", core.OPEN), self.opened)

    def test_opening_a_placed_worktree_session_records_its_repo(self):
        wt = "/w/k/.claude/worktrees/f"
        self.poll(session("w", wt))
        self.app.refresh()
        self.app.placements = {wt: core.Placement("/w/k", "f")}
        self.app.activate(("session", "w"))
        self.assertEqual(self.saved()["recent_folders"][0], "/w/k")

    def test_opening_a_group_header_records_nothing(self):
        self.app.activate(("group", "/p/beta"))
        self.assertNotIn("recent_folders", self.app.prefs)

    def test_reopening_the_first_recent_does_not_rewrite_prefs(self):
        saves = []
        with mock.patch.object(sidebar.App, "save_prefs", lambda app: saves.append(1)):
            self.app.activate(("session", "a"))
            self.app.activate(("session", "a"))
        self.assertEqual(len(saves), 1)

    def test_enter_from_find_records_the_folder(self):
        self.keys(sidebar.CTRL_F, *"beta", "\r")
        self.assertEqual(self.app.prefs["recent_folders"][0], "/p/beta")
        self.assertIn(("b", core.OPEN), self.opened)


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

    def test_slash_opens_find_too_and_its_hint_stays_in_the_panel_while_the_bar_has_none(self):
        self.keys("/", *"alp")
        texts = [t for t, _ in self.app.bottom_lines(39)]
        self.assertIn("> alp▏", texts)
        self.assertIn(sidebar.FIND_HINT, texts)
        self.assertEqual(self.app.bar_hints(), ())

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
INTERACTIVE_CURSORS = json.loads(
    (Path(__file__).parent / "fixtures" / "screens" / "interactive-cursors.json").read_text("utf-8"),
)


class RenameTmux(WindowPanes):
    """The panel (%1) has focus; main (%2) shows `named`; a hidden viewer (%3) shows `b`. Its screen and prompt come from real captures, and tmux's last-moment check answers `gate_says`."""

    def __init__(self):
        self.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 141, active=True),
            Pane("%2", "@1", False, "", "named", 100, 141, agent="claude"),
            Pane("%3", "@2", False, "", "b", 100, 100, agent="claude"),
        ]
        self.show_screen("named-viewer", "prompt-empty")
        self.in_mode = False
        self.gate_says = core.TYPED
        self.prompt_after = None
        self.after_typing = None
        self.empty = None
        self.clear_says = core.TYPED
        self.enter_says = core.TYPED
        self.typed = []
        self.gates = []
        self.enter_gates = []
        self.clear_gates = []
        self.events = []
        self.threads = []

    def show_screen(self, screen, prompt):
        self.screen, _ = fixture_screen(screen)
        text, width = fixture_screen(prompt)
        cursor = {**PROMPT_CURSORS["cursors"], **INTERACTIVE_CURSORS["cursors"]}[prompt]
        lines = text.split("\n")
        self.prompt = (lines[cursor["y"]], lines[cursor["y"] + 1])
        self.cursor_at = (cursor["x"], cursor["y"], cursor["shown"], width)

    def note(self):
        self.threads.append(threading.current_thread() is threading.main_thread())

    def panes(self):
        return list(self.list)

    def capture(self, pane):
        self.note()
        self.events.append("capture")
        return self.screen

    def prompt_probe(self, pane):
        self.note()
        return (*self.cursor_at, self.in_mode)

    def cursor_lines(self, pane, y):
        self.note()
        return list(self.prompt)

    def type_if(self, pane, gate, text, typed, report, enter=True):
        self.note()
        self.gates.append(gate)
        self.events.append("type_if")
        if self.gate_says == typed:
            self.typed.append((pane, text))
            self.type_into_prompt(text)
            if self.after_typing is not None:
                self.after_typing()
            if enter:
                self.press_enter()
        return self.gate_says

    def type_into_prompt(self, text):
        """What the real prompt shows once the line is in it: the line after the mark, the cursor after the line, the rule under it."""
        self.empty = (self.prompt, self.cursor_at)
        x, y, shown, width = self.cursor_at
        self.prompt = ("\x1b[39m❯\xa0" + text, self.prompt[1])
        self.cursor_at = (2 + core.display_width(text), y, shown, width)

    def press_enter(self):
        self.events.append("enter")
        if self.empty is not None:
            self.prompt, self.cursor_at = self.empty
            self.empty = None
        if self.prompt_after == "gone":
            self.prompt_probe = mock.Mock(side_effect=TmuxError("can't find pane"))
        elif self.prompt_after is not None:
            self.show_screen("named-viewer", self.prompt_after)

    def submit_if(self, pane, gate, typed, report):
        self.note()
        self.events.append("submit_if")
        self.enter_gates.append(gate)
        if self.enter_says == typed:
            self.press_enter()
        return self.enter_says

    def clear_if(self, pane, gate, typed, report):
        self.note()
        self.events.append("clear_if")
        self.clear_gates.append(gate)
        if self.clear_says == typed and self.empty is not None:
            self.prompt, self.cursor_at = self.empty
            self.empty = None
        return self.clear_says


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

        def fetch(engine):
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
            "0 0 1 2 26 b": "session changed",
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
                self.app.tmux.show_screen("named-viewer", prompt)
                self.rename_to("fresh name")
                self.settle()
                self.refused("emx-test-named's prompt isn't empty, so emx didn't type into it.")

    def test_the_pane_tagged_with_the_sessions_key_is_typed_into_whatever_name_its_screen_shows(self):
        for screen in ("cross-wired", "interactive-unnamed-empty", "interactive-named-empty"):
            with self.subTest(screen=screen):
                self.app.tmux = RenameTmux()
                self.listings = [[self.named]]
                self.app.tmux.show_screen(screen, "prompt-empty")
                self.rename_to("fresh name")
                self.settle()
                self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])

    def test_a_pane_holding_the_key_without_the_agent_tag_is_never_typed_into(self):
        tmux = self.app.tmux
        tmux.list = [replace(p, agent="") if p.session == "named" else p for p in tmux.list]
        self.rename_to("fresh name")
        self.settle()
        self.refused("session has closed")
        self.assertEqual(tmux.gates, [])

    def test_an_unnamed_sessions_own_empty_prompt_is_typed_into(self):
        unnamed = core.Session("named", "a1b2c3d4", "/p/alpha", "blocked", "", "", 0.0, True, named=False)
        self.poll(unnamed)
        self.app.refresh()
        self.listings = [[unnamed]]
        self.app.tmux.show_screen("interactive-unnamed-empty", "interactive-prompt-empty-unnamed")
        self.keys(sidebar.CTRL_R)
        self.assertEqual(self.app.rename_edit["text"], "")
        self.keys(*"fresh name", "\r")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])
        self.assertEqual(self.app.notice, "Renamed a1b2c3d4 to fresh name.")

    def test_claudes_title_landing_on_an_unnamed_row_during_the_wait_is_not_taken_for_the_new_name(self):
        unnamed = core.Session("named", "a1b2c3d4", "/p/alpha", "blocked", "", "", 0.0, True, named=False)
        self.poll(unnamed)
        self.app.refresh()
        self.listings = [[unnamed]]
        self.after = core.Session("named", "Fixing the login bug", "/p/alpha", "blocked", "", "", 0.0, True, named=False)
        self.app.tmux.show_screen("interactive-unnamed-empty", "interactive-prompt-empty-unnamed")
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])
        self.assertNotIn("Renamed", self.app.notice)
        self.assertIn("doesn't show a new name yet", self.app.notice)

    def test_ctrl_r_on_a_running_session_opens_the_edit_with_no_tmux_call(self):
        self.app.tmux.panes = mock.Mock(side_effect=AssertionError("asked tmux"))
        self.keys(sidebar.CTRL_R)
        self.assertEqual(self.app.rename_edit["text"], "emx-test-named")

    def test_a_typed_rename_wakes_the_transcript_reader_while_it_waits_for_the_name(self):
        self.app.poller.transcripts.wake.clear()
        self.after = session("named", "/p/alpha", state="blocked")
        self.rename_to("fresh name")
        self.settle()
        self.assertTrue(self.app.poller.transcripts.wake.is_set())

    def test_a_stopped_session_is_never_typed_into(self):
        self.listings = [[session("named", "/p/alpha", state="stopped", pid=None)]]
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [])
        self.assertTrue(self.app.notice_error)

    def test_a_working_session_is_typed_into_and_the_queued_rename_is_said_plainly(self):
        working = session("named", "/p/alpha", state="working")
        self.listings = [[working]]
        self.after = working
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])
        self.assertIn("applies the name when this turn ends", self.app.notice)
        self.assertNotIn("doesn't show", self.app.notice)
        self.assertFalse(self.app.notice_error)

    def test_a_working_session_whose_name_applies_at_once_is_reported_renamed(self):
        self.listings = [[session("named", "/p/alpha", state="working")]]
        self.after = core.Session("named", "fresh name", "/p/alpha", "working", "", "", 0.0, True)
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(self.app.tmux.typed, [("%2", "/rename fresh name")])
        self.assertEqual((self.app.notice, self.app.notice_error), ("Renamed emx-test-named to fresh name.", False))

    def test_a_working_session_whose_prompt_holds_text_is_never_typed_into(self):
        self.listings = [[session("named", "/p/alpha", state="working")]]
        self.app.tmux.show_screen("named-viewer", "prompt-text")
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual((self.app.tmux.typed, self.app.tmux.gates), ([], []))
        self.assertIn("prompt isn't empty", self.app.notice)
        self.assertTrue(self.app.notice_error)

    def test_a_prompt_drawn_a_column_over_is_gated_at_its_own_cursor_column(self):
        tmux = self.app.tmux
        x, y, shown, width = tmux.cursor_at
        line, below = tmux.prompt
        tmux.prompt = (" " + line, below)
        tmux.cursor_at = (x + 1, y, shown, width)
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(tmux.gates, [core.type_gate("%2", "%1", "named", y, sidebar.SESSION_OPT, x + 1)])
        self.assertEqual(tmux.typed, [("%2", "/rename fresh name")])

    def test_the_line_goes_in_without_enter_and_enter_follows_only_the_exact_line_check(self):
        self.rename_to("fresh name")
        self.settle()
        tmux = self.app.tmux
        self.assertEqual(tmux.events, ["type_if", "submit_if", "enter"])
        at = 2 + len("/rename fresh name")
        self.assertEqual(tmux.enter_gates, [core.type_gate("%2", "%1", "named", 26, sidebar.SESSION_OPT, at)])
        self.assertEqual(tmux.typed, [("%2", "/rename fresh name")])
        self.assertEqual(tmux.clear_gates, [])

    def test_a_wide_name_gets_its_enter_gate_pinned_by_display_width(self):
        self.rename_to("名前です")
        self.settle()
        tmux = self.app.tmux
        self.assertEqual(tmux.events, ["type_if", "submit_if", "enter"])
        at = 2 + core.display_width("/rename 名前です")
        self.assertEqual(tmux.enter_gates, [core.type_gate("%2", "%1", "named", 26, sidebar.SESSION_OPT, at)])

    def slow_to_draw(self, whole_after):
        """The prompt first shows only the start of the line; the rest is drawn at the `whole_after`th read of the prompt line."""
        tmux = self.app.tmux
        captures = []

        def partial():
            tmux.full = (tmux.prompt, tmux.cursor_at)
            text = "/rename fre"
            tmux.prompt = ("\x1b[39m❯\xa0" + text, tmux.prompt[1])
            tmux.cursor_at = (2 + len(text), *tmux.cursor_at[1:])

        def prompt_probe(pane):
            captures.append(1)
            if len(captures) >= whole_after:
                tmux.prompt, tmux.cursor_at = tmux.full
            return (*tmux.cursor_at, tmux.in_mode)

        tmux.after_typing = partial
        tmux.prompt_probe = prompt_probe

    def test_enter_waits_while_the_prompt_shows_only_the_start_of_the_line(self):
        self.slow_to_draw(whole_after=4)
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(self.app.tmux.events[-2:], ["submit_if", "enter"])
        self.assertEqual(self.app.tmux.clear_gates, [])

    def test_enter_waits_while_claudes_grey_name_hint_still_follows_the_command(self):
        tmux = self.app.tmux
        reads = []

        def hint():
            tmux.full = (tmux.prompt, tmux.cursor_at)
            tmux.prompt = ("\x1b[39m❯\xa0\x1b[38;5;153m/rename\x1b[39m  \x1b[38;5;246m[name]\x1b[39m", tmux.prompt[1])
            tmux.cursor_at = (2 + len("/rename "), *tmux.cursor_at[1:])

        def prompt_probe(pane):
            reads.append(1)
            if len(reads) >= 4:
                tmux.prompt, tmux.cursor_at = tmux.full
            return (*tmux.cursor_at, tmux.in_mode)

        tmux.after_typing, tmux.prompt_probe = hint, prompt_probe
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(tmux.events[-2:], ["submit_if", "enter"])
        self.assertEqual(tmux.clear_gates, [])
        self.assertEqual(self.app.notice, "Renamed emx-test-named to fresh name.")

    def test_a_line_that_never_finishes_drawing_gets_no_enter_after_the_bounded_wait(self):
        self.slow_to_draw(whole_after=10 ** 6)
        with mock.patch.object(sidebar, "PROMPT_SETTLE_SECONDS", 0.1), \
                mock.patch.object(sidebar, "PROMPT_SETTLE_STEP", 0.01):
            self.rename_to("fresh name")
            self.settle()
        self.assertNotIn("submit_if", self.app.tmux.events)
        self.assertNotIn("enter", self.app.tmux.events)
        self.assertTrue(self.app.notice_error)

    def test_an_enter_whose_tmux_command_fails_is_not_reported_as_not_pressed(self):
        tmux = self.app.tmux
        tmux.submit_if = mock.Mock(side_effect=TmuxError("lost"))
        self.rename_to("fresh name")
        self.settle()
        self.assertIn("couldn't confirm Enter", self.app.notice)
        self.assertNotIn("didn't press", self.app.notice)

    def test_a_dialog_opening_between_the_typing_and_enter_gets_no_enter(self):
        tmux = self.app.tmux
        tmux.after_typing = lambda: tmux.show_screen("prompt-permission", "prompt-permission")
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(tmux.typed, [("%2", "/rename fresh name")])
        self.assertNotIn("submit_if", tmux.events)
        self.assertNotIn("enter", tmux.events)
        self.assertNotIn("clear_if", tmux.events)
        self.assertIn("didn't press Enter", self.app.notice)
        self.assertIn("may still be in it", self.app.notice)
        self.assertTrue(self.app.notice_error)

    def test_a_draft_added_to_the_line_gets_no_enter_and_is_not_cleared(self):
        tmux = self.app.tmux

        def add_draft():
            tmux.prompt = (tmux.prompt[0] + " and more", tmux.prompt[1])
            tmux.cursor_at = (tmux.cursor_at[0] + 9, *tmux.cursor_at[1:])

        tmux.after_typing = add_draft
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual([e for e in tmux.events if e not in ("capture", "type_if")], [])
        self.assertIn("may still be in it", self.app.notice)

    def test_an_enter_tmux_refuses_clears_the_line_and_says_so(self):
        tmux = self.app.tmux
        tmux.enter_says = "0 0 1 20 27 named"
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(tmux.events[-2:], ["submit_if", "clear_if"])
        self.assertNotIn("enter", tmux.events)
        self.assertIn("cleared the line", self.app.notice)

    def test_a_line_that_cannot_be_cleared_is_left_and_said_so(self):
        tmux = self.app.tmux
        tmux.enter_says = "0 0 1 20 27 named"
        tmux.clear_says = "0 0 1 20 26 named"
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(len(tmux.clear_gates), 1)
        self.assertIn("may still be in it", self.app.notice)

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

    def test_a_running_row_with_no_pane_of_its_own_gets_nothing_typed(self):
        self.app.cursor = ("session", "c")
        self.listings = [[session("c", "/p/gamma")]]
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual((self.app.tmux.typed, self.app.tmux.gates), ([], []))
        self.assertEqual(self.app.notice, "emx-test-c's session has closed, so emx didn't rename it.")

    def test_a_name_the_listing_never_shows_is_reported(self):
        stopped_turn = session("named", "/p/alpha", state="done")
        self.after = stopped_turn
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(len(self.app.tmux.typed), 1)
        self.assertIn("doesn't show a new name yet", self.app.notice)

    def test_no_new_name_and_text_left_in_the_prompt_is_said_plainly(self):
        self.after = self.named
        self.app.tmux.prompt_after = "prompt-multiline"
        self.rename_to("fresh name")
        self.settle()
        self.assertEqual(len(self.app.tmux.typed), 1)
        self.assertIn("prompt isn't empty", self.app.notice)
        self.assertTrue(self.app.notice_error)

    def test_a_tmux_error_rechecking_the_prompt_still_counts_the_rename_as_typed(self):
        self.after = self.named
        self.app.tmux.prompt_after = "gone"
        self.rename_to("fresh name")
        self.settle()
        self.assertIn("doesn't show a new name yet", self.app.notice)

    def test_the_help_offers_rename(self):
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

        def fetch(engine):
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

    def titled(self, sid, uuid, *titles):
        lines = [json.dumps({"type": kind, key: value, "sessionId": uuid}) for kind, key, value in titles]
        self.write("-tmp-a", uuid, *lines, transcript_line("assistant", [{"type": "text", "text": "Hi."}]))

    def test_a_rename_in_the_transcript_is_recorded_once_per_name(self):
        recorder = mock.Mock()
        recorder.set_name.return_value = (True, None)
        self.reader.engine = recorder
        self.titled("s1", U1, ("custom-title", "customTitle", "emx-test-renamed"))
        for state in ("done", "working"):
            with self.subTest(state=state):
                self.reader.want([core.Session("s1", "emx-test-old", "/tmp/a", state, "", "", None, True, U1)])
                self.reader.read_all()
                self.reader.read_all()
        recorder.set_name.assert_called_once_with("s1", "emx-test-renamed")
        recorder.set_title.assert_not_called()

    def test_a_name_the_row_already_shows_is_not_recorded_again(self):
        recorder = mock.Mock()
        self.reader.engine = recorder
        self.titled("s1", U1, ("custom-title", "customTitle", "emx-test-same"))
        self.reader.want([core.Session("s1", "emx-test-same", "/tmp/a", "done", "", "", None, True, U1)])
        self.reader.read_all()
        recorder.set_name.assert_not_called()

    def test_claudes_title_is_recorded_for_an_unnamed_session_only(self):
        recorder = mock.Mock()
        recorder.set_title.return_value = (True, None)
        self.reader.engine = recorder
        self.titled("s1", U1, ("ai-title", "aiTitle", "Fixing the login bug"))
        self.titled("s2", U2, ("ai-title", "aiTitle", "Another title"))
        self.reader.want([
            core.Session("s1", U1[:8], "/tmp/a", "done", "", "", None, True, U1, named=False),
            core.Session("s2", "emx-test-named", "/tmp/a", "done", "", "", None, True, U2),
        ])
        self.reader.read_all()
        self.reader.read_all()
        recorder.set_title.assert_called_once_with("s1", "Fixing the login bug")
        recorder.set_name.assert_not_called()

    def test_reads_the_transcript_where_claude_code_keeps_it_for_the_folder(self):
        self.write("-tmp-a-proj", U1, transcript_line("assistant", [{"type": "text", "text": "Hi."}]))
        self.reader.want([self.row("s1", "/tmp/a.proj", U1), self.row("s2", "/tmp/b", "")])
        self.reader.read_all()
        self.assertEqual({sid: t.reply for sid, t in self.reader.take().items()}, {"s1": "Hi."})
        self.assertIsNone(self.reader.take())

    def test_the_path_the_hooks_reported_is_read_without_a_search(self):
        path = self.write("-somewhere-else", U1, transcript_line("assistant", [{"type": "text", "text": "From the hook."}]))
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("searched")):
            self.assertEqual(self.reader.locate("/tmp/a", U1, path), os.path.realpath(path))
        self.reader.want([replace(self.row("s1", "/tmp/a", U1), transcript=path)])
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("searched")):
            self.reader.read_all()
        self.assertEqual(self.reader.take()["s1"].reply, "From the hook.")

    def test_a_hook_path_outside_the_projects_folder_or_for_another_session_is_ignored(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        stray = os.path.join(outside, f"{U1}.jsonl")
        with open(stray, "w") as fh:
            fh.write(transcript_line("assistant", [{"type": "text", "text": "Not Claude's."}]) + "\n")
        other = self.write("-tmp-a", U2, transcript_line("assistant", [{"type": "text", "text": "Another session."}]))
        sneaky = os.path.join(self.root, "projects", "..", os.path.relpath(stray, self.root))
        own = self.write("-tmp-a", U1, transcript_line("assistant", [{"type": "text", "text": "Its own."}]))
        for hinted in (stray, other, sneaky):
            with self.subTest(hinted=hinted):
                self.assertEqual(self.reader.locate("/tmp/a", U1, hinted), own)

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
        poller = sidebar.Poller(FakeEngine())
        rows = [self.row("s1", "/tmp/a", U1)]
        with mock.patch.object(sidebar, "fetch_sessions", return_value=(rows, None)):
            poller.poll()
        self.assertEqual(poller.transcripts.wanted, {"s1": ("/tmp/a", U1, "")})
        hooked = replace(rows[0], transcript="/c/projects/-tmp-a/" + U1 + ".jsonl")
        with mock.patch.object(sidebar, "fetch_sessions", return_value=([hooked], None)):
            poller.poll()
        self.assertEqual(poller.transcripts.wanted, {"s1": ("/tmp/a", U1, hooked.transcript)})
        self.assertEqual(set(poller.summaries.wanted), {"s1"})

    def test_an_empty_listing_keeps_what_the_workers_want(self):
        poller = sidebar.Poller(FakeEngine())
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

    @staticmethod
    def uid(n):
        return f"{n:08d}-0000-4000-8000-000000000000"

    def stub(self, uuid, folder="-tmp-a"):
        return self.write(
            folder, uuid, json.dumps({"type": "ai-title", "aiTitle": "A title"}),
            json.dumps({"type": "agent-name", "agentName": "a-name"}),
        )

    def earlier(self, uuid, into, reply="Went there.", folder="-tmp-a"):
        """A made-up transcript with one turn, which the session `into` continued."""
        return self.write(
            folder, uuid, transcript_line("user", "go"), transcript_line("assistant", [{"type": "text", "text": reply}]),
            json.dumps({"type": "continued-in", "sessionId": uuid, "continuedInSessionId": into}),
            json.dumps({"type": "last-prompt", "lastPrompt": "go"}), json.dumps({"type": "cost-state"}),
        )

    def stub_and_earlier(self):
        self.stub(U2)
        self.write("-tmp-a", f"{U2}.orphaned-1-ab", json.dumps({"type": "agent-name"}))
        return self.earlier(U1, U2)

    def test_a_stub_session_reads_the_transcript_that_continued_in_it(self):
        self.stub_and_earlier()
        self.reader.want([self.row("s2", "/tmp/a", U2)])
        self.reader.read_all()
        self.assertEqual(self.reader.take()["s2"].reply, "Went there.")
        self.assertTrue(self.reader.path("s2").endswith(f"{U1}.jsonl"))

    def test_a_session_with_turns_of_its_own_never_looks_for_a_predecessor(self):
        own = self.write("-tmp-a", U2, transcript_line("user", "mine"))
        self.earlier(U1, U2)
        self.reader.want([self.row("s2", "/tmp/a", U2)])
        with mock.patch.object(sidebar.os, "scandir", side_effect=AssertionError("looked")):
            self.reader.read_all()
        self.assertEqual(self.reader.path("s2"), own)
        self.assertEqual(self.reader.take()["s2"].prompt, "mine")

    def link(self, me, into, *lines):
        """A made-up transcript `me` that continued into `into`, holding `lines` before the link."""
        entry = {"type": "continued-in", "sessionId": me, "continuedInSessionId": into}
        return self.write("-tmp-a", me, *lines, json.dumps(entry))

    def test_the_chain_is_followed_across_hops(self):
        a, b, c = self.uid(1), self.uid(2), self.uid(3)
        self.link(a, b, transcript_line("user", "go"), transcript_line("assistant", [{"type": "text", "text": "From a."}]))
        self.link(b, c, json.dumps({"type": "agent-name"}))
        self.stub(c)
        self.reader.want([self.row("s", "/tmp/a", c)])
        self.reader.read_all()
        self.assertTrue(self.reader.path("s").endswith(f"{a}.jsonl"))
        self.assertEqual(self.reader.take()["s"].reply, "From a.")

    def test_a_cycle_ends_at_the_own_file(self):
        b, c = self.uid(2), self.uid(3)
        self.link(b, c)
        self.link(c, b)
        self.reader.want([self.row("s", "/tmp/a", b)])
        self.reader.read_all()
        self.assertTrue(self.reader.path("s").endswith(f"{b}.jsonl"))
        self.assertIsNone(self.reader.take())

    def test_a_chain_longer_than_the_cap_stops_there(self):
        length = core.MAX_CONTINUATION_HOPS + 2
        self.stub(self.uid(1))
        for n in range(2, length):
            self.link(self.uid(n), self.uid(n - 1))
        self.link(self.uid(length), self.uid(length - 1), transcript_line("user", "far away"))
        self.reader.want([self.row("s", "/tmp/a", self.uid(1))])
        with mock.patch.object(self.reader, "predecessor", wraps=self.reader.predecessor) as lookups:
            self.reader.read_all()
        self.assertLessEqual(lookups.call_count, core.MAX_CONTINUATION_HOPS)
        self.assertTrue(self.reader.path("s").endswith(f"{self.uid(1)}.jsonl"))
        self.assertIsNone(self.reader.take())

    def test_two_files_continuing_into_the_same_session_resolve_to_the_first_by_name_every_pass(self):
        first, second = self.uid(5), self.uid(6)
        self.stub(U2)
        self.earlier(second, U2, reply="Second.")
        self.earlier(first, U2, reply="First.")
        self.reader.want([self.row("s", "/tmp/a", U2)])
        for _ in range(2):
            self.reader.read_all()
            self.assertTrue(self.reader.path("s").endswith(f"{first}.jsonl"))
        self.assertEqual(self.reader.take()["s"].reply, "First.")

    def test_a_stub_whose_own_file_gains_a_turn_switches_back_to_it(self):
        self.stub_and_earlier()
        self.reader.want([self.row("s2", "/tmp/a", U2)])
        self.reader.read_all()
        self.assertTrue(self.reader.path("s2").endswith(f"{U1}.jsonl"))
        self.write("-tmp-a", U2, transcript_line("user", "again"), transcript_line("assistant", [{"type": "text", "text": "Own."}]))
        self.reader.read_all()
        self.assertTrue(self.reader.path("s2").endswith(f"{U2}.jsonl"))
        self.assertEqual(self.reader.take()["s2"].reply, "Own.")

    def test_a_found_link_is_not_searched_again_and_a_miss_backs_off(self):
        self.stub_and_earlier()
        self.stub(self.uid(7))
        self.reader.want([self.row("s2", "/tmp/a", U2), self.row("s7", "/tmp/a", self.uid(7))])
        self.reader.read_all()
        self.assertTrue(self.reader.path("s2").endswith(f"{U1}.jsonl"))
        self.assertTrue(self.reader.path("s7").endswith(f"{self.uid(7)}.jsonl"))
        with mock.patch.object(sidebar.os, "scandir") as scandir:
            self.reader.read_all()
        self.assertFalse(scandir.called)
        self.assertTrue(self.reader.path("s2").endswith(f"{U1}.jsonl"))
        with mock.patch.object(sidebar.time, "monotonic", return_value=time.monotonic() + 1e6), \
                mock.patch.object(sidebar, "read_continuation", wraps=sidebar.read_continuation) as tails:
            self.reader.read_all()
        self.assertEqual(tails.call_count, 0)
        self.assertNotIn(self.uid(7), self.reader.previous)

    def test_only_the_sessions_own_folder_is_scanned(self):
        self.stub(U2)
        self.earlier(U1, U2, folder="-tmp-elsewhere")
        self.reader.want([self.row("s2", "/tmp/a", U2)])
        self.reader.read_all()
        self.assertTrue(self.reader.path("s2").endswith(f"-tmp-a/{U2}.jsonl"))

    def test_unchanged_files_of_a_stub_chain_are_not_read_again(self):
        self.stub_and_earlier()
        self.reader.want([self.row("s2", "/tmp/a", U2)])
        self.reader.read_all()
        with mock.patch.object(sidebar, "read_transcript", wraps=sidebar.read_transcript) as reads, \
                mock.patch.object(sidebar, "read_continuation", wraps=sidebar.read_continuation) as tails:
            self.reader.read_all()
            self.reader.read_all()
        self.assertEqual((reads.call_count, tails.call_count), (0, 0))
        self.assertEqual(self.reader.take()["s2"].reply, "Went there.")


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


class ReadLatestRequest(unittest.TestCase):
    SID = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.home = os.path.join(self.dir, "claude-home")
        self.project = os.path.join(self.home, "projects", "-tmp-work")
        os.makedirs(self.project)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.home})
        env.start()
        self.addCleanup(env.stop)

    def write(self, lines, folder=None, name=None):
        path = os.path.join(folder or self.project, name or f"{self.SID}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
        return path

    def padding(self, size):
        result = transcript_line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "o" * 1000}])
        return [result] * (size // len(result) + 1)

    def test_read_latest_request_finds_a_request_beyond_the_first_tail(self):
        lines = [transcript_line("user", "older request"), transcript_line("user", "Fix the pager")]
        lines += self.padding(core.TRANSCRIPT_FIRST_BYTES * 3)
        path = self.write(lines)
        self.assertGreater(os.stat(path).st_size, core.TRANSCRIPT_FIRST_BYTES * 3)
        self.assertEqual(sidebar.read_latest_request(path), "Fix the pager")

    def test_read_latest_request_gives_up_at_the_cap(self):
        lines = [transcript_line("user", "Fix the pager")] + self.padding(8192)
        path = self.write(lines)
        with mock.patch.object(core, "TRANSCRIPT_FIRST_BYTES", 1024), mock.patch.object(core, "ASK_REQUEST_MAX_BYTES", 4096):
            self.assertEqual(sidebar.read_latest_request(path), "")
            self.assertEqual(sidebar.read_latest_request(self.write([transcript_line("user", "Fix the pager")]
                                                                    + self.padding(1024))), "Fix the pager")

    def test_read_latest_request_refuses_a_path_outside_claude_projects(self):
        lines = [transcript_line("user", "Fix the pager")]
        outside = os.path.join(self.dir, "elsewhere")
        os.makedirs(outside)
        paths = [self.write(lines, folder=outside), self.write(lines, name="not-a-session.jsonl"), "",
                 os.path.join(self.project, "33333333-3333-4333-8333-333333333333.jsonl")]
        with mock.patch.object(sidebar, "open", wraps=open, create=True) as opened:
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(sidebar.read_latest_request(path), "")
        self.assertEqual([c.args[0] for c in opened.call_args_list], [paths[3]])

    def test_read_latest_request_reads_read_only_and_never_writes(self):
        path = self.write([transcript_line("user", "Fix the pager")])
        before = os.stat(path)
        with mock.patch.object(sidebar, "open", wraps=open, create=True) as opened:
            self.assertEqual(sidebar.read_latest_request(path), "Fix the pager")
        self.assertEqual([c.args[1] for c in opened.call_args_list], ["rb"])
        self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)

    def test_read_latest_request_keeps_a_line_with_u2028_whole(self):
        text = "Fix the pager\u2028then the footer"
        path = self.write([json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)])
        with open(path, encoding="utf-8") as fh:
            self.assertIn("\u2028", fh.read())
        self.assertEqual(sidebar.read_latest_request(path), text)

    def test_read_latest_request_never_raises(self):
        os.mkdir(os.path.join(self.project, "44444444-4444-4444-8444-444444444444.jsonl"))
        garbage = os.path.join(self.project, f"{self.SID}.jsonl")
        with open(garbage, "wb") as fh:
            fh.write(b"\xff\xfe not json\n{\"type\": \n\x80\x81\n")
        for path in (None, os.path.join(self.project, "44444444-4444-4444-8444-444444444444.jsonl"), garbage):
            with self.subTest(path=path):
                self.assertEqual(sidebar.read_latest_request(path), "")


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

    def test_tab_on_a_stub_session_reads_the_continued_transcripts_requests(self):
        folder = Path(self.dir, "projects", "-tmp-a")
        folder.mkdir(parents=True)
        requests_file(folder, "write a poem", name=f"{U1}.jsonl")
        with open(folder / f"{U1}.jsonl", "a") as f:
            f.write(json.dumps({"type": "continued-in", "sessionId": U1, "continuedInSessionId": U2}) + "\n")
        (folder / f"{U2}.jsonl").write_text(json.dumps({"type": "agent-name", "agentName": "x"}) + "\n")
        reader = sidebar.Transcripts(self.dir)
        reader.want([core.Session("s2", "s2", "/tmp/a", "done", "", "", None, True, U2)])
        reader.read_all()
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, NAME_JSON)) as run:
            name, error, _ = self.run_namer(reader.path("s2"))
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


def haiku_json(result):
    return json.dumps({"result": result, "is_error": False, "duration_ms": 600, "total_cost_usd": 0.0001,
                       "usage": {"input_tokens": 2, "output_tokens": 1}, "modelUsage": {"claude-haiku-5-5": {}}})


class Classify(unittest.TestCase):
    def test_an_exact_word_is_the_verdict_and_the_reply_goes_on_stdin(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, haiku_json("NEEDS"))) as run:
            self.assertEqual(sidebar.classify("claude", "Which one?", "/tmp/scratch"), ("needs", None))
        self.assertEqual(run.call_args.kwargs["input"], "Which one?")
        self.assertEqual(run.call_args.args[0], core.ask_args("claude"))
        self.assertEqual(run.call_args.kwargs["timeout"], core.ASK_TIMEOUT)
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, haiku_json("DONE\n"))):
            self.assertEqual(sidebar.classify("claude", "x", "/tmp"), ("done", None))

    def test_any_other_answer_or_a_failure_is_an_error_line(self):
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, haiku_json("maybe"))):
            verdict, error = sidebar.classify("claude", "x", "/tmp")
        self.assertEqual((verdict, error), (None, sidebar.ASK_UNREADABLE))
        for exc in (FileNotFoundError(), subprocess.TimeoutExpired("claude", 15), OSError("boom")):
            with self.subTest(exc=exc), mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                verdict, error = sidebar.classify("claude", "x", "/tmp")
                self.assertIsNone(verdict)
                self.assertIsInstance(error, str)


class VerdictEngine:
    claude = "claude"

    def __init__(self):
        self.verdicts = []

    def set_verdict(self, key, stop_key, verdict):
        self.verdicts.append((key, stop_key, verdict))
        return True, None


class AsksWorker(unittest.TestCase):
    def setUp(self):
        self.engine = VerdictEngine()
        self.wake = threading.Event()
        self.worker = sidebar.Asks(self.engine, self.wake)
        self.calls = []
        self.answer = ("needs", None)

        def fake(claude, excerpt, cwd):
            self.calls.append(excerpt)
            return self.answer

        self.request = ""
        self.read = []

        def read_request(path):
            self.read.append(path)
            return self.request

        for patch in (mock.patch.object(sidebar, "classify", fake),
                      mock.patch.object(sidebar, "read_latest_request", read_request),
                      mock.patch.object(sidebar, "summary_scratch", return_value="/tmp/emx-test-scratch")):
            patch.start()
            self.addCleanup(patch.stop)
        self.clock = [1000.0]
        clock = mock.patch.object(sidebar.time, "monotonic", lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)

    def stopped(self, sid="a", asking=True):
        return replace(session(sid, "/p/alpha", "done"), reply="Which one?", stop_key="k1", asking=asking)

    def test_a_stop_is_asked_about_once_and_its_verdict_recorded(self):
        self.worker.want([self.stopped()])
        self.worker.step()
        self.worker.step()
        self.assertEqual(self.calls, [f"{core.ASK_REPLY_LABEL} Which one?"])
        self.assertEqual(self.engine.verdicts, [("a", "k1", "needs")])
        self.assertTrue(self.wake.is_set())

    def test_the_call_gets_the_request_and_the_reply(self):
        self.request = "Fix the pager"
        stop = replace(self.stopped(), transcript="/claude/projects/x/11111111-1111-4111-8111-111111111111.jsonl")
        self.worker.want([stop])
        self.worker.step()
        self.assertEqual(self.read, [stop.transcript])
        self.assertEqual(self.calls, [core.ask_excerpt(stop, "Fix the pager")])
        self.assertEqual(self.calls[0].splitlines()[0], f"{core.ASK_REQUEST_LABEL} Fix the pager")

    def test_no_request_found_still_asks_on_the_reply(self):
        self.worker.want([self.stopped()])
        self.worker.step()
        self.assertEqual(self.calls, ["Final message: Which one?"])
        self.assertEqual(self.engine.verdicts, [("a", "k1", "needs")])
        self.assertEqual(self.worker.failures, {})

    def test_a_session_not_asking_is_never_asked_about(self):
        self.worker.want([self.stopped(asking=False)])
        self.worker.step()
        self.assertEqual((self.calls, self.engine.verdicts), ([], []))

    def test_a_failure_waits_out_its_backoff(self):
        self.answer = (None, "boom")
        self.worker.want([self.stopped()])
        self.worker.step()
        self.clock[0] += core.retry_after(1) - 0.1
        self.worker.step()
        self.assertEqual(len(self.calls), 1)
        self.clock[0] += 0.2
        self.worker.step()
        self.assertEqual(len(self.calls), 2)

    def test_after_the_last_try_fails_the_stop_reads_unknown_and_is_not_asked_again(self):
        self.answer = (None, "boom")
        self.worker.want([self.stopped()])
        for _ in range(core.ASK_MAX_TRIES + 2):
            self.worker.step()
            self.clock[0] += core.RETRY_MAX_SECONDS
        self.assertEqual(len(self.calls), core.ASK_MAX_TRIES)
        self.assertEqual(self.engine.verdicts, [("a", "k1", "unknown")])

    def test_a_refused_scratch_folder_counts_as_a_failure(self):
        self.worker.want([self.stopped()])
        with mock.patch.object(sidebar, "summary_scratch", side_effect=OSError("refused")):
            self.worker.step()
        self.assertEqual((self.calls, self.engine.verdicts, self.worker.failures[("a", "k1")][0]), ([], [], 1))


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
        self.assertIn("^x again to close", self.screen())
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
        at = next(y for y, line in enumerate(self.screen()) if "emx-test-a" in line)
        self.assertIn("Needs input · approve", self.screen()[at + 1])

    def test_space_on_a_session_opens_the_peek_and_it_is_drawn_boxed_and_read_only(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)
        self.app.draw()
        text = "\n".join(self.screen())
        for want in ("╭─ emx-test-a", "Needs input · for", "Run the tests", "$ npm test", "permission prompt", "╰"):
            self.assertIn(want, text)
        self.assertTrue(any(line.startswith(" ✻ emx-test-a") for line in self.screen()))

    def test_the_peek_edge_carries_its_keys(self):
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.app.draw()
        edge = next(line for line in self.screen() if line.startswith("╰"))
        self.assertEqual(core.display_width(edge), 39)
        self.assertTrue(edge.startswith("╰─ → open · ␣ close ─"), edge)
        self.assertTrue(edge.endswith("─╯"))
        self.assertEqual(edge.strip("╰╯").strip("─ ").replace("→ open · ␣ close", "").strip("─ "), "")
        self.scr.size = (20, 21)
        self.app.draw()
        edge = next(line for line in self.screen() if line.startswith("╰"))
        self.assertEqual(edge, "╰" + "─" * 18 + "╯")

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
        self.scr.size = (5, 40)
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
        self.scr.size = (5, 40)
        self.app.handle_key(curses.KEY_RESIZE)
        self.assertFalse(self.app.peeking)
        self.assertEqual(self.app.notice, sidebar.PEEK_NO_ROOM)
        self.app.draw()
        self.assertFalse(any(line.startswith("╭") for line in self.screen()))

    def box_rows(self):
        return sum(1 for line in self.screen() if line[:1] in ("╭", "│", "╰"))

    def test_an_error_line_draws_the_peek_shorter_and_leaves_it_open(self):
        self.app.cursor = ("session", "a")
        self.scr.size = (8, 40)
        self.app.handle_key(" ")
        self.app.draw()
        before = self.box_rows()
        self.app.list_error = "listing broken"
        self.app.refresh()
        self.assertTrue(self.app.peeking)
        self.assertTrue(0 < self.box_rows() < before, (before, self.screen()))

    def test_an_error_squeezing_the_box_to_nothing_closes_it_without_calling_the_pane_short(self):
        self.app.cursor = ("session", "a")
        self.scr.size = (8, 40)
        self.app.handle_key(" ")
        self.app.list_error = "listing broken " * 6
        self.app.refresh()
        self.assertFalse(self.app.peeking)
        self.assertNotEqual(self.app.notice, sidebar.PEEK_NO_ROOM)
        screen = self.screen()
        self.assertFalse(any(line.startswith("╭") for line in screen))
        self.assertNotIn("␣ Esc close", self.bar())

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

    def test_the_help_mentions_it_and_the_bar_offers_its_keys_while_it_is_open(self):
        self.assertIn("space", dict(sidebar.HELP))
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.assertIn("␣ Esc close", self.bar())


LONG_WAIT = "approve Bash: python3 -m unittest discover -s tests -v"


class TwoLineRowsDrawn(AppCase):
    """A live row takes the name line and as many status lines as it needs; clicks, the cursor and scrolling count all of them."""

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

    def test_a_live_row_is_the_name_then_its_whole_status_from_column_3(self):
        ys = self.rows_of("a")
        self.assertEqual(ys, list(range(ys[0], ys[0] + len(ys))))
        self.assertGreater(len(ys), 2)
        first, *rest = (self.screen()[y] for y in ys)
        self.assertTrue(first.startswith(" ✻ emx-test-a"), first)
        self.assertTrue(rest[0].startswith("   Needs input · approve Bash"), rest[0])
        for line in rest:
            self.assertTrue(line.startswith("   ") and line[3] != " ", line)
            self.assertLessEqual(core.display_width(line), 39)
        self.assertEqual(" ".join(line.strip() for line in rest), "Needs input · " + LONG_WAIT)
        self.assertFalse(any("…" in line for line in rest))

    def test_the_main_marker_is_on_the_first_line_only(self):
        self.app.main_pane = Pane("%2", "@1", False, "viewer", "", 100, 141, view="a")
        self.app.draw()
        first, *rest = (self.screen()[y] for y in self.rows_of("a"))
        self.assertTrue(first.startswith("▎"), first)
        self.assertFalse(any(line.startswith("▎") for line in rest), rest)

    def test_an_idle_row_in_the_by_state_view_still_says_idle(self):
        self.poll(idle_row("c", "/p/alpha"), session("b", "/p/alpha"))
        self.app.refresh()
        self.app.toggle_view()
        self.app.draw()
        second = self.screen()[self.rows_of("c")[1]]
        self.assertTrue(second.startswith("   Idle"), second)
        self.assertEqual(len(self.rows_of("b")), 1)

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
            self.assertGreater(len(ys), 2, sid)
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
        self.assertIn("x again", self.screen()[before[0][0]])

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
        self.assertGreater(len(self.rows_of("a")), 1)
        box = next(y for y, line in enumerate(self.screen()) if line.startswith("╭─"))
        self.assertLess(self.rows_of("a")[-1], box)

    def test_a_wide_panel_shows_the_whole_request_on_the_second_line(self):
        self.scr.size = (20, 100)
        self.app.draw()
        self.assertEqual(len(self.rows_of("a")), 2)
        self.assertIn("Needs input · " + LONG_WAIT, self.screen()[self.rows_of("a")[1]])


class WhatsNew(AppCase):
    """The first panel to run a newer version shows its changelog lines once, then records the version."""

    LOG = "# Changelog\n\n## 1.1.0\n\n- Shiny thing.\n\n## 1.0.0\n\n- First.\n"

    def start(self, recorded=None, version="1.1.0", log=LOG, tmux=None):
        if recorded is not None:
            core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "last_version": recorded})
        with mock.patch.object(sidebar, "__version__", version), mock.patch.object(sidebar, "read_changelog", return_value=log):
            app = sidebar.App(FakeScr(), tmux=tmux, claude="claude")
        app.housekeep = lambda fresh_list: None
        return app

    def recorded(self):
        return core.load_prefs(core.prefs_path()).get("last_version")

    def test_a_newer_version_shows_its_lines_once_and_records_itself(self):
        app = self.start("1.0.0")
        self.assertEqual(app.notice, "emx updated to 1.1.0.\nNew in emx 1.1.0:\n· Shiny thing.")
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
        self.assertTrue(app.notice.startswith("emx updated to 1.1.0."))
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
        app = self.start("1.0.0", tmux=ClientTmux((1, "", "", 0.0)))
        app.time_whats_new()
        self.assertAlmostEqual(app.notice_until - time.monotonic(), sidebar.WHATS_NEW_SECONDS, delta=5)
        app.notice_until = time.monotonic() - 1
        app.refresh()
        self.assertEqual(app.notice, "")

    def test_it_shows_under_the_list(self):
        app = self.start("1.0.0")
        self.assertEqual([t for t, _ in app.bottom_lines(38)][:3], ["emx updated to 1.1.0.", "New in emx 1.1.0:", "· Shiny thing."])

    def test_prefs_without_a_record_stay_quiet_and_record(self):
        core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "side": "left"})
        app = self.start()
        self.assertEqual(app.notice, "")
        self.assertEqual(self.recorded(), "1.1.0")

    def test_a_skipped_version_s_lines_show_too(self):
        log = "# Changelog\n\n## 1.2.0\n\n- Newest.\n\n## 1.1.0\n\n- Middle.\n\n## 1.0.0\n\n- First.\n"
        app = self.start("1.0.0", version="1.2.0", log=log)
        self.assertEqual(app.notice, "emx updated to 1.2.0.\nNew in emx 1.2.0:\n· Newest.\nNew in emx 1.1.0:\n· Middle.")

    def test_the_shipped_1_0_0_note_is_whole_or_says_what_is_cut(self):
        log = cli.read_changelog()
        app = self.start("0.9.0", version="1.0.0", log=log)
        note = [t for t, _ in app.bottom_lines(39, 40)]
        self.assertLessEqual(len(note), max(6, 40 // 2))
        joined = " ".join(note)
        whole = all(line in joined for line in core.changelog_entry(log, "1.0.0"))
        self.assertTrue(whole or re.fullmatch(r"… \d+ more in CHANGELOG\.md", note[-1]), note)

    def test_a_cut_note_says_how_much_is_missing(self):
        log = "# Changelog\n\n## 1.1.0\n\n" + "".join(f"- Line number {n}.\n" for n in range(30))
        app = self.start("1.0.0", log=log)
        lines = [t for t, _ in app.bottom_lines(39, 20)]
        self.assertEqual(lines[:3], ["emx updated to 1.1.0.", "New in emx 1.1.0:", "· Line number 0."])
        self.assertEqual(lines[9], "… 23 more in CHANGELOG.md")

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


class UpdateTmux(ClientTmux):
    socket = "emx-test-upd"


class StubUpdater:
    def __init__(self, *results):
        self.results = list(results)

    def take(self):
        return self.results.pop(0) if self.results else None


class PanelUpdates(AppCase):
    """An installed panel moves onto a newer `current` at a quiet moment, through the new code's `relaunch`; a major is only announced."""

    def setUp(self):
        super().setUp()
        for p in (
            mock.patch.object(emx, "ROOT", "/tmp/emx-test-root"),
            mock.patch.object(emx, "RUNNING", "9.0.0"),
            mock.patch.object(emx, "BIN", "/tmp/emx-test-root/current/bin/emx"),
        ):
            p.start()
            self.addCleanup(p.stop)
        popen = mock.patch.object(sidebar.subprocess, "Popen")
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        self.tmux = UpdateTmux((0, "", "", None))
        self.app = sidebar.App(self.scr, tmux=self.tmux, claude="claude")
        self.app.housekeep = lambda fresh_list: None

    def moved(self, *results):
        self.app.updater = StubUpdater(*(results or [("9.0.1", None, None)]))

    def test_a_checkout_panel_has_no_updater(self):
        self.assertIsInstance(self.app.updater, sidebar.Updater)
        self.assertFalse(self.app.updater.is_alive())
        with mock.patch.object(emx, "ROOT", None):
            app = sidebar.App(self.scr, tmux=self.tmux, claude="claude")
        self.assertIsNone(app.updater)
        app.check_update()
        self.popen.assert_not_called()

    def test_relaunch_fires_once_when_current_moved_and_quiet(self):
        self.moved()
        self.app.check_update()
        self.app.check_update()
        self.assertEqual(self.popen.call_count, 1)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [sidebar.sys.executable, "/tmp/emx-test-root/current/bin/emx", "relaunch", "emx-test-upd"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_no_relaunch_while_attached_and_active(self):
        self.tmux.state = (1, "1", "%1", 30.0)
        self.moved()
        self.app.check_update()
        self.popen.assert_not_called()

    def test_relaunch_after_idle(self):
        self.tmux.state = (1, "1", "%1", 200.0)
        self.moved()
        self.app.check_update()
        self.assertEqual(self.popen.call_count, 1)

    def test_no_relaunch_when_current_is_this_panel(self):
        self.moved(("9.0.0", None, None))
        self.app.check_update()
        self.popen.assert_not_called()

    def test_a_tmux_error_skips_the_tick(self):
        self.tmux.state = TmuxError("no server")
        self.moved()
        self.app.check_update()
        self.popen.assert_not_called()
        self.assertEqual(self.app.notice, "")
        self.tmux.state = (0, "", "", None)
        self.app.check_update()
        self.assertEqual(self.popen.call_count, 1)

    def test_no_relaunch_while_any_edit_or_worker_is_open(self):
        opened = {
            "prompt": {"folder": "/p"}, "find": "", "rename_edit": {"hint": ""}, "confirm": ("close", "a"),
            "history_view": 0, "starter": object(), "closer": object(), "renamer": object(), "namer": object(),
            "help": True, "peeking": True, "interrupters": {"a": object()},
        }
        for flag, value in opened.items():
            with self.subTest(flag=flag):
                was = getattr(self.app, flag)
                setattr(self.app, flag, value)
                self.moved()
                self.app.check_update()
                setattr(self.app, flag, was)
                self.popen.assert_not_called()
        with self.subTest(flag="awaiting_started_row"):
            self.app.started = ("a", time.monotonic())
            self.app.wanted_cursor = ("session", "a")
            self.moved()
            self.app.check_update()
            self.popen.assert_not_called()

    def test_a_major_is_announced_once_and_nothing_is_relaunched(self):
        self.moved(("9.0.0", "10.0.0", None), ("9.0.0", "10.0.0", "couldn't fetch updates: x"))
        self.app.check_update()
        self.assertEqual(self.app.notice, "emx 10.0.0 available: run emx update")
        self.assertIsNone(self.app.notice_until)
        self.app.say("")
        self.app.check_update()
        self.assertEqual(self.app.notice, "")
        self.popen.assert_not_called()

    def start_on(self, version, recorded):
        core.save_prefs(core.prefs_path(), {**core.DEFAULT_PREFS, "last_version": recorded})
        log = f"# Changelog\n\n## {version}\n\n- Shiny thing.\n"
        with mock.patch.object(sidebar, "__version__", version), mock.patch.object(sidebar, "read_changelog", return_value=log):
            return sidebar.App(FakeScr(), tmux=self.tmux, claude="claude")

    def test_whats_new_after_an_update_starts_with_updated_to(self):
        app = self.start_on("9.0.1", "9.0.0")
        self.assertEqual(app.notice.splitlines(), ["emx updated to 9.0.1.", "New in emx 9.0.1:", "· Shiny thing."])

    def test_whats_new_timer_waits_for_a_client(self):
        app = self.start_on("9.0.1", "9.0.0")
        app.time_whats_new()
        self.assertIsNone(app.notice_until)
        self.tmux.state = TmuxError("no server")
        app.time_whats_new()
        self.assertIsNone(app.notice_until)
        self.tmux.state = (1, "", "%1", 0.0)
        app.time_whats_new()
        self.assertAlmostEqual(app.notice_until - time.monotonic(), sidebar.WHATS_NEW_SECONDS, delta=5)
        until = app.notice_until
        app.time_whats_new()
        self.assertEqual(app.notice_until, until)


class UpdaterWorker(unittest.TestCase):
    """The worker that fetches and unpacks updates for an installed panel."""

    def setUp(self):
        for p in (
            mock.patch.object(emx, "ROOT", "/tmp/emx-test-root"),
            mock.patch.object(emx, "RUNNING", "9.0.0"),
            mock.patch.object(sidebar.install, "current", return_value="9.0.0"),
            mock.patch.object(sidebar.install, "updates_on", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)
        prune = mock.patch.object(sidebar.install, "prune", return_value=([], None))
        self.prune = prune.start()
        self.addCleanup(prune.stop)

    def test_the_interval_is_an_int_of_at_least_five_seconds(self):
        for value, seconds in (("", 600), ("60", 60), ("5", 5), ("4", 600), ("x", 600), ("1.5", 600)):
            with self.subTest(value=value):
                self.assertEqual(sidebar.update_interval({"EMX_UPDATE_SECONDS": value}), seconds)
        self.assertEqual(sidebar.update_interval({}), core.UPDATE_CHECK_SECONDS)

    def test_the_first_check_waits_then_checks_each_interval_with_the_guard_and_no_wait(self):
        with mock.patch.object(sidebar.install, "update", return_value=(("9.0.0", "9.0.0", None), None)) as update:
            worker = sidebar.Updater(interval=10)
            start = time.monotonic()
            worker.step(start)
            update.assert_not_called()
            worker.step(start + sidebar.UPDATE_FIRST_SECONDS)
            worker.step(start + sidebar.UPDATE_FIRST_SECONDS + 5)
            worker.step(start + sidebar.UPDATE_FIRST_SECONDS + 10)
        self.assertEqual(update.call_count, 2)
        update.assert_called_with("/tmp/emx-test-root", "9.0.0", allow_major=False, apply_only_if_running_current=True, wait=False)

    def test_it_prunes_once_while_on_current(self):
        worker = sidebar.Updater(interval=10)
        worker.step(time.monotonic())
        worker.step(time.monotonic())
        self.prune.assert_called_once_with("/tmp/emx-test-root", "9.0.0")

    def test_updates_off_checks_nothing(self):
        with mock.patch.object(sidebar.install, "updates_on", return_value=False), \
                mock.patch.object(sidebar.install, "update") as update:
            worker = sidebar.Updater(interval=10)
            worker.step(time.monotonic() + 3600)
        update.assert_not_called()

    def test_a_held_lock_skips_the_cycle_and_is_not_an_error(self):
        with mock.patch.object(sidebar.install, "update", return_value=(None, sidebar.install.BUSY)):
            worker = sidebar.Updater(interval=10)
            worker.step(time.monotonic() + 3600)
        self.assertEqual(worker.take(), ("9.0.0", None, None))
        self.assertIsNone(worker.take())

    def test_take_hands_a_change_once(self):
        with mock.patch.object(sidebar.install, "update", return_value=(("9.0.0", "9.0.1", "10.0.0"), None)), \
                mock.patch.object(sidebar.install, "current", side_effect=["9.0.0", "9.0.1"]):
            worker = sidebar.Updater(interval=10)
            worker.step(time.monotonic() + 3600)
        self.assertEqual(worker.take(), ("9.0.1", "10.0.0", None))
        self.assertIsNone(worker.take())

    def test_updater_thread_survives_a_failing_update(self):
        calls = []

        def fail(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("disk on fire")

        with mock.patch.object(sidebar.install, "update", fail), mock.patch.object(sidebar, "UPDATER_TICK_SECONDS", 0.01), \
                mock.patch.object(sidebar, "UPDATE_FIRST_SECONDS", 0):
            worker = sidebar.Updater(interval=0)
            worker.start()
            try:
                wait_until(self, lambda: len(calls) >= 3, "three failed checks")
            finally:
                worker.stopping.set()
                worker.join(5)
        taken = worker.take()
        self.assertEqual(taken[0], "9.0.0")
        self.assertIn("disk on fire", taken[2])
        self.assertFalse(worker.is_alive())


class UpdaterOffTheCursesThread(AppCase):
    def test_the_curses_thread_never_calls_git(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "versions", "9.0.0", "bin"))
        os.makedirs(os.path.join(tmp, "source.git"))
        os.symlink("versions/9.0.0", os.path.join(tmp, "current"))
        threads = []

        def git(*args, **kwargs):
            threads.append(threading.current_thread())
            return None, "no git in tests"

        for p in (
            mock.patch.object(emx, "ROOT", tmp), mock.patch.object(emx, "RUNNING", "9.0.0"),
            mock.patch.object(sidebar.install, "git", git), mock.patch.object(sidebar.subprocess, "Popen"),
            mock.patch.object(sidebar, "UPDATER_TICK_SECONDS", 0.01), mock.patch.object(sidebar, "UPDATE_FIRST_SECONDS", 0),
        ):
            p.start()
            self.addCleanup(p.stop)
        app = sidebar.App(self.scr, tmux=UpdateTmux(), claude="claude")
        app.housekeep = lambda fresh_list: (app.time_whats_new(), app.check_update())
        app.updater.interval = 0
        app.updater.start()
        try:
            for _ in range(20):
                app.last_housekeep = 0.0
                app.refresh()
            wait_until(self, lambda: threads, "the worker's fetch")
        finally:
            app.updater.stopping.set()
            app.updater.join(5)
        self.assertNotIn(threading.main_thread(), threads)
        self.assertFalse(app.notice_error, app.notice)


class AttrScr(RecordingScr):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drawn = []

    def addstr(self, y, x, text, attr=0):
        super().addstr(y, x, text, attr)
        self.drawn.append((y, x, text, attr))


class TonePalette:
    can_shade = False

    def use(self, scheme, shade=False):
        pass

    def attr(self, tone, selected=False):
        return 1000 + sum(map(ord, tone))


def nearest_256(r, g, b):
    levels = (0, 95, 135, 175, 215, 255)
    colours = {16 + 36 * i + 6 * j + k: (levels[i], levels[j], levels[k]) for i in range(6) for j in range(6) for k in range(6)}
    colours |= {232 + n: (8 + 10 * n,) * 3 for n in range(24)}
    return min(colours, key=lambda c: sum((a - b) ** 2 for a, b in zip(colours[c], (r, g, b))))


class Tones(unittest.TestCase):
    def test_the_panel_draws_the_indexes_the_phone_page_turns_into_hex(self):
        self.assertEqual({tone: TONES[0] for tone, TONES in sidebar.TONES.items() if tone != "plain"}, core.TONE_INDEX)
        self.assertEqual(sidebar.TONES["plain"], (-1, -1))

    def test_copper_is_the_nearest_256_colour_to_d08a4a(self):
        self.assertEqual(sidebar.TONES["copper"][0], nearest_256(0xD0, 0x8A, 0x4A))

    def test_remote_is_the_nearest_256_colour_to_af87ff(self):
        self.assertEqual(sidebar.TONES["remote"][0], nearest_256(0xAF, 0x87, 0xFF))

    def test_idle_is_the_nearest_256_colour_to_8787af(self):
        self.assertEqual(sidebar.TONES["idle"][0], nearest_256(0x87, 0x87, 0xAF))
        self.assertEqual(sidebar.TONES["idle"][0], 103)

    def test_border_is_238(self):
        self.assertEqual(sidebar.TONES["border"][0], 238)
        self.assertEqual(sidebar.TONES["border"][0], core.TONE_INDEX["border"])

    def test_the_selection_bar_is_blue_24_focused_236_not(self):
        self.assertEqual(sidebar.BAR_BG, {"focused": 24, "unfocused": 236})


class IdleRows(AppCase):
    def drawn(self, *sessions, width=60):
        scr = AttrScr(20, width)
        self.app.scr = scr
        self.app.palette = TonePalette()
        self.poll(*sessions)
        self.app.refresh()
        return scr.drawn

    def test_an_idle_row_draws_glyph_and_word_in_idle(self):
        drawn = self.drawn(idle_row("a", "/p/alpha"))
        idle = TonePalette().attr("idle")
        glyph = [d for d in drawn if d[1] == 1 and d[2] == "✻"]
        word = [d for d in drawn if d[2] == "Idle"]
        self.assertEqual([d[3] for d in glyph], [idle])
        self.assertEqual([d[3] for d in word], [idle])

    def test_a_working_row_keeps_a_muted_glyph_and_plain_word(self):
        drawn = self.drawn(session("a", "/p/alpha"))
        glyph = next(d for d in drawn if d[1] == 1 and d[2] in core.WORKING_FRAMES)
        word = next(d for d in drawn if d[2] == "Working")
        self.assertEqual(glyph[3], TonePalette().attr("muted"))
        self.assertEqual(word[3], TonePalette().attr("plain"))

    def test_the_peek_title_of_an_idle_session_is_idle(self):
        scr = AttrScr(20, 60)
        self.app.scr = scr
        self.app.palette = TonePalette()
        self.poll(idle_row("a", "/p/alpha"))
        self.app.refresh()
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        scr.drawn.clear()
        self.app.draw()
        title = next(d for d in scr.drawn if d[1] == 2 and d[3] & curses.A_BOLD)
        self.assertEqual(title[3], TonePalette().attr("idle") | curses.A_BOLD)


class PanelGround(AppCase):
    """2.6.0 gave the panel its own background; it is the terminal's again, and a workspace upgrading from 2.6.0 loses the option."""

    def test_housekeep_resets_the_panel_style_to_the_terminals_once(self):
        self.no_scheme()
        calls = []

        class Housekeeping(ClientTmux):
            socket = "emx-test-ground"

            def panes(self):
                return [Pane("%1", "@1", False, "sidebar", "", 40, 141), Pane("%2", "@1", False, "", "", 100, 141)]

            def __getattr__(self, name):
                return lambda *args, **kwargs: calls.append((name, args))

        self.app.tmux = Housekeeping()
        self.app.me = "%1"
        for _ in range(2):
            sidebar.App.housekeep(self.app, False)
        self.assertEqual([c for c in calls if c[0] == "unset_pane_option"], [("unset_pane_option", ("%1", "window-style"))])
        self.assertEqual([c for c in calls if c[0] == "set_pane_option"], [])


class KeyWording(AppCase):
    """Keys are written ^x, Esc and Enter wherever a hint shows them."""

    def hint_texts(self):
        lists = [core.REST_HINTS, core.EMPTY_HINTS, core.PEEK_HINTS, core.CLOSING_HINTS, core.LOADING_HINTS]
        for state, status in (("blocked", ""), ("working", ""), ("working", "idle"), ("done", ""), ("stopped", "")):
            for pinned in (False, True):
                lists.append(core.session_hints(core.Session("i", "n", "", state, status, "", None, True), pinned))
        for view in (core.PROJECT, core.BY_STATE):
            for collapsed in (False, True):
                lists.append(core.header_hints(view, "/p/alpha", collapsed))
        return [core.hint_text(pairs) for pairs in lists] + [core.header_inline(True), "→ answer", "^x interrupt"]

    def test_no_hint_says_ctrl_or_a_return_arrow(self):
        self.poll(session("a", "/p/alpha"))
        self.app.refresh()
        self.app.cursor = ("session", "a")
        texts = [*(f"{key} {what}" for key, what in sidebar.HELP), sidebar.CONFIRM_FOOTER, sidebar.RENAME_FOOTER,
                 sidebar.FIND_HINT, sidebar.HISTORY_HINT, sidebar.ROUTINES_HINT,
                 core.PEEK_EDGE, core.NOTICE_KEY, *self.hint_texts(),
                 core.confirm_label(core.Confirm("a", "n", True, 0.0)), core.group_confirm_label(1), core.group_confirm_label(3),
                 core.GROUP_CLOSE_REFUSAL, core.PINNED_CLOSE_REFUSAL]
        for key in ("n", "\t"):
            self.app.handle_key(key)
            texts += [t for t, _ in self.app.prompt_lines(39)]
        self.assertIsNotNone(self.app.prompt)
        self.assertEqual(self.app.prompt["stage"], "folder")
        for text in texts:
            with self.subTest(text=text):
                self.assertNotRegex(text, r"[Cc]trl\+|↵|\besc\b|\benter\b")

    def test_every_help_key_fits_its_column(self):
        for key, _ in sidebar.HELP:
            self.assertLess(core.display_width(key), sidebar.HELP_KEY_WIDTH, key)

    def test_the_hints_use_the_spelled_keys(self):
        self.assertIn(("→ Enter", "open the session and go to it"), sidebar.HELP)
        self.assertEqual(sidebar.RENAME_FOOTER, "Tab suggest · Enter save · Esc cancel")
        self.assertEqual(sidebar.CONFIRM_FOOTER, "^x to confirm")
        self.assertEqual(core.confirm_label(core.Confirm("a", "n", True, 0.0)), "^x again to close")


class BarFormat(unittest.TestCase):
    def test_hash_and_percent_in_text_are_doubled_so_nothing_runs_or_styles(self):
        for text, want in (
            ("tests #12 failed", "tests ##12 failed"),
            ("#(touch x)", "##(touch x)"),
            ("#[fg=red]", "##[fg=red]"),
            ("50% %H", "50%% %%H"),
        ):
            with self.subTest(text=text):
                self.assertEqual(sidebar.bar_format([(text, "plain", False)]), f"#[fg=default,nobold]{want}#[default]")

    def test_tones_become_their_256_colours_and_bold_is_explicit(self):
        out = sidebar.bar_format([("✻1", "needs", True), ("✻1", "idle", True), ("⇄ host", "remote", True), ("x", "muted", False)], "dark")
        self.assertIn("#[fg=colour221,bold]✻1", out)
        self.assertIn("#[fg=colour103,bold]✻1", out)
        self.assertIn("#[fg=colour141,bold]⇄ host", out)
        self.assertIn("#[fg=colour245,nobold]x", out)
        self.assertTrue(out.endswith("#[default]"))


WIDE_BAR_GOLDEN = Path(__file__).parent / "fixtures" / "bar" / "wide-2.10.0.json"


class WideBarIsUnchanged(unittest.TestCase):
    """The laptop's bar at 200 columns is byte-for-byte what 2.10.0 gave: the expected values were captured from tag v2.10.0's code, not from the code under test."""

    def test_every_captured_case_gives_the_same_parts_and_the_same_tmux_format(self):
        cases = json.loads(WIDE_BAR_GOLDEN.read_text("utf-8"))["cases"]
        self.assertGreater(len(cases), 100)
        for i, case in enumerate(cases):
            a = case["args"]
            latest = core.Entry(**a["latest"]) if a["latest"] else None
            hints = tuple(tuple(tuple(p) for p in h) if isinstance(h, list) else h for h in a["hints"])
            with self.subTest(case=i):
                self.assertEqual(a["width"], 200)
                parts = core.bar_parts(
                    a["version"], [tuple(c) for c in a["counts"]], latest, a["now"], a["tag"], a["remote"], hints,
                    a["width"], a["loaded"], a["notice_key"],
                )
                self.assertEqual([list(p) for p in parts], case["parts"])
                self.assertEqual(sidebar.bar_format(parts, "dark"), case["format"])


ROWS_GOLDEN = Path(__file__).parent / "fixtures" / "rows" / "wide-2.14.0.json"


class RowStub:
    """Enough of the App for draw_session and draw_header, recording every put with the palette's (tone, selection) as one number."""

    class Palette:
        CODES = {"plain": 0, "muted": 1, "needs": 2, "done": 3, "stopped": 4, "accent": 5, "path": 6, "error": 7, "copper": 8, "remote": 9, "idle": 10}

        can_shade = False

        def use(self, scheme, shade=False):
            pass

        def attr(self, tone, selected=None):
            return self.CODES[tone] * 4 + {None: 0, "focused": 1}.get(selected, 2)

    def __init__(self, view="state", collapsed=frozenset(), narrow=False):
        self.palette = self.Palette()
        self.puts = []
        self._view, self._collapsed, self.narrow, self.focused = view, collapsed, narrow, True

    def put(self, y, x, text, attr):
        self.puts.append([y, x, text, attr])

    def view(self):
        return self._view

    def collapsed(self):
        return self._collapsed


class RowsAreUnchanged(unittest.TestCase):
    """The panel's rows and headers draw exactly what 2.14.0 drew: the expected puts were captured from tag v2.14.0's code, not from the code under test."""

    def test_every_captured_case_draws_the_same_puts(self):
        cases = json.loads(ROWS_GOLDEN.read_text("utf-8"))["cases"]
        self.assertGreater(len(cases), 50)
        for i, case in enumerate(cases):
            app = RowStub(*([case["view"], frozenset([case["key"]]) if case["collapsed"] else frozenset(), case["narrow"]] if case["kind"] == "header" else []))
            if case["kind"] == "session":
                s = core.Session(id="id", name="the-name", cwd="/w", state=case["state"], status=case["status"], waiting_for="", started_at=1000.0, running=case["running"])
                rows = core.row_lines(s.name, "", core.state_word(s), "did a thing", "5m", 40, core.category(s) == core.STOPPED)
                sidebar.App.draw_session(app, 3, list(range(3, 3 + len(rows))), rows, s, case["selected"], case["in_main"], case["frame"])
            else:
                group = core.Group(key=case["key"], sessions=(core.Session(id="a", name="n", cwd="/w", state="done", status="", waiting_for=""),))
                sidebar.App.draw_header(app, 2, group, case["selected"], False, 30, case["label"])
            with self.subTest(case=i):
                self.assertEqual(app.puts, case["puts"])


class StatusBarPush(unittest.TestCase):
    def test_the_same_text_twice_is_one_call(self):
        tmux = mock.Mock()
        bar = sidebar.StatusBar(tmux)
        bar.push("a")
        bar.push("a")
        bar.push("b")
        self.assertEqual([c.args for c in tmux.set_bar.call_args_list], [("a", 0), ("b", 0)])

    def test_the_same_text_is_pushed_again_after_the_repush_interval(self):
        tmux = mock.Mock()
        bar = sidebar.StatusBar(tmux)
        with mock.patch.object(sidebar.time, "monotonic", side_effect=[100.0, 100.0 + sidebar.REPUSH_SECONDS + 1]):
            bar.push("a")
            bar.push("a")
        self.assertEqual(tmux.set_bar.call_count, 2)

    def test_a_tmux_error_is_swallowed_and_the_text_is_tried_again(self):
        tmux = mock.Mock()
        tmux.set_bar.side_effect = [sidebar.TmuxError("gone"), None]
        bar = sidebar.StatusBar(tmux)
        bar.push("a")
        bar.push("a")
        self.assertEqual(tmux.set_bar.call_count, 2)

    def test_no_tmux_pushes_nothing(self):
        sidebar.StatusBar(None).push("a")


class AppBar(AppCase):
    def setUp(self):
        super().setUp()
        self.poll(blocked("a", "/p/alpha"), idle_row("b", "/p/alpha"))
        self.app.refresh()

    def test_it_holds_the_mark_name_version_and_a_count_per_state(self):
        text = self.bar()
        self.assertIn("⚒", text)
        self.assertIn(f" emx {sidebar.__version__}", text)
        self.assertEqual(text.count("✻1"), 2)
        self.assertIn("Enter fold", text)
        self.assertEqual(core.display_width(text), 100)

    def test_refresh_pushes_it_once_the_width_is_known(self):
        self.assertEqual(self.app.bar.pushed, [])
        self.app.window_width = 100
        self.app.refresh()
        self.assertEqual(len(self.app.bar.pushed), 1)
        self.assertIn("⚒", plain_bar(self.app.bar.pushed[0]))

    def test_the_idle_count_is_lavender(self):
        self.app.prefs = core.set_scheme(self.app.prefs, "dark")
        self.app.window_width = 100
        self.app.update_bar()
        self.assertIn("#[fg=colour103,bold]✻1", self.app.bar.pushed[-1])

    def test_a_remote_host_shows_after_the_version(self):
        self.app.relay_host = "myserver"
        self.assertIn(f"{sidebar.__version__} ⇄ myserver", self.bar())

    def test_voice_mode_shows_the_word(self):
        self.app.handle_key("m")
        self.assertIn("voice", self.bar())

    def test_the_hints_follow_the_context(self):
        self.app.cursor = ("session", "b")
        self.assertIn(REST_BAR, self.bar(200))
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.assertIn("→ open  ↑↓ next  ␣ Esc close", self.bar())
        self.app.handle_key(" ")
        self.app.confirm = core.Confirm("a", "emx-test-a", True, time.monotonic())
        self.assertIn("^x to confirm", self.bar())
        self.app.confirm = None
        self.app.handle_key("?")
        self.assertIn("? close", self.bar())
        self.app.handle_key("x")
        self.app.handle_key("/")
        self.assertNotIn("? keys", self.bar())

    def test_the_notifications_view_shows_its_hint(self):
        self.app.handle_key("N")
        self.assertIn(sidebar.HISTORY_HINT, self.bar())

    def test_the_newest_notification_shows(self):
        self.app.history = [core.Entry("a", core.NOTICE_DONE, "a · alpha", "Alpha finished the tax fix.", time.monotonic() - 70)]
        self.assertIn("✻ 1m Alpha finished the tax fix.", self.bar())

    def test_the_list_starts_on_the_first_row_with_nothing_above_or_below_it(self):
        scr = RecordingScr(20, 40)
        self.app.scr = scr
        self.app.draw()
        self.assertTrue(scr.cells[0].startswith("▾ /p/alpha"), scr.cells[0])
        everything = "\n".join(scr.cells.values())
        for gone in ("⚒", "─", "Notifications (", "? keys", "emalatxana"):
            self.assertNotIn(gone, everything)
        self.assertEqual(self.app.bottom_lines(39, 20), [])


class HintsInTheBar(AppCase):
    """The bar offers the keys that act on what the cursor is on."""

    def setUp(self):
        super().setUp()
        self.poll(blocked("a", "/p/alpha"), session("w", "/p/alpha"), session("d", "/p/alpha", state="done"),
                  idle_row("b", "/p/alpha"), session("e", "/p/beta"))
        self.app.refresh()

    def test_the_bar_shows_rest_hints_when_the_panel_is_unfocused(self):
        self.app.cursor = ("session", "a")
        self.app.focused = False
        self.assertIn(REST_BAR, self.bar(200))

    def test_the_bar_shows_the_needs_input_rows_actions(self):
        self.app.cursor = ("session", "a")
        self.app.focused = True
        self.assertIn("→ answer  ␣ peek  ^r rename  ^x close  ? keys", self.bar(200))

    def test_the_bar_shows_interrupt_for_a_working_row(self):
        self.app.cursor = ("session", "w")
        self.app.focused = True
        self.assertIn("→ open  ␣ peek  ^x interrupt  ? keys", self.bar(200))

    def test_the_bar_shows_pin_then_unpin_for_a_done_row(self):
        self.app.cursor = ("session", "d")
        self.app.focused = True
        self.assertIn("^t pin  ^x close", self.bar(200))
        self.app.handle_key(sidebar.CTRL_T)
        self.assertIn("^t unpin", self.bar(200))

    def test_an_idle_row_gets_the_rest_hints(self):
        self.app.cursor = ("session", "b")
        self.app.focused = True
        self.assertIn(REST_BAR, self.bar(200))

    def test_a_header_shows_fold_new_here_and_close_group(self):
        self.app.focused = True
        self.app.cursor = ("group", "/p/alpha")
        self.assertIn("Enter fold  n new here  ^x ^x close group  ? keys", self.bar(200))
        self.app.handle_key("\n")
        self.assertIn("Enter unfold", self.bar(200))
        self.app.handle_key("\n")
        self.app.handle_key(sidebar.CTRL_S)
        self.app.cursor = ("group", core.NEEDS_INPUT)
        text = self.bar(200)
        self.assertIn("Enter fold  n new  ? keys", text)
        self.assertNotIn("close group", text)

    def test_n_on_a_header_starts_in_that_folder(self):
        self.app.focused = True
        self.app.cursor = ("group", "/p/alpha")
        self.app.handle_key("n")
        self.assertEqual(self.app.prompt["folder"], "/p/alpha")
        self.assertEqual(self.app.prompt["stage"], "message")

    def test_the_bar_offers_peek_and_keys_for_a_closing_row(self):
        self.app.cursor = ("session", "w")
        self.app.focused = True
        self.app.closing = {"w": object()}
        self.assertIn("␣ peek  ? keys", self.bar(200))
        self.assertNotIn("^x", self.bar(200).split("│")[-1])

    def test_right_arrow_in_the_peek_opens_the_session(self):
        self.app.focused = True
        self.app.cursor = ("session", "a")
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)
        self.app.handle_key(curses.KEY_RIGHT)
        self.assertFalse(self.app.peeking)
        self.assertEqual(self.opened, [("a", core.OPEN)])

    def test_n_all_sits_next_to_the_notification(self):
        self.app.history = [core.Entry("a", core.NOTICE_DONE, "a · alpha", "Alpha finished the tax fix.", time.monotonic() - 70)]
        self.assertIn("Alpha finished the tax fix.  N all", self.bar(200))
        self.app.handle_key("N")
        self.assertNotIn("N all", self.bar(200))
        self.app.handle_key("\x1b")
        self.app.handle_key("?")
        self.assertNotIn("N all", self.bar(200))
        self.app.handle_key("x")
        self.app.history = []
        self.assertNotIn("N all", self.bar(200))

    def test_n_all_is_gone_while_the_routines_view_has_the_keys(self):
        self.app.history = [core.Entry("a", core.NOTICE_DONE, "a · alpha", "Alpha finished the tax fix.", time.monotonic() - 70)]
        self.app.handle_key("R")
        self.assertIsNotNone(self.app.routines_view)
        self.assertNotIn("N all", self.bar(200))
        self.app.handle_key("\x1b")
        self.assertIsNone(self.app.routines_view)
        self.assertIn("N all", self.bar(200))

    def test_the_routines_view_bar_names_only_keys_it_handles(self):
        self.app.handle_key("R")
        self.assertEqual(self.app.bar_hints(), (sidebar.ROUTINES_HINT, "Esc back"))
        self.assertNotRegex(self.bar(200), r"\^x|\^r|\^f|peek|new|close")

    def test_n_all_keeps_two_spaces_from_the_counts_at_every_width(self):
        self.app.history = [core.Entry("a", core.NOTICE_DONE, "a · alpha", "Alpha finished the tax fix.", time.monotonic() - 70)]
        seen = 0
        for width in range(40, 140):
            text = self.bar(width)
            if "N all" in text:
                seen += 1
                self.assertNotRegex(text, r"N all \S", msg=f"width {width}: {text!r}")
        self.assertGreater(seen, 0)

    def test_close_and_rename_hints_go_while_sessions_cannot_be_listed(self):
        self.app.list_error = "listing broken"
        for hints in (
            core.session_hints(core.Session("i", "n", "", "blocked", "", "", None, True), False, False),
            core.session_hints(core.Session("i", "n", "", "done", "", "", None, True), False, False),
            core.session_hints(core.Session("i", "n", "", "working", "", "", None, True), False, False),
            core.header_hints(core.PROJECT, "/p/alpha", False, False),
            core.rest_hints(False),
        ):
            with self.subTest(hints=hints):
                self.assertFalse([k for k, _ in hints if k in ("^x", "^r", "^x ^x")])
        self.assertIsNone(core.session_inline(core.Session("i", "n", "", "working", "", "", None, True), False))
        self.poll(session("a", "/p/alpha"))
        self.app.refresh()
        self.app.list_error = "listing broken"
        for cursor in (("session", "a"), ("group", "/p/alpha")):
            self.app.cursor = cursor
            for variants in (self.app.bar_hints(),):
                for variant in variants:
                    self.assertFalse([k for k, _ in variant if k.startswith("^x") or k == "^r"])
        self.app.list_error = ""
        self.app.cursor = ("session", "a")
        self.assertTrue(any(k == "^x" for variant in self.app.bar_hints() for k, _ in variant))


class HintsOnEmptyPanel(AppCase):
    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(20, 40)
        self.app.scr = self.scr
        self.poll()
        self.app.refresh()

    def test_the_empty_bar_offers_new_session(self):
        self.assertIn("n new session  ? keys", self.bar(200))

    def test_the_empty_panel_says_press_n_in_a_folder(self):
        drawn = []
        put = self.app.put
        self.app.put = lambda y, x, text, attr=0: (drawn.append((text, attr)), put(y, x, text, attr))[1]
        self.app.draw()
        self.assertEqual(self.scr.cells[1].rstrip(), " No sessions yet.")
        self.assertEqual(self.scr.cells[2].rstrip(), " Press n to start one in a folder.")
        self.assertIn(("n", curses.A_BOLD), drawn)


class HintsOnRows(AppCase):
    """The selected row and header repeat their likeliest key at the right end of their last line, without moving anything."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(20, 40)
        self.app.scr = self.scr
        self.poll(blocked("a", "/p/alpha", waiting=LONG_WAIT), session("b", "/p/alpha"), session("d", "/p/beta", state="done"))
        self.app.refresh()
        self.app.focused = True

    def screen(self):
        return [self.scr.cells.get(y, "") for y in range(self.scr.size[0])]

    def rows_of(self, sid):
        return sorted(y for y, item in self.app.row_map.items() if item == ("session", sid))

    def test_the_selected_row_repeats_its_action_on_its_last_line(self):
        self.poll(blocked("a", "/p/alpha", waiting=LONG_WAIT + " and then run it"), session("b", "/p/alpha"))
        self.app.refresh()
        self.app.cursor = ("session", "a")
        self.app.draw()
        ys = self.rows_of("a")
        self.assertGreaterEqual(len(ys), 3)
        last = self.screen()[ys[-1]].rstrip()
        self.assertTrue(last.endswith("→ answer"), last)
        self.assertLessEqual(core.display_width(last), 39)
        for y in ys[:-1]:
            self.assertNotIn("→ answer", self.screen()[y])

    def test_no_inline_hint_when_unfocused_armed_or_closing(self):
        self.app.cursor = ("session", "a")
        self.app.focused = False
        self.app.draw()
        self.assertNotIn("→ answer", "\n".join(self.screen()))
        self.app.focused = True
        self.app.confirm = core.Confirm("a", "emx-test-a", True, time.monotonic())
        self.app.draw()
        self.assertNotIn("→ answer", "\n".join(self.screen()))
        self.app.confirm = None
        self.app.closing = {"a": object()}
        self.app.draw()
        self.assertNotIn("→ answer", "\n".join(self.screen()))

    def test_the_inline_hint_never_changes_row_heights(self):
        self.poll(blocked("a", "/p/alpha", waiting=LONG_WAIT), blocked("n", "/p/alpha", waiting="ok"),
                  session("d", "/p/beta", state="done"), blocked("c", "/p/gamma", waiting=LONG_WAIT * 6))
        self.app.refresh()
        for sid in ("a", "n", "d", "c"):
            with self.subTest(sid=sid):
                self.app.cursor = ("session", sid)
                on = self.app.list_layout(40, 6)[2]
                self.app.cursor = next(line for line in self.app.items() if line[0] == "group")
                off = self.app.list_layout(40, 6)[2]
                self.assertEqual(on, off)

    def test_a_done_row_with_no_detail_carries_it_on_the_state_line(self):
        self.app.cursor = ("session", "d")
        self.app.draw()
        ys = self.rows_of("d")
        self.assertEqual(len(ys), 2)
        self.assertTrue(self.screen()[ys[-1]].rstrip().endswith("→ open"), self.screen()[ys[-1]])

    def test_the_selected_header_shows_enter_fold(self):
        self.app.cursor = ("group", "/p/alpha")
        self.app.draw()
        y = next(y for y, item in self.app.row_map.items() if item == ("group", "/p/alpha"))
        self.assertTrue(self.screen()[y].rstrip().endswith("Enter fold"), self.screen()[y])
        self.app.focused = False
        self.app.draw()
        self.assertNotIn("Enter fold", self.screen()[y])


class InterruptTmux(RenameTmux):
    """The panel (%1) has focus; main (%2) shows `working` mid-turn. Records every call in order, with the screen a capture returned."""

    def __init__(self):
        super().__init__()
        self.list[1] = Pane("%2", "@1", False, "", "working", 100, 141, agent="claude")
        self.show_screen("working-streaming", "prompt-empty")
        self.screen_name = "working-streaming"
        self.screens = None
        self.events = []
        self.keys_sent = []

    def show_screen(self, screen, prompt):
        super().show_screen(screen, prompt)
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

    def new_hidden_window(self, command, cwd=None):
        self.note()
        self.events.append(("new_hidden_window", command))
        self.list.append(Pane("%5", "@3", False, "", "", 100, 100))
        return "%5"

    def set_pane_option(self, pane, name, value):
        self.events.append(("set_pane_option", pane, name, value))

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

        def fetch(engine):
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

    def record_closes(self):
        """The close of each session goes into tmux.events as ("close", id), so it shares one order with the panes' calls; CloseSession tests what a close does."""
        def close_one(closer, confirm):
            self.app.tmux.events.append(("close", confirm.id))
            return True, core.closed_text(confirm)

        p = mock.patch.object(sidebar.Closer, "close_one", close_one)
        p.start()
        self.addCleanup(p.stop)

    def test_one_ctrl_x_on_a_working_row_sends_escape_through_key_if_off_the_curses_thread(self):
        self.press(sidebar.CTRL_X)
        self.assertEqual(self.app.notice, "Interrupting emx-test-working…")
        self.settle()
        self.assertEqual(self.app.tmux.keys_sent, [("%2", "Escape")])
        self.assertEqual(self.app.tmux.gates, [core.type_gate("%2", "%1", "working", 26, sidebar.SESSION_OPT, cursor_x=2)])
        self.assertEqual([e for e in self.app.tmux.events if e[0] == "key_if"], [("key_if", "%2", "Escape")])
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
        self.assertEqual(self.app.bar_hints(), (sidebar.CONFIRM_FOOTER,))
        self.settle()

    def test_a_draft_in_the_prompt_still_interrupts(self):
        self.app.tmux.show_screen("working-streaming", "prompt-text")
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

    def test_a_scrolled_back_pane_or_one_with_its_cursor_hidden_gets_nothing_and_says_why(self):
        cases = {
            "scrolled back": lambda tmux: setattr(tmux, "in_mode", True),
            "isn't showing its prompt": lambda tmux: tmux.show_screen("interactive-named-permission", "interactive-prompt-permission"),
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
        self.record_closes()
        self.press(sidebar.CTRL_X)
        first = self.app.interrupters["working"]
        first.join(5)
        self.press(sidebar.CTRL_X)
        self.assertIs(self.app.interrupters["working"], first)
        self.assertIsNotNone(self.app.closer)
        self.settle()
        self.assertEqual(self.kinds().count("key_if"), 1)
        self.assertEqual([e for e in self.app.tmux.events if e[0] == "close"], [("close", "working")])

    def test_the_interrupted_row_reads_idle_with_a_still_glyph(self):
        scr = RecordingScr(20, 60)
        self.app.scr = scr
        self.poll(idle_row("working", "/p/alpha"), session("b", "/p/beta"))
        self.app.refresh()
        ys = sorted(y for y, line in scr.cells.items() if "emx-test-working" in line)
        row, below = scr.cells[ys[0]], scr.cells[ys[0] + 1]
        self.assertTrue(row.startswith(" ✻ emx-test-working"), row)
        self.assertTrue(below.startswith("   Idle"), below)
        self.assertNotIn("Working", row + below)
        with mock.patch.object(sidebar.time, "monotonic", return_value=12345.6):
            self.app.draw()
        self.assertTrue(next(line for line in scr.cells.values() if "emx-test-working" in line).startswith(" ✻ "))

    def no_viewer(self):
        tmux = self.app.tmux
        tmux.list = [p for p in tmux.list if p.session != "working"]

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

    def test_a_running_session_with_no_pane_of_its_own_gets_no_escape_and_no_new_window(self):
        for arrange in ("no pane", "a key with no agent tag", "a dead pane"):
            with self.subTest(arrange=arrange):
                self.app.tmux = InterruptTmux()
                tmux = self.app.tmux
                if arrange == "no pane":
                    self.no_viewer()
                else:
                    flag = {"agent": ""} if arrange == "a key with no agent tag" else {"dead": True}
                    tmux.list = [replace(p, **flag) if p.session == "working" else p for p in tmux.list]
                self.listings = [[self.working]]
                self.app.confirm = None
                self.press(sidebar.CTRL_X)
                self.settle()
                self.assertNotIn("new_hidden_window", self.kinds())
                self.assertNotIn("key_if", self.kinds())
                self.assertEqual(self.app.notice, "emx-test-working isn't running, so emx didn't interrupt it.")
                self.assertTrue(self.app.notice_error)

    def test_of_two_running_unnamed_sessions_the_escape_goes_to_the_pane_tagged_with_the_pressed_ones_key(self):
        tmux = self.app.tmux
        tmux.list = [
            Pane("%1", "@1", False, "sidebar", "", 40, 141, active=True),
            Pane("%2", "@1", False, "", "other", 100, 141, agent="claude"),
            Pane("%3", "@2", False, "", "working", 100, 100, agent="claude"),
        ]
        tmux.show_screen("interactive-unnamed-empty", "interactive-prompt-empty-unnamed")
        first = core.Session("working", "working", "/p/alpha", "working", "", "", 0.0, True)
        second = core.Session("other", "other", "/p/alpha", "working", "", "", 0.0, True)
        self.listings = [[first, second]]
        self.after = core.Session("working", "working", "/p/alpha", "working", "idle", "", 0.0, True)
        self.poll(first, second)
        self.app.refresh()
        self.app.cursor = ("session", "working")
        self.press(sidebar.CTRL_X)
        self.settle()
        cursor = INTERACTIVE_CURSORS["cursors"]["interactive-prompt-empty-unnamed"]
        self.assertEqual(tmux.keys_sent, [("%3", "Escape")])
        self.assertEqual(tmux.gates, [core.type_gate("%3", "%1", "working", cursor["y"], sidebar.SESSION_OPT, cursor_x=cursor["x"])])
        self.assertEqual(self.app.notice, "Interrupted working.")

    def test_a_close_started_before_the_escape_cancels_it_and_waits_for_it(self):
        self.record_closes()
        held, release = threading.Event(), threading.Event()
        calls = []

        def fetch(engine):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                held.set()
                release.wait(5)
            return [self.working], None

        with mock.patch.object(sidebar, "fetch_sessions", fetch):
            self.press(sidebar.CTRL_X)
            interrupter = self.app.interrupters["working"]
            self.assertTrue(held.wait(5))
            self.press(sidebar.CTRL_X)
            self.assertTrue(interrupter.cancel.is_set())
            self.assertEqual(self.app.closer.wait_for, (interrupter,))
            time.sleep(0.1)
            self.assertNotIn("close", self.kinds())
            release.set()
            self.app.closer.join(5)
            interrupter.join(5)
            self.app.refresh()
        self.assertNotIn("new_hidden_window", self.kinds())
        self.assertNotIn("key_if", self.kinds())
        self.assertEqual([e for e in self.app.tmux.events if e[0] == "close"], [("close", "working")])
        self.assertEqual(self.app.notice, "Closed emx-test-working.")

    def group_close(self):
        return core.GroupConfirm("/p", (core.Confirm("working", "emx-test-working", True, 0.0),
                                        core.Confirm("b", "emx-test-b", True, 0.0)), 0.0)

    def test_a_group_close_held_back_by_a_running_interrupt_says_none_of_the_group_was_closed(self):
        _, release = self.held_interrupts()
        self.record_closes()
        self.press(sidebar.CTRL_X)
        with mock.patch.object(sidebar, "INTERRUPT_JOIN_SECONDS", 0):
            self.app.start_close(self.group_close())
            self.app.closer.join(5)
            self.app.refresh()
        release.set()
        self.assertNotIn("close", self.kinds())
        self.assertEqual(self.app.notice, "emx is still interrupting emx-test-working, so it didn't close any of them.")

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
        self.record_closes()
        self.press(sidebar.CTRL_X)
        self.app.cursor = ("session", "b")
        self.press(sidebar.CTRL_X)
        both = (self.app.interrupters["b"], self.app.interrupters["working"])
        act = core.GroupConfirm("/p", (core.Confirm("working", "emx-test-working", True, 0.0),
                                       core.Confirm("b", "emx-test-b", True, 0.0)), 0.0)
        self.app.start_close(act)
        self.assertEqual(self.app.closer.wait_for, both)
        self.assertTrue(all(i.cancel.is_set() for i in both))
        time.sleep(0.1)
        self.assertNotIn("close", self.kinds())
        release.set()
        self.app.closer.join(5)
        self.assertTrue(all(not i.is_alive() for i in both))
        self.assertEqual([e for e in self.app.tmux.events if e[0] == "close"], [("close", "working"), ("close", "b")])

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
        self.assertEqual(run.call_args.args[0], core.notice_args("claude", core.VOICE_MODE))
        self.assertEqual(run.call_args.kwargs["input"], "--Project: app")
        with mock.patch.object(sidebar.subprocess, "run", return_value=completed(0, LONG_JSON)) as run:
            sidebar.announce("claude", "x", "/tmp/scratch", core.NOTIFICATION_MODE)
        self.assertEqual(run.call_args.args[0], core.notice_args("claude", core.NOTIFICATION_MODE))
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
        self.spoken = []
        self.fallbacks = []

    def speak(self, text, at=None, fallback=None):
        self.spoken.append(text)
        self.fallbacks.append(fallback)


class NoticesWorker(unittest.TestCase):
    def setUp(self):
        self.transcripts = sidebar.Transcripts("/nonexistent")
        self.speaker = StubVoice()
        self.notifier = FakeNotifier()
        self.worker = sidebar.Notices("claude", self.transcripts, sidebar.Resolver("/h"), self.speaker, self.notifier)
        self.worker.mode = core.VOICE_MODE
        self.alpha = session("a", "/p/alpha", "done")
        self.worker.want([self.alpha])
        self.transcripts.found = {"a": core.Transcript(prompt="Fix it", reply="Fixed.", reply_line="Fixed.", answered=True, latest="reply")}
        self.asked = []
        self.answer = (core.SummaryCall("Alpha, sir: all fixed.", "claude-haiku-5-5", 1, 1, 0.0, 1, 1, 0, 0), None)

        def fake(claude, excerpt, cwd, mode=core.VOICE_MODE):
            self.asked.append(excerpt)
            self.modes.append(mode)
            return self.answer

        self.modes = []

        for name, value in (("announce", fake), ("summary_scratch", lambda: "/tmp/emx-test-scratch")):
            patch = mock.patch.object(sidebar, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def notice(self, kind=core.NOTICE_DONE, age=0.0, sid="a", named=True):
        notice = core.Notice(sid, kind, f"emx-test-{sid}", "/p/alpha", time.monotonic() - age, named)
        self.worker.add(notice)
        return notice

    def texts(self):
        return [text for _, _, _, text, _ in self.worker.take()]

    def test_a_good_answer_reaches_the_outbox_the_voice_and_the_calls(self):
        notice = self.notice()
        self.assertTrue(self.worker.step())
        self.assertEqual(self.worker.outbox, [("a", core.NOTICE_DONE, "emx-test-a · alpha", "Alpha, sir: all fixed.", notice.at)])
        self.assertEqual(self.texts(), ["Alpha, sir: all fixed."])
        self.assertEqual(self.speaker.spoken, ["Alpha, sir: all fixed."])
        self.assertEqual([c.text for c in self.worker.calls], ["Alpha, sir: all fixed."])
        self.assertEqual(self.worker.pending, [])
        self.assertTrue(self.asked[0].startswith("Project: alpha\nSession: emx-test-a\nWhat happened: it has finished"))
        self.assertEqual(self.texts(), [])

    def relay_records(self, path):
        return [core.parse_relay(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_worded_notice_is_appended_to_the_relay_file(self):
        sid = str(uuid.uuid4())
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.worker.relay = folder / "relay.jsonl"
        self.worker.want([session(sid, "/p/alpha", "done")])
        self.transcripts.found = {sid: core.Transcript(prompt="Fix it", reply="Fixed.", reply_line="Fixed.", answered=True, latest="reply")}
        self.notice(sid=sid)
        self.assertTrue(self.worker.step())
        (record,) = self.relay_records(self.worker.relay)
        self.assertEqual(
            {k: record[k] for k in ("kind", "title", "text", "sid", "mode")},
            {"kind": "notice", "title": f"emx-test-{sid} · alpha", "text": "Alpha, sir: all fixed.", "sid": sid, "mode": "voice"},
        )
        self.assertTrue(record["plain"])

    def test_the_relay_records_the_mode_the_notice_went_out_in(self):
        sid = str(uuid.uuid4())
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        self.worker.relay = folder / "relay.jsonl"
        self.worker.mode = core.NOTIFICATION_MODE
        self.worker.want([session(sid, "/p/alpha", "done")])
        self.transcripts.found = {sid: core.Transcript(prompt="Fix it", reply="Fixed.", reply_line="Fixed.", answered=True, latest="reply")}
        self.notice(sid=sid)
        self.assertTrue(self.worker.step())
        (record,) = self.relay_records(self.worker.relay)
        self.assertEqual(record["mode"], "notification")

    def test_with_no_relay_nothing_is_written_anywhere(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(folder), "HOME": str(folder)}):
            self.notice()
            self.assertTrue(self.worker.step())
        self.assertEqual(list(folder.iterdir()), [])

    def test_a_mode_switch_during_the_wording_decides_where_the_notice_goes(self):
        for before, after in ((core.VOICE_MODE, core.NOTIFICATION_MODE), (core.NOTIFICATION_MODE, core.VOICE_MODE)):
            with self.subTest(before=before):
                self.speaker.spoken.clear()
                self.notifier.posts.clear()
                self.worker.mode = before
                answer = self.answer

                def switching(claude, excerpt, cwd, mode=core.VOICE_MODE, after=after):
                    self.worker.mode = after
                    return answer

                with mock.patch.object(sidebar, "announce", switching):
                    self.notice()
                    self.assertTrue(self.worker.step())
                self.assertEqual(len(self.worker.take()), 1)
                if after == core.VOICE_MODE:
                    self.assertEqual((len(self.speaker.spoken), len(self.notifier.posts)), (1, 0))
                else:
                    self.assertEqual((len(self.speaker.spoken), len(self.notifier.posts)), (0, 1))

    def test_an_unnamed_session_is_asked_about_without_a_session_line(self):
        self.notice(named=False)
        self.worker.step()
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(self.asked[0].startswith("Project: alpha\nWhat happened: it has finished"))
        self.assertNotIn("Session:", self.asked[0])

    def test_an_error_a_refusal_or_an_empty_answer_gives_the_plain_line(self):
        empty = core.SummaryCall("", "m", 1, 1, 0.0, 1, 1, 0, 0)
        for answer in ((None, "boom"), (None, core.HAIKU_DECLINED), (empty, None)):
            with self.subTest(answer=answer):
                self.answer = answer
                self.notice()
                self.worker.step()
                self.assertEqual(self.texts(), ["emx-test-a, in alpha, is done."])
        self.assertEqual(len(self.worker.calls), 1)

    def test_an_empty_excerpt_makes_no_call_and_gives_the_plain_line(self):
        self.transcripts.found = {}
        self.notice(age=core.NOTICE_SETTLE_SECONDS + 1)
        self.worker.step()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.texts(), ["emx-test-a, in alpha, is done."])
        self.assertEqual(self.worker.calls, [])

    def test_a_needs_input_fallback_says_needs_you(self):
        self.worker.want([session("a", "/p/alpha", "blocked")])
        self.transcripts.found = {}
        self.answer = (None, "boom")
        self.notice(core.NOTICE_NEEDS, age=core.NOTICE_SETTLE_SECONDS + 1)
        self.worker.step()
        self.assertEqual(self.texts(), ["emx-test-a, in alpha, needs you."])

    def test_not_ready_sends_nothing_until_the_settle_time_has_passed(self):
        self.transcripts.found = {"a": core.Transcript(prompt="Fix it", latest="prompt")}
        self.notice(age=1)
        self.assertFalse(self.worker.step())
        self.assertEqual((self.texts(), self.asked), ([], []))
        self.worker.pending.clear()
        self.notice(age=core.NOTICE_SETTLE_SECONDS)
        self.assertTrue(self.worker.step())
        self.assertEqual(len(self.texts()), 1)

    def test_a_notice_for_a_session_now_in_another_state_is_dropped(self):
        self.worker.want([session("a", "/p/alpha", "working")])
        self.notice()
        self.worker.step()
        self.assertEqual((self.texts(), self.worker.pending, self.asked, self.speaker.spoken), ([], [], [], []))

    def test_a_done_from_a_queued_message_is_never_spoken_when_the_next_turn_starts_inside_the_window(self):
        self.worker.want([replace(self.alpha, queued=True)])
        self.notice(age=2)
        self.assertFalse(self.worker.step())
        self.assertEqual(len(self.worker.pending), 1)
        self.worker.want([session("a", "/p/alpha", "working")])
        self.assertTrue(self.worker.step())
        self.assertEqual((self.texts(), self.worker.pending, self.asked, self.speaker.spoken), ([], [], [], []))

    def test_a_done_with_a_queued_message_and_its_words_ready_still_waits_for_the_window(self):
        self.worker.want([replace(self.alpha, queued=True)])
        self.notice(age=2)
        self.assertFalse(self.worker.step())
        self.assertEqual((self.texts(), self.asked), ([], []))
        self.worker.pending.clear()
        self.notice(age=core.NOTICE_SETTLE_SECONDS)
        self.assertTrue(self.worker.step())
        self.assertEqual(self.texts(), ["Alpha, sir: all fixed."])

    def test_a_done_with_nothing_queued_and_its_words_ready_goes_at_once(self):
        self.notice(age=0)
        self.assertTrue(self.worker.step())
        self.assertEqual(self.texts(), ["Alpha, sir: all fixed."])

    def test_an_unnamed_sessions_plain_line_says_a_session_in_its_project(self):
        self.answer = (None, "boom")
        self.notice(named=False)
        self.worker.step()
        self.assertEqual(self.texts(), ["A session in alpha is done."])

    def test_a_session_that_changed_state_during_the_call_is_dropped_too(self):
        def slow(claude, excerpt, cwd, mode=core.VOICE_MODE):
            self.worker.want([session("a", "/p/alpha", "working")])
            return self.answer

        with mock.patch.object(sidebar, "announce", slow):
            self.notice()
            self.worker.step()
        self.assertEqual((self.texts(), self.worker.pending, self.speaker.spoken), ([], [], []))

    def test_a_session_absent_from_the_listing_keeps_its_notice(self):
        self.worker.want([session("b", "/p/beta")])
        self.transcripts.found = {}
        self.notice(age=1)
        self.assertFalse(self.worker.step())
        self.assertEqual(len(self.worker.pending), 1)
        self.worker.pending.clear()
        self.notice(age=core.NOTICE_SETTLE_SECONDS + 1)
        self.worker.step()
        self.assertEqual(self.texts(), ["emx-test-a, in alpha, is done."])

    def test_in_notification_mode_the_outbox_still_gets_it_and_nothing_is_spoken(self):
        self.worker.mode = core.NOTIFICATION_MODE
        self.notice()
        self.worker.step()
        self.assertEqual(self.texts(), ["Alpha, sir: all fixed."])
        self.assertEqual(self.speaker.spoken, [])

    def named_alpha(self):
        alpha = replace(self.alpha, name="Alpha")
        self.worker.want([alpha])
        notice = core.Notice("a", core.NOTICE_DONE, "Alpha", "/p/alpha", time.monotonic(), True)
        self.worker.add(notice)
        return notice

    def test_notification_mode_posts_and_never_speaks(self):
        self.worker.mode = core.NOTIFICATION_MODE
        self.answer = (core.SummaryCall("The alpha app: all fixed.", "m", 1, 1, 0.0, 1, 1, 0, 0), None)
        self.named_alpha()
        self.worker.step()
        self.assertEqual(self.notifier.posts, [("Alpha · alpha", "The alpha app: all fixed.", "a")])
        self.assertEqual(self.speaker.spoken, [])

    def test_voice_mode_speaks_and_never_posts(self):
        self.named_alpha()
        self.worker.step()
        self.assertEqual(self.speaker.spoken, ["Alpha, sir: all fixed."])
        self.assertEqual(self.speaker.fallbacks, [("Alpha · alpha", "Alpha, in alpha, is done.", "a")])
        self.assertEqual(self.notifier.posts, [])

    def test_the_wording_follows_the_mode_before_the_call_and_delivery_the_mode_after_it(self):
        def switching(claude, excerpt, cwd, mode=core.VOICE_MODE):
            self.modes.append(mode)
            self.worker.mode = core.NOTIFICATION_MODE
            return self.answer

        with mock.patch.object(sidebar, "announce", switching):
            self.named_alpha()
            self.worker.step()
        self.assertEqual(self.modes, [core.VOICE_MODE])
        self.assertEqual(self.speaker.spoken, [])
        self.assertEqual(self.notifier.posts, [("Alpha · alpha", "Alpha, sir: all fixed.", "a")])

    def test_notification_mode_uses_the_notification_instruction_and_voice_the_butler_one(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv[argv.index("--system-prompt") + 1])
            return completed(0, LONG_JSON)

        with mock.patch.object(sidebar, "announce", REAL_ANNOUNCE), mock.patch.object(sidebar.subprocess, "run", run):
            for mode in (core.NOTIFICATION_MODE, core.VOICE_MODE):
                self.worker.mode = mode
                self.notice()
                self.worker.step()
        self.assertEqual(seen, [core.NOTIFICATION_INSTRUCTION, core.NOTICE_INSTRUCTION])

    def test_a_failed_haiku_posts_the_plain_line(self):
        self.worker.mode = core.NOTIFICATION_MODE
        self.answer = (None, "boom")
        self.named_alpha()
        self.worker.step()
        self.assertEqual(self.notifier.posts, [("Alpha · alpha", "Alpha, in alpha, is done.", "a")])

    def test_a_refused_scratch_folder_still_gives_the_plain_line(self):
        with mock.patch.object(sidebar, "summary_scratch", side_effect=OSError("refused")):
            self.notice()
            self.worker.step()
        self.assertEqual((self.asked, self.texts()), ([], ["emx-test-a, in alpha, is done."]))

    def test_hostile_words_are_cleaned_before_they_are_shown_or_spoken(self):
        self.answer = (core.SummaryCall("Done [[slnc 9000]] \x1b]9;x\x07 now.", "m", 1, 1, 0.0, 1, 1, 0, 0), None)
        self.notice()
        self.worker.step()
        (text,) = self.texts()
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

    def test_a_sentence_put_after_a_mute_is_dropped(self):
        self.voice.muted = True
        self.voice.put(("say", "late"))
        self.assertEqual(self.voice.queue, [])

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
        self.assertEqual(self.voice.queue, [("say", "heard", None)])

    def test_under_test_nothing_real_is_spoken(self):
        with mock.patch.dict(sidebar.os.environ, {"EMX_SHOW_TEST_SESSIONS": "1"}), \
                mock.patch.object(sidebar.subprocess, "run") as run, \
                mock.patch.object(sidebar, "list_voices") as voices:
            sidebar.os.environ.pop("EMX_SAY", None)
            self.voice.start()
            self.voice.speak("quiet")
            self.voice.speak_aloud("quiet", self.voice.generation)
            wait_until(self, lambda: not self.voice.queue, "the voice queue to drain")
        self.assertEqual(FakeSay.made, [])
        run.assert_not_called()
        voices.assert_not_called()

    def test_under_test_with_emx_say_set_only_that_command_runs(self):
        with mock.patch.dict(sidebar.os.environ, {"EMX_SHOW_TEST_SESSIONS": "1", "EMX_SAY": "fake-say"}), \
                mock.patch.object(sidebar.subprocess, "run") as run:
            voice = sidebar.Voice()
            voice.speak_aloud("heard", voice.generation)
        (proc,) = FakeSay.made
        self.assertEqual(proc.argv[0], "fake-say")
        run.assert_not_called()

    def test_a_spoken_notice_runs_only_the_say_command(self):
        self.voice.say = "fake-say"
        with mock.patch.object(sidebar.subprocess, "run") as run:
            self.voice.speak("x")
            self.voice.step()
        (proc,) = FakeSay.made
        self.assertEqual(proc.argv[0], "fake-say")
        run.assert_not_called()

    def test_hush_stops_the_playing_process_and_drops_the_queue_but_does_not_mute(self):
        FakeSay.reset(hold=5)
        self.voice.start()
        for text in ("one", "two", "three"):
            self.voice.speak(text)
        wait_until(self, lambda: FakeSay.made and FakeSay.made[0].entered.is_set(), "the first to start")
        self.voice.silence()
        self.assertTrue(FakeSay.made[0].ended.is_set())
        self.assertEqual(self.voice.queue, [])
        self.assertFalse(self.voice.muted)
        FakeSay.hold = 0.05
        self.voice.speak("later")
        wait_until(self, lambda: any(p.text == "later" and p.returncode == 0 for p in FakeSay.made), "the later text")

    def test_a_notice_from_before_the_hush_is_never_spoken_and_one_after_is(self):
        before = time.monotonic()
        self.voice.silence()
        after = time.monotonic()
        self.voice.speak("old", before)
        self.assertEqual(self.voice.queue, [])
        self.voice.speak("new", after)
        self.assertEqual(self.voice.queue, [("say", "new", None)])
        self.voice.speak("untimed")
        self.assertEqual(len(self.voice.queue), 2)

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
            poller = sidebar.Poller(FakeEngine())
        self.assertEqual(threading.active_count(), before)
        self.assertIsNone(poller.voice.voice)
        self.assertFalse(poller.notices.is_alive() or poller.voice.is_alive() or poller.notifier.is_alive())
        self.assertFalse(poller.asks.is_alive())
        self.assertIs(poller.asks.wake, poller.wake)
        self.assertIs(poller.voice.fallback_post.__self__, poller.notifier)
        self.assertIs(poller.notices.notifier, poller.notifier)


class FailingSay(FakeSay):
    def communicate(self, text=None, timeout=None):
        out = super().communicate(text, timeout)
        self.returncode = 1
        return out


class SayFallback(unittest.TestCase):
    """In voice mode a `say` that fails posts the plain line as a notification; a hush never does."""

    PLAIN = "Alpha, in alpha, is done."

    def setUp(self):
        FakeSay.reset()
        for target, name, value in ((sidebar, "list_voices", lambda say: []),):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.posts = []
        self.voice = sidebar.Voice()
        self.voice.fallback_post = lambda *args: self.posts.append(args)

    def speak_once(self, popen):
        with mock.patch.object(sidebar.subprocess, "Popen", popen):
            self.voice.speak("Alpha, sir: all fixed.", 5.0, fallback=("Alpha · alpha", self.PLAIN, "a"))
            self.voice.step()

    def test_a_failed_say_posts_a_banner_in_voice_mode(self):
        self.speak_once(FailingSay)
        self.assertEqual(self.posts, [("Alpha · alpha", self.PLAIN, "a", 5.0)])
        self.assertNotIn("sir", self.posts[0][1])

    def test_a_missing_say_posts_a_banner(self):
        self.speak_once(mock.Mock(side_effect=FileNotFoundError("say")))
        self.assertEqual(len(self.posts), 1)

    def test_a_say_that_speaks_posts_nothing(self):
        self.speak_once(FakeSay)
        self.assertEqual(self.posts, [])

    def test_a_hushed_say_does_not_post(self):
        FakeSay.reset(hold=5)

        class HushedFailing(FailingSay):
            def communicate(inner, text=None, timeout=None):
                threading.Timer(0.05, self.voice.silence).start()
                return super().communicate(text, timeout)

        self.speak_once(HushedFailing)
        self.assertEqual(self.posts, [])

    def test_no_fallback_given_posts_nothing(self):
        with mock.patch.object(sidebar.subprocess, "Popen", FailingSay):
            self.voice.speak("x")
            self.voice.step()
        self.assertEqual(self.posts, [])


class RecordingHelper:
    """A fake emx-notify script in a temp folder: logs each verb and its stdin, answers with the `auth` in a file the test sets (into the result file for `authorize`)."""

    def __init__(self, test, auth="notDetermined"):
        tmp = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, tmp, True)
        self.log = os.path.join(tmp, "log")
        self.auth = os.path.join(tmp, "auth")
        self.path = os.path.join(tmp, "fake-notify")
        self.set(auth)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                f'input=$(cat); printf "%s %s\\n" "$1" "$input" >> {shlex.quote(self.log)}\n'
                f"auth=$(cat {shlex.quote(self.auth)})\n"
                'line="{\\"auth\\": \\"$auth\\", \\"alerts\\": \\"enabled\\", \\"posted\\": true, \\"shown\\": true, \\"delivered\\": 1}"\n'
                'if [ "$1" = authorize ]; then echo "$line" > "$2"; else echo "$line"; fi\n'
            )
        os.chmod(self.path, 0o700)

    def set(self, auth):
        with open(self.auth, "w", encoding="utf-8") as fh:
            fh.write(auth)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as fh:
                return [line.rstrip("\n").split(" ", 1) for line in fh]
        except FileNotFoundError:
            return []


class NotifierWorker(unittest.TestCase):
    def env(self, **values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("EMX_NOTIFY", "EMX_NOTIFY_HOME", "EMX_SHOW_TEST_SESSIONS"):
            if name not in values:
                os.environ.pop(name, None)

    def test_under_test_without_emx_notify_nothing_is_built_or_posted(self):
        self.env(EMX_SHOW_TEST_SESSIONS="1")
        notifier = sidebar.Notifier("emx-test-x")
        with mock.patch.object(sidebar.notify.subprocess, "run", side_effect=AssertionError("run")) as run, \
                mock.patch.object(sidebar.notify.subprocess, "Popen", side_effect=AssertionError("Popen")), \
                mock.patch.object(sidebar.notify, "ensure_built", side_effect=AssertionError("ensure_built")):
            notifier.prepare()
            notifier.post("t", "b", "s")
            notifier.step()
        run.assert_not_called()
        self.assertEqual(notifier.state, core.HELPER_UNAVAILABLE)
        self.assertEqual(notifier.queue, [])

    def test_under_test_with_a_helper_home_still_builds_nothing(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY_HOME=home)
        notifier = sidebar.Notifier("emx-test-x")
        with mock.patch.object(sidebar.notify.subprocess, "run", side_effect=AssertionError("run")) as run, \
                mock.patch.object(sidebar.notify, "ensure_built", side_effect=AssertionError("ensure_built")) as built:
            notifier.prepare()
        run.assert_not_called()
        built.assert_not_called()
        self.assertEqual(os.listdir(home), [])
        self.assertEqual(notifier.state, core.HELPER_UNAVAILABLE)

    def test_a_notification_mode_start_asks_the_permission_once_and_then_posts(self):
        helper = RecordingHelper(self)
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.prepare()
        self.assertEqual(notifier.state, core.HELPER_PENDING)
        helper.set("authorized")
        notifier.post("Alpha · alpha", "-Alpha is done.", "sid-1")
        notifier.step()
        notifier.step()
        notifier.authorize()
        notifier.step()
        verbs = [verb for verb, _ in helper.calls()]
        self.assertEqual(verbs, ["status", "authorize", "post"])
        sent = json.loads(helper.calls()[2][1])
        self.assertEqual((sent["title"], sent["body"], sent["open"]["sid"]), ("Alpha · alpha", "-Alpha is done.", "sid-1"))
        self.assertEqual(sent["open"]["env"]["EMX_SOCKET"], "emx-test-x")
        self.assertEqual(notifier.state, core.HELPER_OK)

    def test_a_voice_mode_start_asks_nothing_until_switched(self):
        helper = RecordingHelper(self)
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.mode = core.VOICE_MODE
        notifier.prepare()
        notifier.checked_at = time.monotonic()
        with mock.patch.object(notifier.wake, "wait"):
            notifier.step()
        self.assertEqual([verb for verb, _ in helper.calls()], ["status"])
        notifier.authorize()
        notifier.step()
        self.assertEqual([verb for verb, _ in helper.calls()], ["status", "authorize"])

    def test_a_permission_still_undecided_after_the_ask_reads_blocked(self):
        helper = RecordingHelper(self, auth="notDetermined")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.prepare()
        self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), "")
        notifier.step()
        notifier.post("t", "b", "sid-1")
        notifier.step()
        self.assertEqual([verb for verb, _ in helper.calls()], ["status", "authorize", "post"])
        self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), core.BLOCKED_TAG)

    def switch_to_voice_and_back(self, notifier):
        notifier.mode = core.VOICE_MODE
        notifier.mode = core.NOTIFICATION_MODE
        notifier.authorize()
        notifier.step()

    def test_an_unanswered_ask_is_asked_again_on_a_switch_back_and_reads_blocked_meanwhile(self):
        helper = RecordingHelper(self, auth="notDetermined")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.prepare()
        notifier.step()
        self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), core.BLOCKED_TAG)
        self.switch_to_voice_and_back(notifier)
        self.assertEqual([verb for verb, _ in helper.calls()], ["status", "authorize", "authorize"])
        self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), core.BLOCKED_TAG)

    def test_a_denied_ask_is_not_asked_again_on_a_switch_back(self):
        helper = RecordingHelper(self, auth="notDetermined")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.prepare()
        notifier.step()
        helper.set("denied")
        notifier.check()
        self.assertEqual(notifier.state, core.HELPER_BLOCKED)
        notifier.mode = core.VOICE_MODE
        notifier.mode = core.NOTIFICATION_MODE
        notifier.authorize()
        with mock.patch.object(notifier.wake, "wait"):
            notifier.step()
        self.assertEqual([verb for verb, _ in helper.calls()], ["status", "authorize", "status"])
        self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), core.BLOCKED_TAG)

    def test_before_any_ask_the_tag_stays_empty(self):
        helper = RecordingHelper(self, auth="notDetermined")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.mode = core.VOICE_MODE
        notifier.prepare()
        self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), "")

    def test_a_permission_not_yet_asked_for_still_reads_pending(self):
        helper = RecordingHelper(self, auth="notDetermined")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.mode = core.VOICE_MODE
        notifier.prepare()
        self.assertEqual(notifier.state, core.HELPER_PENDING)

    def test_a_denied_report_reads_blocked(self):
        helper = RecordingHelper(self, auth="denied")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.prepare()
        self.assertEqual(notifier.state, core.HELPER_BLOCKED)
        with mock.patch.object(notifier.wake, "wait") as wait:
            notifier.step()
        wait.assert_called_once()
        self.assertEqual([verb for verb, _ in helper.calls()], ["status"])

    def test_a_refused_ask_is_not_asked_again_on_a_switch_back_when_no_check_ran_between(self):
        helper = RecordingHelper(self, auth="notDetermined")
        self.env(EMX_SHOW_TEST_SESSIONS="1", EMX_NOTIFY=helper.path)
        notifier = sidebar.Notifier("emx-test-x")
        notifier.prepare()
        refused = sidebar.notify.HelperReport("notDetermined", "notSupported", False, False, 0, "UNErrorDomain Code=1")
        with mock.patch.object(sidebar.notify, "authorize", return_value=(refused, None)) as asked:
            notifier.step()
            self.assertEqual(core.mode_tag(core.NOTIFICATION_MODE, notifier.state), core.BLOCKED_TAG)
            notifier.mode = core.VOICE_MODE
            notifier.mode = core.NOTIFICATION_MODE
            notifier.authorize()
            with mock.patch.object(notifier.wake, "wait"):
                notifier.step()
        asked.assert_called_once()
        self.assertEqual(notifier.state, core.HELPER_BLOCKED)


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

    def main_shows(self, sid, state=(1, "1", "%2", 3.0), shown=True):
        """The view's slot (%2) shows `sid`, or with `shown` False shows no session (the placeholder)."""
        self.app.main_pane = Pane("%2", "@1", False, "viewer" if shown else "placeholder", "", 100, 141, view=sid if shown else "")
        self.app.tmux = ClientTmux(state, shows=sid if shown else "")

    def test_working_then_done_gives_one_notice_and_a_third_identical_poll_none(self):
        self.step(session("a", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "done"))
        (notice,) = self.pending()
        self.assertEqual((notice.id, notice.kind, notice.name, notice.cwd), ("a", core.DONE, "emx-test-a", "/p/alpha"))
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(len(self.pending()), 1)

    def test_a_notice_remembers_whether_the_session_has_a_name_of_its_own(self):
        unnamed = replace(session("a", "/p/alpha", "working"), name="1a2b3c4d", named=False)
        self.step(unnamed, session("b", "/p/alpha", "working"))
        self.step(replace(unnamed, state="done"), session("b", "/p/alpha", "done"))
        self.assertEqual([(n.id, n.named) for n in self.pending()], [("a", False), ("b", True)])

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

    def test_a_slot_showing_no_session_is_not_in_front(self):
        self.main_shows("a", shown=False)
        self.step(session("a", "/p/alpha", "working"))
        self.step(session("a", "/p/alpha", "done"))
        self.assertEqual(len(self.pending()), 1)

    def test_an_unnamed_session_in_front_is_quiet_whatever_its_screen_shows(self):
        self.main_shows("a")
        unnamed = [core.Session(sid, sid[:8], "/p/alpha", state, "", "", 0.0, True, named=False)
                   for sid, state in (("a", "working"), ("b", "working"))]
        self.step(*unnamed)
        self.step(*[replace(s, state="done") for s in unnamed])
        self.assertEqual([n.id for n in self.pending()], ["b"])

class NoticesWaitForTheVerdict(AppCase):
    """A stop Haiku reads as needing him is never announced as done, in either mode."""

    answered = core.Transcript(prompt="Pick one", reply="Which one?", reply_line="Which one?", answered=True, latest="reply")

    def setUp(self):
        super().setUp()
        self.app.poller.transcripts.found = {"a": self.answered, "b": self.answered}

    def listing(self, *sessions):
        self.poll(*sessions)
        self.app.refresh()
        self.app.poller.notices.want(sessions)
        with mock.patch.object(sidebar, "announce", lambda *args: (None, "no haiku")), \
                mock.patch.object(sidebar, "summary_scratch", lambda: "/tmp/emx-test-scratch"):
            while self.app.poller.notices.step():
                pass
        self.app.take_notices()

    def stopped(self, sid="a", asking=True, state="done"):
        return replace(session(sid, "/p/alpha", state), reply="Which one?", stop_key="k1", asking=asking)

    def run_modes(self, *listings):
        for mode in core.MODES:
            with self.subTest(mode=mode):
                self.app.history, self.app.notice_seen = [], None
                self.app.poller.notices.pending.clear()
                self.app.poller.notifier.posts.clear()
                self.spoken.clear()
                self.app.prefs = {**self.app.prefs, "mode": mode}
                self.app.apply_mode()
                for listing in listings:
                    self.listing(*listing)
                said = [p[1] for p in self.app.poller.notifier.posts] + [s[0] for s in self.spoken]
                yield mode, [e.kind for e in self.app.history], said

    def test_done_then_needs_input_gives_only_the_needs_notice(self):
        working = [session("a", "/p/alpha", "working")]
        for mode, kinds, said in self.run_modes(working, working, [self.stopped()], [self.stopped(state="blocked", asking=False)]):
            self.assertEqual(kinds, [core.NOTICE_NEEDS], mode)
            self.assertEqual(len(said), 1, mode)
            self.assertFalse([t for t in said if "is done" in t], mode)

    def test_done_then_a_done_verdict_gives_one_done_notice(self):
        working = [session("a", "/p/alpha", "working")]
        for mode, kinds, said in self.run_modes(working, working, [self.stopped()], [self.stopped(asking=False)]):
            self.assertEqual(kinds, [core.NOTICE_DONE], mode)
            self.assertEqual(len(said), 1, mode)
            self.assertIn("is done", said[0], mode)

    def test_a_second_session_stopping_at_once_is_held_past_75_seconds_then_gives_way_to_needs(self):
        working = [session("a", "/p/alpha", "working"), session("b", "/p/alpha", "working")]
        self.listing(*working)
        self.listing(*working)
        self.listing(self.stopped("a"), self.stopped("b"))
        notices = self.app.poller.notices
        notices.pending = [replace(n, at=n.at - 75) for n in notices.pending]
        self.assertEqual([n.kind for n in notices.pending], [core.NOTICE_DONE, core.NOTICE_DONE])
        self.listing(self.stopped("a", asking=False), self.stopped("b"))
        self.assertEqual([n.id for n in notices.pending], ["b"])
        self.assertEqual([(e.id, e.kind) for e in self.app.history], [("a", core.NOTICE_DONE)])
        (held,) = notices.pending
        notices.want([self.stopped("b", state="blocked", asking=False)])
        self.assertTrue(notices.stale(held))
        self.listing(self.stopped("a", asking=False), self.stopped("b", state="blocked", asking=False))
        self.assertEqual(notices.pending, [])
        self.assertEqual([(e.id, e.kind) for e in self.app.history], [("b", core.NOTICE_NEEDS), ("a", core.NOTICE_DONE)])


class NoBanner(AppCase):
    def test_no_banner_code_is_left(self):
        self.assertFalse(hasattr(core, "osc9"))
        self.assertFalse(hasattr(sidebar.Voice, "banner"))
        self.assertFalse(hasattr(sidebar.Voice, "show_banner"))
        self.assertFalse(hasattr(sidebar.App, "write_out"))
        self.assertFalse(hasattr(Tmux, "allow_passthrough"))
        root = Path(sidebar.__file__).parent
        for path in [*root.glob("*.py"), root / "emx.tmux.conf"]:
            text = path.read_text(encoding="utf-8")
            for word in ("]9;", "allow-passthrough", "display notification"):
                self.assertNotIn(word, text, f"{path.name} mentions {word}")

    def test_housekeep_sets_no_pane_option_but_its_own(self):
        calls = []

        class Housekeeping(ClientTmux):
            socket = "emx-test-nobanner"

            def panes(self):
                return [Pane("%1", "@1", False, "sidebar", "", 40, 141), Pane("%2", "@1", False, "", "", 100, 141)]

            def __getattr__(self, name):
                return lambda *args, **kwargs: calls.append((name, args, kwargs))

        self.app.tmux = Housekeeping()
        self.app.me = "%1"
        sidebar.App.housekeep(self.app, False)
        self.assertNotIn("allow-passthrough", repr(calls))
        self.assertNotIn("allow_passthrough", [name for name, _, _ in calls])

    def test_the_m_help_line_mentions_no_banner(self):
        for key, text in sidebar.HELP:
            self.assertNotIn("banner", text.lower(), key)

    def test_a_delivered_notice_reaches_neither_the_terminal_nor_osascript(self):
        for state in ((1, "0", "%1", 3.0), (0, "", "%1", None)):
            with self.subTest(state=state):
                self.app.tmux = ClientTmux(state)
                self.app.poller.notices.outbox = [("a", core.NOTICE_DONE, "Alpha · alpha", "Alpha is done.", time.monotonic())]
                with mock.patch.object(sidebar.os, "write") as write, \
                        mock.patch.object(sidebar.subprocess, "run") as run, \
                        mock.patch.object(sidebar.subprocess, "Popen") as popen:
                    self.app.refresh()
                for call in write.call_args_list:
                    self.assertNotIn(b"]9;", call.args[1])
                    self.assertNotIn(b"\x1bPtmux;", call.args[1])
                for call in [*run.call_args_list, *popen.call_args_list]:
                    self.assertNotEqual(call.args[0][0], "osascript")
                self.assertEqual(self.app.poller.notices.outbox, [])


class SwitchTmux(ClientTmux):
    """The tmux calls a real `switch_to` makes, over one viewer already open for session s1; `fail` makes the select raise."""

    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail
        self.selected = []

    def panes(self):
        return [
            Pane("%1", "@1", False, "sidebar", "", 40, 141),
            Pane("%2", "@1", False, "", "other", 100, 141),
            Pane("%3", "@2", False, "", "s1", 100, 141, agent="claude"),
        ]

    def capture(self, pane):
        return ""

    def link_agent(self, window, key, inert):
        pass

    def viewer_command(self, key):
        return f"viewer {key}"

    def clear_history(self, pane):
        pass

    def respawn(self, pane, command, cwd=None):
        pass

    def set_pane_option(self, pane, name, value):
        pass

    def select(self, pane):
        if self.fail:
            raise TmuxError("gone")
        self.selected.append(pane)


def entry(sid, text, at, kind=core.NOTICE_DONE, title=None):
    return (sid, kind, title or f"{sid} · alpha", text, at)


class TallScr(FakeScr):
    def __init__(self, keys=(), height=20, width=40):
        super().__init__(keys)
        self.size = (height, width)
        self.drawn = {}

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        self.drawn[(y, x)] = (text, attr)

    def erase(self):
        self.drawn = {}

    def text(self):
        return [t for (_, _), (t, _) in sorted(self.drawn.items())]


class HistoryWiring(AppCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: self.assertEqual(self.announced, [], "Haiku was asked on the curses thread"))

    def deliver(self, *items):
        self.app.poller.notices.outbox = list(items)
        self.app.refresh()

    def bottom(self, height=20):
        return [text for text, _ in self.app.bottom_lines(39, height)]

    def open_s1(self, fail=False):
        self.app.tmux = SwitchTmux(fail)
        self.app.me = "%1"
        self.poll(session("s1", "/p/alpha"))
        self.app.refresh()
        sidebar.App.switch_to(self.app, "s1", core.OPEN)

    def listed(self, *sids):
        self.poll(*[session(sid, "/p/alpha") for sid in sids])
        self.app.refresh()

    def test_a_taken_notice_shows_in_the_bar_and_not_in_the_panel(self):
        self.deliver(entry("s1", "Alpha is done.", time.monotonic() - 5))
        self.assertEqual(self.bottom(), [])
        self.assertRegex(self.bar(), r"✻ \ds Alpha is done\.")

    def test_the_bar_shows_the_newest_of_several_notices(self):
        self.deliver(entry("s1", "Alpha is done.", time.monotonic() - 5), entry("s2", "Beta is done.", time.monotonic() - 9),
                     entry("s3", "Gamma is done.", time.monotonic() - 9))
        bar = self.bar()
        self.assertIn("Alpha is done.", bar)
        self.assertNotIn("Beta is done.", bar)

    def test_a_taken_notice_lands_in_the_history_in_both_modes(self):
        for mode in core.MODES:
            with self.subTest(mode=mode):
                self.app.history = []
                self.app.prefs = {**self.app.prefs, "mode": mode}
                self.deliver(entry("s1", "Alpha is done.", time.monotonic()))
                self.assertEqual([(e.id, e.kind, e.title, e.text) for e in self.app.history],
                                 [("s1", core.NOTICE_DONE, "s1 · alpha", "Alpha is done.")])

    def test_two_notices_of_one_session_are_two_entries(self):
        self.deliver(entry("s1", "First.", time.monotonic() - 5), entry("s1", "Second.", time.monotonic() - 1, core.NOTICE_NEEDS))
        self.assertEqual([(e.text, e.kind) for e in self.app.history], [("Second.", core.NOTICE_NEEDS), ("First.", core.NOTICE_DONE)])

    def test_a_notice_is_listed_whether_attached_or_detached(self):
        for state in ((1, "0", "%1", 3.0), (0, "", "%1", None)):
            with self.subTest(state=state):
                self.app.history = []
                self.app.tmux = ClientTmux(state)
                self.deliver(entry("s1", "Alpha is done.", time.monotonic()))
                self.assertEqual(len(self.app.history), 1)

    def test_opening_a_session_from_the_list_dims_its_entries_and_keeps_them(self):
        self.deliver(entry("s1", "Alpha is done.", time.monotonic() - 5), entry("other", "Beta is done.", time.monotonic() - 4),
                     entry("s1", "Alpha again.", time.monotonic() - 3))
        self.open_s1()
        self.assertEqual([(e.id, e.read) for e in self.app.history], [("s1", True), ("other", False), ("s1", True)])

    def test_a_refused_or_failed_open_keeps_it_unread(self):
        self.deliver(entry("s1", "Alpha is done.", time.monotonic() - 5))
        self.open_s1(fail=True)
        self.assertEqual([(e.id, e.read) for e in self.app.history], [("s1", False)])
        self.assertNotIn("s1", self.app.opened)

    def test_a_refused_attach_keeps_it_unread(self):
        class NoViewerTmux(SwitchTmux):
            def panes(self):
                return [p for p in super().panes() if p.session != "s1"]

        self.deliver(entry("s1", "Alpha is done.", time.monotonic() - 5))
        self.app.tmux = NoViewerTmux()
        self.app.me = "%1"
        self.poll(session("s2", "/p/beta"))
        self.app.refresh()
        self.assertIsNone(self.app.session("s1"))
        self.assertFalse(core.attach_allowed(self.app.session("s1"), core.OPEN))
        sidebar.App.switch_to(self.app, "s1", core.OPEN)
        self.assertEqual([(e.id, e.read) for e in self.app.history], [("s1", False)])
        self.assertNotIn("s1", self.app.opened)

    def test_only_an_open_marks_it_read(self):
        self.deliver(entry("s1", "Alpha is done.", time.monotonic() - 5))
        self.app.tmux = SwitchTmux()
        self.app.me = "%1"
        self.poll(session("s1", "/p/alpha"))
        self.app.refresh()
        sidebar.App.switch_to(self.app, "s1", core.NEW, (session("s1", "/p/alpha"), True))
        self.assertEqual([(e.id, e.read) for e in self.app.history], [("s1", False)])

    def test_a_notice_landing_after_the_open_lands_read(self):
        before = time.monotonic() - 5
        self.open_s1()
        self.deliver(entry("s1", "Alpha is done.", before))
        self.assertEqual([(e.id, e.read) for e in self.app.history], [("s1", True)])

    def test_the_bar_has_no_hints_while_the_prompt_find_or_rename_is_open(self):
        self.deliver(entry("s1", "Alpha is done.", time.monotonic()))
        self.assertIn("? keys", self.bar())
        self.app.handle_key("/")
        self.assertNotIn("? keys", self.bar())
        self.app.find = None
        self.app.rename_edit = {"id": "s1", "old": "x", "hint": "", "text": ""}
        self.assertNotIn("? keys", self.bar())
        self.app.rename_edit = None
        self.app.prompt = {"stage": "message", "folder": "/p", "text": "", "hint": ""}
        self.assertNotIn("? keys", self.bar())

    def test_an_in_front_skip_adds_nothing_to_the_history(self):
        self.app.main_pane = Pane("%2", "@1", False, "", "a", 100, 141, agent="claude")
        self.app.tmux = ClientTmux((1, "1", "%2", 3.0), shows="a")
        self.poll(session("a", "/p/alpha", "working"))
        self.app.refresh()
        self.poll(session("a", "/p/alpha", "done"))
        self.app.refresh()
        self.assertEqual(self.app.poller.notices.pending, [])
        self.assertEqual(self.app.history, [])

    def test_the_bar_dims_a_read_newest_notice(self):
        self.app.prefs = core.set_scheme(self.app.prefs, "dark")
        self.deliver(entry("other", "Beta is done.", time.monotonic() - 5), entry("s1", "Alpha is done.", time.monotonic() - 4))
        self.app.window_width = 140
        self.app.update_bar()
        self.assertIn("#[fg=default,nobold]Alpha is done.", self.app.bar.pushed[-1])
        self.open_s1()
        self.app.update_bar()
        self.assertIn("#[fg=colour245,nobold]Alpha is done.", self.app.bar.pushed[-1])

    def test_the_history_is_empty_in_a_new_app(self):
        self.assertEqual(self.app.history, [])
        again = sidebar.App(FakeScr(), tmux=ClientTmux(), claude="claude")
        self.assertEqual(again.history, [])
        self.assertIsNone(again.history_view)

    def test_the_help_has_the_N_line(self):
        self.assertIn(("N", "notifications: the last 20; Enter opens one"), sidebar.HELP)


class HistoryView(AppCase):
    def setUp(self):
        super().setUp()
        self.scr = TallScr()
        self.app.scr = self.scr
        self.listed("s1", "s2")
        now = time.monotonic()
        self.deliver(entry("s1", "Alpha finished the tax fix.", now - 20, title="Alpha · alpha"),
                     entry("s2", "Beta needs your yes on a library.", now - 10, core.NOTICE_NEEDS, title="Beta · beta"))
        self.opened.clear()

    deliver = HistoryWiring.deliver
    listed = HistoryWiring.listed

    def draw(self):
        self.app.draw()
        return self.scr.text()

    def test_N_opens_the_view_and_marks_nothing_read(self):
        self.app.handle_key("N")
        self.assertEqual(self.app.history_view, 0)
        self.assertEqual(core.history_unread(self.app.history), 2)
        self.assertEqual(self.opened, [])

    def test_the_view_draws_a_header_and_wrapped_sentence_per_entry(self):
        self.app.handle_key("N")
        drawn = self.draw()
        self.assertIn("Notifications", drawn)
        self.assertTrue(any(t.startswith("● ") and t.endswith("Beta · beta") for t in drawn), drawn)
        self.assertTrue(any(t.startswith("✓ ") and t.endswith("Alpha · alpha") for t in drawn), drawn)
        self.assertIn("  Beta needs your yes on a library.", drawn)
        self.assertIn("  Alpha finished the tax fix.", drawn)
        self.assertEqual(self.app.bar_hints(), (sidebar.HISTORY_HINT, "Esc back"))
        self.assertFalse(any(t.startswith("Notifications (") for t in drawn), drawn)

    def test_down_and_enter_open_the_second_entrys_session_and_mark_it_read(self):
        self.app.handle_key("N")
        self.app.handle_key(curses.KEY_DOWN)
        self.app.handle_key("\n")
        self.assertEqual(self.opened, [("s1", core.OPEN)])
        self.assertIsNone(self.app.history_view)
        self.assertEqual(self.app.cursor, ("session", "s1"))
        self.app.switch_to = lambda sid, trigger, checked=None: sidebar.App.switch_to(self.app, sid, trigger, checked)
        self.app.tmux = SwitchTmux()
        self.app.me = "%1"
        self.assertEqual(core.history_unread(self.app.history), 2)
        self.app.handle_key("N")
        self.app.handle_key("j")
        self.app.handle_key("\n")
        self.assertEqual([(e.id, e.read) for e in self.app.history], [("s2", False), ("s1", True)])

    def test_a_click_on_an_entry_opens_its_session(self):
        self.app.handle_key("N")
        self.draw()
        (y,) = [y for y, item in self.app.row_map.items() if item == ("entry", 1)][:1]
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 0, y, 0, curses.BUTTON1_PRESSED)):
            self.app.handle_key(curses.KEY_MOUSE)
        self.assertEqual(self.opened, [("s1", core.OPEN)])
        self.assertIsNone(self.app.history_view)

    def test_esc_closes_the_view_without_opening_anything(self):
        self.app.handle_key("N")
        self.app.handle_key("\x1b")
        self.assertIsNone(self.app.history_view)
        self.assertEqual(self.opened, [])
        self.assertEqual(core.history_unread(self.app.history), 2)

    def test_other_keys_do_nothing_in_the_view(self):
        self.app.handle_key("N")
        for key in ("n", "m", "q", "?", "s", "N"):
            self.app.handle_key(key)
        self.assertEqual(self.app.history_view, 0)
        self.assertIsNone(self.app.prompt)
        self.assertFalse(self.app.help)
        self.assertEqual(core.notice_mode(self.app.prefs), core.NOTIFICATION_MODE)

    def test_N_in_the_find_box_is_text(self):
        self.app.handle_key("/")
        self.app.handle_key("N")
        self.assertEqual(self.app.find, "N")
        self.assertIsNone(self.app.history_view)

    def test_an_entry_for_a_gone_session_says_so_and_switches_nothing(self):
        self.listed("s2")
        self.app.handle_key("N")
        self.app.handle_key(curses.KEY_DOWN)
        self.app.handle_key("\n")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.notice, sidebar.OPEN_GONE)

    def test_an_empty_history_says_so(self):
        self.app.history = []
        self.app.handle_key("N")
        self.assertIn(sidebar.HISTORY_EMPTY, self.draw())
        self.app.handle_key("\n")
        self.assertEqual(self.opened, [])

    def test_the_view_scrolls_to_keep_the_cursor_visible(self):
        self.scr.size = (12, 40)
        now = time.monotonic()
        self.app.history = []
        self.deliver(*[entry("s1", f"Entry number {i} is done.", now - 100 + i, title=f"Entry {i} · alpha") for i in range(20)])
        self.assertEqual(len(self.app.history), 20)
        self.app.handle_key("N")
        for _ in range(25):
            self.app.handle_key("j")
        self.assertEqual(self.app.history_view, 19)
        drawn = self.draw()
        self.assertTrue(any(t.endswith("Entry 0 · alpha") for t in drawn), drawn)
        self.assertFalse(any(t.endswith("Entry 19 · alpha") for t in drawn), drawn)


class HushTmux(ClientTmux):
    """What a real housekeep asks: one panel pane and no main pane, and anything else it may call does nothing."""
    socket = "emx-test-hush"

    def panes(self):
        return [Pane("%1", "@1", False, "sidebar", "", 40, 141)]

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class CollapseTmux(ClientTmux):
    """A panel pane %1 and a main pane %2 in one window; records the calls a collapse tick makes."""
    socket = "emx-test-collapse"

    def __init__(self):
        super().__init__()
        self.zoomed = False
        self.window_width = 161
        self.panel_width = 40
        self.main_id = "%2"
        self.calls = []

    def panes(self):
        return [
            Pane("%1", "@1", False, "sidebar", "", self.panel_width, self.window_width, zoomed=self.zoomed),
            Pane(self.main_id, "@1", False, "", "", 100, self.window_width, zoomed=self.zoomed),
        ]

    def bind_collapse(self, key):
        self.calls.append(("bind_collapse", key))

    def bind_paste(self, key, check):
        self.calls.append(("bind_paste", key, check))

    def resize_width(self, pane, width):
        self.calls.append(("resize_width", pane, width))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


class CollapseWiring(AppCase):
    def setUp(self):
        super().setUp()
        self.app.tmux = CollapseTmux()
        self.app.me = "%1"

    def tick(self, **state):
        for name, value in state.items():
            setattr(self.app.tmux, name, value)
        sidebar.App.housekeep(self.app, False)

    def saved(self):
        return core.load_prefs(self.app.prefs_file)

    def test_binds_the_key_once_and_a_new_slot_needs_no_rebinding(self):
        self.tick()
        self.assertEqual(self.app.tmux.of("bind_collapse"), [("bind_collapse", sidebar.COLLAPSE_KEY)])
        self.tick()
        self.tick(main_id="%3")
        self.assertEqual(len(self.app.tmux.of("bind_collapse")), 1)

    def test_a_follower_views_hidden_panel_is_its_own_and_never_saved(self):
        self.app.primary = False
        self.tick(zoomed=True)
        self.assertNotIn("panel_collapsed", self.saved())

    def test_a_follower_views_dragged_or_stepped_width_is_its_own_and_never_saved(self):
        self.app.primary = False
        self.app.applied = (161, 40)
        self.tick(panel_width=60)
        self.assertEqual(self.app.own_width, 60)
        self.app.change_panel_width(-sidebar.core.WIDTH_STEP)
        self.tick()
        self.assertEqual(self.app.own_width, 55)
        self.assertEqual(self.app.tmux.of("resize_width")[-1], ("resize_width", "%1", 55))
        self.assertEqual(self.app.prefs["panel_width"], 40)
        self.assertNotEqual(self.saved()["panel_width"], 55)
        self.assertNotEqual(self.saved()["panel_width"], 60)

    def test_binds_paste_once_with_the_relay_file(self):
        self.tick()
        self.tick()
        (call,) = self.app.tmux.of("bind_paste")
        self.assertEqual(call[1], sidebar.PASTE_KEY)
        self.assertTrue(call[2].endswith(f"paste {self.app.tmux.socket} {shlex.quote(str(self.app.relay_file))}"), call[2])

    def test_hiding_is_saved_and_showing_is_saved(self):
        self.tick(zoomed=True)
        self.assertIs(self.saved()["panel_collapsed"], True)
        self.tick(zoomed=False)
        self.assertIs(self.saved()["panel_collapsed"], False)

    def test_no_resize_while_hidden(self):
        self.app.applied = (121, 40)
        self.tick(zoomed=True, window_width=161, panel_width=60)
        self.assertEqual(self.app.tmux.of("resize_width"), [])
        self.assertEqual(self.app.prefs["panel_width"], 40)
        self.assertEqual(self.saved()["panel_width"], 40)

    def test_showing_again_applies_the_saved_width(self):
        self.app.applied = (121, 40)
        self.tick(zoomed=True, window_width=161, panel_width=60)
        self.tick(zoomed=False)
        self.assertEqual(self.app.tmux.of("resize_width"), [("resize_width", "%1", core.compute_layout(161, 40).panel)])
        self.assertEqual(self.app.prefs["panel_width"], 40)

    def test_a_hidden_start_at_the_same_width_still_reapplies(self):
        self.app.applied = (161, 40)
        self.tick(zoomed=True)
        self.tick(zoomed=False, panel_width=39)
        self.assertEqual(self.app.tmux.of("resize_width"), [("resize_width", "%1", 40)])
        self.assertEqual(self.app.prefs["panel_width"], 40)

    def test_help_lists_the_key(self):
        (entry,) = [text for key, text in sidebar.HELP if key == "^q"]
        self.assertIn("^\\", entry)
        self.assertIn("←", entry)


class HushWiring(AppCase):
    def setUp(self):
        super().setUp()
        self.app.tmux = HushTmux()
        self.app.me = "%1"
        self.app.prefs = {**self.app.prefs, "mode": core.VOICE_MODE}
        self.app.apply_mode()

    def housekeep(self, value):
        self.app.tmux.hush = value
        sidebar.App.housekeep(self.app, False)

    def test_h_hushes_without_muting(self):
        before = dict(self.app.prefs)
        self.app.handle_key("h")
        self.assertEqual(len(self.silenced), 1)
        self.assertEqual(self.app.prefs, before)
        self.assertFalse(self.app.poller.voice.muted)
        self.assertEqual(self.app.notice, "Stopped speaking.")

    def test_the_first_read_is_a_baseline_and_a_changed_token_hushes_once(self):
        self.housekeep("100")
        self.assertEqual(self.silenced, [])
        self.housekeep("100")
        self.assertEqual(self.silenced, [])
        self.housekeep("200")
        self.assertEqual(len(self.silenced), 1)
        self.housekeep("200")
        self.assertEqual(len(self.silenced), 1)

    def test_a_tmux_error_while_reading_changes_nothing(self):
        self.housekeep("100")
        self.housekeep(TmuxError("gone"))
        self.assertEqual(self.silenced, [])
        self.assertEqual(self.app.hush_seen, "100")
        self.housekeep("100")
        self.assertEqual(self.silenced, [])

    def test_the_help_lists_h_and_m(self):
        keys = dict(sidebar.HELP)
        self.assertIn("stop the voice", keys["h"])
        self.assertIn("voice mode or notification mode", keys["m"])

    def test_h_in_notification_mode_calls_no_voice_and_shows_no_message(self):
        self.app.prefs = {**self.app.prefs, "mode": core.NOTIFICATION_MODE}
        self.app.apply_mode()
        self.app.say("before")
        self.app.handle_key("h")
        self.housekeep("100")
        self.housekeep("200")
        self.assertEqual(self.silenced, [])
        self.assertEqual(self.app.notice, "before")


class RelayTmux(HushTmux):
    def __init__(self):
        super().__init__()
        self.options = {}

    def session_option(self, name):
        return self.options.get(name, "")


class AppRelay(AppCase):
    def setUp(self):
        super().setUp()
        self.app.tmux = RelayTmux()

    def lines(self):
        if not self.app.relay_file.exists():
            return []
        return self.app.relay_file.read_text(encoding="utf-8").splitlines()

    def test_h_in_voice_mode_appends_one_hush_line(self):
        self.app.prefs = {**self.app.prefs, "mode": core.VOICE_MODE}
        self.app.apply_mode()
        self.app.handle_key("h")
        self.assertEqual([core.parse_relay(l) for l in self.lines()], [{"kind": "hush"}])

    def test_h_in_notification_mode_appends_nothing(self):
        self.app.handle_key("h")
        self.assertEqual(self.lines(), [])

    def test_switching_to_notification_mode_appends_one_hush_line_and_to_voice_none(self):
        self.app.handle_key("m")
        self.assertEqual(self.lines(), [])
        self.app.handle_key("m")
        self.assertEqual([core.parse_relay(l) for l in self.lines()], [{"kind": "hush"}])

    def test_a_connected_laptop_takes_the_unavailable_tag_off_the_bar(self):
        self.app.poller.notifier.state = core.HELPER_UNAVAILABLE
        self.app.tmux.options[RELAY_OPT] = str(time.time())
        sidebar.App.housekeep(self.app, False)
        self.assertNotIn("notifications unavailable", self.bar())
        self.app.tmux.options[RELAY_OPT] = str(time.time() - 60)
        sidebar.App.housekeep(self.app, False)
        self.assertIn("notifications unavailable", self.bar())

    def test_a_connected_laptop_puts_its_host_on_the_bar(self):
        now = time.time()
        self.app.tmux.options[RELAY_OPT] = str(now)
        self.app.tmux.options[RELAY_HOST_OPT] = core.relay_host_value("myserver", now)
        sidebar.App.housekeep(self.app, False)
        self.assertIn("⇄ myserver", self.bar())
        self.app.tmux.options[RELAY_OPT] = str(now - 60)
        self.app.tmux.options[RELAY_HOST_OPT] = core.relay_host_value("myserver", now - 60)
        sidebar.App.housekeep(self.app, False)
        self.assertNotIn("⇄", self.bar())

    def test_no_host_option_no_mark(self):
        self.app.tmux.options[RELAY_OPT] = str(time.time())
        sidebar.App.housekeep(self.app, False)
        self.assertNotIn("⇄", self.bar())


class ModeKey(AppCase):
    def prefs_on_disk(self):
        return json.loads(self.app.prefs_file.read_text(encoding="utf-8"))

    def test_a_new_panel_starts_in_notification_mode(self):
        self.assertEqual(core.notice_mode(self.app.prefs), core.NOTIFICATION_MODE)
        self.assertEqual(self.app.poller.notices.mode, core.NOTIFICATION_MODE)
        self.assertEqual(self.app.poller.notifier.mode, core.NOTIFICATION_MODE)
        self.assertTrue(self.app.poller.voice.muted)

    def test_m_switches_to_voice_saves_it_and_a_new_panel_keeps_it(self):
        self.app.handle_key("m")
        self.assertEqual(self.prefs_on_disk()["mode"], "voice")
        self.assertNotIn("voice", self.prefs_on_disk())
        self.assertEqual(self.app.poller.notices.mode, core.VOICE_MODE)
        self.assertEqual(self.app.notice, "Voice mode")
        again = sidebar.App(FakeScr(), tmux=ClientTmux(), claude="claude")
        self.assertEqual(again.poller.notices.mode, core.VOICE_MODE)
        self.assertFalse(again.poller.voice.muted)

    def test_m_again_returns_to_notification_mode(self):
        self.app.handle_key("m")
        self.app.handle_key("m")
        self.assertEqual(self.prefs_on_disk()["mode"], "notification")
        self.assertEqual(self.app.poller.notices.mode, core.NOTIFICATION_MODE)
        self.assertEqual(self.app.notice, "Notification mode")

    def test_m_sets_the_voice_muted_flag_both_ways(self):
        self.app.poller.voice = sidebar.Voice()
        self.app.poller.voice.muted = True
        self.app.handle_key("m")
        self.assertFalse(self.app.poller.voice.muted)
        self.app.handle_key("m")
        self.assertTrue(self.app.poller.voice.muted)

    def test_entering_notification_mode_silences_the_voice_and_leaving_it_does_not(self):
        self.app.handle_key("m")
        self.assertEqual(self.silenced, [])
        self.app.handle_key("m")
        self.assertEqual(len(self.silenced), 1)

    def test_entering_notification_mode_asks_the_notifier_for_the_permission(self):
        self.app.handle_key("m")
        self.assertEqual(self.app.poller.notifier.asked, 0)
        self.app.handle_key("m")
        self.assertEqual(self.app.poller.notifier.asked, 1)

    def test_the_help_has_the_m_line(self):
        self.assertIn(("m", "voice mode or notification mode"), sidebar.HELP)

    def test_a_saved_old_voice_setting_starts_in_its_mode(self):
        for voice, mode in ((True, core.VOICE_MODE), (False, core.NOTIFICATION_MODE)):
            with self.subTest(voice=voice):
                self.app.prefs_file.parent.mkdir(parents=True, exist_ok=True)
                self.app.prefs_file.write_text(json.dumps({"voice": voice}), encoding="utf-8")
                again = sidebar.App(FakeScr(), tmux=ClientTmux(), claude="claude")
                self.assertEqual(again.poller.notices.mode, mode)

    def test_m_while_typing_in_the_find_box_is_text(self):
        self.app.handle_key("/")
        self.app.handle_key("m")
        self.assertEqual(core.notice_mode(self.app.prefs), core.NOTIFICATION_MODE)

    def test_the_bar_shows_the_mode_word_only_in_voice_mode(self):
        plain = self.bar()
        self.assertNotIn("voice", plain)
        self.assertNotIn("notifications blocked", plain)
        self.assertNotIn("notifications unavailable", plain)
        self.app.handle_key("m")
        self.assertIn("voice", self.bar())
        self.app.handle_key("m")
        self.assertEqual(self.bar(), plain)

    def test_the_bar_shows_blocked_or_unavailable_only_in_notification_mode(self):
        for state, label in ((core.HELPER_BLOCKED, "notifications blocked"), (core.HELPER_UNAVAILABLE, "notifications unavailable")):
            with self.subTest(state=state):
                self.app.poller.notifier.state = state
                self.assertIn(label, self.bar())
                self.app.handle_key("m")
                self.assertNotIn(label, self.bar())
                self.assertIn("voice", self.bar())
                self.app.handle_key("m")
        self.app.poller.notifier.state = core.HELPER_PENDING
        self.assertNotIn("notifications blocked", self.bar())
        self.assertNotIn("notifications unavailable", self.bar())


class OpenTmux(HushTmux):
    """Session options by name; `fail` makes every read raise. `ttys` are the attached clients' ttys."""

    def __init__(self):
        super().__init__()
        self.options = {}
        self.fail = False
        self.ttys = []
        self.tty_reads = 0

    def client_ttys(self):
        self.tty_reads += 1
        return list(self.ttys)

    def session_option(self, name):
        if self.fail:
            raise TmuxError("gone")
        return self.options.get(name, "")

    def set_session_option(self, name, value):
        self.options[name] = value

    def clear_session_option_if(self, name, value, session=SESSION):
        if self.options.get(name) == value:
            self.options[name] = ""


class OpenRequests(AppCase):
    SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def setUp(self):
        super().setUp()
        self.no_scheme()
        self.app.tmux = OpenTmux()
        self.app.me = "%1"

    def housekeep(self):
        sidebar.App.housekeep(self.app, False)

    def listed(self, *sids):
        self.poll(*[session(sid, "/p/alpha") for sid in sids])
        self.app.refresh()

    def test_an_open_request_switches_to_that_session_and_clears_the_option(self):
        self.listed(self.SID, "other")
        self.app.tmux.options[OPEN_OPT] = self.SID
        self.housekeep()
        self.assertEqual(self.opened, [(self.SID, core.OPEN)])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "")
        self.assertEqual(self.app.cursor, ("session", self.SID))
        self.housekeep()
        self.assertEqual(len(self.opened), 1)

    def test_a_second_click_landing_before_the_clear_survives_for_the_next_read(self):
        self.listed(self.SID, "other")
        self.app.tmux.options[OPEN_OPT] = self.SID
        real = self.app.tmux.clear_session_option_if

        def clearing(name, value, session=SESSION):
            self.app.tmux.options[OPEN_OPT] = "second"
            real(name, value, session)

        with mock.patch.object(self.app.tmux, "clear_session_option_if", clearing):
            self.housekeep()
        self.assertEqual(self.opened, [(self.SID, core.OPEN)])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "second")

    def test_an_open_request_written_before_the_first_read_is_honoured(self):
        self.app.tmux.options[OPEN_OPT] = self.SID
        self.housekeep()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], self.SID)
        self.listed(self.SID)
        self.housekeep()
        self.assertEqual(self.opened, [(self.SID, core.OPEN)])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "")

    def test_an_open_request_for_an_unknown_session_says_so_and_switches_nothing(self):
        self.listed("other")
        self.app.tmux.options[OPEN_OPT] = self.SID
        self.housekeep()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.notice, sidebar.OPEN_GONE)
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "")

    def test_a_garbage_request_is_cleared_and_opens_nothing(self):
        self.listed(self.SID)
        self.app.tmux.options[OPEN_OPT] = "-x; rm"
        self.housekeep()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "")

    def test_an_open_request_for_a_closing_session_does_not_resume_it(self):
        self.listed(self.SID)
        self.app.closing = {self.SID: True}
        self.app.tmux.options[OPEN_OPT] = self.SID
        self.housekeep()
        self.assertEqual(self.opened, [])
        self.assertIn("closing", self.app.notice.lower())

    def test_a_failed_read_changes_nothing(self):
        self.listed(self.SID)
        self.app.tmux.options[OPEN_OPT] = self.SID
        self.app.tmux.fail = True
        self.housekeep()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], self.SID)

    def test_a_request_marks_that_sessions_history_read(self):
        self.app.switch_to = lambda sid, trigger, checked=None: sidebar.App.switch_to(self.app, sid, trigger, checked)

        class Held(OpenTmux):
            def panes(inner):
                return [Pane("%1", "@1", False, "sidebar", "", 40, 141), Pane("%2", "@1", False, "", "", 100, 141),
                        Pane("%3", "@2", False, "", self.SID, 100, 141, agent="claude")]

        self.app.tmux = Held()
        self.listed(self.SID)
        self.app.poller.notices.outbox = [(self.SID, core.NOTICE_DONE, "x · alpha", "Done.", time.monotonic())]
        self.app.refresh()
        self.app.tmux.options[OPEN_OPT] = self.SID
        self.housekeep()
        self.assertEqual([e.read for e in self.app.history], [True])


class OpenBringsTheItermTabForward(AppCase):
    """On a click, an iTerm2 client gets StealFocus written straight to its tty; nothing else does."""

    SID = OpenRequests.SID

    def setUp(self):
        super().setUp()
        self.app.tmux = OpenTmux()
        self.app.me = "%1"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tty = os.path.join(tmp.name, "ttys-fake")
        Path(self.tty).write_bytes(b"")
        self.app.tmux.ttys = [self.tty]
        self.poll(session(self.SID, "/p/alpha"))
        self.app.refresh()

    def click(self, terminal):
        if terminal is not None:
            self.app.tmux.options[TERMINAL_OPT] = terminal
        self.app.tmux.options[OPEN_OPT] = self.SID
        sidebar.App.housekeep(self.app, False)

    def test_iterm2_gets_exactly_the_steal_focus_sequence_on_the_client_tty(self):
        self.click("com.googlecode.iterm2")
        self.assertEqual(self.opened, [(self.SID, core.OPEN)])
        self.assertEqual(Path(self.tty).read_bytes(), b"\x1b]1337;StealFocus\x07")

    def test_terminal_app_or_no_recorded_terminal_gets_nothing(self):
        for terminal in ("com.apple.Terminal", None):
            with self.subTest(terminal=terminal):
                self.app.tmux.options.pop(TERMINAL_OPT, None)
                self.click(terminal)
                self.assertEqual(Path(self.tty).read_bytes(), b"")
                self.assertEqual(self.app.tmux.tty_reads, 0)
        self.assertEqual(self.opened, [(self.SID, core.OPEN)] * 2)

    def test_a_tty_that_cannot_be_written_does_not_break_the_open(self):
        self.app.tmux.ttys = [os.path.join(self.tty, "missing"), self.tty]
        with mock.patch.object(sidebar.os, "write", side_effect=OSError("EAGAIN")):
            self.click("com.googlecode.iterm2")
        self.assertEqual(self.opened, [(self.SID, core.OPEN)])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "")

    def test_a_failed_client_lookup_does_not_break_the_open(self):
        def gone():
            raise TmuxError("gone")

        self.app.tmux.client_ttys = gone
        self.click("com.googlecode.iterm2")
        self.assertEqual(self.opened, [(self.SID, core.OPEN)])
        self.assertEqual(self.app.tmux.options[OPEN_OPT], "")


class ModeDelivery(AppCase):
    """Both modes add the same history entries, whatever the notifier's state; notification mode never speaks."""

    def setUp(self):
        super().setUp()
        self.app.poller.transcripts.found = {}
        notices = self.app.poller.notices
        notices.want([replace(session("a", "/p/alpha", "done"), name="Alpha")])

    def deliver(self):
        notices = self.app.poller.notices
        notices.add(core.Notice("a", core.NOTICE_DONE, "Alpha", "/p/alpha", time.monotonic() - 10, True))
        with mock.patch.object(sidebar, "announce", lambda *args: (None, "no haiku")), \
                mock.patch.object(sidebar, "summary_scratch", lambda: "/tmp/emx-test-scratch"):
            self.assertTrue(notices.step())
        self.app.take_notices()

    def test_a_blocked_notifier_never_falls_back_to_voice(self):
        self.app.poller.notifier.state = core.HELPER_BLOCKED
        self.deliver()
        self.assertEqual(self.spoken, [])
        self.assertEqual(len(self.app.poller.notifier.posts), 1)
        self.assertEqual([e.text for e in self.app.history], ["Alpha, in alpha, is done."])

    def test_every_notice_lands_in_the_history_in_both_modes(self):
        for mode in core.MODES:
            for state in core.HELPER_STATES:
                with self.subTest(mode=mode, state=state):
                    self.app.history = []
                    self.app.prefs = {**self.app.prefs, "mode": mode}
                    self.app.apply_mode()
                    self.app.poller.notifier.state = state
                    self.deliver()
                    self.assertEqual(len(self.app.history), 1)


class AppBuildsNoHelper(unittest.TestCase):
    def test_constructing_the_app_runs_no_helper_command(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp, "TMUX_PANE": "%1",
                                          "EMX_NOTIFY_HOME": os.path.join(tmp, "notify")}), \
                mock.patch.object(sidebar, "Palette", FakePalette), \
                mock.patch.object(sidebar.notify.subprocess, "run", side_effect=AssertionError("run")) as run, \
                mock.patch.object(sidebar.notify.subprocess, "Popen", side_effect=AssertionError("Popen")) as popen, \
                mock.patch.object(sidebar.notify, "ensure_built", side_effect=AssertionError("ensure_built")) as built:
            app = sidebar.App(FakeScr(), tmux=ClientTmux(), claude="claude")
        self.assertIsInstance(app.poller.notifier, sidebar.Notifier)
        self.assertFalse(app.poller.notifier.is_alive())
        run.assert_not_called()
        popen.assert_not_called()
        built.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(tmp, "notify")))


class Routines(AppCase):
    def setUp(self):
        super().setUp()
        self.scr = TallScr()
        self.app.scr = self.scr
        self.reads = []
        self.gate = threading.Event()
        self.gate.set()
        self.answer = ([core.RoutineView("backup-nightly", "passed", "done", ((1_800_000_000.0, core.PASSED, ""),), None)], None)

        def read(now):
            self.reads.append(now)
            self.gate.wait(5)
            return self.answer

        patch = mock.patch.object(sidebar.routines, "read", read)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.gate.set)

    def settle(self):
        reader = self.app.routines_reader
        if reader is not None:
            reader.join(5)
        self.app.refresh()

    def drawn(self):
        self.app.draw()
        return [t.strip() for t in self.scr.text()]

    def test_R_opens_the_view_and_starts_one_reader_and_R_or_Esc_closes_it(self):
        self.app.handle_key("R")
        self.assertEqual(self.app.routines_view, 0)
        self.settle()
        self.assertEqual(len(self.reads), 1)
        self.app.handle_key("R")
        self.assertIsNone(self.app.routines_view)
        self.app.handle_key("R")
        self.app.handle_key("\x1b")
        self.assertIsNone(self.app.routines_view)

    def test_open_close_open_with_the_first_reader_alive_starts_no_second_reader(self):
        self.gate.clear()
        self.app.handle_key("R")
        self.app.handle_key("R")
        self.app.handle_key("R")
        self.gate.set()
        self.settle()
        self.assertEqual(len(self.reads), 1)

    def test_enter_right_and_clicks_open_no_session_behind_the_view(self):
        self.poll(session("a", "/p/alpha"))
        self.app.refresh()
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "a"))
        activated = []
        self.app.activate = activated.append
        self.app.handle_key("R")
        for key in ("\n", curses.KEY_RIGHT):
            self.app.handle_key(key)
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.app.handle_key(curses.KEY_MOUSE)
        self.assertEqual((self.opened, activated), ([], []))
        self.assertEqual(self.app.routines_view, 0)

    def test_the_panel_is_busy_while_the_view_is_open(self):
        self.assertFalse(self.app.update_busy())
        self.app.handle_key("R")
        self.assertTrue(self.app.update_busy())

    def test_the_frame_shows_the_title_and_routine_names_but_no_session(self):
        self.poll(session("a", "/p/alpha"))
        self.app.refresh()
        self.app.handle_key("R")
        self.settle()
        drawn = self.drawn()
        self.assertIn(sidebar.ROUTINES_TITLE, drawn)
        self.assertTrue(any(t.startswith("backup-nightly") for t in drawn), drawn)
        self.assertFalse(any("alpha" in t for t in drawn), drawn)
        self.assertNotIn(sidebar.ROUTINES_HINT, drawn)
        self.assertEqual(self.app.bar_hints(), (sidebar.ROUTINES_HINT, "Esc back"))

    def test_it_says_reading_until_the_first_answer(self):
        self.gate.clear()
        self.app.handle_key("R")
        self.assertIn(sidebar.ROUTINES_LOADING, self.drawn())

    def test_a_journal_error_shows_on_its_routine(self):
        self.answer = ([core.RoutineView("backup-nightly", "passed", "done", ((1_800_000_000.0, core.PASSED, ""),), None,
                                         error="No journal files were found.")], None)
        self.app.handle_key("R")
        self.settle()
        drawn = self.drawn()
        self.assertIn("No journal files were found.", drawn)
        self.assertTrue(any(t.startswith("backup-nightly") for t in drawn), drawn)

    def test_a_failed_second_listing_shows_above_the_routines(self):
        message = "Switched-off timers couldn't be read: x"
        self.answer = (self.answer[0], message)
        self.app.scr = self.scr = TallScr(width=300)
        self.app.handle_key("R")
        self.settle()
        drawn = self.drawn()
        error_at = next(i for i, t in enumerate(drawn) if message in t)
        routine_at = next(i for i, t in enumerate(drawn) if t.startswith("backup-nightly"))
        self.assertLess(error_at, routine_at)

    def test_a_failed_second_listing_with_no_loaded_timers_is_only_the_error(self):
        message = "Switched-off timers couldn't be read: x"
        self.answer = ([], message)
        self.app.scr = self.scr = TallScr(width=300)
        self.app.handle_key("R")
        self.settle()
        drawn = self.drawn()
        self.assertTrue(any(message in t for t in drawn))
        self.assertNotIn(sidebar.ROUTINES_NONE, drawn)

    def test_a_machine_without_systemd_gets_the_one_line(self):
        self.answer = (None, sidebar.routines.NO_SYSTEMD)
        self.app.handle_key("R")
        self.settle()
        drawn = " ".join(self.drawn())
        self.assertIn("Routines are read from systemd user timers", drawn)

    def test_no_timers_says_so_and_names_no_file(self):
        self.answer = ([], None)
        self.app.scr = self.scr = TallScr(width=300)
        self.app.handle_key("R")
        self.settle()
        drawn = self.drawn()
        self.assertIn(sidebar.ROUTINES_NONE, drawn)
        self.assertFalse(any(os.environ["XDG_CONFIG_HOME"] in t for t in drawn), drawn)

    def test_the_view_rereads_after_the_interval_and_only_while_open(self):
        self.app.handle_key("R")
        self.settle()
        self.assertEqual(len(self.reads), 1)
        with mock.patch.object(sidebar.time, "monotonic", return_value=time.monotonic() + sidebar.ROUTINES_SECONDS + 1):
            self.app.refresh()
            self.settle()
            self.assertEqual(len(self.reads), 2)
            self.app.handle_key("R")
            self.app.refresh()
            self.settle()
        self.assertEqual(len(self.reads), 2)

    def test_an_answer_that_lands_after_the_view_closed_is_dropped(self):
        self.gate.clear()
        self.app.handle_key("R")
        self.app.handle_key("R")
        self.gate.set()
        self.settle()
        self.assertIsNone(self.app.routine_views)

    def test_the_help_has_the_R_line(self):
        self.assertTrue(any(key == "R" for key, _ in sidebar.HELP))


class ViewTmux(ClientTmux):
    """One view (`view`) with its panel (%1) and slot (`slot`), session KEY_A's own pane (%5, hidden) unless `held` is None; records what changes."""

    socket = "emx-test-views"

    def __init__(self, view=SESSION, slot=None, held="default", attached=1):
        super().__init__((attached, "", "%2", None))
        self.view = view
        self.slot_pane = slot or Pane("%2", "@1", False, "placeholder", "", 100, 141)
        self.held = Pane("%5", "@5", False, "", KEY_A, 100, 100, agent="claude") if held == "default" else held
        self.options = {}
        self.calls = []
        self.dead_output = ("", "")

    def clear_history(self, pane):
        self.calls.append(("clear_history", pane))

    def dead_pane_output(self, pane):
        self.calls.append(("dead_pane_output", pane))
        if isinstance(self.dead_output, Exception):
            raise self.dead_output
        return self.dead_output

    def panes(self, session=SESSION):
        listed = [Pane("%1", "@1", False, "sidebar", "", 40, 141), self.slot_pane]
        return listed + ([self.held] if self.held is not None else [])

    def view_option(self, name):
        return self.options.get(name, "")

    def set_view_option(self, name, value):
        self.calls.append(("set_view_option", name, value))
        self.options[name] = value

    def respawn(self, pane, command, cwd=None):
        self.calls.append(("respawn", pane, command))

    def set_pane_option(self, pane, name, value):
        self.calls.append(("set_pane_option", pane, name, value))

    def unset_pane_option(self, pane, name):
        self.calls.append(("unset_pane_option", pane, name))

    def link_agent(self, window, key, inert):
        self.calls.append(("link_agent", window, key))

    def claim_width(self, slot, key):
        self.calls.append(("claim_width", slot, key))

    def viewer_command(self, key):
        return f"viewer {key}"

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FollowerPanel(AppCase):
    """The panel of a view other than `emx` itself lists for itself but leaves Haiku, the notices, the voice and the updates to the primary."""

    def follower(self, **kwargs):
        with mock.patch.object(emx, "ROOT", "/tmp/emx-test-root"):
            return sidebar.App(self.scr, tmux=ViewTmux(view="emx-view-1", **kwargs), claude="claude")

    def test_a_follower_starts_no_haiku_notice_voice_or_notifier_worker_and_no_updater(self):
        app = self.follower()
        self.assertFalse(app.primary)
        self.assertIsNone(app.updater)
        poller = app.poller
        started = []
        for worker in (poller.resolver, poller.transcripts, poller.summaries, poller.asks, poller.notifier, poller.voice, poller.notices):
            worker.start = lambda w=worker: started.append(w)
        with mock.patch.object(sidebar, "POLL_SECONDS", 0.01):
            threading.Thread(target=poller.run, daemon=True).start()
            deadline = time.monotonic() + 5
            while len(poller.started) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
        self.assertEqual(started, [poller.resolver, poller.transcripts])
        self.assertEqual(poller.started, [poller.resolver, poller.transcripts])

    def test_a_follower_serves_no_phone_page_and_the_primary_serves_one(self):
        from emx import phone

        follower = self.follower()
        with mock.patch.object(phone, "PhoneService") as service:
            follower.start_phone()
        service.assert_not_called()
        self.assertIsNone(follower.phone)
        primary = sidebar.App(self.scr, tmux=ViewTmux(), claude="claude")
        with mock.patch.object(phone, "PhoneService") as service:
            primary.start_phone()
        service.assert_called_once()
        sources = service.call_args.kwargs["sources"]
        self.assertEqual(sources.transcripts, primary.poller.transcripts.snapshot)
        self.assertEqual(sources.placements, primary.poller.resolver.snapshot)
        self.assertEqual(sources.transcript_path, primary.poller.transcripts.path)
        self.assertEqual(sources.claude, "claude")
        first = sources.history()
        primary.history = [core.Entry("a", core.NOTICE_DONE, "t", "x", 1.0)]
        self.assertEqual((first, sources.history()), ([], primary.history))
        service.return_value.start.assert_called_once_with()
        self.assertIs(primary.phone, service.return_value)

    def test_saving_prefs_holds_the_lock_the_phone_page_shares(self):
        app = sidebar.App(self.scr, tmux=ViewTmux(), claude="claude")
        done = threading.Event()
        worker = threading.Thread(target=lambda: (app.save_prefs(), done.set()), daemon=True)
        with sidebar.PREFS_LOCK:
            worker.start()
            self.assertFalse(done.wait(0.3))
        self.assertTrue(done.wait(5))

    def test_the_panel_waits_for_a_post_from_the_phone_before_an_update(self):
        app = sidebar.App(self.scr, tmux=ViewTmux(), claude="claude")
        self.assertFalse(app.update_busy())
        app.phone = mock.Mock(busy=lambda: True)
        self.assertTrue(app.update_busy())

    def test_the_phone_pages_notices_reach_the_status_line(self):
        app = sidebar.App(self.scr, tmux=ViewTmux(), claude="claude")
        notices = [("Phone page: http://box:8787/", None), ("Phone page off: Tailscale isn't connected. Run: sudo tailscale up", 15)]
        app.phone = mock.Mock(take_notice=lambda: notices.pop(0) if notices else None)
        app.check_phone()
        self.assertEqual((app.notice, app.notice_until), ("Phone page: http://box:8787/", None))
        app.check_phone()
        self.assertEqual(app.notice, "Phone page off: Tailscale isn't connected. Run: sudo tailscale up")
        self.assertIsNotNone(app.notice_until)
        app.check_phone()
        self.assertEqual(app.notice, "Phone page off: Tailscale isn't connected. Run: sudo tailscale up")

    def test_the_primary_starts_every_worker(self):
        with mock.patch.object(emx, "ROOT", "/tmp/emx-test-root"):
            app = sidebar.App(self.scr, tmux=ViewTmux(), claude="claude")
        self.assertTrue(app.primary)
        self.assertIsNotNone(app.updater)
        poller = app.poller
        self.assertEqual(poller.workers(), [poller.resolver, poller.transcripts, poller.summaries, poller.asks,
                                            poller.notifier, poller.voice, poller.notices])

    def test_a_follower_shows_the_lines_the_primary_wrote(self):
        primary = sidebar.App(self.scr, tmux=ViewTmux(), claude="claude")
        primary.poller.summaries.take = lambda: {KEY_A: core.Summary("Fixing the parser", core.WORKING, "the excerpt", 5.0)}
        primary.take_summaries()
        path = primary.state / sidebar.SUMMARIES_FILE
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {KEY_A: {"text": "Fixing the parser", "state": core.WORKING}})
        app = self.follower()
        app.poller.summaries.take = lambda: self.fail("a follower asked its own Haiku")
        app.take_summaries()
        self.assertEqual((app.summaries[KEY_A].text, app.summaries[KEY_A].category), ("Fixing the parser", core.WORKING))

    def test_a_follower_queues_no_notice(self):
        app = self.follower()
        app.poller.result = ([session("a", "/p/alpha", "working")], None)
        app.refresh()
        app.poller.result = ([session("a", "/p/alpha", "done")], None)
        app.refresh()
        self.assertEqual(list(app.poller.notices.pending), [])


class SlotTending(AppCase):
    """A view's slot shows its session through a viewer; a view other than `emx` parks it while no client is attached and shows it again on the next attach, and `emx` keeps it."""

    VIEWER = Pane("%2", "@1", False, "viewer", "", 100, 141, view=KEY_A)

    def tend(self, tmux):
        self.app.tmux = tmux
        self.app.me = "%1"
        sidebar.App.tend_slot(self.app, tmux.slot_pane)
        return tmux.calls

    def parked(self):
        return [
            ("respawn", "%2", sidebar.self_command("blank", sidebar.PLACEHOLDER_TEXT)),
            ("set_pane_option", "%2", sidebar.ROLE, "placeholder"), ("unset_pane_option", "%2", sidebar.VIEW_OPT),
        ]

    def test_a_view_other_than_emx_with_no_client_parks_its_session_and_remembers_it(self):
        self.app.primary = False
        self.app.attached = 1
        calls = self.tend(ViewTmux(view="emx-view-1", slot=self.VIEWER, attached=0))
        self.assertEqual(calls, [("set_view_option", sidebar.REOPEN_OPT, KEY_A)] + self.parked())

    def test_the_home_view_with_no_client_keeps_showing_its_session(self):
        self.assertTrue(self.app.primary)
        self.app.attached = 1
        self.assertEqual(self.tend(ViewTmux(slot=self.VIEWER, attached=0)), [])

    def test_the_laptop_coming_back_claims_the_sessions_width_with_no_restart_of_the_viewer(self):
        self.app.attached = 0
        self.assertEqual(self.tend(ViewTmux(slot=self.VIEWER, attached=1)), [("claim_width", "%2", KEY_A)])

    def test_another_views_device_coming_back_claims_nothing(self):
        self.app.primary = False
        self.app.attached = 0
        self.assertEqual(self.tend(ViewTmux(view="emx-view-1", slot=self.VIEWER, attached=1)), [])

    def test_the_next_attach_shows_it_again_and_clears_the_note(self):
        tmux = ViewTmux(attached=1)
        tmux.options[sidebar.REOPEN_OPT] = KEY_A
        self.app.attached = 0
        calls = self.tend(tmux)
        self.assertEqual(calls, [
            ("set_view_option", sidebar.REOPEN_OPT, ""), ("link_agent", "@5", KEY_A), ("clear_history", "%2"), ("respawn", "%2", f"viewer {KEY_A}"),
            ("set_pane_option", "%2", sidebar.ROLE, "viewer"), ("set_pane_option", "%2", sidebar.VIEW_OPT, KEY_A),
        ])

    def test_at_panel_start_with_a_client_attached_the_note_is_honoured(self):
        tmux = ViewTmux(attached=1)
        tmux.options[sidebar.REOPEN_OPT] = KEY_A
        self.app.attached = None
        self.assertIn(("respawn", "%2", f"viewer {KEY_A}"), self.tend(tmux))

    def test_a_note_for_a_session_with_no_pane_any_more_starts_nothing(self):
        tmux = ViewTmux(attached=1, held=None)
        tmux.options[sidebar.REOPEN_OPT] = KEY_A
        self.app.attached = None
        self.assertEqual(self.tend(tmux), [("set_view_option", sidebar.REOPEN_OPT, "")])

    def test_while_attached_a_note_is_not_read_again(self):
        tmux = ViewTmux(attached=1)
        tmux.options[sidebar.REOPEN_OPT] = KEY_A
        self.app.attached = 1
        self.assertEqual(self.tend(tmux), [])

    def test_a_viewer_whose_sessions_pane_is_gone_or_that_exited_gets_the_placeholder(self):
        for tmux in (ViewTmux(slot=self.VIEWER, held=None), ViewTmux(slot=replace(self.VIEWER, dead=True))):
            with self.subTest(held=tmux.held, dead=tmux.slot_pane.dead):
                self.app.attached = 1
                looked = [] if tmux.held is None else [("dead_pane_output", "%2")]
                self.assertEqual(self.tend(tmux), (looked if tmux.slot_pane.dead else []) + self.parked())

    def dead_viewer(self, output, held="default"):
        tmux = ViewTmux(slot=replace(self.VIEWER, dead=True), held=held)
        tmux.dead_output = output
        self.app.attached = 1
        return tmux

    def test_a_viewer_that_died_while_its_session_lives_says_its_last_line(self):
        calls = self.tend(self.dead_viewer(("zsh:1: emx-a-x: not found\n F\n", " F")))
        self.assertEqual(self.app.notice, "The viewer closed: zsh:1: emx-a-x: not found")
        self.assertIs(self.app.notice_error, True)
        self.assertEqual(calls, [("dead_pane_output", "%2")] + self.parked())

    def test_a_viewer_whose_session_has_gone_parks_without_a_notice(self):
        self.app.notice = "before"
        calls = self.tend(self.dead_viewer(("boom\n", ""), held=None))
        self.assertEqual((calls, self.app.notice), (self.parked(), "before"))

    def test_a_dead_viewers_output_that_cannot_be_read_or_is_empty_parks_without_a_notice(self):
        for output in (TmuxError("gone"), (" F\n", " F")):
            with self.subTest(output=output):
                self.app.notice = "before"
                calls = self.tend(self.dead_viewer(output))
                self.assertEqual((calls, self.app.notice), ([("dead_pane_output", "%2")] + self.parked(), "before"))

    def test_showing_a_session_clears_the_slots_history_just_before_its_viewer_starts(self):
        tmux = ViewTmux()
        self.app.tmux = tmux
        sidebar.App.show(self.app, tmux.slot_pane, KEY_A, "@5")
        calls = tmux.calls
        self.assertEqual(calls[calls.index(("clear_history", "%2")) + 1], ("respawn", "%2", f"viewer {KEY_A}"))

    def test_a_viewer_of_a_live_session_with_a_client_is_left_alone(self):
        self.app.attached = 1
        self.assertEqual(self.tend(ViewTmux(slot=self.VIEWER)), [])


PHONE_WIDTH = 48
HELP_2_10_0 = (
    ("→ Enter", "open the session and go to it"),
    ("←", "back here, from an empty prompt"),
    ("↑↓ j k", "move"),
    ("Enter click", "fold a group; a click opens a session"),
    ("space", "peek at what the session asks or did; ↑↓ next, space or Esc close"),
    ("n", "new session where the cursor is; Tab there picks another folder"),
    ("", "in its folder list ^t stars a favourite; ★ and recent folders list first"),
    ("^x", "interrupt a working session; twice closes it"),
    ("", "on a project header, twice closes the group"),
    ("^f /", "find a session by name or folder"),
    ("^t", "pin to the top (in emx; doesn't keep it running)"),
    ("^r", "rename, via /rename in the session; Tab there suggests a name"),
    ("^s", "by project / by state"),
    ("N", "notifications: the last 20; Enter opens one"),
    ("R", "routines: what ran in the past 24 hours; R or Esc back"),
    ("m", "voice mode or notification mode"),
    ("h", "stop the voice now (voice mode)"),
    ("s", "panel to the other side"),
    ("< >", "narrower / wider panel"),
    ("^\\", "jump to main and back"),
    ("^q", "hide the panel; ^q again, ^\\ or ← brings it back"),
    ("q", "detach; sessions keep running"),
    ("?", "close this help"),
)


class PhoneTmux(ViewTmux):
    """A phone's view, 48 columns: the panel (%1) and the slot (%2) with tmux's active and zoomed flags, changed as tmux would by the calls the panel makes."""

    def __init__(self, view="emx-view-1", slot=None, panel_active=True, zoomed=True, width=PHONE_WIDTH, **kwargs):
        super().__init__(view=view, slot=slot, **kwargs)
        self.width = width
        self.active = "%1" if panel_active else "%2"
        self.zoomed = zoomed
        self.slot_pane = replace(self.slot_pane, window_width=width)

    def panes(self, session=SESSION):
        sidebar_pane = Pane("%1", "@1", False, "sidebar", "", 24 if not self.zoomed or self.active != "%1" else self.width,
                            self.width, self.active == "%1", False, self.zoomed)
        slot = replace(self.slot_pane, active=self.active == "%2", zoomed=self.zoomed, window_width=self.width)
        return [sidebar_pane, slot] + ([self.held] if self.held is not None else [])

    def window_panes(self, pane):
        return self.panes()[:2]

    def show_alone(self, pane):
        self.calls.append(("show_alone", pane))
        self.active, self.zoomed = pane, True

    def toggle_zoom(self, pane):
        self.calls.append(("toggle_zoom", pane))
        if self.zoomed:
            self.zoomed = False
        else:
            self.active, self.zoomed = pane, True

    def select(self, pane):
        self.calls.append(("select", pane))
        self.active, self.zoomed = pane, False

    def respawn(self, pane, command, cwd=None):
        super().respawn(pane, command, cwd)
        if pane == "%2":
            viewer = command.startswith("viewer ")
            self.slot_pane = replace(self.slot_pane, role="viewer" if viewer else "placeholder", view=command[7:] if viewer else "")

    def resize_width(self, pane, width):
        self.calls.append(("resize_width", pane, width))


class PhonePanel(AppCase):
    """A narrow view shows its panel or a session, each taking the whole window, and never writes the laptop's prefs."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(height=22, width=PHONE_WIDTH)
        self.app.scr = self.scr
        self.app.me = "%1"
        self.app.primary = False
        self.app.bound = True
        self.app.ground_reset = True
        self.app.window_width = PHONE_WIDTH

    def housekeep(self, tmux):
        self.app.tmux = tmux
        sidebar.App.housekeep(self.app, False)
        return tmux.calls

    def test_the_panel_unzoomed_on_a_phone_takes_the_whole_window(self):
        tmux = PhoneTmux(zoomed=False)
        self.assertIn(("show_alone", "%1"), self.housekeep(tmux))
        self.assertTrue(self.app.narrow)

    def test_back_on_the_panel_the_slot_gives_up_its_session(self):
        tmux = PhoneTmux(slot=Pane("%2", "@1", False, "viewer", "", 24, PHONE_WIDTH, view=KEY_A))
        calls = self.housekeep(tmux)
        self.assertIn(("respawn", "%2", sidebar.self_command("blank", sidebar.PLACEHOLDER_TEXT)), calls)
        self.assertEqual(self.app.main_session(), "")
        self.assertFalse(self.app.slot_active)

    def test_a_session_shown_takes_the_whole_window_and_the_bar_says_back(self):
        tmux = PhoneTmux(slot=Pane("%2", "@1", False, "viewer", "", 24, PHONE_WIDTH, view=KEY_A), panel_active=False, zoomed=False)
        self.assertIn(("show_alone", "%2"), self.housekeep(tmux))
        self.assertTrue(self.app.slot_active)
        self.app.sessions = [core.Session(KEY_A, "phone-view", "/p", "blocked", "", "", 1.0, True)]
        self.app.loaded = True
        text = self.bar(PHONE_WIDTH)
        self.assertTrue(text.startswith(" ‹ sessions  phone-view · Needs input"), text)
        self.assertIn("#[range=user|back]", self.app.bar.pushed[-1])

    def test_with_the_keyboard_up_the_session_bar_is_one_short_line(self):
        self.app.slot_active = True
        self.app.main_pane = Pane("%2", "@1", False, "viewer", "", 48, PHONE_WIDTH, view=KEY_A)
        self.app.sessions = [core.Session(KEY_A, "phone-view", "/p", "blocked", "", "", 1.0, True)]
        self.app.loaded = True
        self.scr.size = (core.SHORT_BELOW - 1, PHONE_WIDTH)
        text = self.bar(PHONE_WIDTH)
        self.assertTrue(text.startswith(" ‹ phone-view "), text)
        self.assertNotIn("Needs input", text)
        self.scr.size = (core.SHORT_BELOW, PHONE_WIDTH)
        self.assertIn("‹ sessions", self.bar(PHONE_WIDTH))

    def test_a_slot_active_with_nothing_in_it_hands_back_to_the_panel(self):
        tmux = PhoneTmux(panel_active=False)
        self.assertIn(("show_alone", "%1"), self.housekeep(tmux))

    def test_a_phone_never_writes_the_prefs_on_zoom_or_widths_even_in_the_home_view(self):
        self.app.primary = True
        self.app.prefs = {**self.app.prefs, "panel_collapsed": False, "panel_width": 40}
        saved = []
        self.app.save_prefs = lambda: saved.append(dict(self.app.prefs))
        for tmux in (PhoneTmux(view=SESSION), PhoneTmux(view=SESSION, zoomed=False), PhoneTmux(view=SESSION, panel_active=False)):
            self.housekeep(tmux)
        self.assertEqual(saved, [])
        self.assertFalse(self.app.prefs["panel_collapsed"])
        self.assertEqual(self.app.prefs["panel_width"], 40)
        self.assertNotIn("resize_width", [c[0] for c in tmux.calls])

    def test_turning_wide_shows_both_panes_again_laid_out_from_prefs(self):
        self.housekeep(PhoneTmux())
        tmux = PhoneTmux(width=120)
        calls = self.housekeep(tmux)
        self.assertEqual(calls[0], ("toggle_zoom", "%1"))
        self.assertFalse(tmux.zoomed)
        self.assertFalse(self.app.narrow)
        self.assertIn(("resize_width", "%1", 40), calls)

    def test_the_home_view_turning_wide_hides_its_panel_again_when_the_laptop_had_it_hidden(self):
        self.app.primary = True
        self.app.prefs = {**self.app.prefs, "panel_collapsed": True}
        self.app.save_prefs = lambda: None
        self.housekeep(PhoneTmux(view=SESSION))
        tmux = PhoneTmux(view=SESSION, width=120)
        calls = self.housekeep(tmux)
        self.assertEqual(calls[:2], [("toggle_zoom", "%1"), ("toggle_zoom", "%2")])
        self.assertEqual((tmux.active, tmux.zoomed), ("%2", True))

    def test_a_wide_view_never_zooms_on_its_own(self):
        self.app.window_width = 0
        tmux = PhoneTmux(width=120, zoomed=False)
        calls = self.housekeep(tmux)
        self.assertNotIn("show_alone", [c[0] for c in calls])
        self.assertNotIn("toggle_zoom", [c[0] for c in calls])

    def test_opening_a_session_on_a_phone_takes_the_whole_window_before_the_viewer_starts(self):
        tmux = PhoneTmux()
        self.app.tmux = tmux
        self.poll(core.Session(KEY_A, "alpha", "/p", "done", "", "", 1.0, True))
        self.app.refresh()
        self.app.housekeep = lambda fresh_list: None
        sidebar.App.switch_to(self.app, KEY_A, core.OPEN)
        names = [c[0] for c in tmux.calls]
        self.assertLess(names.index("show_alone"), names.index("respawn"))
        self.assertIn(("show_alone", "%2"), tmux.calls)
        self.assertNotIn("select", names)
        self.assertTrue(self.app.slot_active)

    def test_a_phone_coming_back_finds_its_session_under_the_cursor_not_opened(self):
        tmux = PhoneTmux(attached=1)
        tmux.options[sidebar.REOPEN_OPT] = KEY_A
        self.app.attached = 0
        self.app.tmux = tmux
        sidebar.App.tend_slot(self.app, tmux.slot_pane)
        self.assertEqual(self.app.wanted_cursor, ("session", KEY_A))
        self.assertNotIn("respawn", [c[0] for c in tmux.calls])

    def test_the_panel_bar_offers_the_phones_keys(self):
        self.poll(session("a", "/p/alpha", "done"))
        self.app.refresh()
        self.app.focused = True
        self.app.cursor = ("session", "a")
        self.assertEqual(self.bar(PHONE_WIDTH).rstrip(), " ⚒ ✻1   ↵ open  n new  a actions  ?")
        self.app.cursor = ("group", self.app.groups[0].key)
        self.assertEqual(self.bar(PHONE_WIDTH).rstrip(), " ⚒ ✻1   ↵ fold  n new  ?")

    def test_a_selected_row_repeats_no_laptop_key_on_a_phone(self):
        self.poll(session("a", "/p/alpha", "done"))
        self.app.refresh()
        self.app.focused = True
        self.app.cursor = ("session", "a")
        self.app.draw()
        self.assertFalse(any("→ open" in line for line in self.scr.cells.values()), self.scr.cells)
        self.app.window_width = 0
        self.app.draw()
        self.assertTrue(any("→ open" in line for line in self.scr.cells.values()), self.scr.cells)

    def test_a_selected_header_repeats_no_laptop_key_on_a_phone(self):
        self.poll(session("a", "/p/alpha", "done"))
        self.app.refresh()
        self.app.focused = True
        self.app.cursor = ("group", self.app.groups[0].key)
        self.app.draw()
        self.assertFalse(any("Enter fold" in line for line in self.scr.cells.values()), self.scr.cells)
        self.app.window_width = 0
        self.app.draw()
        self.assertTrue(any("Enter fold" in line for line in self.scr.cells.values()), self.scr.cells)

    def test_side_and_width_keys_do_nothing_on_a_phone(self):
        before = dict(self.app.prefs)
        self.app.tmux = PhoneTmux()
        for key in ("s", "<", ">", ",", "."):
            self.app.handle_key(key)
        self.assertEqual(self.app.prefs, before)
        self.assertEqual(self.app.tmux.calls, [])

    def test_t_opens_the_session_here_and_on_a_header_does_nothing(self):
        self.poll(session("a", "/p/alpha", "done"))
        self.app.refresh()
        self.app.cursor = ("session", "a")
        self.app.handle_key("t")
        self.assertEqual(self.opened, [("a", core.OPEN)])
        self.app.cursor = ("group", self.app.groups[0].key)
        self.app.handle_key("t")
        self.assertEqual(self.opened, [("a", core.OPEN)])

    def test_t_on_a_laptop_does_nothing(self):
        self.app.window_width = 120
        self.poll(session("a", "/p/alpha", "done"))
        self.app.refresh()
        self.app.cursor = ("session", "a")
        self.app.handle_key("t")
        self.assertEqual(self.opened, [])

    def test_the_routines_view_fits_48_columns_and_R_or_esc_returns(self):
        answer = ([core.RoutineView("a-very-long-routine-name-backup-nightly-and-more", "passed", "done",
                                    tuple((1_800_000_000.0 + i, core.PASSED, "a long note about the run " * 3) for i in range(4)),
                                    1_800_000_500.0, "a note that is far longer than the forty eight columns of a phone")], None)
        with mock.patch.object(sidebar.routines, "read", lambda now: answer):
            for back in ("R", "\x1b"):
                with self.subTest(back=back):
                    self.app.handle_key("R")
                    self.app.routines_reader.join(5)
                    self.app.refresh()
                    self.app.draw()
                    lines = [line for line in self.scr.cells.values()]
                    self.assertIn(sidebar.ROUTINES_TITLE, " ".join(lines))
                    self.assertTrue(any("a-very-long" in line for line in lines), lines)
                    for line in lines:
                        self.assertLessEqual(core.display_width(line), PHONE_WIDTH, line)
                    self.app.handle_key(back)
                    self.assertIsNone(self.app.routines_view)

    def test_help_on_a_phone_lists_the_phones_keys_scrolls_one_line_and_closes_on_another_key(self):
        self.app.handle_key("?")
        self.assertTrue(self.app.help)
        self.app.draw()
        first = dict(self.scr.cells)
        self.assertIn("↵ Enter", first[2])
        self.app.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.app.help_scroll, 1)
        self.app.draw()
        # The last column is the scroll bar, whose thumb moves with the scroll.
        self.assertEqual(self.scr.cells[2][:-1], first[3][:-1])
        self.app.handle_key("k")
        self.assertEqual(self.app.help_scroll, 0)
        self.app.handle_key("q")
        self.assertFalse(self.app.help)
        self.assertEqual(self.opened, [])

    def test_help_on_a_laptop_still_closes_on_any_key_and_is_2_10_0s(self):
        wheel = ("wheel", "scroll; the selection stays, ↑↓ brings it back")
        self.assertEqual(sidebar.HELP, HELP_2_10_0[:3] + (wheel,) + HELP_2_10_0[3:])
        self.app.window_width = 120
        self.app.handle_key("?")
        self.app.handle_key(curses.KEY_DOWN)
        self.assertFalse(self.app.help)

    def test_every_plain_key_of_the_laptops_help_but_the_wide_only_ones_is_in_the_phones(self):
        laptop = {t for keys, _ in sidebar.HELP if keys for t in keys.split()}
        phone = {t for keys, _ in sidebar.NARROW_HELP for t in keys.split()}
        self.assertEqual(laptop - phone, {"s", "<", ">", "wheel"})
        self.assertTrue(set(sidebar.WIDE_ONLY_KEYS) >= {"s", "<", ">"})
        self.assertEqual(phone - laptop, {"↵", "t", "‹", "a"})

    def test_every_key_in_the_phones_help_does_something_on_a_phone(self):
        root = {"‹", "←", "^\\", "^q"}
        a, b = session("a", "/p/alpha", "done"), session("b", "/p/beta", "working")
        checks = {
            "↵": ("\n", lambda: self.opened), "Enter": ("\r", lambda: self.opened),
            "t": ("t", lambda: self.opened), "→": (curses.KEY_RIGHT, lambda: self.opened),
            "a": ("a", lambda: self.app.menu == "a"),
            "click": (curses.KEY_MOUSE, lambda: self.app.cursor == ("session", "b") and not self.opened),
            "↑↓": (curses.KEY_DOWN, lambda: self.app.cursor != ("session", "a")), "j": ("j", lambda: self.app.cursor != ("session", "a")),
            "k": ("k", lambda: self.app.cursor != ("session", "a")), "space": (" ", lambda: self.app.peeking),
            "n": ("n", lambda: self.app.prompt is not None), "^x": (sidebar.CTRL_X, lambda: self.app.confirm is not None),
            "^f": (sidebar.CTRL_F, lambda: self.app.find is not None), "/": ("/", lambda: self.app.find is not None),
            "^t": (sidebar.CTRL_T, lambda: "a" in core.pinned_ids(self.app.prefs)),
            "^r": (sidebar.CTRL_R, lambda: self.app.rename_edit is not None), "^s": (sidebar.CTRL_S, lambda: self.app.view() == core.BY_STATE),
            "N": ("N", lambda: self.app.history_view is not None), "R": ("R", lambda: self.app.routines_view is not None),
            "m": ("m", lambda: self.app.mode() != core.NOTIFICATION_MODE), "h": ("h", lambda: self.silenced or self.app.mode() == core.NOTIFICATION_MODE),
            "q": ("q", lambda: detached), "?": ("?", lambda: self.app.help),
        }
        tokens = {t for keys, _ in sidebar.NARROW_HELP for t in keys.split()}
        self.assertEqual(tokens - set(checks), root)
        for token, (key, done) in checks.items():
            with self.subTest(key=token):
                self.setUp()
                self.scr.size = (40, PHONE_WIDTH)
                detached = []
                self.app.tmux = PhoneTmux()
                self.app.tmux.detach = lambda: detached.append(True)
                self.app.poller.notifier.authorize = lambda: None
                self.poll(a, b)
                self.app.refresh()
                self.app.cursor, self.app.focused = ("session", "a"), True
                if token == "h":
                    self.app.prefs = {**self.app.prefs, "mode": core.VOICE_MODE}
                    self.app.apply_mode()
                self.app.draw()
                y = next(y for y, item in self.app.row_map.items() if item == ("session", "b"))
                with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)), \
                        mock.patch.object(sidebar.routines, "read", lambda path, now: ([], None)):
                    self.app.handle_key(key)
                self.assertTrue(done(), token)


BRIDGE = "session_01AbCdEfGhIjKlMnOpQrStUv"
APP_URL = "https://claude.ai/code/" + BRIDGE


class ClaudeAppLink(AppCase):
    """On a phone the selected Claude session with a known Remote Control link offers the Claude app; ↵ puts the link on screen for a tap."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(height=22, width=PHONE_WIDTH)
        self.app.scr = self.scr
        self.app.window_width = PHONE_WIDTH
        self.app.tmux = PhoneTmux()
        copies = self.copies = []
        self.app.tmux.copy_to_client = copies.append
        self.poll(session("a", "/p/alpha", "done"), session("b", "/p/alpha", "working"))
        self.app.refresh()
        self.app.links = {"a": BRIDGE}
        self.app.poller.take_links = lambda: {"a": BRIDGE}
        self.app.focused = True
        self.app.cursor = ("session", "a")

    def lines(self):
        self.app.draw()
        return [self.scr.cells[y] for y in sorted(self.scr.cells)]

    def test_the_selected_row_gets_the_link_line_under_it(self):
        lines = self.lines()
        at = next(i for i, line in enumerate(lines) if "emx-test-a" in line)
        self.assertEqual(lines[at + 2].strip(), sidebar.APP_LINE)
        link_y = next(y for y, item in self.app.row_map.items() if item == ("link", "a"))
        self.assertEqual(self.scr.cells[link_y].strip(), sidebar.APP_LINE)
        self.assertEqual(sum(sidebar.APP_LINE in line for line in lines), 1)

    def test_a_row_not_selected_or_with_no_link_has_no_line(self):
        self.app.cursor = ("session", "b")
        self.assertFalse(any(sidebar.APP_LINE in line for line in self.lines()))
        self.app.links = {}
        self.app.cursor = ("session", "a")
        self.assertFalse(any(sidebar.APP_LINE in line for line in self.lines()))

    def test_enter_on_a_linked_session_shows_the_link_and_copies_it_and_opens_nothing(self):
        self.app.handle_key("\n")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.copies, [APP_URL])
        self.assertEqual(self.app.notice, "Tap the link to open emx-test-a in the Claude app.")
        bottom = "".join(line.strip() for line in self.lines()[-4:])
        self.assertIn(APP_URL, bottom)

    def test_the_link_stays_whole_when_cut_at_the_edge(self):
        self.app.handle_key("\n")
        rows = [t for t, _ in self.app.bottom_lines(PHONE_WIDTH - 1, 22)]
        self.assertEqual("".join(rows[-2:]), APP_URL)

    def test_the_link_goes_with_its_notice(self):
        self.app.handle_key("\n")
        self.app.say("")
        self.assertFalse(any(APP_URL[:20] in t for t, _ in self.app.bottom_lines(PHONE_WIDTH - 1, 22)))

    def test_enter_with_no_link_opens_here_and_t_always_does(self):
        self.app.cursor = ("session", "b")
        self.app.handle_key("\n")
        self.app.cursor = ("session", "a")
        self.app.handle_key("t")
        self.assertEqual(self.opened, [("b", core.OPEN), ("a", core.OPEN)])
        self.assertEqual(self.copies, [])

    def test_a_tap_on_the_link_line_opens_in_the_app(self):
        self.lines()
        y = next(y for y, item in self.app.row_map.items() if item == ("link", "a"))
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.app.handle_key(curses.KEY_MOUSE)
        self.assertEqual((self.copies, self.opened), ([APP_URL], []))

    def test_the_bar_offers_the_app_on_a_linked_row(self):
        self.assertEqual(self.bar(PHONE_WIDTH).rstrip(), " ⚒ ✻1 ✻1   ↵ open in app  t here  a actions  ?")

    def test_a_stopped_session_has_no_link(self):
        self.poll(session("a", "/p/alpha", "stopped", pid=None), session("b", "/p/alpha", "working"))
        self.app.refresh()
        self.assertIsNone(self.app.link_for(self.app.session("a")))

    def test_a_wide_panel_draws_the_same_rows_with_a_link_known_or_not(self):
        self.app.window_width = 120
        self.scr.size = (22, 40)
        with_link = self.lines()
        self.app.links = {}
        self.assertEqual(self.lines(), with_link)
        self.app.links = {"a": BRIDGE}
        self.app.handle_key("\n")
        self.assertEqual(self.opened, [("a", core.OPEN)])
        self.assertEqual(self.copies, [])


class ActionsMenu(AppCase):
    """On a phone `a`, or a tap on the selected row, opens the session's actions under its row: plain keys or taps, close kept in emx."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(height=30, width=PHONE_WIDTH)
        self.app.scr = self.scr
        self.app.window_width = PHONE_WIDTH
        self.app.tmux = PhoneTmux()
        self.app.tmux.copy_to_client = lambda text: None
        self.poll(session("a", "/p/alpha", "done"), session("b", "/p/alpha", "working"))
        self.app.refresh()
        self.app.links = {"a": BRIDGE}
        self.app.poller.take_links = lambda: dict(self.app.links)
        self.app.focused = True
        self.app.cursor = ("session", "a")
        self.closers = []
        self.interrupts = []
        self.app.start_close = self.closers.append
        self.app.start_interrupt = self.interrupts.append

    def menu_rows(self):
        self.app.draw()
        return {item[1]: y for y, item in self.app.row_map.items() if item and item[0] == "menu"}

    def tap(self, y):
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 5, y, 0, curses.BUTTON1_CLICKED)):
            self.app.handle_key(curses.KEY_MOUSE)

    def test_a_opens_seven_lines_for_a_linked_session_and_six_without(self):
        self.app.handle_key("a")
        self.assertEqual(list(self.menu_rows()), ["↵", "t", "␣", "r", "p", "i", "x"])
        cells = "\n".join(self.scr.cells[y] for y in sorted(self.scr.cells))
        for text in ("╭─ emx-test-a ", "open in the Claude app", "open here, in the terminal", "peek", "rename", "pin to top",
                     "interrupt", "close (it goes offline in the app)", "╰"):
            self.assertIn(text, cells)
        self.assertEqual(self.app.narrow_hints(), (core.MENU_HINT,))
        self.app.handle_key("\x1b")
        self.app.links = {}
        self.app.handle_key("a")
        self.assertEqual(list(self.menu_rows()), ["t", "␣", "r", "p", "i", "x"])
        cells = "\n".join(self.scr.cells[y] for y in sorted(self.scr.cells))
        self.assertNotIn("Claude app", cells)
        self.assertNotIn("goes offline", cells)

    def test_every_line_fits_the_phone(self):
        self.app.handle_key("a")
        self.menu_rows()
        for line in self.scr.cells.values():
            self.assertLessEqual(core.display_width(line), PHONE_WIDTH, line)

    def test_p_pins_and_closes_the_box_and_says_unpin_next_time(self):
        self.app.handle_key("a")
        self.app.handle_key("p")
        self.assertIn("a", core.pinned_ids(self.app.prefs))
        self.assertIn("a", core.pinned_ids(core.load_prefs(core.prefs_path())))
        self.assertIsNone(self.app.menu)
        self.app.handle_key("a")
        self.assertIn(("p", "unpin", ""), self.app.menu_lines(self.app.session("a")))

    def test_x_once_arms_and_twice_closes_through_the_closer(self):
        self.app.handle_key("a")
        self.app.handle_key("x")
        self.assertEqual(self.closers, [])
        self.assertEqual(self.interrupts, [])
        self.assertIsNotNone(self.app.menu)
        self.assertIn(("x", sidebar.MENU_ARMED, ""), self.app.menu_lines(self.app.session("a")))
        self.app.refresh()
        self.assertIn("x", self.menu_rows())
        self.assertIn("│ x  again to close", " ".join(self.scr.cells.values()))
        self.app.handle_key("x")
        self.assertEqual([c.id for c in self.closers], ["a"])
        self.assertIsNone(self.app.menu)

    def test_two_taps_on_the_x_line_close_too(self):
        self.app.handle_key("a")
        self.tap(self.menu_rows()["x"])
        self.assertEqual(self.closers, [])
        self.tap(self.menu_rows()["x"])
        self.assertEqual([c.id for c in self.closers], ["a"])

    def test_another_key_after_x_disarms(self):
        self.app.handle_key("a")
        self.app.handle_key("x")
        self.app.handle_key("\x1b")
        self.assertIsNone(self.app.confirm)
        self.app.handle_key("a")
        self.app.handle_key("x")
        self.assertEqual(self.closers, [])

    def test_esc_closes_the_box_doing_nothing(self):
        self.app.handle_key("a")
        self.app.handle_key("\x1b")
        self.assertIsNone(self.app.menu)
        self.assertEqual((self.opened, self.closers, self.interrupts), ([], [], []))
        self.assertNotIn("a", core.pinned_ids(self.app.prefs))

    def test_a_tap_on_the_t_line_opens_here(self):
        self.app.handle_key("a")
        self.tap(self.menu_rows()["t"])
        self.assertEqual(self.opened, [("a", core.OPEN)])
        self.assertIsNone(self.app.menu)

    def test_enter_opens_in_the_app_space_peeks_r_renames_i_interrupts(self):
        copies = []
        self.app.tmux.copy_to_client = copies.append
        self.app.handle_key("a")
        self.app.handle_key("\n")
        self.assertEqual((copies, self.opened), ([APP_URL], []))
        self.app.handle_key("a")
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)
        self.app.handle_key("\x1b")
        self.app.handle_key("a")
        self.app.handle_key("i")
        self.assertEqual([s.id for s in self.interrupts], ["a"])
        self.app.handle_key("a")
        self.app.handle_key("r")
        self.assertEqual(self.app.rename_edit["id"], "a")

    def test_interrupt_with_nothing_to_interrupt_says_the_existing_message(self):
        del self.app.start_interrupt
        result = []

        class Done(sidebar.Interrupter):
            def run(self):
                result.append(self.interrupt)
                self.result = (True, core.nothing_to_interrupt_text(self.interrupt))

        with mock.patch.object(sidebar, "Interrupter", Done):
            self.app.handle_key("a")
            self.app.handle_key("i")
            self.app.interrupters["a"].join(5)
            self.app.finish_interrupt()
        self.assertEqual(self.app.notice, core.nothing_to_interrupt_text(core.Interrupt("a", "emx-test-a")))

    def test_a_tap_on_another_row_selects_it_and_a_tap_on_the_selected_row_opens_the_menu(self):
        self.app.draw()
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "b"))
        self.tap(y)
        self.assertEqual((self.app.cursor, self.app.menu, self.opened), (("session", "b"), None, []))
        self.app.draw()
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "b"))
        self.tap(y)
        self.assertEqual((self.app.menu, self.opened), ("b", []))

    def test_the_menu_goes_when_its_row_does(self):
        self.app.handle_key("a")
        self.poll(session("b", "/p/alpha", "working"))
        self.app.refresh()
        self.assertIsNone(self.app.menu)

    def test_a_new_session_from_the_phone_stays_on_the_panel_with_the_new_row_selected(self):
        new = core.Session(KEY_A, "dark-toggle", "/p/alpha", "starting", "", "", 2.0, True, session_id=KEY_A)
        done = sidebar.Starter(FakeEngine(), "/p/alpha", "add a dark mode toggle")
        done.result = (new, None)
        self.app.starter = done
        self.app.say("Starting a session…")
        self.app.finish_start()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app.notice, "")
        self.assertEqual(self.app.wanted_cursor, ("session", KEY_A))
        self.assertEqual(self.app.narrow_hints(), ("Started dark-toggle.  ↵ open",))
        self.poll(new, session("b", "/p/alpha", "working"))
        self.app.refresh()
        self.app.links = {KEY_A: BRIDGE}
        self.assertEqual(self.app.cursor, ("session", KEY_A))
        self.assertEqual(self.app.narrow_hints(), ("Started dark-toggle.  ↵ open in app",))
        self.assertIn("Started dark-toggle.  ↵ open in app", self.bar(PHONE_WIDTH))
        self.app.handle_key("j")
        self.assertIsNone(self.app.started_note)

    def test_a_new_session_on_a_laptop_still_opens(self):
        self.app.window_width = 120
        new = core.Session(KEY_A, "dark-toggle", "/p/alpha", "starting", "", "", 2.0, True)
        done = sidebar.Starter(FakeEngine(), "/p/alpha", "")
        done.result = (new, None)
        self.app.starter = done
        self.app.finish_start()
        self.assertEqual(self.opened, [(KEY_A, core.NEW, (new, True))])
        self.assertIsNone(self.app.started_note)

    def test_on_a_laptop_a_and_t_do_nothing_and_a_click_opens_the_session(self):
        self.app.window_width = 120
        self.scr.size = (30, 40)
        before = dict(self.app.prefs)
        for key in ("a", "t"):
            self.app.handle_key(key)
        self.assertEqual((self.app.menu, self.opened, self.app.prefs), (None, [], before))
        self.app.draw()
        self.assertFalse(any(item and item[0] in ("menu", "link") for item in self.app.row_map.values()))
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "a"))
        self.tap(y)
        self.assertEqual(self.opened, [("a", core.OPEN)])

    def tap_selected_row(self):
        """A tap on the line the selected row was drawn on, after whatever was just opened."""
        self.app.draw()
        rows = [y for y, item in self.app.row_map.items() if item == self.app.cursor]
        self.tap(rows[0] if rows else 2)

    def assert_no_menu_and_no_action(self):
        self.assertIsNone(self.app.menu)
        self.assertEqual((self.closers, self.interrupts), ([], []))
        self.assertNotIn("a", core.pinned_ids(self.app.prefs))

    def type_after_a_tap(self, *opening):
        for key in opening:
            self.app.handle_key(key)
        self.tap_selected_row()
        self.assertIsNone(self.app.menu)
        for key in "ixt":
            self.app.handle_key(key)
        self.assert_no_menu_and_no_action()
        self.assertEqual(self.opened, [])

    def test_with_the_message_prompt_open_a_tap_on_the_selected_row_leaves_the_letters_to_it(self):
        self.type_after_a_tap("n")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["text"]), ("message", "ixt"))

    def test_with_the_folder_prompt_open_a_tap_on_the_selected_row_leaves_the_letters_to_it(self):
        self.type_after_a_tap("n", "\t")
        self.assertEqual((self.app.prompt["stage"], self.app.prompt["text"]), ("folder", "ixt"))

    def test_while_renaming_a_tap_on_the_selected_row_leaves_the_letters_to_the_name(self):
        self.type_after_a_tap(sidebar.CTRL_R)
        self.assertEqual(self.app.rename_edit["id"], "a")
        self.assertTrue(self.app.rename_edit["text"].endswith("ixt"), self.app.rename_edit["text"])

    def test_with_find_open_a_tap_on_the_selected_row_leaves_the_letters_to_find(self):
        self.type_after_a_tap("/")
        self.assertEqual(self.app.find, "ixt")

    def test_with_history_open_a_tap_opens_no_menu(self):
        self.type_after_a_tap("N")
        self.assertIsNotNone(self.app.history_view)

    def test_with_help_open_a_tap_on_the_selected_row_opens_no_menu(self):
        self.app.handle_key("?")
        self.tap_selected_row()
        self.app.handle_key("i")
        self.app.handle_key("x")
        self.assert_no_menu_and_no_action()

    def test_while_peeking_a_tap_on_the_selected_row_opens_no_menu(self):
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)
        self.tap_selected_row()
        self.app.handle_key("i")
        self.app.handle_key("x")
        self.assert_no_menu_and_no_action()

    def test_with_a_close_armed_a_tap_on_the_selected_row_opens_no_menu(self):
        self.app.handle_key(sidebar.CTRL_X)
        self.assertIsNotNone(self.app.confirm)
        self.tap_selected_row()
        self.app.handle_key("i")
        self.app.handle_key("x")
        self.app.handle_key("x")
        self.assert_no_menu_and_no_action()

    def test_an_open_input_gets_its_keys_before_a_menu_left_open_under_it(self):
        self.app.handle_key("n")
        self.app.menu = "a"
        for key in "ixtx":
            self.app.handle_key(key)
        self.assertEqual(self.app.prompt["text"], "ixtx")
        self.assertIsNone(self.app.confirm)
        self.assertEqual((self.closers, self.interrupts, self.opened), ([], [], []))


class PollerReadsLinks(unittest.TestCase):
    """The poller reads a link only while a phone's panel wants them, from the session's own live agent pane's process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp.name, "XDG_STATE_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        sessions = [core.Session(KEY_A, "alpha", "/p", "done", "", "", 1.0, True, session_id=CLAUDE_SID),
                    core.Session("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "beta", "/p", "done", "", "", 1.0, True, session_id=KEY_A)]

        class Tmux:
            def panes(self):
                return [Pane("%5", "@5", False, "", KEY_A, 80, 80, agent="claude", pid=4242),
                        Pane("%6", "@6", False, "", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 80, 80, pid=4343)]

        self.sessions = sessions
        self.poller = sidebar.Poller(FakeEngine(listing=(sessions, None), tmux=Tmux()))
        self.reads = []

        def read(pid, sid):
            self.reads.append((pid, sid))
            return BRIDGE if (pid, sid) == (4242, CLAUDE_SID) else None

        patch = mock.patch.object(sidebar, "bridge_link", read)
        patch.start()
        self.addCleanup(patch.stop)
        fetch = mock.patch.object(sidebar, "fetch_sessions", lambda engine: engine.list_sessions())
        fetch.start()
        self.addCleanup(fetch.stop)

    def test_nothing_is_read_while_no_phone_wants_links(self):
        self.poller.poll()
        self.assertEqual((self.reads, self.poller.take_links()), ([], {}))

    def test_a_wanted_link_comes_from_the_agent_panes_pid_and_claudes_session_id_only(self):
        self.poller.links_wanted = True
        self.poller.poll()
        self.assertEqual(self.reads, [(4242, CLAUDE_SID)])
        self.assertEqual(self.poller.take_links(), {KEY_A: BRIDGE})

    def test_an_unchanged_file_is_not_read_again(self):
        self.poller.links_wanted = True
        self.poller.poll()
        self.poller.poll()
        self.assertEqual(self.reads, [(4242, CLAUDE_SID)])
        folder = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "sessions")
        os.makedirs(folder)
        with open(os.path.join(folder, "4242.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.poller.poll()
        self.assertEqual(self.reads, [(4242, CLAUDE_SID)] * 2)


class PrefsAcrossViews(AppCase):
    """Two views' panels share one prefs file; each keeps the other's changes."""

    def test_a_pin_on_one_panel_and_a_side_on_the_other_both_land(self):
        other = sidebar.App(self.scr, tmux=ViewTmux(view="emx-view-1"), claude="claude")
        self.app.prefs = core.toggle_pin(self.app.prefs, KEY_A)
        self.app.save_prefs()
        other.prefs = core.flip_side(other.prefs)
        other.save_prefs()
        saved = core.load_prefs(self.app.prefs_file)
        self.assertEqual((core.pinned_ids(saved), saved["side"]), ({KEY_A}, "left"))

    def test_a_panel_takes_in_what_another_saved_and_keeps_its_own_unsaved_change(self):
        other = sidebar.App(self.scr, tmux=ViewTmux(view="emx-view-1"), claude="claude")
        other.prefs = core.toggle_pin(other.prefs, KEY_A)
        other.save_prefs()
        os.utime(self.app.prefs_file, ns=(1, 1))
        self.app.prefs = {**self.app.prefs, "side": "left"}
        self.app.reload_prefs()
        self.assertEqual((core.pinned_ids(self.app.prefs), self.app.prefs["side"]), ({KEY_A}, "left"))


class StampTmux(ClientTmux):
    def __init__(self, session_stamp):
        super().__init__()
        self.stamps = {"session": session_stamp, "global": ""}

    def session_option(self, name):
        return self.stamps["session"] if name == sidebar.RELAY_PASTE_OPT else ""

    def global_option(self, name):
        return self.stamps["global"]

    def set_global_option(self, name, value):
        self.stamps["global"] = value


class PasteStampCarried(AppCase):
    """An `emx follow` from before 2.10.0 keeps stamping its paste offer on `emx` until he reconnects; the primary panel copies it to the global option a viewer's Ctrl+V reads."""

    def test_a_live_stamp_on_emx_is_copied_to_the_global_option(self):
        now = time.time()
        self.app.tmux = StampTmux(str(now))
        self.app.carry_paste_stamp(now)
        self.assertEqual(self.app.tmux.stamps["global"], str(now))

    def test_a_stale_or_missing_stamp_is_not(self):
        now = time.time()
        for stamp in (str(now - 60), ""):
            with self.subTest(stamp=stamp):
                self.app.tmux = StampTmux(stamp)
                self.app.carry_paste_stamp(now)
                self.assertEqual(self.app.tmux.stamps["global"], "")


class WheelCase(AppCase):
    """A list of five sessions, one folder each, on a 9-row screen: it overflows, so it scrolls and shows the bar."""

    def setUp(self):
        super().setUp()
        self.scr = RecordingScr(9, 40)
        self.app.scr = self.scr
        self.app.palette = TonePalette()
        self.poll(*[session(c, f"/p/{name}") for c, name in zip("abcde", ("alpha", "beta", "gamma", "delta", "epsilon"))])
        self.app.refresh()

    def screen(self):
        return [self.scr.cells.get(y, "") for y in range(self.scr.size[0])]

    def rows_of(self, sid):
        return sorted(y for y, item in self.app.row_map.items() if item == ("session", sid))

    def wheel(self, down=True, times=1):
        state = sidebar.WHEEL_DOWN if down else curses.BUTTON4_PRESSED
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 10, 3, 0, state)):
            for _ in range(times):
                self.app.handle_key(curses.KEY_MOUSE)

    def total(self):
        return self.app.extent[0]


class WheelScroll(WheelCase):
    def test_wheel_down_on_the_ncurses_5_mouse_abi(self):
        probe = "import curses; curses.__dict__.pop('BUTTON5_PRESSED', None); from emx import sidebar; print(sidebar.WHEEL_DOWN == curses.REPORT_MOUSE_POSITION)"
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)
        probe = "import curses; curses.__dict__.pop('BUTTON5_PRESSED', None); from emx import sidebar; print(bool(sidebar.mouse_mask() & curses.REPORT_MOUSE_POSITION))"
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)
        with mock.patch.object(sidebar, "WHEEL_DOWN", curses.REPORT_MOUSE_POSITION):
            self.wheel()
        self.assertEqual(self.app.scroll, sidebar.WHEEL_ROWS)

    def test_setup_curses_asks_for_the_wheel_down_event(self):
        with mock.patch.object(sidebar.curses, "mousemask") as mousemask, mock.patch.object(sidebar.curses, "mouseinterval"), \
                mock.patch.object(sidebar.curses, "curs_set"), mock.patch.object(sidebar.curses, "raw"), mock.patch.object(sidebar.os, "write"):
            sidebar.setup_curses(mock.MagicMock())
        asked = mousemask.call_args[0][0]
        self.assertEqual(asked & sidebar.WHEEL_DOWN, sidebar.WHEEL_DOWN)
        with mock.patch.object(sidebar, "WHEEL_DOWN", curses.REPORT_MOUSE_POSITION), mock.patch.object(sidebar.curses, "mousemask") as mousemask, \
                mock.patch.object(sidebar.curses, "mouseinterval"), mock.patch.object(sidebar.curses, "curs_set"), \
                mock.patch.object(sidebar.curses, "raw"), mock.patch.object(sidebar.os, "write"):
            sidebar.setup_curses(mock.MagicMock())
        self.assertTrue(mousemask.call_args[0][0] & curses.REPORT_MOUSE_POSITION)

    def test_wheel_down_scrolls_three_rows_and_the_selection_stays(self):
        first = self.app.cursor
        self.assertEqual(self.app.scroll, 0)
        self.assertIn(first, self.app.row_map.values())
        self.wheel()
        self.app.draw()
        self.assertEqual(self.app.scroll, sidebar.WHEEL_ROWS)
        self.assertEqual(self.app.cursor, first)
        self.assertNotIn(first, self.app.row_map.values())

    def test_wheel_up_at_the_top_stays_at_the_top(self):
        self.wheel(down=False)
        self.app.draw()
        self.assertEqual(self.app.scroll, 0)

    def test_many_wheel_downs_stop_at_the_end(self):
        self.wheel(times=50)
        self.app.draw()
        total, height = self.app.extent
        self.assertGreater(total, height)
        self.assertEqual(self.app.scroll, total - height)

    def test_the_arrow_after_a_wheel_moves_the_selection_and_brings_it_into_view(self):
        self.wheel(times=50)
        self.app.draw()
        self.app.handle_key(curses.KEY_DOWN)
        self.app.draw()
        self.assertIn(self.app.cursor, self.app.row_map.values())
        self.assertEqual(self.app.cursor, self.app.items()[1])

    def test_the_up_arrow_on_the_first_item_brings_it_back(self):
        self.app.cursor = self.app.items()[0]
        self.app.draw()
        self.wheel(times=50)
        self.app.draw()
        self.assertNotIn(self.app.cursor, self.app.row_map.values())
        self.app.handle_key(curses.KEY_UP)
        self.app.draw()
        self.assertIn(self.app.cursor, self.app.row_map.values())

    def test_a_poll_and_redraw_keep_the_scroll(self):
        self.wheel()
        self.app.draw()
        self.poll(*self.app.sessions)
        self.app.refresh()
        self.app.draw()
        self.assertEqual(self.app.scroll, sidebar.WHEEL_ROWS)

    def test_a_wheel_does_not_close_a_peek(self):
        self.scr.size = (16, 40)
        self.poll(*[session(c, f"/p/{c}dir") for c in "abcdefgh"])
        self.app.refresh()
        self.app.draw()
        self.app.handle_key(" ")
        self.assertTrue(self.app.peeking)
        self.wheel()
        self.app.draw()
        self.assertTrue(self.app.peeking)
        self.assertEqual(self.app.scroll, sidebar.WHEEL_ROWS)
        self.assertTrue(any(row.startswith("╭─") for row in self.screen()), self.screen())

    def test_a_wheel_leaves_the_whats_new_and_the_started_note(self):
        self.app.whats_new = self.app.notice = "2.15.0: new"
        self.app.started_note = ("a", "x")
        self.wheel()
        self.assertEqual((self.app.whats_new, self.app.notice, self.app.started_note), ("2.15.0: new", "2.15.0: new", ("a", "x")))

    def test_a_wheel_keeps_an_armed_confirm(self):
        self.app.confirm = core.Confirm("a", "emx-test-a", True, time.monotonic())
        armed = self.app.confirm
        self.wheel()
        self.assertIs(self.app.confirm, armed)
        self.app.confirm = None

    def test_a_wheel_does_not_close_the_help(self):
        self.app.handle_key("?")
        self.assertTrue(self.app.help)
        self.wheel()
        self.assertTrue(self.app.help)

    def test_a_click_queued_behind_a_wheel_maps_through_the_frame_he_saw(self):
        self.app.draw()
        y = next(y for y, item in self.app.row_map.items() if item == ("session", "b"))
        events = [(0, 10, 3, 0, sidebar.WHEEL_DOWN), (0, 10, y, 0, curses.BUTTON1_CLICKED)]
        self.scr.keys = [curses.KEY_MOUSE, curses.KEY_MOUSE]
        with mock.patch.object(sidebar.curses, "getmouse", side_effect=events):
            self.app.read_input()
        self.assertEqual(self.app.scroll, sidebar.WHEEL_ROWS)
        self.assertEqual(self.opened, [("b", core.OPEN)])

    def test_a_click_without_a_wheel_still_works_through_handle_mouse(self):
        self.app.draw()
        y = self.rows_of("a")[0]
        with mock.patch.object(sidebar.curses, "getmouse", return_value=(0, 10, y, 0, curses.BUTTON1_CLICKED)):
            self.app.handle_mouse()
        self.assertEqual(self.opened, [("a", core.OPEN)])


class ScrollBar(WheelCase):
    def setUp(self):
        super().setUp()
        self.scr = AttrScr(9, 40)
        self.app.scr = self.scr
        self.app.draw()

    def column(self):
        return [self.scr.cells.get(y, "").ljust(40)[39] for y in range(9)]

    def test_the_bar_fills_the_last_column_with_the_thumb_at_the_top(self):
        col = self.column()
        self.assertTrue(set(col) <= {"│", "┃"}, col)
        self.assertEqual(col[0], "┃")
        self.assertNotEqual(col[-1], "┃")

    def test_at_the_end_the_thumb_is_at_the_bottom(self):
        self.wheel(times=50)
        self.app.draw()
        col = self.column()
        self.assertEqual((col[0], col[-1]), ("│", "┃"))

    def test_track_is_border_and_thumb_is_muted(self):
        palette = TonePalette()
        for y, x, text, attr in self.scr.drawn:
            if x == 39 and text == "│":
                self.assertEqual(attr, palette.attr("border"))
            if x == 39 and text == "┃":
                self.assertEqual(attr, palette.attr("muted"))
        self.assertTrue(any(x == 39 and text == "┃" for _, x, text, _ in self.scr.drawn))
        self.assertTrue(any(x == 39 and text == "│" for _, x, text, _ in self.scr.drawn))

    def test_nothing_is_drawn_under_the_bar(self):
        self.app.cursor = self.app.items()[1]
        for _ in range(3):
            self.app.draw()
            self.assertTrue(all(self.scr.cells.get(y, "").ljust(40)[38] == " " for y in range(9)), self.scr.cells)
            self.app.handle_key(curses.KEY_DOWN)

    def test_a_list_that_fits_has_no_bar_and_keeps_its_width(self):
        self.scr.size = (30, 40)
        self.app.draw()
        for y in range(30):
            self.assertNotIn(self.scr.cells.get(y, "").ljust(40)[39], "│┃")
        self.assertLessEqual(self.app.extent[0], self.app.extent[1])

    def test_notifications_scroll_without_moving_the_selected_entry_and_show_the_bar(self):
        self.app.history = [core.Entry("a", core.NOTICE_DONE, f"a · alpha{i}", f"Alpha finished task {i}.", time.monotonic() - 70) for i in range(12)]
        self.app.handle_key("N")
        self.app.draw()
        self.assertEqual(self.column()[1], "┃")
        self.wheel()
        self.app.draw()
        self.assertEqual(self.app.history_scroll, sidebar.WHEEL_ROWS)
        self.assertEqual(self.app.history_view, 0)
        self.assertNotIn(("entry", 0), self.app.row_map.values())
        self.app.handle_key(curses.KEY_DOWN)
        self.app.draw()
        self.assertIn(("entry", 1), self.app.row_map.values())

    def test_routines_scroll_three_rows_and_show_the_bar(self):
        self.app.routines_view = 0
        self.app.routines_error = "x " * 400
        self.app.draw()
        self.assertEqual(self.column()[1], "┃")
        self.wheel()
        self.app.draw()
        self.assertEqual(self.app.routines_view, sidebar.WHEEL_ROWS)

    def test_the_wide_help_scrolls_on_a_short_screen_and_keys_still_close_it(self):
        self.app.handle_key("?")
        self.app.draw()
        self.assertEqual(self.column()[2], "┃")
        before = self.scr.cells[2]
        self.wheel()
        self.app.draw()
        self.assertTrue(self.app.help)
        self.assertEqual(self.app.help_scroll, sidebar.WHEEL_ROWS)
        self.assertNotEqual(self.scr.cells[2][:-1], before[:-1])
        self.app.handle_key("x")
        self.assertFalse(self.app.help)


class SchemeBars(unittest.TestCase):
    PARTS = [("✻1", "needs", True), ("x", "muted", False), ("y", "plain", False)]

    def test_light_and_terminal_colour_the_bar_with_their_own_tones(self):
        light = sidebar.bar_format(self.PARTS, "light")
        self.assertIn("#[fg=colour130,bold]✻1", light)
        self.assertIn("#[fg=colour243,nobold]x", light)
        terminal = sidebar.bar_format(self.PARTS, "terminal")
        self.assertIn("#[fg=colour3,bold]", terminal)
        self.assertIn("#[fg=colour8,nobold]", terminal)

    def test_plain_is_the_default_colour_in_every_scheme(self):
        for name in core.SCHEMES:
            with self.subTest(name=name):
                self.assertIn("#[fg=default,nobold]y", sidebar.bar_format(self.PARTS, name))

    def test_terminal_is_the_default(self):
        self.assertEqual(sidebar.bar_format(self.PARTS), sidebar.bar_format(self.PARTS, "terminal"))


class SchemeFollowsPrefs(AppCase):
    LIGHT = [
        ("status-style", "bg=colour254,fg=default"),
        ("pane-border-style", "fg=colour250"),
        ("pane-active-border-style", "fg=colour25"),
    ]

    def test_a_scheme_saved_by_the_command_reaches_tmux_and_the_palette_in_one_pass(self):
        self.write_scheme("light", 1)
        self.app.reload_prefs()
        self.assertEqual(self.app.tmux.globals_set, self.LIGHT)
        self.assertEqual(self.app.palette.schemes[-1], "light")

    def test_a_second_reload_with_no_change_sets_nothing_more(self):
        self.write_scheme("light", 1)
        self.app.reload_prefs()
        self.app.reload_prefs()
        self.assertEqual(len(self.app.tmux.globals_set), 3)

    def test_the_first_housekeep_sets_the_saved_scheme(self):
        self.write_scheme("terminal", 1)
        self.housekeep_once()
        self.assertEqual(dict(self.app.tmux.globals_set), core.SCHEME_TMUX["terminal"])

    def test_with_nothing_saved_the_first_housekeep_sets_terminal(self):
        self.housekeep_once()
        self.assertEqual(dict(self.app.tmux.globals_set), core.SCHEME_TMUX["terminal"])

    def test_a_tmux_error_is_said_once_and_the_scheme_still_counts_as_applied(self):
        self.app.tmux.set_global_option = mock.Mock(side_effect=sidebar.TmuxError("no"))
        self.write_scheme("light", 1)
        self.app.reload_prefs()
        self.app.reload_prefs()
        self.assertEqual(self.app.tmux.set_global_option.call_count, 1)


class ShadeTmux(ClientTmux):
    """ClientTmux that also records the pane options the selected row's shade is drawn with; `fail_pane_options` makes setting one raise."""

    def __init__(self):
        super().__init__()
        self.fail_pane_options = False

    def set_pane_option(self, pane, name, value):
        if self.fail_pane_options:
            raise TmuxError("no pane-colours")
        self.calls.append(("set_pane_option", pane, name, value))

    def unset_pane_option(self, pane, name):
        self.calls.append(("unset_pane_option", pane, name))


class TerminalShade(AppCase):
    REPLY = list("\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\")
    QUERY = core.BACKGROUND_QUERY

    def setUp(self):
        super().setUp()
        tmux = ShadeTmux()
        self.app.tmux = self.app.bar.tmux = tmux
        self.app.me = "%1"
        self.keys_seen = mock.Mock()
        self.app.handle_key = self.keys_seen
        self.timeouts = []
        self.scr.timeout = self.timeouts.append
        use = self.app.palette.use

        def logged(scheme, shade=False):
            tmux.calls.append(("use", scheme, shade))
            use(scheme, shade)

        self.app.palette.use = logged
        self.at = 100.0
        patch = mock.patch.object(sidebar.time, "monotonic", lambda: self.at)
        patch.start()
        self.addCleanup(patch.stop)

    def feed(self, *keys):
        self.scr.keys = list(keys)
        self.app.read_input()

    def calls(self, kind):
        return [c for c in self.app.tmux.calls if c[0] == kind]

    def shade(self, rgb):
        return core.selection_shade(rgb)

    def test_terminal_asks_the_background_and_dark_never_does(self):
        self.housekeep_once()
        self.assertEqual(TERMINAL_WRITES, [self.QUERY])
        self.write_scheme("dark", 1)
        self.at += 10
        self.housekeep_once()
        self.assertEqual(TERMINAL_WRITES, [self.QUERY])
        self.at += 10
        self.housekeep_once()
        self.assertEqual(TERMINAL_WRITES, [self.QUERY])

    def test_dark_asked_nothing_from_the_start(self):
        self.write_scheme("dark", 1)
        self.housekeep_once()
        self.at += 10
        self.housekeep_once()
        self.assertEqual(TERMINAL_WRITES, [])

    def test_an_unknown_background_is_asked_again_every_5_seconds(self):
        self.housekeep_once()
        for later, asked in ((1, 1), (4, 1), (5, 2)):
            self.at = 100.0 + later
            self.housekeep_once()
            self.assertEqual(len(TERMINAL_WRITES), asked, later)
        self.feed(*self.REPLY)
        self.at += 10
        self.housekeep_once()
        self.assertEqual(len(TERMINAL_WRITES), 2)

    def test_a_focus_report_asks_again(self):
        self.housekeep_once()
        self.app.read_focus = mock.Mock()
        self.feed("\x1b", "[", "I")
        self.assertEqual(TERMINAL_WRITES, [self.QUERY, self.QUERY])
        self.app.read_focus.assert_called_once()

    def test_a_background_reply_is_read_and_never_reaches_the_keys(self):
        self.feed(*self.REPLY)
        self.assertEqual(self.app.background, (30, 30, 30))
        self.keys_seen.assert_not_called()

    def test_a_bell_ended_reply_is_read_too(self):
        self.feed(*"\x1b]11;rgb:ffff/ffff/ffff\x07")
        self.assertEqual(self.app.background, (255, 255, 255))
        self.keys_seen.assert_not_called()

    def test_a_reply_cut_short_is_dropped(self):
        self.feed(*"\x1b]11;rgb:1e")
        self.keys_seen.assert_not_called()
        self.assertIsNone(self.app.background)
        self.assertEqual(self.timeouts[-1], sidebar.KEY_WAIT_MS)

    def test_a_reply_split_after_esc_bracket_is_still_read(self):
        def stall():
            raise curses.error("no input yet")

        for head in (["\x1b", "]"], list("\x1b]11;r")):
            with self.subTest(head=head):
                self.app.background = None
                self.timeouts.clear()
                rest = list(self.REPLY[len(head):])
                self.feed(*head, stall, *rest)
                self.assertEqual(self.app.background, (30, 30, 30))
                self.keys_seen.assert_not_called()
                self.assertIn(sidebar.REPLY_WAIT_MS, self.timeouts)
                self.assertEqual(self.timeouts[-1], sidebar.KEY_WAIT_MS)
                self.assertEqual(self.app.pending_keys, [])

    def test_esc_alone_still_reaches_the_keys(self):
        self.feed("\x1b")
        self.keys_seen.assert_called_once_with("\x1b")
        self.keys_seen.reset_mock()
        self.feed("\x1b", "]", "x")
        self.assertEqual([c.args[0] for c in self.keys_seen.call_args_list], ["\x1b", "]", "x"])

    def test_a_known_background_remaps_16_and_17_before_the_palette_switches(self):
        self.housekeep_once()
        self.app.tmux.calls.clear()
        self.feed(*self.REPLY)
        shade = self.shade((30, 30, 30))
        self.assertEqual(self.app.tmux.calls, [
            ("set_pane_option", "%1", "pane-colours[16]", shade["focused"]),
            ("set_pane_option", "%1", "pane-colours[17]", shade["unfocused"]),
            ("use", "terminal", True),
        ])

    def test_without_a_background_terminal_draws_the_reversed_bar(self):
        self.housekeep_once()
        self.assertEqual(self.app.palette.schemes[-1], "terminal")
        self.assertFalse(self.app.palette.shades[-1])
        self.assertEqual(self.calls("set_pane_option"), [])

    def test_a_pane_colours_error_keeps_the_reversed_bar(self):
        self.housekeep_once()
        self.app.tmux.fail_pane_options = True
        self.feed(*self.REPLY)
        self.assertFalse(self.app.palette.shades[-1])
        self.assertTrue(self.app.notice_error)
        self.app.notice, self.app.notice_error = "", False
        self.feed(*"\x1b]11;rgb:ffff/ffff/ffff\x07")
        self.assertEqual(self.app.notice, "")
        self.assertFalse(self.app.palette.shades[-1])

    def test_terminal_draws_the_rule_above_the_bar(self):
        self.app.window_width = 100
        self.housekeep_once()
        calls = self.app.tmux.calls
        options = [c for c in calls if c[0] == "set_view_option"]
        self.assertEqual(options, [("set_view_option", "status", "2"), ("set_view_option", "status-format[0]", core.BAR_RULE)])
        first_bar = next(i for i, c in enumerate(calls) if c[0] == "set_bar")
        self.assertEqual(calls[first_bar][2], 1)
        self.assertLess(first_bar, calls.index(options[0]))

    def test_switching_to_dark_takes_the_line_and_the_shade_away(self):
        self.app.window_width = 100
        self.housekeep_once()
        self.feed(*self.REPLY)
        self.app.tmux.calls.clear()
        self.write_scheme("dark", 1)
        self.app.reload_prefs()
        calls = self.app.tmux.calls
        self.assertEqual(self.calls("unset_pane_option"), [
            ("unset_pane_option", "%1", "pane-colours[16]"), ("unset_pane_option", "%1", "pane-colours[17]")])
        self.assertIn(("set_view_option", "status", "on"), calls)
        self.assertIn(("unset_view_option", "status-format[1]"), calls)
        self.assertEqual(self.calls("set_bar")[0][2], 0)
        self.assertEqual(self.calls("use"), [("use", "dark", False)])

    def test_the_same_reply_twice_changes_nothing(self):
        self.housekeep_once()
        self.feed(*self.REPLY)
        self.app.tmux.calls.clear()
        self.feed(*self.REPLY)
        self.assertEqual(self.app.tmux.calls, [])

    def test_a_new_background_moves_the_shade(self):
        self.housekeep_once()
        self.feed(*self.REPLY)
        self.feed(*"\x1b]11;rgb:ffff/ffff/ffff\x07")
        pane = self.calls("set_pane_option")
        self.assertEqual(pane[-2][2:], ("pane-colours[16]", self.shade((255, 255, 255))["focused"]))
        self.assertEqual(pane[-1][2:], ("pane-colours[17]", self.shade((255, 255, 255))["unfocused"]))


if __name__ == "__main__":
    unittest.main()
