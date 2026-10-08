import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from emx import __version__, core

ROWS = [
    {"id": "bg-1", "kind": "background", "name": "alpha", "cwd": "/tmp/x/proj", "state": "working", "status": "busy"},
    {"kind": "interactive", "name": "terminal", "cwd": "/tmp/y"},
    {"id": "bg-2", "kind": "background", "state": "blocked", "waitingFor": "approval"},
    {"id": "int-1", "kind": "interactive"},
    {"kind": "background"},
    {"id": "", "kind": "background"},
    {"id": 7, "kind": "background"},
]


class ParseAgents(unittest.TestCase):
    def test_valid_list(self):
        self.assertEqual(len(core.parse_agents(json.dumps(ROWS))), len(ROWS))

    def test_empty_list_is_fine(self):
        self.assertEqual(core.parse_agents("[]"), [])

    def test_non_dict_entries_dropped(self):
        self.assertEqual(core.parse_agents('[1, "x", null, {"id": "a"}]'), [{"id": "a"}])

    def test_bad_inputs_raise_agents_error_not_crash(self):
        for bad in ["", "   ", "{", "not json", "{}", '"str"', "null", "42", "[1,"]:
            with self.subTest(bad=bad):
                with self.assertRaises(core.AgentsError):
                    core.parse_agents(bad)


class BackgroundSessions(unittest.TestCase):
    def test_only_background_rows_with_id(self):
        got = core.background_sessions(core.parse_agents(json.dumps(ROWS)))
        self.assertEqual([s.id for s in got], ["bg-1", "bg-2"])

    def test_interactive_rows_hidden(self):
        rows = [{"kind": "interactive", "name": "n"}, {"kind": "interactive", "id": "x"}]
        self.assertEqual(core.background_sessions(rows), [])

    def test_order_preserved(self):
        rows = [{"id": c, "kind": "background"} for c in "cab"]
        self.assertEqual([s.id for s in core.background_sessions(rows)], ["c", "a", "b"])

    def test_missing_name_falls_back_to_id_and_fields_default_empty(self):
        s = core.background_sessions([{"id": "z", "kind": "background"}])[0]
        self.assertEqual((s.name, s.cwd, s.state, s.status, s.waiting_for), ("z", "", "", "", ""))

    def test_non_string_values_become_text(self):
        s = core.background_sessions([{"id": "z", "kind": "background", "state": 3, "waitingFor": {"a": 1}}])[0]
        self.assertEqual(s.state, "3")
        self.assertEqual(s.waiting_for, '{"a":1}')

    def test_empty(self):
        self.assertEqual(core.background_sessions([]), [])


def mk(**kw):
    base = dict(id="i", name="n", cwd="", state="working", status="", waiting_for="", started_at=None, running=True)
    base.update(kw)
    return core.Session(**base)


# The shapes `claude agents --json --all` (2.1.293) gave for throwaway sessions: running and done rows
# carry pid and status; a row stopped with `claude stop` has neither, and its state is "stopped".
REAL_ROWS = [
    {"pid": 30464, "id": "41673d92", "cwd": "<tmp>/proj-alpha", "kind": "background", "startedAt": 1791434633618,
     "sessionId": "<uuid>", "name": "emx-test-repo", "status": "idle", "state": "done"},
    {"id": "e1a0dfc9", "cwd": "<tmp>/alpha-feat-x", "kind": "background", "startedAt": 1791434634417,
     "sessionId": "<uuid>", "name": "emx-test-wt", "state": "stopped"},
]


class RunningOrStopped(unittest.TestCase):
    def test_real_rows(self):
        done, stopped = core.background_sessions(REAL_ROWS)
        self.assertEqual((done.running, core.category(done)), (True, core.DONE))
        self.assertEqual((stopped.running, core.category(stopped)), (False, core.STOPPED))
        self.assertEqual(stopped.started_at, 1791434634417.0)

    def test_no_pid_is_not_running_whatever_the_state(self):
        for state, shown in (("done", core.DONE), ("working", core.WORKING), ("blocked", core.NEEDS_INPUT), ("", core.WORKING)):
            with self.subTest(state=state):
                (s,) = core.background_sessions([{"id": "a", "kind": "background", "state": state}])
                self.assertFalse(s.running)
                self.assertEqual(core.category(s), shown)

    def test_display_follows_state_only_stopped_shows_stopped(self):
        (s,) = core.background_sessions([{"id": "a", "kind": "background", "state": "stopped"}])
        self.assertEqual((s.running, core.category(s), core.state_word(s)), (False, core.STOPPED, "Stopped"))

    def test_stopped_state_with_a_pid_is_still_stopped(self):
        (s,) = core.background_sessions([{"id": "a", "kind": "background", "pid": 5, "state": "stopped"}])
        self.assertFalse(s.running)

    def test_odd_pid_and_started_at_values(self):
        for pid in (True, "12", None, 1.5):
            with self.subTest(pid=pid):
                (s,) = core.background_sessions([{"id": "a", "kind": "background", "pid": pid, "startedAt": True}])
                self.assertFalse(s.running)
                self.assertIsNone(s.started_at)


class StateWords(unittest.TestCase):
    def test_categories_and_words(self):
        cases = [
            (mk(state="working"), core.WORKING, "Working"),
            (mk(state="blocked"), core.NEEDS_INPUT, "Needs input"),
            (mk(state="done"), core.DONE, "Done"),
            (mk(state="done", running=False), core.DONE, "Done"),
            (mk(state="stopped", running=False), core.STOPPED, "Stopped"),
            (mk(state="weird"), core.WORKING, "weird"),
            (mk(state="", status="busy"), core.WORKING, "busy"),
            (mk(state=""), core.WORKING, "Working"),
        ]
        for s, cat, word in cases:
            with self.subTest(state=s.state, running=s.running):
                self.assertEqual((core.category(s), core.state_word(s)), (cat, word))

    def test_glyphs(self):
        self.assertEqual(core.glyph(core.NEEDS_INPUT, 3), "✻")
        self.assertEqual(core.glyph(core.DONE, 3), "✻")
        self.assertEqual(core.glyph(core.STOPPED, 3), "∙")
        frames = {core.glyph(core.WORKING, f) for f in range(len(core.WORKING_FRAMES))}
        self.assertGreater(len(frames), 1)
        self.assertEqual(core.glyph(core.WORKING, 0), core.glyph(core.WORKING, len(core.WORKING_FRAMES)))


class Ages(unittest.TestCase):
    def test_format_age(self):
        cases = [(None, ""), (-5, "0s"), (0, "0s"), (4.9, "4s"), (59, "59s"), (60, "1m"), (12 * 60 + 59, "12m"),
                 (3599, "59m"), (3600, "1h"), (3 * 3600 + 1, "3h"), (86399, "23h"), (86400, "1d"), (2 * 86400 + 5, "2d")]
        for seconds, want in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(core.format_age(seconds), want)

    def test_session_age_reads_milliseconds(self):
        self.assertEqual(core.session_age(mk(started_at=1_000_000.0), 1_000_000 + 4_000), "4s")
        self.assertEqual(core.session_age(mk(started_at=None), 5_000), "")

    def test_an_interrupted_session_reads_idle(self):
        interrupted = mk(state="working", status="idle")
        self.assertEqual(core.state_word(interrupted), "Idle")
        self.assertEqual(core.category(interrupted), core.WORKING)
        for f in range(len(core.WORKING_FRAMES) + 1):
            with self.subTest(frame=f):
                self.assertEqual(core.session_glyph(interrupted, f), "✻")
        busy = mk(state="working", status="busy")
        self.assertEqual(core.state_word(busy), "Working")
        self.assertGreater(len({core.session_glyph(busy, f) for f in range(len(core.WORKING_FRAMES))}), 1)
        self.assertEqual(core.state_word(mk(state="done", status="idle")), "Done")


class ReopenRule(unittest.TestCase):
    """`claude attach` resumes a session with no live process, so only his explicit open may do it."""

    def test_stopped_session_opens_only_on_his_explicit_open(self):
        stopped = mk(running=False, state="stopped")
        self.assertTrue(core.attach_allowed(stopped, core.OPEN))
        for trigger in (core.RESTART, core.NEW, "respawn", "housekeep", "upgrade", "", "OPEN"):
            for fresh in (True, False):
                with self.subTest(trigger=trigger, fresh=fresh):
                    self.assertFalse(core.attach_allowed(stopped, trigger, fresh=fresh))

    def test_done_session_without_a_process_reopens_only_on_open(self):
        exited = core.background_sessions([{"id": "a", "kind": "background", "state": "done"}])[0]
        self.assertFalse(core.attach_allowed(exited, core.RESTART, fresh=True))
        self.assertTrue(core.attach_allowed(exited, core.OPEN))

    def test_running_session_attaches_for_known_triggers_on_a_fresh_listing(self):
        running = mk(state="working")
        self.assertTrue(core.attach_allowed(running, core.OPEN))
        for trigger in (core.RESTART, core.NEW):
            with self.subTest(trigger=trigger):
                self.assertTrue(core.attach_allowed(running, trigger, fresh=True))
        self.assertFalse(core.attach_allowed(running, "respawn", fresh=True))

    def test_a_row_from_an_old_poll_is_never_enough_except_for_open(self):
        running = mk(state="working")
        for trigger in (core.RESTART, core.NEW, "respawn"):
            with self.subTest(trigger=trigger):
                self.assertFalse(core.attach_allowed(running, trigger))
                self.assertFalse(core.attach_allowed(running, trigger, fresh=False))

    def test_unlisted_session_never_attaches(self):
        for trigger in (core.OPEN, core.RESTART, core.NEW, "respawn"):
            for fresh in (True, False):
                with self.subTest(trigger=trigger, fresh=fresh):
                    self.assertFalse(core.attach_allowed(None, trigger, fresh=fresh))


class AttachAllowed(unittest.TestCase):
    def test_interrupt_attaches_only_a_running_session_on_a_fresh_listing(self):
        running, stopped = mk(state="working"), mk(state="stopped", running=False)
        self.assertTrue(core.attach_allowed(running, core.INTERRUPT, True))
        self.assertFalse(core.attach_allowed(running, core.INTERRUPT, False))
        self.assertFalse(core.attach_allowed(stopped, core.INTERRUPT, True))
        self.assertFalse(core.attach_allowed(None, core.INTERRUPT, True))
        self.assertTrue(core.attach_allowed(stopped, core.OPEN))
        for trigger in (core.RESTART, core.NEW):
            with self.subTest(trigger=trigger):
                self.assertTrue(core.attach_allowed(running, trigger, True))
                self.assertFalse(core.attach_allowed(running, trigger, False))
                self.assertFalse(core.attach_allowed(stopped, trigger, True))

    def test_an_interrupt_refusal_says_no_viewer_was_opened(self):
        self.assertIn("Couldn't list", core.refusal("emx-test-a", core.INTERRUPT, False, None))
        self.assertIn("isn't listed", core.refusal("emx-test-a", core.INTERRUPT, True, None))
        stopped = mk(state="stopped", running=False)
        self.assertEqual(core.refusal("emx-test-a", core.INTERRUPT, True, stopped),
                         "emx-test-a isn't running, so emx didn't open a viewer to interrupt it.")


class PlacementFromGit(unittest.TestCase):
    def test_main_checkout(self):
        got = core.placement_from_git("/r/app", "/r/app/.git\n/r/app\n", "main\n")
        self.assertEqual(got, core.Placement("/r/app"))

    def test_subfolder_of_main_checkout_groups_under_the_repo(self):
        self.assertEqual(core.placement_from_git("/r/app/src", "/r/app/.git\n/r/app\n", "main"), core.Placement("/r/app"))

    def test_worktree_groups_under_its_main_repo_with_its_branch(self):
        got = core.placement_from_git("/w/feat", "/r/app/.git\n/w/feat\n", "feat-x\n")
        self.assertEqual(got, core.Placement("/r/app", "feat-x"))

    def test_detached_worktree_shows_its_folder(self):
        got = core.placement_from_git("/w/feat-dir/src", "/r/app/.git\n/w/feat-dir\n", None)
        self.assertEqual(got, core.Placement("/r/app", "feat-dir"))

    def test_git_failure_or_odd_output_falls_back_to_the_cwd(self):
        for out in (None, "", "\n", "/r/app/.git\n", ".git\n/r/app\n", "../.git\nrel\n"):
            with self.subTest(out=out):
                self.assertEqual(core.placement_from_git("/gone", out, "main"), core.Placement("/gone"))

    def test_bare_repo_or_submodule_groups_by_its_own_top_level(self):
        self.assertEqual(core.placement_from_git("/w/x", "/r/app.git\n/w/x\n", "x"), core.Placement("/w/x"))
        sub = core.placement_from_git("/r/app/lib", "/r/app/.git/modules/lib\n/r/app/lib\n", "main")
        self.assertEqual(sub, core.Placement("/r/app/lib"))

    def test_trailing_slashes_are_normalised(self):
        self.assertEqual(core.placement_from_git("/r/app", "/r/app/.git/\n/r/app/\n", "main"), core.Placement("/r/app"))

    def test_folder_under_a_repo_at_home_groups_by_itself(self):
        out = "/h/me/.git\n/h/me\nnotes/\n"
        self.assertEqual(core.placement_from_git("/h/me/notes", out, "main", "/h/me"), core.Placement("/h/me/notes"))
        self.assertEqual(core.placement_from_git("/h/me", "/h/me/.git\n/h/me\n\n", "main", "/h/me"), core.Placement("/h/me"))
        self.assertEqual(core.placement_from_git("/h/me/notes", out, "main", "/h/me/"), core.Placement("/h/me/notes"))

    def test_a_worktree_of_the_home_repo_groups_by_itself(self):
        got = core.placement_from_git("/w/dots-wt", "/h/me/.git\n/w/dots-wt\n\n", "tweak", "/h/me")
        self.assertEqual(got, core.Placement("/w/dots-wt"))

    def test_a_repo_inside_home_still_groups_as_a_repo(self):
        got = core.placement_from_git("/h/me/code/app/src", "/h/me/code/app/.git\n/h/me/code/app\nsrc/\n", "main", "/h/me")
        self.assertEqual(got, core.Placement("/h/me/code/app"))
        wt = core.placement_from_git("/h/me/wt/feat", "/h/me/code/app/.git\n/h/me/wt/feat\n\n", "feat", "/h/me")
        self.assertEqual(wt, core.Placement("/h/me/code/app", "feat"))

    def test_git_real_paths_come_back_in_the_cwd_form(self):
        got = core.placement_from_git("/tmp/x/app/src", "/private/tmp/x/app/.git\n/private/tmp/x/app\nsrc/\n", "main")
        self.assertEqual(got, core.Placement("/tmp/x/app"))
        wt = core.placement_from_git("/tmp/x/feat", "/private/tmp/x/app/.git\n/private/tmp/x/feat\n\n", "feat")
        self.assertEqual(wt, core.Placement("/tmp/x/app", "feat"))

    def test_a_symlinked_folder_maps_only_what_sits_under_it(self):
        out = "/data/repos/app/.git\n/data/repos/app\nlib/\n"
        self.assertEqual(core.placement_from_git("/h/me/app/lib", out, "main"), core.Placement("/h/me/app"))
        wt_elsewhere = "/data/repos/app/.git\n/scratch/feat\n\n"
        self.assertEqual(core.placement_from_git("/h/me/feat", wt_elsewhere, "feat"), core.Placement("/data/repos/app", "feat"))

    def test_a_rewrite_that_does_not_lead_back_to_git_keeps_gits_path(self):
        links = {"/h/me/solo/feat": "/data/wt/feat", "/h/me/solo/app": "/h/me/solo/app", "/tmp/x/app": "/private/tmp/x/app"}
        real = links.get
        # /h/me/solo/feat is a symlink to the worktree; the guessed /h/me/solo/app doesn't exist.
        wt = core.placement_from_git("/h/me/solo/feat", "/data/repos/app/.git\n/data/wt/feat\n\n", "feat", real=real)
        self.assertEqual(wt, core.Placement("/data/repos/app", "feat"))
        tmp = core.placement_from_git("/tmp/x/app", "/private/tmp/x/app/.git\n/private/tmp/x/app\n\n", "main", real=real)
        self.assertEqual(tmp, core.Placement("/tmp/x/app"))

    def test_realpath_is_not_asked_when_nothing_was_rewritten(self):
        def real(path):
            raise AssertionError(f"realpath({path})")

        got = core.placement_from_git("/r/app/src", "/r/app/.git\n/r/app\nsrc/\n", "main", real=real)
        self.assertEqual(got, core.Placement("/r/app"))

    def test_a_prefix_the_cwd_does_not_end_in_leaves_git_paths_alone(self):
        got = core.placement_from_git("/tmp/x/other", "/private/tmp/x/app/.git\n/private/tmp/x/app\nsrc/\n", "main")
        self.assertEqual(got, core.Placement("/private/tmp/x/app"))

    def test_retry_after_doubles_up_to_a_cap(self):
        self.assertEqual([core.retry_after(n) for n in (1, 2, 3)], [5.0, 10.0, 20.0])
        self.assertEqual(core.retry_after(50), float(core.RETRY_MAX_SECONDS))
        self.assertEqual(core.retry_after(0), 5.0)


class Grouping(unittest.TestCase):
    SESSIONS = [
        mk(id="z1", cwd="/r/zeta", started_at=50.0),
        mk(id="a2", cwd="/w/feat", started_at=20.0, state="blocked"),
        mk(id="a1", cwd="/r/Alpha", started_at=30.0, state="done"),
        mk(id="a0", cwd="/r/Alpha/sub", started_at=None),
        mk(id="b1", cwd="/x/beta", started_at=10.0, state="stopped", running=False),
        mk(id="a3", cwd="/r/Alpha", started_at=20.0),
    ]
    PLACEMENTS = {
        "/w/feat": core.Placement("/r/Alpha", "feat"),
        "/r/Alpha/sub": core.Placement("/r/Alpha"),
    }

    def test_by_project(self):
        groups = core.group_sessions(self.SESSIONS, self.PLACEMENTS, core.PROJECT)
        self.assertEqual([g.key for g in groups], ["/r/Alpha", "/x/beta", "/r/zeta"])
        self.assertEqual([s.id for s in groups[0].sessions], ["a2", "a3", "a1", "a0"])

    def test_by_state_in_fixed_order_without_empty_groups(self):
        groups = core.group_sessions(self.SESSIONS, self.PLACEMENTS, core.BY_STATE)
        self.assertEqual([g.key for g in groups], [core.NEEDS_INPUT, core.WORKING, core.DONE, core.STOPPED])
        self.assertEqual([s.id for s in groups[1].sessions], ["a3", "z1", "a0"])
        only_done = core.group_sessions([mk(state="done")], {}, core.BY_STATE)
        self.assertEqual([g.key for g in only_done], [core.DONE])

    def test_same_folder_name_sorts_by_full_path(self):
        groups = core.group_sessions([mk(id="1", cwd="/b/app"), mk(id="2", cwd="/a/app")], {}, core.PROJECT)
        self.assertEqual([g.key for g in groups], ["/a/app", "/b/app"])

    def test_empty(self):
        self.assertEqual(core.group_sessions([], {}, core.PROJECT), [])


class Navigation(unittest.TestCase):
    GROUPS = [
        core.Group("/a", (mk(id="1"), mk(id="2"))),
        core.Group("/b", (mk(id="3"),)),
    ]

    def test_panel_lines_with_blank_between_groups(self):
        self.assertEqual(core.panel_lines(self.GROUPS, frozenset()), [
            ("group", "/a"), ("session", "1"), ("session", "2"), None, ("group", "/b"), ("session", "3"),
        ])

    def test_collapsed_group_shows_only_its_header(self):
        self.assertEqual(core.panel_lines(self.GROUPS, {"/a"}), [("group", "/a"), None, ("group", "/b"), ("session", "3")])

    def test_move_cursor_over_headers_and_sessions(self):
        items = [i for i in core.panel_lines(self.GROUPS, frozenset()) if i]
        self.assertEqual(core.move_cursor(items, ("session", "2"), 1), ("group", "/b"))
        self.assertEqual(core.move_cursor(items, ("group", "/b"), -1), ("session", "2"))
        self.assertEqual(core.move_cursor(items, ("group", "/a"), -1), ("group", "/a"))
        self.assertEqual(core.move_cursor(items, ("session", "3"), 1), ("session", "3"))
        self.assertEqual(core.move_cursor(items, ("session", "gone"), 1), ("group", "/a"))
        self.assertIsNone(core.move_cursor([], None, 1))

    def test_settle_cursor(self):
        open_items = [i for i in core.panel_lines(self.GROUPS, frozenset()) if i]
        folded = [i for i in core.panel_lines(self.GROUPS, {"/a"}) if i]
        self.assertEqual(core.settle_cursor(open_items, ("session", "2"), self.GROUPS), ("session", "2"))
        self.assertEqual(core.settle_cursor(folded, ("session", "2"), self.GROUPS), ("group", "/a"))
        self.assertEqual(core.settle_cursor(folded, ("session", "gone"), self.GROUPS), ("group", "/a"))
        self.assertEqual(core.settle_cursor(folded, ("group", "/gone"), self.GROUPS), ("group", "/a"))
        self.assertEqual(core.settle_cursor(folded, None, self.GROUPS), ("group", "/a"))
        self.assertIsNone(core.settle_cursor([], ("session", "1"), []))

    def test_a_wanted_session_takes_the_cursor_once_listed(self):
        items = [i for i in core.panel_lines(self.GROUPS, frozenset()) if i]
        here = ("session", "2")
        self.assertEqual(core.settle_cursor(items, here, self.GROUPS, ("session", "new")), here)
        self.assertEqual(core.settle_cursor(items, here, self.GROUPS, ("session", "3")), ("session", "3"))
        self.assertEqual(core.settle_cursor(items, here, self.GROUPS, None), here)

    def test_focus_from_main_puts_the_cursor_on_the_session_there(self):
        open_items = [i for i in core.panel_lines(self.GROUPS, frozenset()) if i]
        folded = [i for i in core.panel_lines(self.GROUPS, {"/a"}) if i]
        here = ("session", "3")
        self.assertEqual(core.cursor_on_focus(open_items, here, self.GROUPS, "2"), ("session", "2"))
        self.assertEqual(core.cursor_on_focus(folded, here, self.GROUPS, "2"), ("group", "/a"))
        for main_sid in ("", "gone"):
            with self.subTest(main_sid=main_sid):
                self.assertEqual(core.cursor_on_focus(open_items, here, self.GROUPS, main_sid), here)

    BEFORE = [("group", "/g1"), ("session", "a"), ("session", "b"), ("session", "c"), ("group", "/g2"), ("session", "d")]

    def after(self, *gone):
        items = [i for i in self.BEFORE if i not in gone]
        groups = [core.Group("/g1", tuple(mk(id=s) for s in "abc" if ("session", s) not in gone)),
                  core.Group("/g2", tuple(mk(id=s) for s in "d" if ("session", s) not in gone))]
        return items, [g for g in groups if ("group", g.key) in items]

    def test_removing_a_middle_row_lands_on_the_row_below(self):
        items, groups = self.after(("session", "b"))
        self.assertEqual(core.settle_cursor(items, ("session", "b"), groups, before=self.BEFORE), ("session", "c"))

    def test_removing_the_last_line_lands_above(self):
        items, groups = self.after(("group", "/g2"), ("session", "d"))
        self.assertEqual(core.settle_cursor(items, ("session", "d"), groups, before=self.BEFORE), ("session", "c"))

    def test_removing_a_whole_group_lands_on_the_next_line(self):
        gone = (("group", "/g1"), ("session", "a"), ("session", "b"), ("session", "c"))
        items, groups = self.after(*gone)
        self.assertEqual(core.settle_cursor(items, ("session", "b"), groups, before=self.BEFORE), ("group", "/g2"))
        self.assertEqual(core.settle_cursor(items, ("group", "/g1"), groups, before=self.BEFORE), ("group", "/g2"))

    def test_a_folded_session_still_lands_on_its_header(self):
        groups = [core.Group("/g1", (mk(id="a"), mk(id="b"), mk(id="c"))), core.Group("/g2", (mk(id="d"),))]
        items = [i for i in core.panel_lines(groups, {"/g1"}) if i]
        self.assertEqual(core.settle_cursor(items, ("session", "b"), groups, before=self.BEFORE), ("group", "/g1"))

    def test_without_before_the_old_fallback_holds(self):
        items, groups = self.after(("session", "b"))
        self.assertEqual(core.settle_cursor(items, ("session", "b"), groups), ("group", "/g1"))
        self.assertEqual(core.settle_cursor(items, ("session", "zz"), groups, before=self.BEFORE), ("group", "/g1"))
        self.assertIsNone(core.settle_cursor([], ("session", "b"), [], before=self.BEFORE))

    def test_cursor_group(self):
        self.assertEqual(core.cursor_group(("session", "3"), self.GROUPS), "/b")
        self.assertEqual(core.cursor_group(("group", "/a"), self.GROUPS), "/a")
        self.assertIsNone(core.cursor_group(("session", "gone"), self.GROUPS))
        self.assertIsNone(core.cursor_group(None, self.GROUPS))


class Summary(unittest.TestCase):
    SESSIONS = [
        mk(state="blocked"), mk(state="working"), mk(state="working"), mk(state="done"), mk(state="stopped", running=False),
    ]

    def test_counts_nonzero_only_in_order(self):
        self.assertEqual(core.summary(self.SESSIONS), [
            ("1 waiting", core.NEEDS_INPUT), ("2 working", core.WORKING), ("1 done", core.DONE), ("1 stopped", core.STOPPED),
        ])
        self.assertEqual(core.summary([mk(state="done")]), [("1 done", core.DONE)])
        self.assertEqual(core.summary([]), [])

    def text(self, placed):
        line = ""
        for x, text, _ in placed:
            line = line.ljust(x) + text
        return line

    def test_the_top_line_gives_up_words_then_name_then_counts(self):
        cases = {
            62: "⚒ emalatxana 1.0.0  1 waiting · 2 working · 1 done · 1 stopped",
            61: "⚒ emalatxana 1.0.0          1 wait · 2 work · 1 done · 1 stop",
            53: "⚒ emalatxana 1.0.0  1 wait · 2 work · 1 done · 1 stop",
            52: "⚒ 1.0.0            1 wait · 2 work · 1 done · 1 stop",
            42: "⚒ 1.0.0  1 wait · 2 work · 1 done · 1 stop",
            41: "⚒ 1.0.0  1 wait · 2 work · 1 done · 1 st…",
            38: "⚒ 1.0.0  1 wait · 2 work · 1 done · 1…",
            30: "⚒ 1.0.0  1 wait · 2 work · 1…",
            10: "⚒ 1.0.0  …",
            9: "⚒ 1.0.0",
            7: "⚒ 1.0.0",
            6: "⚒  1…",
            4: "⚒  …",
            3: "⚒",
            1: "⚒",
            0: "",
        }
        for width, want in cases.items():
            with self.subTest(width=width):
                self.assertEqual(self.text(core.top_line(self.SESSIONS, "1.0.0", width)), want)

    def test_the_version_stays_at_the_default_width(self):
        for count in range(1, 5):
            with self.subTest(categories=count):
                sessions = [mk(state=state) for state in ("blocked", "working", "done", "stopped")[:count]]
                placed = core.top_line(sessions, "1.0.0", 38)
                self.assertIn(("1.0.0", "version"), [(t, tag) for _, t, tag in placed])

    def test_the_name_goes_before_the_version(self):
        for width in range(0, 81):
            with self.subTest(width=width):
                tags = [tag for _, _, tag in core.top_line(self.SESSIONS, "1.0.0", width)]
                if "name" in tags:
                    self.assertIn("version", tags)

    def test_one_count_fits_beside_the_whole_logo_in_a_default_panel(self):
        placed = core.top_line([mk(state="working"), mk(state="working")], "1.0.0", 38)
        self.assertEqual(self.text(placed), "⚒ emalatxana 1.0.0           2 working")
        self.assertEqual([tag for _, _, tag in placed], ["logo", "name", "version", core.WORKING])

    def test_the_top_line_before_the_first_listing_and_with_no_sessions(self):
        self.assertEqual(self.text(core.top_line([], "1.0.0", 38, loaded=False)), "⚒ emalatxana 1.0.0                   …")
        self.assertEqual(self.text(core.top_line([], "1.0.0", 38)), "⚒ emalatxana 1.0.0         no sessions")

    def test_the_top_line_never_overflows_and_always_keeps_the_logo(self):
        self.assertEqual(core.display_width(core.LOGO), 1)
        for sessions in ([], self.SESSIONS):
            for loaded in (True, False):
                for width in range(0, 81):
                    with self.subTest(sessions=len(sessions), loaded=loaded, width=width):
                        placed = core.top_line(sessions, "10.20.30", width, loaded)
                        end = max((x + core.display_width(t) for x, t, _ in placed), default=0)
                        self.assertLessEqual(end, width)
                        if width:
                            self.assertEqual(placed[0], (0, core.LOGO, "logo"))
                        xs = [x for x, _, _ in placed]
                        self.assertEqual(xs, sorted(xs))

    def test_fit_parts_never_exceeds_width(self):
        parts = [("1 waiting", 1), ("22 working", 2), ("3 done", 3)]
        for width in range(0, 40):
            with self.subTest(width=width):
                placed = core.fit_parts(parts, width)
                end = max((x + core.display_width(t) for x, t, _ in placed), default=0)
                self.assertLessEqual(end, width)


class Paths(unittest.TestCase):
    def test_fit_left(self):
        self.assertEqual(core.fit_left("/a/b/c", 10), "/a/b/c")
        self.assertEqual(core.fit_left("/a/b/c", 4), "…b/c")
        self.assertEqual(core.fit_left("/a/b/c", 1), "…")
        self.assertEqual(core.fit_left("/a/b/c", 0), "")
        for w in range(0, 10):
            self.assertLessEqual(core.display_width(core.fit_left("/日本/語のテキスト", w)), w)

    def test_short_path(self):
        self.assertEqual(core.short_path("/h/me/code/app", 40, "/h/me"), "~/code/app")
        self.assertEqual(core.short_path("/h/me", 40, "/h/me/"), "~")
        self.assertEqual(core.short_path("/h/meow/app", 40, "/h/me"), "/h/meow/app")
        self.assertEqual(core.short_path("/h/me/code/app", 7, "/h/me"), "~/…/app")

    def test_short_path_cuts_only_at_slashes(self):
        cases = [
            ("/h/me/work/clients/acme/app", 20, "~/…/clients/acme/app"),
            ("/h/me/work/clients/acme/app", 14, "~/…/acme/app"),
            ("/h/me/work/clients/acme/app", 11, "…/acme/app"),
            ("/h/me/work/clients/acme/app", 8, "~/…/app"),
            ("/h/me/work/clients/acme/app", 5, "…/app"),
            ("/srv/data/projects/app", 15, "…/projects/app"),
            ("/srv/data/projects/app", 4, "app"),
            ("/srv/data/projects/a-long-name", 6, "a-lon…"),
        ]
        for path, width, want in cases:
            with self.subTest(path=path, width=width):
                self.assertEqual(core.short_path(path, width, "/h/me"), want)
        for width in range(0, 30):
            got = core.short_path("/h/me/work/clients/acme/app", width, "/h/me")
            self.assertLessEqual(core.display_width(got), width)


def rendered(parts):
    """The line the parts draw, padded by display columns; parts must not overlap."""
    line = ""
    for x, text, _ in parts:
        used = core.display_width(line)
        assert x >= used, (x, line)
        line += " " * (x - used) + text
    return line


class RowLayout(unittest.TestCase):
    def row(self, width, name="emx-test-perm", worktree="", word="Needs input", detail="", age="12m", align=0):
        return core.row_layout(name, worktree, word, detail, age, width, align)

    def test_full_row_with_worktree(self):
        got = rendered(self.row(60, name="emx-test-wt", worktree="feat-x", word="Working"))
        self.assertEqual(got, "   emx-test-wt ⎇ feat-x  Working" + " " * 24 + "12m")

    def test_age_is_right_aligned_one_column_in(self):
        for width in (30, 40, 60):
            for age in ("4s", "12m", "3h"):
                with self.subTest(width=width, age=age):
                    parts = self.row(width, age=age)
                    x, text, part = parts[-1]
                    self.assertEqual((part, x + core.display_width(text)), ("age", width - 1))

    def test_detail_shortens_then_branch_goes_and_mark_stays(self):
        got = rendered(self.row(50, detail="approve Bash: touch made-by-test.txt"))
        self.assertEqual(got, "   emx-test-perm  Needs input · approve Bash… 12m")
        got = rendered(self.row(41, name="emx-test-wt", worktree="feat-x", word="Needs input", detail="approve it"))
        self.assertEqual(got, "   emx-test-wt ⎇ feat-x  Needs input 12m")
        got = rendered(self.row(36, name="emx-test-wt", worktree="feat-x"))
        self.assertEqual(got, "   emx-test-wt ⎇  Needs input   12m")

    def test_tiny_detail_is_left_out(self):
        got = rendered(self.row(40, detail="approve Bash: touch x"))
        self.assertEqual(got, "   emx-test-perm  Needs input       12m")

    def test_at_30_columns_the_state_word_goes_whole_before_the_name_is_cut(self):
        self.assertEqual(rendered(self.row(30)), "   emx-test-perm" + " " * 10 + "12m")
        self.assertEqual(rendered(self.row(30, name="emx-test-stopped", word="Stopped", age="2d")),
                         "   emx-test-stopped" + " " * 8 + "2d")

    def test_the_state_word_is_never_cut(self):
        for width in range(0, 70):
            for name in ("x", "emx-test-perm", "emx-test-a-very-long-session-name"):
                with self.subTest(width=width, name=name):
                    statuses = [t for _, t, p in self.row(width, name=name, detail="approve it") if p == "status"]
                    for status in statuses:
                        self.assertTrue(status.startswith("Needs input"), status)

    def test_worktree_mark_survives_at_40_by_shortening_its_branch(self):
        got = rendered(self.row(40, name="emx-test-wt", worktree="feat-x"))
        self.assertEqual(got, "   emx-test-wt ⎇ feat…  Needs input 12m")
        got = rendered(self.row(40, name="emx-test-wt", worktree="feature/3-grouped-panel", word="Working"))
        self.assertEqual(got, "   emx-test-wt ⎇ feature/…  Working 12m")

    def test_states_line_up_to_the_column_given(self):
        a = self.row(40, name="emx-test-a", word="Working", age="1m", align=13)
        b = self.row(40, name="emx-test-long", word="Done", age="2m", align=13)
        status_x = [next(x for x, _, p in parts if p == "status") for parts in (a, b)]
        self.assertEqual(status_x, [3 + 13 + 2] * 2)
        self.assertEqual(rendered(a), "   emx-test-a     Working" + " " * 12 + "1m")

    def test_a_row_that_cannot_afford_the_alignment_ignores_it(self):
        got = self.row(30, name="emx-test", word="Needs input", age="1m", align=13)
        self.assertEqual(rendered(got), "   emx-test  Needs input" + " " * 3 + "1m")

    def test_no_state_word_leaves_the_detail(self):
        got = rendered(self.row(40, word="", detail="approve Bash: x"))
        self.assertEqual(got, "   emx-test-perm  approve Bash: x   12m")
        self.assertEqual(rendered(self.row(40, word="")), "   emx-test-perm" + " " * 20 + "12m")
        got = rendered(self.row(30, word="", detail="approve Bash: touch x"))
        self.assertEqual(got, "   emx-test-perm  approv… 12m")

    def test_name_column(self):
        def column(names, width=40):
            return core.name_column([(n, "") for n in names], width)

        self.assertEqual(column(["emx-test-a", "emx-test-long"]), 13)
        self.assertEqual(column(["x", "emx-test-a-very-long-session-name"]), 1)
        self.assertEqual(column(["emx-test-quick", "emx-test-stopped", "emx-test-a-very-long-session-name"]), 16)
        self.assertEqual(column(["emx-test-a-very-long-session-name"]), 0)
        self.assertEqual(column([]), 0)
        self.assertEqual(core.row_label("emx-test-wt", "feat-x"), "emx-test-wt ⎇ feat-x")
        self.assertEqual(core.row_label("emx-test-wt", ""), "emx-test-wt")

    def test_states_line_up_in_a_group_with_a_worktree_row(self):
        groups = [
            [("emx-test-a", ""), ("emx-test-wt", "feat-x")],
            [("emx-test-a", ""), ("emx-test-wt", "feature/3-grouped-panel")],
            [("emx-test-wt1", "feature/3-grouped-panel"), ("emx-test-wt2", "fix/x")],
        ]
        for group in groups:
            for width in range(26, 81):
                column = core.name_column(group, width)
                with self.subTest(group=group, width=width, column=column):
                    rows = [self.row(width, name=n, worktree=w, word="Working", align=column) for n, w in group]
                    status_x = [next((x for x, _, p in parts if p == "status"), None) for parts in rows]
                    if None not in status_x:
                        self.assertEqual(len(set(status_x)), 1, [rendered(r) for r in rows])
                    for (n, w), parts in zip(group, rows):
                        if w and next(t for _, t, p in parts if p == "name") == n:
                            self.assertIn("⎇", rendered(parts))
                    if group[0][1] == "" and width in (40, 50, 60):
                        self.assertEqual(status_x, [3 + column + 2] * len(rows))
                        self.assertIn("⎇", rendered(rows[-1]))
        long = [("emx-test-a", ""), ("emx-test-wt", "feature/3-grouped-panel")]
        self.assertIn("⎇ feature/3-gro…  Working", rendered(self.row(60, "emx-test-wt", "feature/3-grouped-panel", "Working",
                                                                      align=core.name_column(long, 60))))

    def test_a_row_alone_in_its_group_keeps_its_whole_branch(self):
        alone = [("emx-test-wt", "feature/3-grouped-panel")]
        self.assertEqual(core.name_column(alone, 40), 0)
        got = rendered(self.row(40, "emx-test-wt", "feature/3-grouped-panel", "Working", align=core.name_column(alone, 40)))
        self.assertEqual(got, "   emx-test-wt ⎇ feature/…  Working 12m")

    def test_long_name_is_cut_to_keep_the_state_word(self):
        got = rendered(self.row(40, name="emx-test-a-very-long-session-name", age="3h"))
        self.assertEqual(got, "   emx-test-a-very-lon…  Needs input 3h")

    def test_name_takes_everything_when_nothing_else_fits(self):
        got = rendered(self.row(30, name="emx-test-a-very-long-session-name", age="3h"))
        self.assertEqual(got, "   emx-test-a-very-long-s… 3h")

    def test_never_wider_than_the_pane_less_one(self):
        for width in range(0, 70):
            for name in ("x", "emx-test-a-very-long-session-name", "日本語の名前"):
                with self.subTest(width=width, name=name):
                    line = rendered(self.row(width, name=name, worktree="feat", detail="approve Bash: x"))
                    self.assertLessEqual(core.display_width(line), max(0, width - 1))

    def test_no_age(self):
        got = self.row(30, age="")
        self.assertNotIn("age", [p for _, _, p in got])


class RowLines(unittest.TestCase):
    """A row too long for the panel wraps onto indented lines instead of being cut."""

    NAMES = ("x", "emx-test-perm", "emx-test-a-very-long-session-name", "日本語の名前", "emx test 🎉 party")
    DETAILS = ("", "approve Bash: x", "Running the whole unittest suite with verbose output", "完了しました 🎉 すべて合格")

    def lines(self, width, name="emx-test-perm", worktree="", word="Needs input", detail="", age="12m", align=0):
        return core.row_lines(name, worktree, word, detail, age, width, align)

    def test_a_row_that_fits_is_row_layouts_line_unchanged(self):
        self.assertEqual(self.lines(60, name="emx-test-wt", worktree="feat-x", word="Working"),
                         [core.row_layout("emx-test-wt", "feat-x", "Working", "", "12m", 60)])
        a = self.lines(40, name="emx-test-a", word="Working", age="1m", align=13)
        self.assertEqual(a, [core.row_layout("emx-test-a", "", "Working", "", "1m", 40, 13)])
        self.assertEqual(rendered(a[0]), "   emx-test-a     Working" + " " * 12 + "1m")

    def test_at_40_columns_the_detail_wraps_under_the_row_and_nothing_is_cut(self):
        got = [rendered(p) for p in self.lines(40, detail="approve Bash: touch made-by-test.txt")]
        self.assertEqual(got, [
            "   emx-test-perm  Needs input       12m",
            "     approve Bash: touch",
            "     made-by-test.txt",
        ])

    def test_at_60_columns_the_detail_continues_after_the_state_word(self):
        got = [rendered(p) for p in self.lines(60, detail="approve Bash: touch made-by-test.txt and more words")]
        self.assertEqual(got, [
            "   emx-test-perm  Needs input · approve Bash: touch     12m",
            "     made-by-test.txt and more words",
        ])

    def test_a_long_name_and_branch_wrap_whole_and_the_age_stays_on_the_first_line(self):
        got = [rendered(p) for p in self.lines(40, "emx-test-a-very-long-session-name", "feature/3-grouped-panel",
                                               "Working", "Running the tests", "3h")]
        self.assertEqual(got, [
            "   emx-test-a-very-long-session-name 3h",
            "     ⎇ feature/3-grouped-panel  Working",
            "     Running the tests",
        ])

    def test_a_branch_cut_only_to_line_up_is_shown_whole_without_the_column(self):
        long = [("emx-test-a", ""), ("emx-test-wt", "feature/3-grouped-panel")]
        got = self.lines(60, "emx-test-wt", "feature/3-grouped-panel", "Working", align=core.name_column(long, 60))
        self.assertEqual(got, [core.row_layout("emx-test-wt", "feature/3-grouped-panel", "Working", "", "12m", 60)])
        self.assertIn("⎇ feature/3-grouped-panel  Working", rendered(got[0]))

    def test_wide_characters_wrap_by_display_columns(self):
        got = [rendered(p) for p in self.lines(30, name="日本語の名前のセッションです", word="Done", detail="完了 🎉 nice", age="1d")]
        self.assertEqual(got, [
            "   日本語の名前のセッショ  1d",
            "     ンです  Done · 完了 🎉",
            "     nice",
        ])

    def test_name_branch_and_state_are_whole_and_only_a_summary_past_two_lines_is_cut(self):
        for width in range(0, 71):
            for name in self.NAMES:
                for detail in self.DETAILS:
                    for worktree in ("", "feature/3-grouped-panel"):
                        with self.subTest(width=width, name=name, detail=detail, worktree=worktree):
                            self.check_sweep(width, name, worktree, detail)

    def check_sweep(self, width, name, worktree, detail):
        lines = self.lines(width, name=name, worktree=worktree, detail=detail)
        by_part = {"name": [], "extra": [], "status": []}
        word_ends = None
        for i, parts in enumerate(lines):
            self.assertLessEqual(core.display_width(rendered(parts)), max(0, width - 1))
            for _, text, part in parts:
                if part != "age":
                    by_part[part].append(text)
            if word_ends is None and len("".join(" ".join(by_part["status"]).split())) >= len("Needsinput"):
                word_ends = i
        if len(lines) > 1:
            self.assertEqual(lines[0][-1], (width - 1 - 3, "12m", "age"))
            for parts in lines[1:]:
                self.assertGreaterEqual(parts[0][0], core.ROW_INDENT)
        if width - 1 - core.ROW_INDENT < 2:
            return
        squeeze = lambda texts: "".join(" ".join(texts).split())
        self.assertEqual(squeeze(by_part["name"]), squeeze([name]))
        self.assertEqual(squeeze(by_part["extra"]), squeeze([core.WORKTREE_MARK + worktree] if worktree else []))
        status = squeeze(by_part["status"])
        self.assertTrue(status.startswith("Needsinput"), status)
        shown = status[len("Needsinput"):].removeprefix("·")
        self.assertLessEqual(len(lines) - 1 - word_ends, core.DETAIL_LINES)
        if shown == squeeze([detail]):
            return
        self.assertTrue(shown.endswith("…"), shown)
        self.assertTrue(squeeze([detail]).startswith(shown[:-1]), shown)
        with mock.patch.object(core, "DETAIL_LINES", 99):
            uncut = self.lines(width, name=name, worktree=worktree, detail=detail)
        self.assertGreater(len(uncut), len(lines))

    def test_the_summary_takes_two_lines_of_its_own_then_ends_in_an_ellipsis(self):
        done = "I ran the command, and it created the empty file `made-by-emx-test-n.txt` in the working directory."
        got = [rendered(p) for p in self.lines(40, name="emx-test-19-short", word="Done", detail=done, age="1m")]
        self.assertEqual(got, [
            "   emx-test-19-short  Done · I ran   1m",
            "     the command, and it created the",
            "     empty file `made-by-emx-test-n.tx…",
        ])

    def test_a_summary_that_fits_its_two_lines_is_not_cut(self):
        fits = "I ran the command, and it created the empty file here"
        got = [rendered(p) for p in self.lines(40, name="emx-test-19-short", word="Done", detail=fits, age="1m")]
        self.assertEqual(got, [
            "   emx-test-19-short  Done · I ran   1m",
            "     the command, and it created the",
            "     empty file here",
        ])
        once_more = [rendered(p) for p in self.lines(40, name="emx-test-19-short", word="Done", detail=fits + " x", age="1m")]
        self.assertEqual(once_more[-1], "     empty file here x")

    def test_a_summary_starting_on_its_own_line_gets_two_lines(self):
        wait = "approve Bash: python3 -m unittest discover -s tests -v and then some more words"
        got = [rendered(p) for p in self.lines(34, detail=wait)]
        self.assertEqual(got, [
            "   emx-test-perm  Needs input 12m",
            "     approve Bash: python3 -m",
            "     unittest discover -s tests…",
        ])

    def test_the_armed_label_wraps_too(self):
        self.assertEqual(core.confirm_lines("ctrl+x again to stop", 40), [[(3, "ctrl+x again to stop", "confirm")]])
        got = [rendered(p) for p in core.confirm_lines("ctrl+x again to remove", 19)]
        self.assertEqual(got, ["   ctrl+x again to", "     remove"])

    def test_the_armed_label_is_padded_to_the_rows_height(self):
        self.assertEqual(core.confirm_lines("ctrl+x again to stop", 40, 3), [[(3, "ctrl+x again to stop", "confirm")], [], []])
        self.assertEqual(len(core.confirm_lines("ctrl+x again to remove", 19, 1)), 2)


class ListWindow(unittest.TestCase):
    """Scrolling and clicks count screen rows, so a wrapped row is kept whole on screen and every line of it is his click."""

    def test_one_row_lines_scroll_as_before(self):
        self.assertEqual(core.list_window([1] * 10, 0, 0, 4), 0)
        self.assertEqual(core.list_window([1] * 10, 6, 0, 4), 3)
        self.assertEqual(core.list_window([1] * 10, 4, 3, 4), 3)
        self.assertEqual(core.list_window([1] * 10, 1, 3, 4), 1)
        self.assertEqual(core.list_window([1] * 3, 0, 5, 4), 0)

    def test_the_cursor_row_is_shown_whole(self):
        heights = [1, 3, 1, 4, 1]
        self.assertEqual(core.list_window(heights, 3, 0, 5), 4)
        self.assertEqual(core.list_window(heights, 1, 3, 5), 1)
        self.assertEqual(core.list_window(heights, 4, 0, 5), 5)

    def test_a_row_taller_than_the_view_shows_from_its_top(self):
        self.assertEqual(core.list_window([1, 6, 1], 1, 0, 3), 1)
        self.assertEqual(core.list_window([1, 6, 1], 1, 4, 3), 1)

    def test_every_line_of_a_wrapped_row_maps_to_it(self):
        lines = [("group", "/p"), ("session", "a"), ("session", "b"), None, ("group", "/q"), ("session", "c")]
        heights = [1, 3, 1, 1, 1, 2]
        self.assertEqual(core.row_hits(lines, heights, 0, 2, 20), {
            2: ("group", "/p"), 3: ("session", "a"), 4: ("session", "a"), 5: ("session", "a"),
            6: ("session", "b"), 8: ("group", "/q"), 9: ("session", "c"), 10: ("session", "c"),
        })

    def test_a_row_cut_by_the_view_maps_only_its_lines_on_screen(self):
        lines = [("session", "a"), ("session", "b"), ("session", "c")]
        self.assertEqual(core.row_hits(lines, [3, 3, 3], 2, 2, 5), {
            2: ("session", "a"), 3: ("session", "b"), 4: ("session", "b"), 5: ("session", "b"), 6: ("session", "c"),
        })


class HeaderLayout(unittest.TestCase):
    def test_project_header_keeps_the_folder_name_last_to_go(self):
        got = core.header_layout(core.PROJECT, "/h/me/code/app", 3, True, 40, "/h/me")
        self.assertEqual(got, [(0, "▸", "arrow"), (2, "~/code/", "dir"), (9, "app", "name"), (13, "3", "count")])
        got = core.header_layout(core.PROJECT, "/very/long/path/to/the/project-name", 12, True, 24, "/h/me")
        self.assertEqual(rendered(got), "▸ …/the/project-name 12")
        got = core.header_layout(core.PROJECT, "/x/a-really-long-project-folder", 1, True, 16, "/h/me")
        self.assertEqual(rendered(got), "▸ a-really-l… 1")

    def test_count_shows_only_when_collapsed(self):
        got = core.header_layout(core.PROJECT, "/h/me/code/app", 3, False, 40, "/h/me")
        self.assertEqual(got, [(0, "▾", "arrow"), (2, "~/code/", "dir"), (9, "app", "name")])
        got = core.header_layout(core.PROJECT, "/x/a-really-long-project-folder", 1, False, 16, "/h/me")
        self.assertEqual(rendered(got), "▾ a-really-lon…")

    def test_header_path_is_cut_at_slashes(self):
        got = core.header_layout(core.PROJECT, "/h/me/work/clients/acme/app", 2, False, 16, "/h/me")
        self.assertEqual(rendered(got), "▾ ~/…/acme/app")
        got = core.header_layout(core.PROJECT, "/h/me/work/clients/acme/app", 2, False, 12, "/h/me")
        self.assertEqual(rendered(got), "▾ ~/…/app")
        got = core.header_layout(core.PROJECT, "/h/me/work/clients/acme/app", 2, False, 9, "/h/me")
        self.assertEqual(rendered(got), "▾ …/app")

    def test_state_header(self):
        got = core.header_layout(core.BY_STATE, core.NEEDS_INPUT, 2, False, 40)
        self.assertEqual(rendered(got), "▾ Needs input")
        got = core.header_layout(core.BY_STATE, core.NEEDS_INPUT, 2, True, 40)
        self.assertEqual(rendered(got), "▸ Needs input 2")

    def test_never_wider_than_the_pane_less_one(self):
        for width in range(0, 50):
            with self.subTest(width=width):
                line = rendered(core.header_layout(core.PROJECT, "/x/y/some-project", 10, False, width, "/h"))
                self.assertLessEqual(core.display_width(line), max(0, width - 1))


class TextHelpers(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(core.fit("hello", 10), "hello")
        self.assertEqual(core.fit("hello", 5), "hello")
        self.assertEqual(core.fit("hello", 4), "hel…")
        self.assertEqual(core.fit("hello", 1), "…")
        self.assertEqual(core.fit("hello", 0), "")
        self.assertEqual(core.fit("hello", -3), "")

    def test_fit_never_exceeds_width_with_wide_chars(self):
        for w in range(0, 12):
            with self.subTest(w=w):
                self.assertLessEqual(core.display_width(core.fit("日本語のテキスト", w)), w)

    def test_wrap_respects_width(self):
        for w in (2, 3, 7):
            for line in core.wrap("abcdefghij\n日本語klm", w):
                with self.subTest(w=w, line=line):
                    self.assertLessEqual(core.display_width(line), w)

    def test_wrap_words_breaks_between_words(self):
        text = "Couldn't list sessions to check emx-test-c1 is still running"
        self.assertEqual(core.wrap_words(text, 20), ["Couldn't list", "sessions to check", "emx-test-c1 is still", "running"])
        self.assertEqual(core.wrap_words("a-very-long-unbroken-word", 10), ["a-very-lon", "g-unbroken", "-word"])
        self.assertEqual(core.wrap_words("", 5), [""])
        self.assertEqual(core.wrap_words("one\ntwo", 10), ["one", "two"])
        self.assertEqual(core.wrap_words("abc", 0), [])

    def test_wrap_words_respects_display_width(self):
        for w in (2, 3, 7):
            for line in core.wrap_words("日本語のテキスト and more 日本語", w):
                with self.subTest(w=w, line=line):
                    self.assertLessEqual(core.display_width(line), w)

    def test_wrap_degenerate(self):
        self.assertEqual(core.wrap("abc", 0), [])
        self.assertEqual(core.wrap("abc", -1), [])
        self.assertEqual(core.wrap("", 5), [""])

    def test_strip_ansi_and_first_message(self):
        self.assertEqual(core.strip_ansi("\x1b[1;31mred\x1b[0m"), "red")
        self.assertEqual(core.first_message("\n  \x1b[31mboom\x1b[0m\nsecond"), "boom")
        self.assertEqual(core.first_message(""), "")
        self.assertEqual(core.first_message("\n \n"), "")


class Layout(unittest.TestCase):
    def test_main_takes_the_rest(self):
        lay = core.compute_layout(300, 40)
        self.assertEqual((lay.panel, lay.main), (40, 259))

    def test_widths_always_positive_and_fill(self):
        for ww in range(0, 400):
            for pw in (1, 16, 40, 500):
                lay = core.compute_layout(ww, pw)
                with self.subTest(ww=ww, pw=pw):
                    self.assertGreaterEqual(lay.panel, 1)
                    self.assertGreaterEqual(lay.main, 1)
                    if ww >= 3:
                        self.assertEqual(lay.panel + lay.main + 1, ww)

    def test_narrow_terminal_keeps_min_main(self):
        lay = core.compute_layout(60, 40)
        self.assertEqual((lay.panel, lay.main), (39, core.MIN_MAIN))
        self.assertEqual(lay.panel + lay.main + 1, 60)

    def test_width_action(self):
        self.assertEqual(core.width_action(None, (200, 40)), "apply")
        self.assertEqual(core.width_action((200, 40), (180, 40)), "apply")
        self.assertEqual(core.width_action((200, 40), (200, 45)), "adopt-panel")
        self.assertIsNone(core.width_action((200, 40), (200, 40)))


class Prefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "emx" / "prefs.json"

    def test_missing_file_gives_defaults(self):
        self.assertEqual(core.load_prefs(self.path), core.DEFAULT_PREFS)

    def test_corrupt_and_wrong_shape_give_defaults(self):
        self.path.parent.mkdir(parents=True)
        for content in ["{not json", "", "[1,2]", "null"]:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(core.load_prefs(self.path), core.DEFAULT_PREFS)

    def test_non_utf8_bytes_give_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(core.load_prefs(self.path), core.DEFAULT_PREFS)

    def test_roundtrip_creates_parent_dirs(self):
        prefs = {"side": "left", "panel_width": 30, "last_folder": "/x"}
        core.save_prefs(self.path, prefs)
        self.assertEqual(core.load_prefs(self.path), prefs)
        self.assertFalse(self.path.with_name("prefs.json.tmp").exists())

    def test_old_file_with_main_width_loads_and_drops_it(self):
        self.path.parent.mkdir(parents=True)
        old = {"side": "left", "main_width": 80, "panel_width": 30, "last_folder": "/x"}
        self.path.write_text(json.dumps(old), encoding="utf-8")
        got = core.load_prefs(self.path)
        self.assertEqual(got, {"side": "left", "panel_width": 30, "last_folder": "/x"})
        core.save_prefs(self.path, got)
        self.assertNotIn("main_width", json.loads(self.path.read_text(encoding="utf-8")))

    def test_normalize_rejects_bad_values(self):
        got = core.normalize_prefs({"side": "up", "panel_width": True, "last_folder": ""})
        self.assertEqual(got, core.DEFAULT_PREFS)
        self.assertEqual(core.normalize_prefs({"panel_width": "9"}), core.DEFAULT_PREFS)

    def test_normalize_clamps(self):
        self.assertEqual(core.normalize_prefs({"panel_width": 5})["panel_width"], core.MIN_PANEL)
        self.assertEqual(core.normalize_prefs({"panel_width": 10**9})["panel_width"], core.MAX_WIDTH)

    def test_last_version_survives_a_save(self):
        prefs = {**core.DEFAULT_PREFS, "last_version": "1.2.3"}
        core.save_prefs(self.path, prefs)
        self.assertEqual(core.load_prefs(self.path), prefs)

    def test_normalize_drops_a_last_version_that_is_not_semver(self):
        for bad in ("", "1.2", "v1.2.3", 123, None):
            with self.subTest(bad=bad):
                self.assertNotIn("last_version", core.normalize_prefs({"last_version": bad}))

    def test_normalize_non_dict(self):
        for raw in (None, [], "s", 3):
            self.assertEqual(core.normalize_prefs(raw), core.DEFAULT_PREFS)

    def test_normalize_does_not_mutate_defaults(self):
        core.normalize_prefs({"side": "left", "last_folder": "/q"})
        self.assertEqual(core.DEFAULT_PREFS, {"side": "right", "panel_width": 40})

    def test_prefs_path_xdg(self):
        self.assertEqual(core.prefs_path({"XDG_CONFIG_HOME": "/cfg"}), Path("/cfg/emx/prefs.json"))

    def test_prefs_path_ignores_empty_or_relative_xdg(self):
        home = os.path.expanduser("~")
        want = Path(home) / ".config" / "emx" / "prefs.json"
        self.assertEqual(core.prefs_path({}), want)
        self.assertEqual(core.prefs_path({"XDG_CONFIG_HOME": ""}), want)
        self.assertEqual(core.prefs_path({"XDG_CONFIG_HOME": "rel/dir"}), want)

    def test_adjust_panel_width_clamped(self):
        p = dict(core.DEFAULT_PREFS)
        self.assertEqual(core.adjust_panel_width(p, -1000)["panel_width"], core.MIN_PANEL)
        self.assertEqual(core.adjust_panel_width(p, 10**6)["panel_width"], core.MAX_WIDTH)
        self.assertEqual(core.adjust_panel_width(p, 5)["panel_width"], 45)
        self.assertEqual(core.adjust_panel_width(p, -5)["panel_width"], 35)
        self.assertEqual(p, core.DEFAULT_PREFS)

    def test_flip_side(self):
        p = dict(core.DEFAULT_PREFS)
        self.assertEqual(core.flip_side(p)["side"], "left")
        self.assertEqual(core.flip_side(core.flip_side(p))["side"], "right")
        self.assertEqual(p["side"], "right")

    def test_view_and_folded_groups_round_trip(self):
        prefs = core.toggle_view(dict(core.DEFAULT_PREFS))
        prefs = core.toggle_collapsed(prefs, core.BY_STATE, core.DONE)
        prefs = core.toggle_collapsed(prefs, core.PROJECT, "/r/app")
        core.save_prefs(self.path, prefs)
        got = core.load_prefs(self.path)
        self.assertEqual(core.current_view(got), core.BY_STATE)
        self.assertEqual(core.collapsed_keys(got, core.BY_STATE), {core.DONE})
        self.assertEqual(core.collapsed_keys(got, core.PROJECT), {"/r/app"})

    def test_toggles(self):
        p = dict(core.DEFAULT_PREFS)
        self.assertEqual(core.current_view(p), core.PROJECT)
        self.assertEqual(core.current_view(core.toggle_view(core.toggle_view(p))), core.PROJECT)
        folded = core.toggle_collapsed(p, core.PROJECT, "/a")
        self.assertEqual(core.collapsed_keys(folded, core.PROJECT), {"/a"})
        self.assertEqual(core.collapsed_keys(folded, core.BY_STATE), frozenset())
        self.assertEqual(core.collapsed_keys(core.toggle_collapsed(folded, core.PROJECT, "/a"), core.PROJECT), frozenset())
        self.assertEqual(p, core.DEFAULT_PREFS)

    def test_normalize_drops_bad_view_and_folded_groups(self):
        raw = {"view": "flat", "collapsed": {"project": ["/a", "", 3, "/a"], "state": "done", "other": ["/x"]}}
        self.assertEqual(core.normalize_prefs(raw), {**core.DEFAULT_PREFS, "collapsed": {"project": ["/a"]}})
        for bad in (None, [], {"project": []}, "x"):
            with self.subTest(collapsed=bad):
                self.assertEqual(core.normalize_prefs({"collapsed": bad}), core.DEFAULT_PREFS)


class Folders(unittest.TestCase):
    def test_tilde_expansion(self):
        home = os.path.expanduser("~")
        self.assertEqual(core.resolve_folder("~", "/base"), os.path.normpath(home))
        self.assertEqual(core.resolve_folder("~/a/b", "/base"), os.path.join(home, "a", "b"))

    def test_empty_means_base(self):
        self.assertEqual(core.resolve_folder("", "/base"), "/base")
        self.assertEqual(core.resolve_folder("   ", "/base"), "/base")

    def test_relative_and_absolute(self):
        self.assertEqual(core.resolve_folder("a/../b", "/base"), "/base/b")
        self.assertEqual(core.resolve_folder("/x//y/", "/base"), "/x/y")
        self.assertEqual(core.resolve_folder("  /x  ", "/base"), "/x")


def bg(sid, cwd, state="working"):
    return core.Session(sid, sid, cwd, state, "", "")


HOME = "/h/me"


class DefaultFolder(unittest.TestCase):
    def setUp(self):
        self.sessions = [bg("a", "/r/app/sub"), bg("b", "/r/app-wt"), bg("c", "")]
        self.groups = core.group_sessions(self.sessions, {"/r/app-wt": core.Placement("/r/app", "feat")}, core.PROJECT)

    def pick(self, cursor, view=core.PROJECT, last=""):
        return core.default_folder(cursor, self.groups, self.sessions, view, last)

    def test_a_session_row_means_that_sessions_own_folder(self):
        self.assertEqual(self.pick(("session", "a")), "/r/app/sub")
        self.assertEqual(self.pick(("session", "b")), "/r/app-wt")

    def test_a_project_header_means_the_groups_folder(self):
        self.assertEqual(self.pick(("group", "/r/app")), "/r/app")

    def test_a_by_state_header_falls_back_to_the_last_folder_or_the_picker(self):
        self.assertEqual(self.pick(("group", core.WORKING), core.BY_STATE, "/r/last"), "/r/last")
        self.assertIsNone(self.pick(("group", core.WORKING), core.BY_STATE))

    def test_a_session_with_no_folder_or_no_cursor_falls_back_too(self):
        self.assertEqual(self.pick(("session", "c"), last="/r/last"), "/r/last")
        self.assertEqual(self.pick(None, last="/r/last"), "/r/last")
        self.assertIsNone(self.pick(None))

    def test_with_no_sessions_at_all_he_picks(self):
        self.assertIsNone(core.default_folder(None, [], [], core.PROJECT, "/r/last"))


class RecentFolders(unittest.TestCase):
    def test_newest_first_without_repeats_and_capped(self):
        prefs = dict(core.DEFAULT_PREFS)
        for i in range(core.RECENT_FOLDERS + 5):
            prefs = core.remember_folder(prefs, f"/f/{i}")
        prefs = core.remember_folder(prefs, "/f/10")
        recent = prefs["recent_folders"]
        self.assertEqual(len(recent), core.RECENT_FOLDERS)
        self.assertEqual(recent[:2], ["/f/10", f"/f/{core.RECENT_FOLDERS + 4}"])
        self.assertEqual(recent.count("/f/10"), 1)
        self.assertEqual(prefs["last_folder"], "/f/10")
        self.assertNotIn("recent_folders", core.DEFAULT_PREFS)

    def test_they_survive_a_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "prefs.json"
            core.save_prefs(path, core.remember_folder(dict(core.DEFAULT_PREFS), "/f/a"))
            self.assertEqual(core.load_prefs(path)["recent_folders"], ["/f/a"])

    def test_normalize_drops_bad_entries_repeats_and_extras(self):
        raw = {"recent_folders": ["/a", "", 3, None, "/a", "/b"] + [f"/x{i}" for i in range(30)]}
        recent = core.normalize_prefs(raw)["recent_folders"]
        self.assertEqual(recent[:3], ["/a", "/b", "/x0"])
        self.assertEqual(len(recent), core.RECENT_FOLDERS)
        for bad in ("/a", [], [""], None):
            self.assertNotIn("recent_folders", core.normalize_prefs({"recent_folders": bad}))


class FuzzyMatch(unittest.TestCase):
    FOLDERS = [
        "~/Workspace/email-export",
        "~/Downloads/web-stuff/memex",
        "~/Workspace/emberlynx",
        "~/Documents/Notes",
        "~/Workspace/website",
    ]

    def ranked(self, query, folders=FOLDERS):
        matches = [(core.fuzzy_match(query, f), f) for f in folders]
        return [f for m, f in sorted(m for m in matches if m[0] is not None)]

    def test_subsequence_in_any_case(self):
        for query in ("emx", "EMX", "eMx", "wsemx", "WsEmX"):
            with self.subTest(query=query):
                self.assertIsNotNone(core.fuzzy_match(query, "~/Workspace/emberlynx"))
        self.assertIsNone(core.fuzzy_match("xme", "~/Workspace/emberlynx"))
        self.assertIsNone(core.fuzzy_match("emxz", "~/Workspace/emberlynx"))

    def test_wsemx_and_emx_find_the_folder_among_others_that_match(self):
        for query in ("wsemx", "emx"):
            with self.subTest(query=query):
                ranked = self.ranked(query)
                self.assertGreater(len(ranked), 1)
                self.assertEqual(ranked[0], "~/Workspace/emberlynx")

    def test_letters_together_beat_letters_spread(self):
        self.assertEqual(self.ranked("site"), ["~/Workspace/website"])
        self.assertEqual(self.ranked("web")[0], "~/Workspace/website")
        self.assertEqual(self.ranked("exp")[0], "~/Workspace/email-export")

    def test_the_best_alignment_counts_not_the_earliest(self):
        self.assertEqual(core.fuzzy_match("ab", "a_xb_ab")[0], -1)
        self.assertEqual(core.fuzzy_match("abc", "a_b_abc")[0], -2)
        self.assertEqual(core.fuzzy_match("ab", "xab/ab"), (-1, False, 6))

    def test_a_match_starting_the_folder_name_beats_one_inside_it(self):
        self.assertEqual(self.ranked("not", ["~/a/denote", "~/a/notes-long"]), ["~/a/notes-long", "~/a/denote"])

    def test_then_the_shorter_path(self):
        self.assertEqual(self.ranked("api", ["~/x/y/api", "~/api"]), ["~/api", "~/x/y/api"])

    def test_empty_query_matches_everything(self):
        self.assertIsNotNone(core.fuzzy_match("", "~/anything"))

    def test_letters_that_grow_when_lowercased_never_shift_the_match(self):
        listed = {"/h/me": ["İşlər", "Proje-İçerik", "Docs"]}
        expected = {
            "r": ["~/İşlər", "~/Proje-İçerik"], "~/r": ["~/İşlər", "~/Proje-İçerik"], "ər": ["~/İşlər"],
            "k": ["~/Proje-İçerik"], "~/k": ["~/Proje-İçerik"], "~/ik": ["~/Proje-İçerik"], "işl": ["~/İşlər"],
        }
        for query, shown in expected.items():
            with self.subTest(query=query):
                self.assertEqual([r.shown for r in core.folder_suggestions(query, [], [], listed, "/h/me")], shown)
        self.assertEqual(core.fuzzy_match("x", "~/İx"), (0, True, 4))
        self.assertEqual(core.fuzzy_match("ix", "~/İx"), (-1, False, 4))
        self.assertIsNotNone(core.fuzzy_match("işlər", "~/İşlər"))
        self.assertIsNotNone(core.fuzzy_match("ŞLƏR", "~/İşlər"))
        self.assertEqual(core.fuzzy_match("ik", "~/Proje-İçerik")[0], -1)

    def test_odd_unicode_names_never_raise(self):
        names = ["İİİx", "Straße", "ﬁle-ﬂow", "e\u0301te\u0301", "ΣΑΣ", "Ǆemal", "\u0130\u0307i", "ǰx", "ẞig"]
        for name in names:
            for query in ("x", "e", "i", "s", "w", "l", "ss", name, name.lower(), name.upper()):
                with self.subTest(name=name, query=query):
                    core.fuzzy_match(query, "~/" + name)
                    core.fuzzy_match(query, name)


class FolderSuggestions(unittest.TestCase):
    def shown(self, text, sessions=(), recent=(), listed=None, limit=core.PICKER_ROWS, roots=None):
        rows = core.folder_suggestions(text, sessions, recent, listed or {}, HOME, limit, roots)
        return [(r.shown, r.source) for r in rows]

    def test_sources_in_order_each_keeping_its_own_order_when_nothing_is_typed(self):
        got = self.shown("", ["/h/me/zeta", "/h/me/alpha"], ["/h/me/beta", "/h/me/zeta/"], {HOME: ["Gamma", "alpha", ".git"]})
        self.assertEqual(got, [
            ("~/zeta", core.SESSIONS), ("~/alpha", core.SESSIONS), ("~/beta", core.RECENT), ("~/Gamma", core.LISTED),
        ])

    def test_a_bare_word_ranks_every_source_together_and_drops_non_matches(self):
        got = self.shown("emx", ["/h/me/Workspace/email-export", "/h/me/Workspace/emberlynx", "/h/me/notes"],
                         ["/h/me/memex", "/h/me/other"], {HOME: ["Emx-lab", "Workspace"]})
        self.assertEqual(got, [
            ("~/Emx-lab", core.LISTED), ("~/Workspace/emberlynx", core.SESSIONS),
            ("~/Workspace/email-export", core.SESSIONS), ("~/memex", core.RECENT),
        ])

    def test_the_source_breaks_a_tie(self):
        got = self.shown("kiw", [], ["/h/me/kiwi"], {HOME: ["kiwi", "kiwa"]})
        self.assertEqual(got, [("~/kiwi", core.RECENT), ("~/kiwa", core.LISTED)])
        got = self.shown("kiw", ["/h/me/kiwa"], ["/h/me/kiwi"])
        self.assertEqual(got, [("~/kiwa", core.SESSIONS), ("~/kiwi", core.RECENT)])

    def test_a_bare_word_searches_one_level_inside_the_folders_he_uses(self):
        listed = {"/h/me/Workspace": ["alpha", "beta", "quokka-tools"], HOME: ["Workspace"], "/h/me/elsewhere": ["quokka"]}
        got = self.shown("quok", ["/h/me/Workspace/alpha"], ["/h/me/Workspace/beta"], listed)
        self.assertEqual(got, [("~/Workspace/quokka-tools", core.LISTED)])

    def test_the_searched_folders_are_home_then_the_busiest_parents(self):
        sessions = ["/h/me/w/a", "/h/me/w/b", "/q/x", "/h/me/top", "/h/me/w/a/"]
        recent = ["/r/1", "/r/2", "/r/3", "/q/y"]
        self.assertEqual(core.search_folders(sessions, recent, HOME), [HOME, "/r", "/h/me/w", "/q"])
        self.assertEqual(core.search_folders(sessions, recent, HOME, limit=2), [HOME, "/r", "/h/me/w"])
        self.assertEqual(core.search_folders([], [], HOME), [HOME])

    def test_a_placed_worktree_searches_its_repos_parent(self):
        worktree, plain = "/h/me/Workspace/kiwi/.claude/worktrees/feat", "/h/me/notes/sub"
        placements = {worktree: core.Placement("/h/me/Workspace/kiwi", "feat")}
        roots = core.repo_roots([worktree, plain, worktree, ""], placements)
        self.assertEqual(roots, ["/h/me/Workspace/kiwi", plain])
        self.assertEqual(core.search_folders(roots, [], HOME), [HOME, "/h/me/Workspace", "/h/me/notes"])
        self.assertEqual(core.folders_to_list("quok", [worktree], [], HOME, roots), [HOME, "/h/me/Workspace", "/h/me/notes"])
        unplaced = core.repo_roots([worktree], {})
        self.assertEqual(core.search_folders(unplaced, [], HOME), [HOME, "/h/me/Workspace/kiwi/.claude/worktrees"])
        listed = {"/h/me/Workspace": ["kiwi", "quokka"], "/h/me/Workspace/kiwi/.claude/worktrees": ["quokka-wt"]}
        got = self.shown("quok", [worktree], listed=listed, roots=roots)
        self.assertEqual(got, [("~/Workspace/quokka", core.LISTED)])

    def test_nothing_after_the_slash_lists_alphabetically_then_fuzzy_once_typed(self):
        listed = {"/h/me/Workspace": ["zeta", "Beta", "alpha-long-name", "b"]}
        known = ["/h/me/Workspace/Beta/.claude/worktrees/x", "/h/me/Workspace/Cat"]
        got = [shown for shown, _ in self.shown("~/Workspace/", known, listed=listed)]
        self.assertEqual(got, [
            "~/Workspace/alpha-long-name", "~/Workspace/b", "~/Workspace/Beta", "~/Workspace/Cat",
            "~/Workspace/zeta", "~/Workspace/Beta/.claude/worktrees/x",
        ])
        got = [shown for shown, _ in self.shown("~/Workspace/b", known, listed=listed)]
        self.assertEqual(got[:2], ["~/Workspace/b", "~/Workspace/Beta"])

    def test_the_folders_to_list_follow_what_is_typed(self):
        sessions = ["/h/me/w/a"]
        self.assertEqual(core.folders_to_list("~/w/x", sessions, [], HOME), ["/h/me/w"])
        self.assertEqual(core.folders_to_list("~", sessions, [], HOME), [HOME])
        self.assertEqual(core.folders_to_list("ab", sessions, [], HOME), [HOME, "/h/me/w"])
        self.assertEqual(core.folders_to_list("", sessions, [], HOME), [HOME, "/h/me/w"])

    def test_a_typed_parent_shows_its_subfolders_even_with_many_known_folders_under_it(self):
        known = [f"/h/me/Workspace/kiwi/.claude/worktrees/task-{i}" for i in range(9)]
        listed = {"/h/me/Workspace": ["kiwi", "newrepo", "zebra-notes"]}
        got = self.shown("~/Workspace/", known, ["/h/me/Workspace/kiwi"], listed)
        self.assertEqual(len(got), core.PICKER_ROWS)
        self.assertEqual(got[:3], [
            ("~/Workspace/kiwi", core.RECENT), ("~/Workspace/newrepo", core.LISTED), ("~/Workspace/zebra-notes", core.LISTED),
        ])

    def test_the_exact_listed_folder_ranks_ahead_of_long_worktree_paths(self):
        known = ["/h/me/Workspace/foo/.claude/worktrees/feature+foo-fix", "/h/me/Workspace/foo/.claude/worktrees/foo-docs"]
        listed = {"/h/me/Workspace": ["foo", "foobar-app", "other"]}
        got = self.shown("~/Workspace/foo", known, listed=listed)
        self.assertEqual(got[:2], [("~/Workspace/foo", core.LISTED), ("~/Workspace/foobar-app", core.LISTED)])
        self.assertEqual(len(got), 4)

    def test_a_name_starting_with_the_typed_text_ranks_first(self):
        known = ["/h/me/Workspace/kiwi/.claude/worktrees/renew-cache", "/h/me/other/newthing"]
        listed = {"/h/me/Workspace": ["kiwi", "renewal", "newrepo"]}
        got = self.shown("~/Workspace/new", known, listed=listed)
        self.assertEqual(got[0], ("~/Workspace/newrepo", core.LISTED))
        self.assertNotIn(("~/other/newthing", core.SESSIONS), got)

    def test_known_folders_outside_the_typed_parent_are_not_offered(self):
        got = self.shown("~/Workspace/k", ["/h/me/kiwi", "/h/me/Workspace", "/h/me/Workspace/kiwi"], ["/h/me/Workspaces/k"])
        self.assertEqual(got, [("~/Workspace/kiwi", core.SESSIONS)])

    def test_subfolders_of_the_typed_parent_match_the_last_segment(self):
        listed = {"/h/me/Workspace": ["Alpha-Site", "beta", "alps"]}
        got = self.shown("~/Workspace/AL", listed=listed)
        self.assertEqual(got, [("~/Workspace/alps", core.LISTED), ("~/Workspace/Alpha-Site", core.LISTED)])
        self.assertEqual(self.shown("~/Workspace/al", listed={"/h/me/Other": ["alps"]}), [])

    def test_hidden_folders_only_for_a_dot(self):
        listed = {"/h/me/w": [".cache", "code"]}
        self.assertEqual(self.shown("~/w/", listed=listed), [("~/w/code", core.LISTED)])
        self.assertEqual(self.shown("~/w/.", listed=listed), [("~/w/.cache", core.LISTED)])

    def test_absolute_typing_matches_absolute_paths(self):
        got = self.shown("/opt/pro", ["/opt/projects/x", "/h/me/proj"], listed={"/opt": ["projects"]})
        self.assertEqual(got, [("/opt/projects", core.LISTED), ("/opt/projects/x", core.SESSIONS)])

    def test_capped(self):
        listed = {HOME: [f"d{i}" for i in range(20)]}
        self.assertEqual(len(self.shown("", [f"/s/{i}" for i in range(5)], listed=listed)), core.PICKER_ROWS)
        self.assertEqual(len(self.shown("", [f"/s/{i}" for i in range(5)], listed=listed, limit=3)), 3)

    def test_the_query_splits_into_the_folder_to_list_and_the_name(self):
        self.assertEqual(core.picker_query("emx", HOME), (HOME, "emx"))
        self.assertEqual(core.picker_query("~", HOME), (HOME, ""))
        self.assertEqual(core.picker_query("~/", HOME), (HOME, ""))
        self.assertEqual(core.picker_query("~/Work/em", HOME), ("/h/me/Work", "em"))
        self.assertEqual(core.picker_query("/opt/x/", HOME), ("/opt/x", ""))
        self.assertEqual(core.picker_query("code/ap", HOME), ("/h/me/code", "ap"))

    def test_tab_completes_into_the_folder(self):
        row = core.Suggestion("/h/me/w", "~/w", core.LISTED)
        self.assertEqual(core.completed_text(row), "~/w/")

    def test_a_typed_folder_is_known_only_from_what_emx_already_has(self):
        self.assertTrue(core.folder_exists("/r/app/", ["/r/app"], None))
        self.assertTrue(core.folder_exists("/", [], None))
        self.assertIsNone(core.folder_exists("/r/new", [], None))
        self.assertIsNone(core.folder_exists("/r/new", [], ("/q", ["new"])))
        self.assertTrue(core.folder_exists("/r/new", [], ("/r", ["new"])))
        self.assertFalse(core.folder_exists("/r/New", [], ("/r", ["new"])))
        self.assertTrue(core.folder_exists("/r/new", [], None, readable=True))
        self.assertFalse(core.folder_exists("/r/new", [], None, readable=False))

    def test_a_listing_cut_short_leaves_the_answer_to_a_read_of_the_folder_itself(self):
        full = ("/r", [f"d{i}" for i in range(core.MAX_LISTED_NAMES)])
        self.assertIsNone(core.folder_exists("/r/big", [], full))
        self.assertTrue(core.folder_exists("/r/big", [], full, readable=True))
        self.assertFalse(core.folder_exists("/r/big", [], full, readable=False))

    def test_enter_takes_the_highlight_then_a_confirmed_typed_folder_then_the_top_suggestion(self):
        rows = [core.Suggestion("/r/top", "/r/top", core.LISTED)]
        self.assertEqual(core.picker_enter("/r/hi", "/r/typed", True, rows), ("/r/hi", None))
        self.assertEqual(core.picker_enter(None, "/r/typed", True, rows), ("/r/typed", None))
        self.assertEqual(core.picker_enter(None, "/r/typed", False, rows), ("/r/top", None))
        folder, hint = core.picker_enter(None, "/r/typed", None, rows)
        self.assertIsNone(folder)
        self.assertIn("Still reading", hint)
        folder, hint = core.picker_enter(None, "/r/typed", False, [])
        self.assertIsNone(folder)
        self.assertIn("Not a folder", hint)

    def test_enter_with_nothing_typed_takes_the_top_suggestion(self):
        rows = [core.Suggestion("/r/top", "/r/top", core.SESSIONS)]
        self.assertEqual(core.picker_enter(None, HOME, True, rows, nothing_typed=True), ("/r/top", None))
        self.assertEqual(core.picker_enter(None, HOME, None, rows, nothing_typed=True), ("/r/top", None))
        self.assertEqual(core.picker_enter("/r/hi", HOME, True, rows, nothing_typed=True), ("/r/hi", None))
        self.assertEqual(core.picker_enter(None, HOME, True, [], nothing_typed=True), (HOME, None))
        folder, hint = core.picker_enter(None, HOME, None, [], nothing_typed=True)
        self.assertIsNone(folder)
        self.assertIn("Still reading", hint)

    def test_the_panels_folders_are_its_groups_then_other_session_folders(self):
        sessions = [bg("a", "/r/app/sub"), bg("b", "/r/app"), bg("c", "")]
        groups = core.group_sessions(sessions, {"/r/app/sub": core.Placement("/r/app")}, core.PROJECT)
        self.assertEqual(core.session_folders(sessions, groups, core.PROJECT), ["/r/app", "/r/app/sub"])
        state_groups = core.group_sessions(sessions, {}, core.BY_STATE)
        self.assertEqual(core.session_folders(sessions, state_groups, core.BY_STATE), ["/r/app/sub", "/r/app"])


class OpenWhileStarting(unittest.TestCase):
    def test_enter_and_right_open_nothing_while_a_session_starts(self):
        self.assertIsNone(core.open_blocked(False))
        self.assertEqual(core.open_blocked(True), "Still starting the last session…")

    def test_a_group_header_still_folds_while_a_session_starts(self):
        self.assertIsNone(core.open_blocked(True, ("group", "/p/beta")))
        self.assertEqual(core.open_blocked(True, ("session", "s")), "Still starting the last session…")


class ClaudeCommands(unittest.TestCase):
    def test_empty_message_gives_no_prompt(self):
        for m in ("", "   ", "\n\t"):
            self.assertEqual(core.new_session_args("claude", m), ["claude", "--bg"])

    def test_message_passed_after_double_dash(self):
        self.assertEqual(core.new_session_args("claude", "hi there"), ["claude", "--bg", "--", "hi there"])

    def test_dash_message_still_a_prompt(self):
        self.assertEqual(core.new_session_args("claude", "-n oops"), ["claude", "--bg", "--", "-n oops"])
        self.assertEqual(core.new_session_args("claude", "--help"), ["claude", "--bg", "--", "--help"])

    def test_no_name_flag(self):
        self.assertNotIn("-n", core.new_session_args("claude", "x"))

    def test_parse_session_id(self):
        self.assertEqual(core.parse_new_session_id("started\n  claude attach abc123\n"), "abc123")
        self.assertEqual(core.parse_new_session_id("\x1b[1mclaude attach\x1b[0m \x1b[36mabc123\x1b[0m"), "abc123")
        self.assertEqual(core.parse_new_session_id("backgrounded · deadbeef"), "deadbeef")
        self.assertEqual(core.parse_new_session_id("backgrounded  ·  deadbeef extra"), "deadbeef")

    def test_parse_session_id_none(self):
        for out in ("", "nothing here", "claude attach", "claude attach "):
            with self.subTest(out=out):
                self.assertIsNone(core.parse_new_session_id(out))

    def test_viewer_command_quotes(self):
        self.assertEqual(core.viewer_command("claude", "abc"), "exec claude attach abc")
        cmd = core.viewer_command("/p with space/claude", "a;b")
        self.assertEqual(cmd, "exec '/p with space/claude' attach 'a;b'")


class StaleViewers(unittest.TestCase):
    def test_live_viewer_not_stale_and_misses_cleared(self):
        self.assertEqual(core.stale_viewers(["a"], ["a"], {"a": 1}), ({}, []))

    def test_one_miss_is_not_enough(self):
        self.assertEqual(core.stale_viewers(["a"], [], {}), ({"a": 1}, []))

    def test_second_consecutive_miss_closes(self):
        misses, close = core.stale_viewers(["a"], [], {"a": 1})
        self.assertEqual(close, ["a"])
        self.assertNotIn("a", misses)

    def test_miss_streak_resets_when_seen(self):
        misses, _ = core.stale_viewers(["a"], [], {})
        misses, _ = core.stale_viewers(["a"], ["a"], misses)
        misses, close = core.stale_viewers(["a"], [], misses)
        self.assertEqual(close, [])

    def test_no_viewers(self):
        self.assertEqual(core.stale_viewers([], [], {"gone": 1}), ({}, []))

    def test_accepts_sets_and_generators(self):
        _, close = core.stale_viewers({"a", "b"}, (x for x in ["a"]), {"b": 1})
        self.assertEqual(close, ["b"])


@dataclass(frozen=True)
class P:
    id: str
    window: str = "@1"
    role: str = ""
    session: str = ""
    dead: bool = False


def old_release(side="right"):
    """The panes a workspace from the release with the reading-width cap holds after one session was opened."""
    home = [P("%2", role="spacer"), P("%3", session="s1"), P("%1", role="sidebar")]
    if side == "left":
        home.reverse()
    return home + [P("%0", window="@2", role="placeholder"), P("%4", window="@3", session="s2")]


class CodeVersion(unittest.TestCase):
    FILES = [("bin/emx", b"launcher"), ("emx/core.py", b"core"), ("emx/emx.tmux.conf", b"conf")]

    def test_order_does_not_matter(self):
        self.assertEqual(core.code_version(self.FILES), core.code_version(list(reversed(self.FILES))))

    def test_a_content_change_changes_it(self):
        changed = [*self.FILES[:2], ("emx/emx.tmux.conf", b"conf2")]
        self.assertNotEqual(core.code_version(self.FILES), core.code_version(changed))

    def test_a_rename_or_new_file_changes_it(self):
        renamed = [("bin/emx", b"launcher"), ("emx/core2.py", b"core"), ("emx/emx.tmux.conf", b"conf")]
        self.assertNotEqual(core.code_version(self.FILES), core.code_version(renamed))
        self.assertNotEqual(core.code_version(self.FILES), core.code_version([*self.FILES, ("emx/x.py", b"")]))

    def test_moving_bytes_between_name_and_content_changes_it(self):
        self.assertNotEqual(core.code_version([("a", b"bc")]), core.code_version([("ab", b"c")]))

    def test_bytecode_dotfiles_and_editor_backups_do_not_count(self):
        for rel in (
            "emx/__pycache__/core.cpython-314.pyc", "emx/core.pyc", "emx/.DS_Store", ".hidden/x.py",
            "emx/cli.py~", "emx/emx.tmux.conf~",
        ):
            with self.subTest(rel=rel):
                self.assertFalse(core.counts_for_version(rel))
        for rel in ("bin/emx", "emx/cli.py", "emx/emx.tmux.conf"):
            with self.subTest(rel=rel):
                self.assertTrue(core.counts_for_version(rel))

    def test_needs_upgrade(self):
        self.assertTrue(core.needs_upgrade("", "abc"))
        self.assertTrue(core.needs_upgrade("old", "abc"))
        self.assertFalse(core.needs_upgrade("abc", "abc"))


class RelaunchPlan(unittest.TestCase):
    def test_upgrade_from_old_release_respawns_sidebar_and_drops_spacer(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                self.assertEqual(core.relaunch_plan(old_release(side), upgrade=True), (["%1"], ["%2"]))

    def test_viewers_and_placeholder_never_touched(self):
        respawn, drop = core.relaunch_plan(old_release(), upgrade=True)
        self.assertFalse({"%0", "%3", "%4"} & set(respawn + drop))

    def test_same_version_only_revives_a_dead_sidebar(self):
        self.assertEqual(core.relaunch_plan(old_release(), upgrade=False), ([], []))
        panes = [P("%1", role="sidebar", dead=True), P("%3", session="s1")]
        self.assertEqual(core.relaunch_plan(panes, upgrade=False), (["%1"], []))

    def test_current_layout_upgrade_only_respawns_sidebar(self):
        panes = [P("%3", session="s1"), P("%1", role="sidebar"), P("%0", window="@2", role="placeholder")]
        self.assertEqual(core.relaunch_plan(panes, upgrade=True), (["%1"], []))

    def test_a_pane_holding_a_session_is_never_chosen_whatever_its_role(self):
        panes = [P("%1", role="sidebar", session="s1", dead=True), P("%2", role="spacer", session="s2"), P("%3")]
        for upgrade in (True, False):
            with self.subTest(upgrade=upgrade):
                self.assertEqual(core.relaunch_plan(panes, upgrade=upgrade), ([], []))

    def test_never_drops_the_last_pane_of_a_window(self):
        panes = [P("%1", role="sidebar"), P("%3"), P("%5", window="@4", role="spacer")]
        self.assertEqual(core.relaunch_plan(panes, upgrade=True), (["%1"], []))

    def test_two_spacers_alone_in_a_window_keep_one(self):
        panes = [P("%5", window="@4", role="spacer"), P("%6", window="@4", role="spacer")]
        self.assertEqual(core.relaunch_plan(panes, upgrade=True), ([], ["%5"]))

    def test_no_panes(self):
        self.assertEqual(core.relaunch_plan([], upgrade=True), ([], []))


def viewer_screen(rule_line, prompt="❯ ", width=40, transcript=("some answer text", "❯ an earlier message", "")):
    return "\n".join([*transcript, rule_line, prompt, "─" * width, "  ➜ folder  │  model", "  ⏵⏵ mode line", "", ""])


def named_rule(name, width=40):
    return "─" * max(0, width - len(name) - 3) + f" {name} ─"


AGENT_VIEW = "\n".join([
    " ▐▛███▛█   Claude Code",
    " ▝▝   ▝▝   2 awaiting input · 1 working · 3 completed",
    "✻ alpha            working · doing a thing      1m",
    "✻ beta             done · finished               2h",
    "─" * 70,
    "❯ describe a task for a new session",
    "─" * 70,
    "  enter to return · space to reply · ctrl+x to delete · ? for short…",
    "",
])


class MainViewState(unittest.TestCase):
    def test_matching_name_is_ok(self):
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("alpha")), "alpha", 40), "ok")

    def test_other_name_is_mismatch(self):
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("beta")), "alpha", 40), "mismatch")

    def test_agent_view_by_footer(self):
        self.assertEqual(core.main_view_state(AGENT_VIEW, "alpha", 70), "agent_view")

    def test_agent_view_by_counts_when_footer_is_cut(self):
        screen = "\n".join(AGENT_VIEW.splitlines()[:-1] + ["  ? for shortcuts"])
        self.assertEqual(core.main_view_state(screen, "alpha", 70), "agent_view")

    def test_agent_view_for_unnamed_session(self):
        self.assertEqual(core.main_view_state(AGENT_VIEW, "", 70), "agent_view")

    def test_ansi_codes_are_ignored(self):
        screen = viewer_screen("\x1b[2m" + named_rule("alpha") + "\x1b[0m")
        self.assertEqual(core.main_view_state(screen, "alpha", 40), "ok")

    def test_names_with_spaces_and_extra_whitespace(self):
        screen = viewer_screen(named_rule("my  long name", width=60), width=60)
        self.assertEqual(core.main_view_state(screen, "my long name", 60), "ok")

    def test_truncated_name_matches_by_prefix(self):
        for rule in (" a very long session na… ─", "───── a very long session na… ─"):
            with self.subTest(rule=rule):
                screen = viewer_screen(rule, width=len(rule))
                self.assertEqual(core.main_view_state(screen, "a very long session name indeed", len(rule)), "ok")
                self.assertEqual(core.main_view_state(screen, "a very different name", len(rule)), "mismatch")

    def test_multiline_prompt_input(self):
        screen = viewer_screen(named_rule("alpha"), prompt="❯ first line\n  second line\n  third line")
        self.assertEqual(core.main_view_state(screen, "alpha", 40), "ok")
        self.assertEqual(core.main_view_state(screen, "beta", 40), "mismatch")

    def test_unnamed_session_never_mismatches(self):
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("generated-name")), "", 40), "unknown")
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("x")), "   ", 40), "unknown")

    def test_unsure_screens_are_unknown(self):
        no_prompt = "\n".join(["Do you want to allow this?", " ❯ 1. Yes", "   2. No", "─" * 40, "Esc to cancel"])
        bare_rule_no_counts = viewer_screen("─" * 40)
        startup = "Loading…\n"
        for screen in ("", "\n\n   \n", startup, no_prompt, bare_rule_no_counts):
            with self.subTest(screen=screen):
                self.assertEqual(core.main_view_state(screen, "alpha", 40), "unknown")

    def test_transcript_text_raises_no_alarm(self):
        transcript = (
            "──── beta ─",
            "❯ an earlier message about beta",
            "──────────",
            "noted: 2 awaiting input · 1 working",
            "enter to return, said the doc",
        )
        screen = viewer_screen(named_rule("alpha"), transcript=transcript)
        self.assertEqual(core.main_view_state(screen, "alpha", 40), "ok")
        no_input_box = "\n".join([*transcript, "still thinking…", "", ""])
        self.assertEqual(core.main_view_state(no_input_box, "alpha", 40), "unknown")

    def test_prompt_far_above_bottom_is_ignored(self):
        screen = named_rule("beta") + "\n❯ old\n" + "─" * 40 + "\n" + "\n".join(f"output line {i}" for i in range(30))
        self.assertEqual(core.main_view_state(screen, "alpha", 40), "unknown")

    def test_count_words_at_line_end_are_not_the_agent_header(self):
        for top in ("Step 3 completed", "build: 4 working", "noted: 2 awaiting input · 1 working"):
            with self.subTest(top=top):
                screen = viewer_screen("─" * 40, transcript=(top, "more text", ""))
                self.assertEqual(core.main_view_state(screen, "alpha", 40), "unknown")

    def test_agent_header_needs_two_counts(self):
        screen = "\n".join(AGENT_VIEW.splitlines()[:-1] + ["  ? for shortcuts"])
        one_count = screen.replace("2 awaiting input · 1 working · 3 completed", "3 completed")
        self.assertEqual(core.main_view_state(one_count, "alpha", 70), "unknown")

    def test_wide_emoji_line_does_not_hide_the_input_box(self):
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        transcript = (family * 8, "some answer text", "")
        self.assertGreater(core.display_width(family * 8), 40)
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("alpha"), transcript=transcript), "alpha", 40), "ok")
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("beta"), transcript=transcript), "alpha", 40), "mismatch")

    def test_rule_narrower_than_the_pane_is_not_the_input_box(self):
        self.assertEqual(core.main_view_state(viewer_screen(named_rule("beta")), "alpha", 41), "unknown")


FIXTURES = Path(__file__).parent / "fixtures" / "screens"


def real_screen(name):
    """A screen captured from a throwaway Claude Code session; the file name carries the pane width.

    The prompt-* screens keep their styling (`capture-pane -e`) and come from
    the version in prompt-cursors.json, with the cursor tmux reported; the
    rest are plain text, agent-view and cross-wired from 2.1.294 (agent-view
    from `claude agents --cwd`, so only throwaway sessions show) and the
    others from 2.1.293.
    """
    (path,) = FIXTURES.glob(f"{name}.w*.txt")
    return path.read_text(encoding="utf-8"), int(path.stem.rsplit(".w", 1)[1])


PROMPT_CURSORS = json.loads((FIXTURES / "prompt-cursors.json").read_text(encoding="utf-8"))


def at_cursor(name):
    """(cursor line, line under it, cursor x, cursor shown, pane width) for a prompt-* screen, as `emx left` gets them."""
    text, width = real_screen(name)
    cursor = PROMPT_CURSORS["cursors"][name]
    lines = text.split("\n")
    return lines[cursor["y"]], lines[cursor["y"] + 1], cursor["x"], cursor["shown"], width


class EmptyPromptOnRealScreens(unittest.TestCase):
    CASES = {
        "prompt-empty": True,
        "prompt-empty-bare": True,
        "prompt-text": False,
        "prompt-text-at-start": False,
        "prompt-multiline": False,
        "prompt-multiline-empty-first": False,
        "prompt-slash-menu": False,
        "prompt-permission": False,
        "prompt-model-menu": False,
    }

    def test_every_capture_is_covered(self):
        self.assertEqual(set(PROMPT_CURSORS["cursors"]), set(self.CASES))

    def test_cases(self):
        for name, want in self.CASES.items():
            with self.subTest(screen=name):
                self.assertEqual(core.empty_prompt(*at_cursor(name)), want)

    def test_the_example_text_only_counts_while_it_is_dim(self):
        line, below, x, shown, width = at_cursor("prompt-empty")
        self.assertIn("\x1b[2m", line)
        self.assertFalse(core.empty_prompt(line.replace("\x1b[2m", ""), below, x, shown, width))

    def test_an_empty_prompt_needs_the_cursor_shown_after_the_mark_and_the_rule_under_it(self):
        line, below, x, shown, width = at_cursor("prompt-empty-bare")
        self.assertTrue(core.empty_prompt(line, below, x, shown, width))
        self.assertFalse(core.empty_prompt(line, below, x + 1, shown, width))
        self.assertFalse(core.empty_prompt(line, below, x, False, width))
        self.assertFalse(core.empty_prompt(line, below, x, shown, width + 1))
        self.assertFalse(core.empty_prompt(line, "  second line", x, shown, width))
        self.assertFalse(core.empty_prompt(" " + line, below, x, shown, width))


class StyledCells(unittest.TestCase):
    def test_dim_follows_sgr_and_skips_colour_numbers(self):
        cases = [
            ("\x1b[2mab", True),
            ("\x1b[2m\x1b[22mab", False),
            ("\x1b[2m\x1b[0mab", False),
            ("\x1b[2m\x1b[mab", False),
            ("\x1b[1;2mab", True),
            ("\x1b[38;5;2mab", False),
            ("\x1b[38;2;2;2;2mab", False),
            ("\x1b[48;5;244;2mab", True),
            ("\x1b[2m\x1b[38;5;246mab", True),
        ]
        for line, dim in cases:
            with self.subTest(line=line):
                self.assertEqual(core._styled_cells(line), [("a", dim), ("b", dim)])

    def test_other_escapes_are_dropped(self):
        self.assertEqual(core._styled_cells("\x1b]8;;http://x\x07a\x1b[?25lb"), [("a", False), ("b", False)])


class FocusReports(unittest.TestCase):
    def test_reports(self):
        self.assertTrue(core.focus_report(["\x1b", "[", "I"]))
        self.assertFalse(core.focus_report(["\x1b", "[", "O"]))
        for keys in (["\x1b"], ["\x1b", "["], ["\x1b", "[", "A"], ["\x1b", "x"], ["[", "I"], ["\x1b", "[", "I", "x"]):
            with self.subTest(keys=keys):
                self.assertIsNone(core.focus_report(keys))

    def test_only_a_report_in_progress_is_worth_reading_on(self):
        self.assertTrue(core.may_be_focus_report(["\x1b"]))
        self.assertTrue(core.may_be_focus_report(["\x1b", "["]))
        for keys in (["x"], ["\x1b", "x"], ["\x1b", "[", "I"], [258]):
            with self.subTest(keys=keys):
                self.assertFalse(core.may_be_focus_report(keys))


class CameBack(unittest.TestCase):
    def test_the_left_mark_counts_only_once_the_panel_is_active(self):
        self.assertTrue(core.came_back(active=True, marked=True))
        self.assertFalse(core.came_back(active=False, marked=True))
        self.assertFalse(core.came_back(active=True, marked=False))


class NoticeExpiry(unittest.TestCase):
    def test_a_timed_notice_goes_at_its_end_and_an_untimed_one_stays(self):
        self.assertFalse(core.notice_expired(None, 1e9))
        self.assertFalse(core.notice_expired(10.0, 9.9))
        self.assertTrue(core.notice_expired(10.0, 10.0))
        self.assertTrue(core.notice_expired(10.0, 11.0))


class FocusStyle(unittest.TestCase):
    def test_selection_bar_and_title_follow_focus(self):
        self.assertIsNone(core.selection_bar(False, True))
        self.assertIsNone(core.selection_bar(False, False))
        self.assertEqual(core.selection_bar(True, True), "focused")
        self.assertEqual(core.selection_bar(True, False), "unfocused")
        self.assertEqual((core.title_style(True), core.title_style(False)), ("bold", "dim"))


class AutoRestore(unittest.TestCase):
    NOW = 1000.0

    def decide(self, state="agent_view", tried=False, checking=False, last_restore=None):
        return core.auto_restore(state, "%2", tried, checking, last_restore, self.NOW)

    def test_only_the_agent_view_is_brought_back_once_and_never_over_a_running_check(self):
        self.assertEqual(self.decide(), core.RESTORE)
        self.assertIsNone(self.decide(tried=True))
        self.assertIsNone(self.decide(checking=True))
        for state in ("mismatch", "ok", "unknown"):
            with self.subTest(state=state):
                self.assertIsNone(self.decide(state))

    def test_the_agent_view_back_soon_after_emxs_own_restore_gives_up(self):
        soon = ("%2", self.NOW - core.RESTORE_BACKOFF_SECONDS + 1, 1)
        self.assertEqual(self.decide(last_restore=soon), core.GIVE_UP)
        self.assertIsNone(self.decide(last_restore=soon, tried=True))
        self.assertIsNone(self.decide("unknown", last_restore=soon))

    def test_a_restore_long_ago_or_of_another_pane_does_not_hold_it_back(self):
        long_ago = ("%2", self.NOW - core.RESTORE_BACKOFF_SECONDS - 1, 1)
        self.assertEqual(self.decide(last_restore=long_ago), core.RESTORE)
        self.assertEqual(self.decide(last_restore=("%7", self.NOW, 1)), core.RESTORE)

    def test_the_agent_view_back_after_a_second_restore_gives_up_however_long_ago(self):
        long_ago = self.NOW - 10 * core.RESTORE_BACKOFF_SECONDS
        self.assertEqual(self.decide(last_restore=("%2", long_ago, 2)), core.GIVE_UP)
        self.assertEqual(self.decide(last_restore=("%7", long_ago, 2)), core.RESTORE)

    def test_restores_of_one_pane_add_up_and_another_pane_starts_again(self):
        first = core.record_restore(None, "%2", 1.0)
        self.assertEqual(first, ("%2", 1.0, 1))
        self.assertEqual(core.record_restore(first, "%2", 9.0), ("%2", 9.0, 2))
        self.assertEqual(core.record_restore(first, "%7", 9.0), ("%7", 9.0, 1))

    def test_his_r_is_always_carried_out_and_emxs_restore_only_while_the_agent_view_shows(self):
        self.assertTrue(core.restore_still_wanted(False, None))
        self.assertTrue(core.restore_still_wanted(False, "ok"))
        self.assertTrue(core.restore_still_wanted(True, "agent_view"))
        for state in ("ok", "mismatch", "unknown", None):
            with self.subTest(state=state):
                self.assertFalse(core.restore_still_wanted(True, state))

    def test_an_ok_read_of_the_restored_pane_ends_the_backoff(self):
        last = ("%2", self.NOW, 1)
        self.assertIsNone(core.last_restore_after(last, "%2", "ok"))
        for pane, state in (("%2", "agent_view"), ("%2", "unknown"), ("%2", "mismatch"), ("%7", "ok")):
            with self.subTest(pane=pane, state=state):
                self.assertEqual(core.last_restore_after(last, pane, state), last)
        self.assertIsNone(core.last_restore_after(None, "%2", "ok"))

    def test_a_pane_stays_tried_only_while_it_is_flagged(self):
        self.assertEqual(core.still_tried(frozenset({"%2", "%3"}), frozenset({"%3", "%4"})), frozenset({"%3"}))
        self.assertEqual(core.still_tried(frozenset({"%2"}), frozenset()), frozenset())

    def test_restoring_main_means_emxs_own_check_on_the_pane_in_main(self):
        self.assertTrue(core.restoring_main(("s1", "%2"), True, "%2"))
        self.assertFalse(core.restoring_main(("s1", "%2"), False, "%2"))
        self.assertFalse(core.restoring_main(("s1", "%2"), True, "%5"))
        self.assertFalse(core.restoring_main(("s1", "%2"), True, None))

    def test_the_off_main_warning_waits_while_emx_brings_it_back(self):
        flagged = frozenset({"%2"})
        self.assertTrue(core.off_main_warning(flagged, "%2", restoring=False))
        self.assertFalse(core.off_main_warning(flagged, "%2", restoring=True))
        self.assertFalse(core.off_main_warning(flagged, "%5", restoring=False))
        self.assertFalse(core.off_main_warning(flagged, None, restoring=False))


class MainViewStateOnRealScreens(unittest.TestCase):
    CASES = [
        ("named-viewer", "emx-test-named", "ok"),
        ("generated-name-viewer", "ping-pong-reply", "ok"),
        ("unnamed-empty-viewer", "", "unknown"),
        ("unnamed-empty-viewer", "emx-test-named", "unknown"),
        ("agent-view", "emx-test-named", "agent_view"),
        ("agent-view", "", "agent_view"),
        ("working-streaming", "emx-test-working", "ok"),
        ("working-spinner", "emx-test-perm", "ok"),
        ("permission-dialog", "emx-test-perm", "unknown"),
        ("permission-dialog", "emx-test-named", "unknown"),
        ("permission-dismissed", "emx-test-perm", "ok"),
        ("cross-wired", "emx-test-named", "mismatch"),
        ("cross-wired", "emx-test-perm", "ok"),
    ]

    def test_cases(self):
        for name, expected, want in self.CASES:
            with self.subTest(screen=name, expected=expected):
                text, width = real_screen(name)
                self.assertEqual(core.main_view_state(text, expected, width), want)

    def test_agent_view_header_alone(self):
        text, width = real_screen("agent-view")
        lines = [line for line in text.splitlines() if line.strip()]
        cut = "\n".join(lines[:-1] + ["  ? for shortcuts"])
        self.assertEqual(core.main_view_state(cut, "emx-test-named", width), "agent_view")


class LatchOffMain(unittest.TestCase):
    def test_off_states_flag_the_pane(self):
        for state in ("agent_view", "mismatch"):
            with self.subTest(state=state):
                self.assertEqual(core.latch_off_main(frozenset(), "%1", state), (frozenset({"%1"}), True))

    def test_ok_clears_only_that_pane(self):
        self.assertEqual(core.latch_off_main(frozenset({"%1", "%2"}), "%1", "ok"), (frozenset({"%2"}), False))

    def test_unknown_keeps_the_flag(self):
        self.assertEqual(core.latch_off_main(frozenset({"%1"}), "%1", "unknown"), (frozenset({"%1"}), True))
        self.assertEqual(core.latch_off_main(frozenset(), "%1", "unknown"), (frozenset(), False))

    def test_flag_survives_another_session_in_main(self):
        flagged, _ = core.latch_off_main(frozenset(), "%A", "agent_view")
        flagged, respawn_c = core.latch_off_main(flagged, "%C", "ok")
        self.assertFalse(respawn_c)
        flagged, respawn_a = core.latch_off_main(flagged, "%A", "unknown")
        self.assertTrue(respawn_a)
        self.assertEqual(flagged, frozenset({"%A"}))

    def test_input_set_is_not_mutated(self):
        before = frozenset({"%1"})
        core.latch_off_main(before, "%2", "mismatch")
        core.latch_off_main(before, "%1", "ok")
        self.assertEqual(before, frozenset({"%1"}))


class ListedSession(unittest.TestCase):
    ROWS = [{"id": "abc123", "kind": "background", "state": "working"}, {"kind": "interactive"}, {"id": 5}]

    def test_exact_id_is_listed_with_its_running_state(self):
        found = core.listed_session(self.ROWS, "abc123")
        self.assertEqual((found.id, found.running), ("abc123", False))
        with_pid = core.listed_session([{**self.ROWS[0], "pid": 9}], "abc123")
        self.assertTrue(with_pid.running)

    def test_prefix_suffix_or_case_variants_are_not(self):
        for sid in ("abc", "abc1234", "ABC123", " abc123", "bc123"):
            with self.subTest(sid=sid):
                self.assertIsNone(core.listed_session(self.ROWS, sid))

    def test_empty_inputs(self):
        self.assertIsNone(core.listed_session([], "abc123"))
        self.assertIsNone(core.listed_session(self.ROWS, ""))
        self.assertIsNone(core.listed_session([{"id": "", "kind": "background"}], ""))


class RestartCheck(unittest.TestCase):
    """The listing a restart waits on arrives later, on another thread."""

    def test_checked_row_from_a_listing(self):
        a, b = mk(id="a"), mk(id="b")
        self.assertEqual(core.checked_row([a, b], "b"), (b, True))
        self.assertEqual(core.checked_row([a], "b"), (None, True))
        self.assertEqual(core.checked_row(None, "b"), (None, False))

    def test_the_answer_acts_only_on_the_session_and_pane_it_was_asked_about(self):
        asked = ("s1", "%2")
        self.assertTrue(core.check_still_applies(asked, "%2", "s1"))
        for main_id, main_session in (("%2", "s2"), ("%5", "s1"), (None, None), ("%2", "")):
            with self.subTest(main_id=main_id, main_session=main_session):
                self.assertFalse(core.check_still_applies(asked, main_id, main_session))
        self.assertFalse(core.check_still_applies(("", "%2"), "%2", ""))

    def test_refusal_says_why(self):
        self.assertIn("Couldn't list", core.refusal("emx-test-a", core.RESTART, False, mk()))
        self.assertIn("isn't listed", core.refusal("emx-test-a", core.RESTART, True, None))
        self.assertIn("isn't running", core.refusal("emx-test-a", core.NEW, True, mk(running=False)))
        self.assertTrue(core.refusal("emx-test-a", core.OPEN, False, None).startswith("emx-test-a isn't listed"))


class CloseConfirm(unittest.TestCase):
    """Ctrl+X twice on one row closes that session: a running one is stopped, then removed; anything else in between cancels."""

    def setUp(self):
        self.running = mk(id="a", name="emx-test-a")
        self.stopped = mk(id="a", name="emx-test-a", state="stopped", running=False)
        self.on_a = ("session", "a")

    def test_close_args_stop_then_remove_a_running_session(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertEqual(core.close_args("claude", armed), [["claude", "stop", "a"], ["claude", "rm", "a"]])
        for args in core.close_args("claude", armed):
            with self.subTest(args=args):
                self.assertFalse(any(a.startswith("--") for a in args))
                self.assertNotIn("--discard-unpushed", args)
                self.assertNotIn("--force-remove-worktree", args)

    def test_close_args_only_remove_a_stopped_or_exited_one(self):
        exited = mk(id="a", state="done", running=False)
        for row in (self.stopped, exited):
            with self.subTest(state=row.state):
                armed, _ = core.press_close(None, self.on_a, row, 0.0)
                self.assertEqual(core.close_args("claude", armed), [["claude", "rm", "a"]])

    def test_the_second_press_within_the_window_acts_on_the_first_press(self):
        armed, act = core.press_close(None, self.on_a, self.running, 10.0)
        self.assertIsNone(act)
        kept, act = core.press_close(armed, self.on_a, self.running, 10.0 + core.CONFIRM_SECONDS - 0.01)
        self.assertEqual((kept, act), (None, armed))

    def test_a_late_second_press_only_rearms(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 10.0)
        rearmed, act = core.press_close(armed, self.on_a, self.running, 10.0 + core.CONFIRM_SECONDS)
        self.assertIsNone(act)
        self.assertEqual(rearmed.at, 10.0 + core.CONFIRM_SECONDS)

    def test_a_press_on_another_row_arms_that_row_and_never_acts(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        b = mk(id="b", name="emx-test-b")
        rearmed, act = core.press_close(armed, ("session", "b"), b, 0.5)
        self.assertIsNone(act)
        self.assertEqual(rearmed.id, "b")

    def test_no_row_or_a_row_that_is_not_the_cursors_does_nothing(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        for cursor, row in ((("group", "/p"), None), (None, None), (self.on_a, None), (("session", "b"), self.running),
                            (("group", "/p"), core.Group("/q", (self.running,)))):
            with self.subTest(cursor=cursor, row=row):
                self.assertEqual(core.press_close(armed, cursor, row, 0.5), (None, None))

    def test_the_confirm_holds_only_on_its_row_in_its_state_inside_the_window(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertTrue(core.confirm_holds(armed, self.on_a, self.running, 1.0))
        cases = {
            "cursor moved": (("session", "b"), self.running, 1.0),
            "cursor on a header": (("group", "/p"), self.running, 1.0),
            "no longer listed": (self.on_a, None, 1.0),
            "stopped meanwhile": (self.on_a, self.stopped, 1.0),
            "timed out": (self.on_a, self.running, core.CONFIRM_SECONDS),
            "clock went back": (self.on_a, self.running, -1.0),
        }
        for why, (cursor, row, now) in cases.items():
            with self.subTest(why):
                self.assertFalse(core.confirm_holds(armed, cursor, row, now))
        self.assertFalse(core.confirm_holds(None, self.on_a, self.running, 0.0))

    def test_a_state_change_since_the_first_press_rearms_instead_of_acting(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        rearmed, act = core.press_close(armed, self.on_a, self.stopped, 0.5)
        self.assertIsNone(act)
        self.assertFalse(rearmed.running)

    def test_the_acted_id_and_name_are_the_armed_ones(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        renamed = mk(id="a", name="emx-test-renamed")
        _, act = core.press_close(armed, self.on_a, renamed, 0.5)
        self.assertEqual((act.id, act.name), ("a", "emx-test-a"))

    def test_any_other_key_cancels_but_a_resize_does_not(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertIsNone(core.confirm_after_key(armed, close_key=False, passive=False))
        self.assertIs(core.confirm_after_key(armed, close_key=True, passive=False), armed)
        self.assertIs(core.confirm_after_key(armed, close_key=False, passive=True), armed)

    def test_blocked_while_listing_fails_or_another_close_runs(self):
        busy, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertIsNone(core.close_blocked(False, None))
        self.assertIn("Can't list sessions", core.close_blocked(True, None))
        self.assertEqual(core.close_blocked(False, busy), "Still closing emx-test-a…")

    def test_a_close_runs_only_if_a_fresh_listing_still_shows_the_row_as_he_saw_it(self):
        stop, _ = core.press_close(None, self.on_a, self.running, 0.0)
        remove, _ = core.press_close(None, self.on_a, self.stopped, 0.0)
        self.assertTrue(core.close_still_applies(stop, self.running, True))
        self.assertTrue(core.close_still_applies(remove, self.stopped, True))
        self.assertFalse(core.close_still_applies(remove, self.running, True))
        self.assertEqual(core.close_refusal(remove, self.running, True), "emx-test-a is running again, so emx didn't close it.")
        self.assertFalse(core.close_still_applies(stop, self.stopped, True))
        self.assertIn("no longer running", core.close_refusal(stop, self.stopped, True))
        self.assertFalse(core.close_still_applies(stop, None, True))
        self.assertIn("isn't listed", core.close_refusal(stop, None, True))
        self.assertFalse(core.close_still_applies(stop, self.running, False))
        self.assertIn("Couldn't list", core.close_refusal(stop, None, False))

    def test_notices(self):
        stop, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertEqual((core.closing_text(stop), core.closed_text(stop)), ("Closing emx-test-a…", "Closed emx-test-a."))
        self.assertEqual(core.stopped_not_removed_text(stop, "worktree has 2 unpushed commits"),
                         "Stopped emx-test-a, but didn't remove it: worktree has 2 unpushed commits")

    def test_a_closed_notice_carries_what_rm_printed_past_its_first_line(self):
        remove, _ = core.press_close(None, self.on_a, self.stopped, 0.0)
        output = "\n\x1b[1mremoved a\x1b[0m\n\n  worktree directory left at /x/wt\n  (git no longer recognized it)\n"
        self.assertEqual(
            core.closed_text(remove, output), "Closed emx-test-a. worktree directory left at /x/wt (git no longer recognized it)",
        )
        self.assertEqual(core.closed_text(remove, "removed a\n"), "Closed emx-test-a.")

    def test_one_line_keeps_every_line_of_a_refusal(self):
        self.assertEqual(core.one_line("\x1b[31mrefused:\x1b[0m\n\n  2 unpushed commits\n"), "refused: 2 unpushed commits")
        self.assertEqual(core.one_line(""), "")

    def group(self, *sessions, key="/r/app"):
        return core.Group(key, tuple(sessions))

    def test_header_press_arms_a_snapshot_of_its_sessions_and_the_second_acts_on_it(self):
        b = mk(id="b", name="emx-test-b", state="stopped", running=False)
        header = ("group", "/r/app")
        armed, act = core.press_close(None, header, self.group(self.running, b), 5.0)
        self.assertIsNone(act)
        self.assertIsInstance(armed, core.GroupConfirm)
        self.assertEqual([(c.id, c.name, c.running) for c in armed.sessions],
                         [("a", "emx-test-a", True), ("b", "emx-test-b", False)])
        renamed = mk(id="a", name="emx-test-renamed")
        kept, act = core.press_close(armed, header, self.group(renamed, b), 6.0)
        self.assertEqual((kept, act), (None, armed))
        self.assertEqual(core.close_targets(act), armed.sessions)
        self.assertEqual(core.closing_text(act), "Closing 2 sessions…")
        self.assertEqual(core.close_targets(core.press_close(None, self.on_a, self.running, 0.0)[0])[0].id, "a")

    def test_a_group_confirm_drops_when_a_session_joins_leaves_or_changes_running(self):
        b = mk(id="b", name="emx-test-b")
        header = ("group", "/r/app")
        armed, _ = core.press_close(None, header, self.group(self.running, b), 0.0)
        self.assertTrue(core.confirm_holds(armed, header, self.group(b, self.running), 1.0))
        cases = {
            "joined": (header, self.group(self.running, b, mk(id="c")), 1.0),
            "left": (header, self.group(self.running), 1.0),
            "stopped": (header, self.group(self.stopped, b), 1.0),
            "cursor moved": (self.on_a, self.group(self.running, b), 1.0),
            "other header": (("group", "/r/other"), self.group(self.running, b, key="/r/other"), 1.0),
            "timed out": (header, self.group(self.running, b), core.CONFIRM_SECONDS),
            "group gone": (header, None, 1.0),
        }
        for why, (cursor, row, now) in cases.items():
            with self.subTest(why):
                self.assertFalse(core.confirm_holds(armed, cursor, row, now))
        rearmed, act = core.press_close(armed, header, self.group(self.running), 1.0)
        self.assertIsNone(act)
        self.assertEqual([c.id for c in rearmed.sessions], ["a"])

    def test_only_a_project_header_arms(self):
        pinned = core.Group(core.PINNED, (self.running,))
        self.assertEqual(core.press_close(None, ("group", core.PINNED), pinned, 0.0, core.PROJECT), (None, None))
        working = core.Group(core.WORKING, (self.running,))
        self.assertEqual(core.press_close(None, ("group", core.WORKING), working, 0.0, core.BY_STATE), (None, None))
        armed, _ = core.press_close(None, ("group", "/r/app"), self.group(self.running), 0.0, core.PROJECT)
        self.assertIsInstance(armed, core.GroupConfirm)
        self.assertFalse(core.group_closable(core.PROJECT, core.PINNED))
        self.assertFalse(core.group_closable(core.BY_STATE, core.WORKING))
        self.assertTrue(core.group_closable(core.PROJECT, "/r/app"))
        self.assertEqual(core.GROUP_CLOSE_REFUSAL, "Ctrl+X closes a project's group from the project view.")

    def test_labels_read_close_and_count(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertEqual(core.confirm_label(armed), "ctrl+x again to close")
        self.assertEqual(core.group_confirm_label(1), "ctrl+x again to close 1 session")
        self.assertEqual(core.group_confirm_label(3), "ctrl+x again to close 3 sessions")
        group, _ = core.press_close(None, ("group", "/r/app"), self.group(self.running, mk(id="b")), 0.0)
        self.assertEqual(core.confirm_label(group), "ctrl+x again to close 2 sessions")
        self.assertEqual(core.CLOSING_LABEL, "closing…")

    def test_a_header_reads_its_armed_label_or_closing_only_for_its_own_group(self):
        b = mk(id="b", name="emx-test-b")
        app, other = self.group(self.running, b), self.group(mk(id="z"), key="/r/other")
        armed, _ = core.press_close(None, ("group", "/r/app"), app, 0.0)
        self.assertEqual(core.header_label(armed, app, {}), "ctrl+x again to close 2 sessions")
        self.assertIsNone(core.header_label(armed, other, {}))
        self.assertEqual(core.header_label(None, app, {"a": None, "b": True}), "closing…")
        self.assertIsNone(core.header_label(None, app, {"a": None}))
        row, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertIsNone(core.header_label(row, app, {}))

    def test_closing_ids_stay_until_a_listing_lacks_them_after_success_and_go_at_once_on_failure(self):
        closing = {"a": None, "b": None}
        self.assertEqual(core.closing_after(closing, {}, ["a", "b"]), closing)
        self.assertEqual(core.closing_after(closing, {}, ["c"]), closing)
        after = core.closing_after(closing, {"a": True, "b": False}, None)
        self.assertEqual(after, {"a": True})
        self.assertEqual(core.closing_after(after, {}, ["a", "c"]), {"a": True})
        self.assertEqual(core.closing_after(after, {}, []), {"a": True})
        self.assertEqual(core.closing_after(after, {}, None), {"a": True})
        self.assertEqual(core.closing_after(after, {}, ["c"]), {})


def named(sid, name, cwd="/r/app", state="blocked", running=True, started=None):
    return core.Session(sid, name, cwd, state, "", "", started, running)


class Find(unittest.TestCase):
    def setUp(self):
        self.sessions = [
            named("a", "Fix Login", "/h/me/work/shop"),
            named("b", "docs pass", "/h/me/work/Blog"),
            named("c", "c", "/srv/tools"),
        ]

    def ids(self, query):
        return [s.id for s in core.find_matches(self.sessions, query, "/h/me")]

    def test_a_substring_of_the_name_any_case(self):
        self.assertEqual(self.ids("LOGIN"), ["a"])
        self.assertEqual(self.ids("ss"), ["b"])

    def test_a_substring_of_the_folder_as_shown(self):
        self.assertEqual(self.ids("blog"), ["b"])
        self.assertEqual(self.ids("work/"), ["a", "b"])
        self.assertEqual(self.ids("srv"), ["c"])

    def test_the_home_path_itself_never_matches_every_session(self):
        self.assertEqual(self.ids("/h/me"), [])

    def test_letters_apart_do_not_match(self):
        self.assertEqual(self.ids("flg"), [])

    def test_nothing_typed_shows_every_session(self):
        self.assertEqual(self.ids(""), ["a", "b", "c"])
        self.assertEqual(self.ids("  "), ["a", "b", "c"])

    def test_the_top_match_is_the_first_session_row(self):
        groups = core.group_sessions(self.sessions, {}, core.PROJECT)
        items = [line for line in core.panel_lines(groups, frozenset()) if line]
        self.assertEqual(core.top_match(items), items[1])
        self.assertIsNone(core.top_match([("group", "/x")]))
        self.assertIsNone(core.top_match([]))

    def test_arrows_while_finding_skip_headers(self):
        items = [("group", "/x"), ("session", "a"), ("group", "/y"), ("session", "b")]
        self.assertEqual(core.find_move(items, ("session", "a"), 1), ("session", "b"))
        self.assertEqual(core.find_move(items, ("session", "b"), -1), ("session", "a"))
        self.assertEqual(core.find_move(items, ("group", "/y"), 1), ("session", "a"))
        self.assertIsNone(core.find_move([("group", "/x")], None, 1))

    def test_enter_opens_the_session_under_the_cursor_else_the_top_match(self):
        items = [("group", "/x"), ("session", "a"), ("session", "b")]
        self.assertEqual(core.find_target(items, ("session", "b")), ("session", "b"))
        self.assertEqual(core.find_target(items, ("group", "/x")), ("session", "a"))
        self.assertEqual(core.find_target(items, None), ("session", "a"))
        self.assertIsNone(core.find_target([("group", "/x")], ("group", "/x")))

    def test_nothing_is_folded_while_finding(self):
        prefs = core.toggle_collapsed(dict(core.DEFAULT_PREFS), core.PROJECT, "/r/app")
        self.assertEqual(core.finding_collapsed(prefs, core.PROJECT, False), {"/r/app"})
        self.assertEqual(core.finding_collapsed(prefs, core.PROJECT, True), frozenset())


class Pins(unittest.TestCase):
    def setUp(self):
        self.sessions = [
            named("a", "alpha", "/r/app", "working", started=1.0),
            named("b", "beta", "/r/app", "done", started=2.0),
            named("c", "gamma", "/r/zed", "blocked", started=3.0),
        ]

    def test_pinned_sessions_leave_their_group_for_a_pinned_group_at_the_top_in_both_views(self):
        for view in core.VIEWS:
            with self.subTest(view=view):
                groups = core.group_sessions(self.sessions, {}, view, frozenset({"c", "b"}))
                self.assertEqual(groups[0], core.Group(core.PINNED, (self.sessions[1], self.sessions[2])))
                self.assertEqual([s.id for g in groups[1:] for s in g.sessions], ["a"])

    def test_no_pins_no_pinned_group(self):
        groups = core.group_sessions(self.sessions, {}, core.PROJECT, frozenset({"gone"}))
        self.assertNotIn(core.PINNED, [g.key for g in groups])

    def test_the_pinned_key_is_no_folder_and_no_state(self):
        self.assertFalse(core.PINNED.startswith("/"))
        self.assertNotIn(core.PINNED, core.CATEGORIES)

    def test_the_pinned_header_says_pinned_in_both_views(self):
        for view in core.VIEWS:
            with self.subTest(view=view):
                parts = core.header_layout(view, core.PINNED, 2, True, 40)
                self.assertEqual([(t, p) for _, t, p in parts], [("▸", "arrow"), ("Pinned", "name"), ("2", "count")])

    def test_toggle_pins_then_unpins(self):
        prefs = core.toggle_pin(dict(core.DEFAULT_PREFS), "a")
        prefs = core.toggle_pin(prefs, "b")
        self.assertEqual(core.pinned_ids(prefs), {"a", "b"})
        prefs = core.toggle_pin(prefs, "a")
        self.assertEqual(prefs["pinned"], ["b"])

    def test_pins_survive_saving_and_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "emx" / "prefs.json"
            core.save_prefs(path, core.toggle_pin(dict(core.DEFAULT_PREFS), "a"))
            self.assertEqual(core.pinned_ids(core.load_prefs(path)), {"a"})

    def test_bad_pins_in_the_file_are_dropped(self):
        prefs = core.normalize_prefs({"pinned": ["a", "", 3, None, "a", "b"]})
        self.assertEqual(prefs["pinned"], ["a", "b"])
        self.assertNotIn("pinned", core.normalize_prefs({"pinned": "a"}))
        self.assertNotIn("pinned", core.normalize_prefs({"pinned": []}))

    def test_a_pin_is_pruned_once_its_session_misses_two_listings_in_a_row(self):
        prefs = {**core.DEFAULT_PREFS, "pinned": ["a", "b"]}
        prefs, misses, changed = core.prune_pins(prefs, ["a"], {})
        self.assertEqual((prefs["pinned"], changed), (["a", "b"], False))
        prefs, misses, changed = core.prune_pins(prefs, ["a"], misses)
        self.assertEqual((prefs["pinned"], changed), (["a"], True))

    def test_a_session_back_in_the_listing_resets_its_misses(self):
        prefs = {**core.DEFAULT_PREFS, "pinned": ["b"]}
        prefs, misses, _ = core.prune_pins(prefs, ["x"], {})
        prefs, misses, _ = core.prune_pins(prefs, ["b"], misses)
        prefs, misses, changed = core.prune_pins(prefs, ["x"], misses)
        self.assertEqual((prefs["pinned"], changed), (["b"], False))

    def test_a_listing_with_no_sessions_prunes_nothing(self):
        prefs = {**core.DEFAULT_PREFS, "pinned": ["a", "b"]}
        misses = {}
        for _ in range(core.STALE_AFTER_MISSES + 1):
            prefs, misses, changed = core.prune_pins(prefs, [], misses)
            self.assertEqual((prefs["pinned"], misses, changed), (["a", "b"], {}, False))

    def test_an_edit_is_gone_only_after_its_session_misses_two_listings_in_a_row(self):
        misses, gone = core.edit_session_gone("a", ["b"], {})
        self.assertEqual((misses, gone), ({"a": 1}, False))
        misses, gone = core.edit_session_gone("a", ["b"], misses)
        self.assertTrue(gone)

    def test_an_empty_listing_never_counts_against_an_edit(self):
        misses = {"a": 1}
        for listing in ([], iter([])):
            misses, gone = core.edit_session_gone("a", listing, misses)
            self.assertEqual((misses, gone), ({"a": 1}, False))

    def test_a_listing_that_has_the_edits_session_resets_its_misses(self):
        misses, _ = core.edit_session_gone("a", ["b"], {})
        misses, gone = core.edit_session_gone("a", ["a"], misses)
        self.assertEqual((misses, gone), ({}, False))

    def test_n_on_the_pinned_header_never_takes_its_key_for_a_folder(self):
        groups = core.group_sessions(self.sessions, {}, core.PROJECT, frozenset({"a"}))
        self.assertEqual(core.default_folder(("group", core.PINNED), groups, self.sessions, core.PROJECT, "/r/last"), "/r/last")
        self.assertNotIn(core.PINNED, core.session_folders(self.sessions, groups, core.PROJECT))

    def test_only_by_state_headers_other_than_pinned_name_a_state(self):
        self.assertTrue(core.state_group(core.BY_STATE, core.WORKING))
        self.assertFalse(core.state_group(core.BY_STATE, core.PINNED))
        self.assertFalse(core.state_group(core.PROJECT, "/r/app"))
        self.assertFalse(core.state_group(core.PROJECT, core.PINNED))

    def test_the_notice_says_whether_the_session_is_now_pinned(self):
        prefs = core.toggle_pin(dict(core.DEFAULT_PREFS), "a")
        self.assertEqual(core.pin_notice(prefs, self.sessions[0]), "Pinned alpha.")
        self.assertEqual(core.pin_notice(core.toggle_pin(prefs, "a"), self.sessions[0]), "Unpinned alpha.")


def renamed_rule(name):
    """The named-viewer capture with its rule naming `name` instead, drawn as Claude Code draws a rule name."""
    text, width = real_screen("named-viewer")
    rule = next(line for line in text.split("\n") if line.endswith(" emx-test-named ─"))
    return text.replace(rule, "─" * (width - core.display_width(name) - 3) + f" {name} ─"), width


def rename_screen(viewer, prompt, in_mode=False, flagged=False):
    captured, _ = real_screen(viewer)
    line, below, x, shown, width = at_cursor(prompt)
    return core.RenameScreen(captured, line, below, x, 26, shown, width, in_mode, flagged)


class RenameChecks(unittest.TestCase):
    def setUp(self):
        self.rename = core.Rename("a", "emx-test-named", "beta")
        self.session = named("a", "emx-test-named")
        self.others = [named("b", "bee"), named("c", "c", state="stopped", running=False)]

    def test_the_edit_opens_with_the_name_or_empty_for_an_unnamed_session(self):
        self.assertEqual(core.rename_start(named("a", "alpha")), "alpha")
        self.assertEqual(core.rename_start(named("a", "a")), "")

    def test_a_request_is_one_plain_line_and_emoji_joiners_are_plain(self):
        self.assertEqual(core.rename_request("a", "old", "  my name; with spaces "),
                         (core.Rename("a", "old", "my name; with spaces"), None))
        family = "fam \U0001f468\u200d\U0001f469\u200d\U0001f467"
        self.assertEqual(core.rename_request("a", "old", family)[0].new, family)
        self.assertTrue(core.name_char_allowed("\u200d"))
        for bad in ("", "   ", "two\nlines", "tab\there", "esc\x1bseq", "del\x7f", "line\u2028sep", "para\u2029sep",
                    "x" * (core.RENAME_MAX + 1)):
            with self.subTest(bad=bad):
                rename, error = core.rename_request("a", "old", bad)
                self.assertIsNone(rename)
                self.assertTrue(error)
        self.assertEqual(core.rename_request("a", "old", "x" * core.RENAME_MAX)[0].new, "x" * core.RENAME_MAX)

    def test_a_name_ending_in_a_backslash_is_refused_and_one_holding_a_backslash_or_at_sign_is_typed(self):
        for bad in ("name\\", "name\\  ", "name\\\\", "\\"):
            with self.subTest(bad=bad):
                rename, error = core.rename_request("a", "old", bad)
                self.assertIsNone(rename)
                self.assertIn("end with", error)
        for good in ("a\\b", "a@b", "x @y", "x @", "@x"):
            with self.subTest(good=good):
                self.assertEqual(core.rename_request("a", "old", good), (core.Rename("a", "old", good), None))

    def test_no_new_name_with_text_left_in_the_prompt_says_so(self):
        rename = core.Rename("a", "alpha", "beta")
        self.assertEqual(core.renamed_text(rename, "beta", False), "Renamed alpha to beta.")
        self.assertIn("doesn't show a new name yet", core.renamed_text(rename, None))
        left = core.renamed_text(rename, None, False)
        self.assertIn("prompt isn't empty", left)
        self.assertIn("may not have been sent", left)

    def test_an_unchanged_name_is_no_request_and_no_error_even_when_too_long_to_type(self):
        self.assertEqual(core.rename_request("a", "old", " old "), (None, None))
        long = "x" * (core.RENAME_MAX + 10)
        self.assertEqual(core.rename_request("a", long, long), (None, None))

    def test_the_longest_line_stays_under_what_claude_code_takes_for_a_paste(self):
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        for unit in ("x", "あ", "\U0001f600", family):
            with self.subTest(unit=unit):
                longest = unit * (core.RENAME_MAX // core.typed_length(unit))
                self.assertIsNotNone(core.rename_request("a", "old", longest)[0])
                self.assertLess(core.typed_length(core.rename_line(longest)), 63)
                self.assertIsNone(core.rename_request("a", "old", longest + unit)[0])
        self.assertEqual(core.typed_length("\U0001f600"), 2)
        self.assertEqual(core.typed_length(family), 8)

    def test_the_line_typed_is_exactly_the_slash_command(self):
        self.assertEqual(core.rename_line("my name"), "/rename my name")

    def test_the_live_viewer_is_the_one_emx_opened_on_the_session_and_not_dead(self):
        @dataclass
        class P:
            id: str
            session: str
            dead: bool = False

        panes = [P("%1", ""), P("%2", "a", dead=True), P("%3", "b"), P("%4", "a")]
        self.assertEqual(core.live_viewer(panes, "a").id, "%4")
        self.assertIsNone(core.live_viewer(panes[:3], "a"))

    def test_ctrl_r_needs_a_running_session_with_an_open_viewer_and_a_healthy_listing(self):
        running = named("a", "alpha")
        self.assertIsNone(core.rename_blocked(False, None, running, True))
        self.assertIn("Open alpha once", core.rename_blocked(False, None, running, False))
        stopped = named("a", "alpha", state="stopped", running=False)
        self.assertIn("isn't running", core.rename_blocked(False, None, stopped, True))
        self.assertIn("Can't list", core.rename_blocked(True, None, running, True))
        self.assertIn("Still renaming", core.rename_blocked(False, self.rename, running, True))

    def test_the_fresh_listing_must_show_it_running_and_not_working(self):
        self.assertIsNone(core.rename_listing_refusal(self.rename, named("a", "alpha", state="blocked"), True))
        self.assertIsNone(core.rename_listing_refusal(self.rename, named("a", "alpha", state="done"), True))
        cases = {
            "Couldn't list": (named("a", "alpha"), False),
            "isn't listed": (None, True),
            "isn't running": (named("a", "alpha", state="stopped", running=False), True),
            "is working": (named("a", "alpha", state="working"), True),
        }
        for want, (session, fresh) in cases.items():
            with self.subTest(want=want):
                self.assertIn(want, core.rename_listing_refusal(self.rename, session, fresh))

    def identity(self, session, others, screen):
        text, width = real_screen(screen) if isinstance(screen, str) else screen
        return core.viewer_identity(session, [session] + others, text, width)

    def test_a_viewer_is_its_sessions_only_when_no_other_listed_session_fits_its_screen(self):
        unnamed = named("u", "u")
        stopped_unnamed = named("v", "v", state="stopped", running=False)
        cases = [
            (self.session, self.others, "named-viewer", "ok"),
            (self.session, self.others + [named("d", "emx-test-named")], "named-viewer", "ambiguous"),
            (named("a", "someone-else"), self.others, "named-viewer", "other"),
            (self.session, self.others, "agent-view", "other"),
            (self.session, self.others, "cross-wired", "other"),
            (self.session, self.others, "unnamed-empty-viewer", "other"),
            (unnamed, self.others, "unnamed-empty-viewer", "ok"),
            (unnamed, self.others + [named("v", "v")], "unnamed-empty-viewer", "ambiguous"),
            (unnamed, self.others + [stopped_unnamed], "unnamed-empty-viewer", "ok"),
            (unnamed, self.others, "named-viewer", "other"),
            (unnamed, self.others, "cross-wired", "other"),
            (unnamed, self.others, "agent-view", "other"),
        ]
        for session, others, screen, want in cases:
            with self.subTest(session=session.name, others=len(others), screen=screen):
                self.assertEqual(self.identity(session, others, screen), want)

    def test_a_name_cut_to_fit_proves_the_session_only_when_it_fits_no_other(self):
        long = named("a", "emx-test-a-rather-long-session-name")
        cut = renamed_rule("emx-test-a-rather…")
        self.assertEqual(self.identity(long, self.others, cut), "ok")
        self.assertEqual(self.identity(long, self.others + [named("d", "emx-test-a-rather-short")], cut), "ambiguous")

    def test_a_stopped_session_with_the_same_name_is_no_twin(self):
        stopped_twin = named("d", "emx-test-named", state="stopped", running=False)
        self.assertEqual(self.identity(self.session, self.others + [stopped_twin], "named-viewer"), "ok")

    def test_the_screen_must_show_this_sessions_empty_prompt_outside_any_mode(self):
        sessions = [self.session] + self.others

        def refusal(screen, listed=sessions):
            return core.rename_screen_refusal(self.rename, self.session, listed, screen)

        self.assertIsNone(refusal(rename_screen("named-viewer", "prompt-empty")))
        self.assertIn("scrolled back", refusal(rename_screen("named-viewer", "prompt-empty", in_mode=True)))
        self.assertIn("isn't showing its prompt", refusal(rename_screen("named-viewer", "prompt-empty", flagged=True)))
        self.assertIn("isn't showing its prompt", refusal(rename_screen("agent-view", "prompt-empty")))
        for prompt in ("prompt-text", "prompt-text-at-start", "prompt-slash-menu", "prompt-permission", "prompt-multiline"):
            with self.subTest(prompt=prompt):
                self.assertIn("isn't empty", refusal(rename_screen("named-viewer", prompt)))
        twin = refusal(rename_screen("named-viewer", "prompt-empty"), sessions + [named("d", "emx-test-named")])
        self.assertIn("looks the same as another session's", twin)

    def test_an_unnamed_session_twinned_by_another_unnamed_one_is_sent_to_rename_itself(self):
        unnamed = named("u", "u")
        rename = core.Rename("u", "u", "fresh")
        screen = rename_screen("unnamed-empty-viewer", "prompt-empty")
        refusal = core.rename_screen_refusal(rename, unnamed, [unnamed, named("v", "v")], screen)
        self.assertIn("both unnamed", refusal)
        self.assertIn("Rename it in the session itself: /rename", refusal)

    def test_the_rule_name_is_read_off_the_input_box(self):
        cases = {"named-viewer": "emx-test-named", "unnamed-empty-viewer": "", "cross-wired": "emx-test-perm",
                 "permission-dialog": None}
        for screen, want in cases.items():
            with self.subTest(screen=screen):
                self.assertEqual(core.rule_name(*real_screen(screen)), want)

    def test_the_gate_takes_only_plain_ids(self):
        self.assertIsNotNone(core.type_gate("%3", "%1", "f00dcafe", 26, "@emx_session"))
        bad = (("%3,", "%1", "a"), ("%3", "1", "a"), ("%3", "%1", "a}"), ("%3", "%1", "a b"), ("%3", "%1", ""))
        for viewer, panel, sid in bad:
            with self.subTest(viewer=viewer, panel=panel, sid=sid):
                self.assertIsNone(core.type_gate(viewer, panel, sid, 26, "@emx_session"))

    def test_the_gate_names_the_viewer_the_session_the_cursor_and_the_panel(self):
        gate = core.type_gate("%3", "%1", "abc", 26, "@emx_session")
        for part in ("#{==:#{pane_id},%3}", "#{==:#{pane_in_mode},0}", "#{==:#{pane_dead},0}",
                     "#{==:#{@emx_session},abc}", "#{==:#{cursor_flag},1}", "#{==:#{cursor_x},2}",
                     "#{==:#{cursor_y},26}", "#{==:#{pane_id},%1}"):
            with self.subTest(part=part):
                self.assertIn(part, gate)
        self.assertEqual(gate.count("{"), gate.count("}"))
        self.assertNotIn("%", core.gate_report("@emx_session"))

    def test_what_tmux_printed_says_whether_it_typed_and_why_not(self):
        r = core.Rename("abc", "alpha", "beta")
        self.assertIsNone(core.gate_refusal(r, core.TYPED, 26))
        cases = {
            "1 0 1 2 26 abc": "scrolled back",
            "0 1 1 2 26 abc": "viewer changed",
            "0 0 1 2 26 other": "viewer changed",
            "0 0 1 2 26": "viewer changed",
            "0 0 0 2 26 abc": "prompt changed",
            "0 0 1 7 26 abc": "prompt changed",
            "0 0 1 2 25 abc": "prompt changed",
            "0 0 1 2 26 abc": "Focus left the panel",
            "": "viewer changed",
        }
        for report, want in cases.items():
            with self.subTest(report=report):
                self.assertIn(want, core.gate_refusal(r, report, 26))

    def test_the_rename_counts_once_the_listing_shows_any_new_name(self):
        r = core.Rename("a", "alpha", "beta")
        self.assertIsNone(core.renamed_to(r, named("a", "alpha")))
        self.assertIsNone(core.renamed_to(r, None))
        self.assertEqual(core.renamed_to(r, named("a", "beta")), "beta")
        self.assertEqual(core.renamed_to(r, named("a", "fam 👨 👩 👧")), "fam 👨 👩 👧")
        self.assertIsNone(core.renamed_to(core.Rename("a", "a", "beta"), named("a", "a")))
        self.assertEqual(core.renamed_text(r, "beta"), "Renamed alpha to beta.")
        self.assertIn("doesn't show a new name", core.renamed_text(r, None))

    def test_the_new_name_on_the_viewer_is_his_session_until_the_listing_catches_up(self):
        text, width = real_screen("named-viewer")
        self.assertEqual(core.main_view_state(text, "old-name", width), "mismatch")
        self.assertEqual(core.view_state_for(text, ["old-name", "emx-test-named"], width), "ok")
        self.assertEqual(core.view_state_for(text, ["old-name"], width), "mismatch")
        self.assertEqual(core.view_state_for(text, ["", "other"], width), "unknown")
        renaming = core.rename_started({}, core.Rename("a", "old-name", "emx-test-named"))
        self.assertEqual(core.viewer_names(named("a", "old-name"), renaming), ["old-name", "emx-test-named"])
        self.assertEqual(core.viewer_names(named("a", "a"), {}), [""])

    def test_a_rename_that_typed_nothing_is_dropped_at_once(self):
        r = core.Rename("a", "alpha", "beta")
        renaming = core.rename_started({"b": ("bee", "bea", 50.0)}, r)
        self.assertEqual(core.pending_renames(renaming, [named("a", "alpha")], "a", 10.0)["a"], ("alpha", "beta", None))
        self.assertEqual(core.rename_finished(renaming, r, False, 10.0), {"b": ("bee", "bea", 50.0)})

    def test_a_typed_rename_is_pending_while_the_listing_shows_the_old_name_until_its_time_is_up(self):
        r = core.Rename("a", "alpha", "beta")
        renaming = core.rename_finished(core.rename_started({}, r), r, True, 100.0)
        sessions = [named("a", "alpha")]
        self.assertIn("a", core.pending_renames(renaming, sessions, None, 100.0 + core.RENAME_PENDING_SECONDS - 1))
        self.assertEqual(core.pending_renames(renaming, sessions, None, 100.0 + core.RENAME_PENDING_SECONDS), {})
        self.assertEqual(core.pending_renames(renaming, [named("a", "beta")], None, 101.0), {})
        self.assertEqual(core.pending_renames(renaming, [], None, 101.0), {})


def interrupt_screen(viewer, x=2, y=26, shown=True, in_mode=False, flagged=False):
    captured, width = real_screen(viewer)
    return core.RenameScreen(core.strip_ansi(captured), "", "", x, y, shown, width, in_mode, flagged)


class InterruptChecks(unittest.TestCase):
    def setUp(self):
        self.interrupt = core.Interrupt("w", "emx-test-working")
        self.working = named("w", "emx-test-working", state="working")

    def test_only_a_working_running_not_idle_row_is_interruptible(self):
        self.assertTrue(core.interruptible(mk(state="working", status="busy")))
        self.assertTrue(core.interruptible(mk(state="working")))
        for s in (mk(state="working", status="idle"), mk(state="blocked"), mk(state="done", status="idle"),
                  mk(state="working", running=False), mk(state="stopped", running=False)):
            with self.subTest(state=s.state, status=s.status, running=s.running):
                self.assertFalse(core.interruptible(s))

    def test_listing_refusals(self):
        self.assertIsNone(core.interrupt_listing_refusal(self.interrupt, self.working, True))
        cases = {
            "Couldn't list": (self.working, False),
            "isn't listed": (None, True),
            "isn't running": (named("w", "emx-test-working", state="stopped", running=False), True),
        }
        for want, (session, fresh) in cases.items():
            with self.subTest(want=want):
                refusal = core.interrupt_listing_refusal(self.interrupt, session, fresh)
                self.assertIn(want, refusal)
                self.assertIn("emx-test-working", refusal)
        for done in (core.Session("w", "emx-test-working", "", "working", "idle", "", None, True),
                     named("w", "emx-test-working", state="blocked")):
            with self.subTest(state=done.state):
                self.assertIs(core.interrupt_listing_refusal(self.interrupt, done, True), core.NOTHING_TO_INTERRUPT)
        self.assertIn("nothing to interrupt", core.nothing_to_interrupt_text(self.interrupt))

    def test_screen_checks_on_real_captures(self):
        perm = named("p", "emx-test-perm", state="working")
        others = [named("b", "bee")]
        ok = {"working-streaming": (self.working, self.interrupt), "working-spinner": (perm, core.Interrupt("p", "emx-test-perm"))}
        for screen, (session, interrupt) in ok.items():
            with self.subTest(screen=screen):
                self.assertIsNone(core.interrupt_screen_refusal(interrupt, session, [session] + others, interrupt_screen(screen)))
        sessions = [self.working] + others

        def refusal(screen):
            return core.interrupt_screen_refusal(self.interrupt, self.working, sessions, screen)

        self.assertIn("scrolled back", refusal(interrupt_screen("working-streaming", in_mode=True)))
        self.assertIn("isn't showing its prompt", refusal(interrupt_screen("working-streaming", flagged=True)))
        self.assertIn("isn't showing its prompt", refusal(interrupt_screen("agent-view")))
        self.assertIn("isn't showing its prompt", refusal(interrupt_screen("working-streaming", shown=False)))
        twin = core.interrupt_screen_refusal(
            self.interrupt, self.working, sessions + [named("d", "emx-test-working", state="working")],
            interrupt_screen("working-streaming"),
        )
        self.assertIn("looks the same as another session's", twin)

    def test_a_draft_does_not_refuse(self):
        focus = named("f", "emx-test-focus", state="working")
        line, below, x, shown, width = at_cursor("prompt-text")
        screen = interrupt_screen("prompt-text", x=x, y=PROMPT_CURSORS["cursors"]["prompt-text"]["y"], shown=shown)
        self.assertIsNone(core.interrupt_screen_refusal(core.Interrupt("f", "emx-test-focus"), focus, [focus], screen))
        self.assertFalse(core.empty_prompt(line, below, x, shown, width))

    def test_what_tmux_printed_names_the_interrupt_and_the_pinned_x(self):
        i = core.Interrupt("abc", "alpha")
        self.assertIsNone(core.gate_refusal(i, core.TYPED, 26, cursor_x=17))
        cases = {
            "1 0 1 17 26 abc": "alpha's viewer was scrolled back just before emx sent Esc, so it didn't. Press q in it, then try again.",
            "0 0 1 17 26 other": "alpha's viewer changed just before emx sent Esc, so emx didn't interrupt it.",
            "0 0 1 2 26 abc": "alpha's prompt changed just before emx sent Esc, so emx didn't interrupt it.",
            "0 0 1 17 26 abc": "Focus left the panel, so emx didn't interrupt alpha.",
        }
        for report, want in cases.items():
            with self.subTest(report=report):
                self.assertEqual(core.gate_refusal(i, report, 26, cursor_x=17), want)

    def test_the_result_says_whether_it_stopped(self):
        self.assertEqual(core.interrupted_text(self.interrupt, True), "Interrupted emx-test-working.")
        self.assertEqual(core.interrupted_text(self.interrupt, False),
                         "emx-test-working is still working; emx sent Esc but it didn't stop.")


class TypeGate(unittest.TestCase):
    def test_cursor_x_is_pinned_where_given(self):
        self.assertIn("#{==:#{cursor_x},17}", core.type_gate("%3", "%1", "abc", 26, "@emx_session", cursor_x=17))
        self.assertNotIn("#{==:#{cursor_x},2}", core.type_gate("%3", "%1", "abc", 26, "@emx_session", cursor_x=17))
        self.assertIn("#{==:#{cursor_x},2}", core.type_gate("%3", "%1", "abc", 26, "@emx_session"))


class ErrorLine(unittest.TestCase):
    def test_first_line_of_message(self):
        self.assertEqual(core.error_line(RuntimeError("first\nsecond")), "first")

    def test_empty_message_falls_back_to_type(self):
        self.assertEqual(core.error_line(KeyError()), "KeyError")


TRANSCRIPTS = Path(__file__).parent / "fixtures" / "transcripts"


def real_transcript(name):
    """A transcript from a throwaway Claude Code 2.1.294 session (each line records its `version`).

    Only allowed fields were copied, the temp folder became `<tmp>` and the
    session id `<uuid>`; attachments keep just their type. `working` is the
    first 48 lines of the session `working-finished` came from, as the file
    stood while its second `sleep` ran.
    """
    return (TRANSCRIPTS / f"{name}.jsonl").read_text(encoding="utf-8")


def ms(iso):
    return core._iso_ms(iso)


def entry(kind, content, at="2026-10-08T09:00:00Z", **extra):
    return json.dumps({"type": kind, "timestamp": at, "message": {"role": kind, "content": content}, **extra})


FIXTURE_KEYS = {"type", "subtype", "uuid", "parentUuid", "isSidechain", "isMeta", "timestamp", "version", "cwd",
                "sessionId", "durationMs", "attachment", "message"}
FIXTURE_MESSAGE_KEYS = {"role", "content", "stop_reason"}


class ParseRealTranscripts(unittest.TestCase):
    def test_the_fixtures_come_from_one_claude_code_version_and_hold_nothing_private(self):
        paths = sorted(TRANSCRIPTS.glob("*.jsonl"))
        self.assertGreater(len(paths), 0)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                entries = [json.loads(line) for line in text.splitlines()]
                self.assertEqual({e.get("version") for e in entries} - {None}, {"2.1.294"})
                for e in entries:
                    self.assertLessEqual(set(e), FIXTURE_KEYS)
                    self.assertLessEqual(set(e.get("message", {})), FIXTURE_MESSAGE_KEYS)
                    self.assertLessEqual(set(e.get("attachment", {})), {"type"})
                    if "cwd" in e:
                        self.assertTrue(e["cwd"].startswith("<tmp>"), e["cwd"])
                    if "sessionId" in e:
                        self.assertEqual(e["sessionId"], "<uuid>")
                for private in ("/Users/", "/private/tmp/", "/var/folders/", "/home/", "@", "claude.ai/code",
                                "organization", "signature"):
                    self.assertNotIn(private, text)

    def test_a_done_session_reply(self):
        t = core.parse_transcript(real_transcript("done"))
        self.assertEqual((t.latest, t.reply, t.answered), ("reply", "Alpha done.\nBeta line\nGamma line", True))
        self.assertEqual(t.reply_at, ms("2026-10-08T09:43:28.872Z"))
        self.assertFalse(t.pending)

    def test_a_permission_request_is_the_pending_bash_call_after_a_thinking_only_entry(self):
        t = core.parse_transcript(real_transcript("permission"))
        self.assertEqual((t.latest, t.pending, t.question), ("tool", True, False))
        self.assertEqual(t.tool, "Bash: echo emx-perm-check > out.txt")
        self.assertEqual(t.tool_about, "Write test string to out.txt")
        self.assertEqual(t.tool_lines, ("Write test string to out.txt", "$ echo emx-perm-check > out.txt"))
        self.assertEqual(t.tool_at, ms("2026-10-08T09:43:33.103Z"))

    def test_a_question_lists_its_options(self):
        t = core.parse_transcript(real_transcript("question"))
        self.assertEqual((t.question, t.pending, t.tool), (True, True, "Which colour do you prefer?"))
        self.assertEqual(t.tool_lines, ("Which colour do you prefer?", "  1. Red", "  2. Blue"))

    def test_a_working_session_is_on_its_running_tool_call(self):
        t = core.parse_transcript(real_transcript("working"))
        self.assertEqual((t.latest, t.pending, t.tool, t.tool_about), ("tool", True, "Bash: sleep 9", "Sleep for 9 seconds"))
        self.assertEqual(t.prompt_at, ms("2026-10-08T09:47:23.943Z"))

    def test_a_dismissed_permission_request_ends_the_turn_as_interrupted(self):
        t = core.parse_transcript(real_transcript("permission-dismissed"))
        self.assertEqual((t.latest, t.pending, t.tool), ("interrupted", False, "Bash: echo emx-perm-check > out.txt"))
        self.assertEqual(t.prompt, "Run this shell command with the Bash tool: echo emx-perm-check > out.txt")

    def test_finished_tool_calls_are_not_pending(self):
        t = core.parse_transcript(real_transcript("working-finished"))
        self.assertEqual((t.latest, t.pending, t.reply, t.answered), ("reply", False, "Slept three times.", True))

    def test_parallel_calls_waiting_for_approval_are_not_in_the_transcript_yet(self):
        # 2.1.294 writes a parallel batch only once all of it is settled, so the row falls back to waitingFor.
        t = core.parse_transcript(real_transcript("parallel-permission"))
        self.assertEqual((t.latest, t.pending, t.tool), ("prompt", False, ""))
        blocked = st("blocked", "permission prompt")
        self.assertEqual(core.row_detail(blocked, t), "permission prompt")
        self.assertEqual(core.peek_content(blocked, t, 0).lines, (("permission prompt", "dim"),))

    def test_a_dismissed_parallel_batch_leaves_nothing_pending(self):
        t = core.parse_transcript(real_transcript("parallel-permission-dismissed"))
        self.assertEqual((t.latest, t.pending), ("interrupted", False))


class ParseTranscriptNeverRaises(unittest.TestCase):
    def test_garbage_gives_none(self):
        for text in ["", "\n\n", "not json", "[1,2]", '"text"', "null", '{"type": "user"}', '{"type": "user", "message": 3}',
                     '{"type": "assistant", "message": {"content": 7}}', '{"type": "assistant", "message": {"content": [1, null]}}']:
            with self.subTest(text=text):
                self.assertIsNone(core.parse_transcript(text))

    def test_bad_lines_among_good_ones_are_skipped(self):
        text = "\n".join(["{broken", entry("user", "do it"), "[]", entry("assistant", [{"type": "text", "text": "Did it."}])])
        t = core.parse_transcript(text)
        self.assertEqual((t.prompt, t.reply, t.answered), ("do it", "Did it.", True))

    def test_odd_values_inside_entries_never_raise(self):
        text = "\n".join([
            entry("assistant", [{"type": "tool_use", "name": "Bash", "input": "not a dict"}], at=5),
            entry("assistant", [{"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [3, {"options": [None, 4]}]}}]),
            entry("assistant", [{"type": "text", "text": None}], at="yesterday"),
        ])
        t = core.parse_transcript(text)
        self.assertEqual((t.tool, t.pending), ("Bash", True))
        q = core.parse_transcript(text.splitlines()[1])
        self.assertEqual((q.tool, q.tool_lines), ("AskUserQuestion", ("  2. 4",)))

    def test_a_cut_first_line_is_dropped(self):
        whole = entry("assistant", [{"type": "text", "text": "Earlier."}])
        text = whole[len(whole) // 2:] + "\n" + entry("user", "next")
        self.assertEqual(core.parse_transcript(text, partial=True).latest, "prompt")
        self.assertIsNone(core.parse_transcript(whole[5:], partial=True))

    def test_sidechain_meta_and_tag_wrapped_entries_are_skipped(self):
        text = "\n".join([
            entry("user", "real prompt"),
            entry("user", "subagent prompt", isSidechain=True),
            entry("user", "meta", isMeta=True),
            entry("user", "<command-name>/clear</command-name>"),
            entry("user", [{"type": "text", "text": "[Request interrupted by user]"}]),
        ])
        self.assertEqual(core.parse_transcript(text).prompt, "real prompt")

    def test_only_known_wrapper_tags_are_skipped_not_every_prompt_starting_with_a_tag(self):
        for wrapped in ("<command-name>/clear</command-name>", "<local-command-stdout>ok</local-command-stdout>",
                        "<bash-input>ls</bash-input>", "<task-notification>done</task-notification>",
                        "<system-reminder>note</system-reminder>", "<ide_opened_file>The user opened a.py</ide_opened_file>"):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(core.parse_transcript("\n".join([entry("user", "real"), entry("user", wrapped)])).prompt, "real")
        self.assertEqual(core.parse_transcript(entry("user", "<div> renders blank, why?")).prompt, "<div> renders blank, why?")

    def test_an_ide_context_block_is_skipped_and_the_prompt_beside_it_kept(self):
        ide = {"type": "text", "text": "<ide_selection>The user selected lines 1-3 of a.py</ide_selection>"}
        self.assertEqual(core.parse_transcript(entry("user", [ide, {"type": "text", "text": "explain this"}])).prompt, "explain this")

    def test_a_compaction_summary_is_skipped(self):
        text = "\n".join([entry("user", "real prompt"), entry("user", "This session is being continued...", isCompactSummary=True)])
        self.assertEqual(core.parse_transcript(text).prompt, "real prompt")

    def test_the_first_line_of_the_reply_is_kept(self):
        t = core.parse_transcript(entry("assistant", [{"type": "text", "text": "\n## Done\nmore"}]))
        self.assertEqual(t.reply_line, "Done")

    def test_a_reply_before_the_latest_prompt_is_not_its_answer(self):
        text = "\n".join([entry("assistant", [{"type": "text", "text": "Old."}]), entry("user", "new question")])
        t = core.parse_transcript(text)
        self.assertEqual((t.reply, t.answered, t.latest), ("Old.", False, "prompt"))


def call(use_id, name, given):
    return {"type": "tool_use", "id": use_id, "name": name, "input": given}


def result(use_id):
    return entry("user", [{"type": "tool_result", "tool_use_id": use_id, "content": "ok"}])


class ParallelToolCalls(unittest.TestCase):
    def test_a_waiting_call_is_kept_when_a_later_call_of_its_batch_has_its_result(self):
        text = "\n".join([entry("user", "clean up"),
                          entry("assistant", [call("t1", "Bash", {"command": "rm x"}), call("t2", "Read", {"file_path": "/a"})]),
                          result("t2")])
        t = core.parse_transcript(text)
        self.assertEqual((t.tool, t.pending, t.latest), ("Bash: rm x", True, "tool"))
        self.assertEqual(core.session_summary(st("blocked", "permission prompt"), t), "approve Bash: rm x")

    def test_of_several_waiting_calls_the_earliest_is_the_one_asked_about(self):
        # 2.1.294 doesn't write a pending parallel batch at all, so this order is an assumption, not observed.
        text = "\n".join([entry("user", "go"),
                          entry("assistant", [call("t1", "Bash", {"command": "touch first.txt"})]),
                          entry("assistant", [call("t2", "Bash", {"command": "touch second.txt"})])])
        self.assertEqual(core.parse_transcript(text).tool, "Bash: touch first.txt")
        self.assertEqual(core.parse_transcript(text + "\n" + result("t1")).tool, "Bash: touch second.txt")

    def test_with_every_call_answered_the_latest_is_shown_and_nothing_is_pending(self):
        text = "\n".join([entry("assistant", [call("t1", "Bash", {"command": "a"}), call("t2", "Bash", {"command": "b"})]),
                          result("t1"), result("t2")])
        t = core.parse_transcript(text)
        self.assertEqual((t.tool, t.pending), ("Bash: b", False))

    def test_a_call_left_without_a_result_before_a_new_prompt_is_not_pending(self):
        text = "\n".join([entry("assistant", [call("t1", "Bash", {"command": "old"})]), entry("user", "next"),
                          entry("assistant", [call("t2", "Bash", {"command": "new"})]), result("t2")])
        t = core.parse_transcript(text)
        self.assertEqual((t.tool, t.pending), ("Bash: new", False))


class SessionIds(unittest.TestCase):
    def test_only_a_uuid_shape_passes(self):
        self.assertTrue(core.is_session_id("816e8d5e-c8a9-4b40-914c-b0d248df02d1".upper()))
        for bad in ("", "u1", "-" * 36, "../../etc/passwd", "816e8d5e-c8a9-4b40-914c-b0d248df02d", "816e8d5e-c8a9-4b40-914c-b0d248df02d1/",
                    "816e8d5e-c8a9-4b40-914c-b0d248df02d1\n", None):
            with self.subTest(bad=bad):
                self.assertFalse(core.is_session_id(bad))


class TailBytes(unittest.TestCase):
    def test_starts_small_and_doubles_while_nothing_is_found(self):
        size = 10 * core.TRANSCRIPT_FIRST_BYTES
        self.assertEqual(core.tail_bytes(None, 0, size), core.TRANSCRIPT_FIRST_BYTES)
        self.assertEqual(core.tail_bytes(None, core.TRANSCRIPT_FIRST_BYTES, size), 2 * core.TRANSCRIPT_FIRST_BYTES)
        self.assertIsNone(core.tail_bytes(core.Transcript(latest="reply"), core.TRANSCRIPT_FIRST_BYTES, size))

    def test_stops_at_the_whole_file_or_the_cap(self):
        self.assertEqual(core.tail_bytes(None, 0, 100), 100)
        self.assertIsNone(core.tail_bytes(None, 100, 100))
        huge = 100 * core.TRANSCRIPT_MAX_BYTES
        self.assertEqual(core.tail_bytes(None, core.TRANSCRIPT_MAX_BYTES - 1, huge), core.TRANSCRIPT_MAX_BYTES)
        self.assertIsNone(core.tail_bytes(None, core.TRANSCRIPT_MAX_BYTES, huge))
        self.assertIsNone(core.tail_bytes(None, 0, 0))

    def test_a_reply_behind_a_line_bigger_than_the_first_read_is_found(self):
        reply = entry("assistant", [{"type": "text", "text": "Found me."}])
        padding = json.dumps({"type": "attachment", "attachment": {"type": "prompt_snapshot", "x": "p" * core.TRANSCRIPT_FIRST_BYTES}})
        data = (reply + "\n" + padding + "\n").encode()
        found, read = None, 0
        while (want := core.tail_bytes(found, read, len(data))) is not None:
            found, read = core.parse_transcript(data[-want:].decode(), partial=want < len(data)), want
        self.assertEqual(found.reply, "Found me.")
        self.assertEqual(read, len(data))


class TranscriptDir(unittest.TestCase):
    def test_every_character_but_letters_and_digits_becomes_a_dash(self):
        self.assertEqual(core.transcript_dir("/private/tmp/emx10.f3TLDx/proj"), "-private-tmp-emx10-f3TLDx-proj")
        self.assertEqual(core.transcript_dir("/r/app/.claude/worktrees/feature+10-x"), "-r-app--claude-worktrees-feature-10-x")


def st(state, waiting=""):
    return mk(state=state, waiting_for=waiting)


class SessionSummary(unittest.TestCase):
    def test_each_state_on_real_transcripts(self):
        cases = [
            ("permission", st("blocked", "permission prompt"), "approve Bash: echo emx-perm-check > out.txt"),
            ("question", st("blocked", "input needed"), "Which colour do you prefer?"),
            ("working", st("working"), "Sleep for 9 seconds"),
            ("done", st("done"), "Alpha done."),
            ("working-finished", st("done"), "Slept three times."),
            # After Esc on its permission prompt, 2.1.294 still lists the session as working.
            ("permission-dismissed", st("working"), "interrupted"),
            ("permission-dismissed", st("blocked", "permission prompt"), ""),
        ]
        for name, session, want in cases:
            with self.subTest(name=name):
                self.assertEqual(core.session_summary(session, core.parse_transcript(real_transcript(name))), want)

    def test_the_listing_decides_the_state_and_a_mismatch_shows_nothing(self):
        done = core.parse_transcript(real_transcript("done"))
        self.assertEqual(core.session_summary(st("blocked", "permission prompt"), done), "")
        waiting = core.parse_transcript(real_transcript("permission"))
        self.assertEqual(core.session_summary(st("done"), waiting), "")
        self.assertEqual(core.session_summary(st("stopped"), done), "")
        self.assertEqual(core.session_summary(st("done"), None), "")

    def test_a_working_session_without_a_description_shows_the_call(self):
        t = core.parse_transcript(entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a/b/notes.md"}}]))
        self.assertEqual(core.session_summary(st("working"), t), "Read: notes.md")
        t = core.parse_transcript(entry("assistant", [{"type": "text", "text": "\n## Looking at it\nmore"}]))
        self.assertEqual(core.session_summary(st("working"), t), "Looking at it")


class RowDetail(unittest.TestCase):
    def test_the_summary_replaces_waiting_for_which_stands_in_only_when_there_is_none(self):
        perm = core.parse_transcript(real_transcript("permission"))
        self.assertEqual(core.row_detail(st("blocked", "permission prompt"), perm),
                         "approve Bash: echo emx-perm-check > out.txt")
        self.assertEqual(core.row_detail(st("blocked", "permission prompt"), None), "permission prompt")
        self.assertEqual(core.row_detail(st("done"), core.parse_transcript(real_transcript("done"))), "Alpha done.")
        self.assertEqual(core.row_detail(st("done", "idle"), core.parse_transcript(real_transcript("done"))), "Alpha done.")


class PeekContent(unittest.TestCase):
    NOW = ms("2026-10-08T09:45:33.103Z")

    def peek(self, name, session):
        return core.peek_content(session, core.parse_transcript(real_transcript(name)), self.NOW)

    def test_a_permission_request_with_how_long_it_has_waited(self):
        p = self.peek("permission", st("blocked", "permission prompt"))
        self.assertEqual(p.title, "Needs input · for 2m")
        self.assertEqual(p.lines, (("Write test string to out.txt", "body"), ("$ echo emx-perm-check > out.txt", "code"),
                                   ("permission prompt", "dim")))

    def test_a_question_and_its_options(self):
        p = self.peek("question", st("blocked", "input needed"))
        self.assertEqual([t for t, _ in p.lines], ["Which colour do you prefer?", "  1. Red", "  2. Blue", "input needed"])

    def test_a_done_reply_and_how_long_ago(self):
        p = self.peek("done", st("done"))
        self.assertEqual(p.title, "Done · 2m ago")
        self.assertEqual(p.lines, (("Alpha done.\nBeta line\nGamma line", "body"),))

    def test_working_shows_the_latest_activity_and_time_since_the_prompt(self):
        p = core.peek_content(st("working"), core.parse_transcript(real_transcript("working")), ms("2026-10-08T09:47:53.943Z"))
        self.assertEqual(p.title, "Working · for 30s")
        self.assertEqual([t for t, _ in p.lines], ["Sleep for 9 seconds", "$ sleep 9"])

    def test_an_interrupted_turn_says_so_and_shows_the_call_it_stopped(self):
        p = self.peek("permission-dismissed", st("working"))
        self.assertEqual(p.lines, (("Interrupted.", "body"), ("Write test string to out.txt", "dim"),
                                   ("$ echo emx-perm-check > out.txt", "dim")))

    def test_nothing_known(self):
        self.assertEqual(core.peek_content(st("working"), None, self.NOW), core.Peek("Working", (("Nothing to show yet.", "dim"),)))
        self.assertEqual(core.peek_content(st("blocked", "input needed"), None, self.NOW).lines, (("input needed", "dim"),))

    def test_a_stopped_session_shows_its_last_reply_with_no_time(self):
        p = self.peek("done", st("stopped"))
        self.assertEqual((p.title, p.lines[0][0][:11]), ("Stopped", "Alpha done."))


class PeekLines(unittest.TestCase):
    def test_wraps_to_the_width_and_cuts_to_the_rows(self):
        peek = core.Peek("t", (("one two three four five six", "body"), ("$ " + "x" * 25, "code")))
        self.assertEqual(core.peek_lines(peek, 10, 10), [
            ("one two", "body"), ("three four", "body"), ("five six", "body"),
            ("$ xxxxxxxx", "code"), ("x" * 10, "code"), ("x" * 7, "code"),
        ])
        self.assertEqual(core.peek_lines(peek, 10, 3), [("one two", "body"), ("three four", "body"), ("…", "dim")])
        self.assertEqual(core.peek_lines(peek, 0, 3), [])
        self.assertEqual(core.peek_lines(peek, 10, 0), [])

    def test_a_very_long_reply_is_cut_without_wrapping_all_of_it(self):
        peek = core.Peek("t", (("word " * 200000 + "\n" + "line\n" * 100000, "body"),))
        lines = core.peek_lines(peek, 30, 4)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[-1], ("…", "dim"))

    def test_every_line_fits_its_width(self):
        peek = core.Peek("t", (("界" * 30, "body"), ("$ " + "界" * 30, "code")))
        for width in range(2, 15):
            for text, _ in core.peek_lines(peek, width, 50):
                self.assertLessEqual(core.display_width(text), width)


class CutLines(unittest.TestCase):
    def test_keeps_what_fits_and_ends_a_cut_in_an_ellipsis(self):
        lines = [("a", "body"), ("b", "body"), ("c", "body")]
        self.assertEqual(core.cut_lines(lines, 3), lines)
        self.assertEqual(core.cut_lines(lines, 2), [("a", "body"), ("…", "dim")])
        self.assertEqual(core.cut_lines(lines, 0), [])


class PeekRoom(unittest.TestCase):
    def test_the_rows_between_the_top_lines_and_the_bottom_lines_or_none_when_too_narrow(self):
        self.assertEqual(core.peek_room(20, 40, 1), 17)
        self.assertEqual(core.peek_room(20, 6, 1), 17)
        self.assertEqual(core.peek_room(20, 5, 1), 0)
        self.assertEqual(core.peek_room(5, 40, 4), 0)
        self.assertEqual(core.peek_height(core.peek_room(10, 40, 1), 1), 4)
        self.assertEqual(core.peek_height(core.peek_room(10, 40, 2), 1), 0)


class PeekHeight(unittest.TestCase):
    def test_body_plus_title_and_borders_capped_and_leaving_list_rows(self):
        self.assertEqual(core.peek_height(40, 2), 5)
        self.assertEqual(core.peek_height(40, 50), 3 + core.PEEK_BODY_LINES)
        self.assertEqual(core.peek_height(40, 0), 4)
        self.assertEqual(core.peek_height(10, 50), 10 - core.PEEK_MIN_LIST_ROWS)
        self.assertEqual(core.peek_height(core.PEEK_MIN_LIST_ROWS + 4, 5), 4)
        self.assertEqual(core.peek_height(core.PEEK_MIN_LIST_ROWS + 3, 5), 0)

    def test_it_leaves_the_cursor_row_whole_when_it_can_else_only_its_top(self):
        self.assertEqual(core.peek_height(12, 50, 5), 12 - 5)
        self.assertEqual(core.peek_height(12, 50, 2), 12 - core.PEEK_MIN_LIST_ROWS)
        self.assertEqual(core.peek_height(10, 50, 7), 10 - core.PEEK_MIN_LIST_ROWS)


CHANGELOG = """# Changelog

Intro text.

## 1.2.0 — 2026-11-01

- Something new.
- Another thing,
  across two lines.

## v1.1.1

* A fix.

## 1.0.0

- The first release.
"""


class Versions(unittest.TestCase):
    def test_parse_version_takes_plain_semver_only(self):
        self.assertEqual(core.parse_version("1.0.0"), (1, 0, 0))
        self.assertEqual(core.parse_version("10.20.300"), (10, 20, 300))
        for bad in (None, "", "1.0", "1.0.0.0", "v1.0.0", "1.0.0-rc1", " 1.0.0", "1.0.0\n", "a.b.c", 1, ["1.0.0"]):
            with self.subTest(bad=bad):
                self.assertIsNone(core.parse_version(bad))

    def test_versions_compare_as_numbers_not_text(self):
        self.assertGreater(core.parse_version("1.10.0"), core.parse_version("1.9.9"))
        self.assertGreater(core.parse_version("2.0.0"), core.parse_version("1.99.99"))
        self.assertGreater(core.parse_version("1.0.10"), core.parse_version("1.0.9"))

    def test_whats_new_shows_only_for_a_newer_version(self):
        self.assertEqual(core.whats_new_due("1.0.0", "1.1.0"), core.SHOW_NEW)
        self.assertEqual(core.whats_new_due("1.9.0", "1.10.0"), core.SHOW_NEW)
        self.assertEqual(core.whats_new_due("1.1.0", "2.0.0"), core.SHOW_NEW)
        self.assertIsNone(core.whats_new_due("1.1.0", "1.1.0"))

    def test_a_first_run_or_an_unreadable_record_only_records(self):
        for recorded in (None, "", "garbage", 3):
            with self.subTest(recorded=recorded):
                self.assertEqual(core.whats_new_due(recorded, "1.0.0"), core.RECORD_ONLY)

    def test_an_older_checkout_neither_shows_nor_lowers_the_record(self):
        self.assertIsNone(core.whats_new_due("1.2.0", "1.1.0"))

    def test_code_with_a_broken_version_does_nothing(self):
        self.assertIsNone(core.whats_new_due("1.0.0", "dev"))

    def test_changelog_entry_is_that_version_only_without_markers(self):
        self.assertEqual(core.changelog_entry(CHANGELOG, "1.2.0"), ["Something new.", "Another thing,", "across two lines."])
        self.assertEqual(core.changelog_entry(CHANGELOG, "1.1.1"), ["A fix."])
        self.assertEqual(core.changelog_entry(CHANGELOG, "1.0.0"), ["The first release."])
        self.assertEqual(core.changelog_entry(CHANGELOG, "9.9.9"), [])
        self.assertEqual(core.changelog_entry("", "1.0.0"), [])

    def test_changelog_versions_newest_first(self):
        self.assertEqual(core.changelog_versions(CHANGELOG), ["1.2.0", "1.1.1", "1.0.0"])

    def test_whats_new_text(self):
        self.assertEqual(core.whats_new_text("1.2.0", ["a", "b"]), "New in emx 1.2.0:\n· a\n· b")

    def test_a_skipped_version_s_lines_show_too(self):
        text = core.whats_new_since(CHANGELOG, "1.0.0", "1.2.0")
        self.assertEqual(
            text,
            "New in emx 1.2.0:\n· Something new.\n· Another thing,\n· across two lines.\nNew in emx 1.1.1:\n· A fix.",
        )
        self.assertNotIn("first release", text)

    def test_only_versions_up_to_the_running_one_show(self):
        self.assertEqual(core.whats_new_since(CHANGELOG, "1.0.0", "1.1.1"), "New in emx 1.1.1:\n· A fix.")
        self.assertEqual(core.whats_new_since(CHANGELOG, "1.2.0", "1.2.0"), "")
        self.assertEqual(core.whats_new_since(CHANGELOG, None, "1.2.0"), "")

    def test_cut_with_pointer_counts_what_it_hides(self):
        lines = [str(n) for n in range(10)]
        self.assertEqual(core.cut_with_pointer(lines, 10, "X"), lines)
        self.assertEqual(core.cut_with_pointer(lines, 6, "X"), ["0", "1", "2", "3", "4", "… 5 more in X"])

    def test_the_shipped_changelog_leads_with_the_code_version(self):
        root = Path(__file__).resolve().parent.parent
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        versions = core.changelog_versions(text)
        self.assertEqual(versions[0], __version__)
        self.assertTrue(core.changelog_entry(text, __version__))
        parsed = [core.parse_version(v) for v in versions]
        self.assertEqual(parsed, sorted(parsed, reverse=True))
        self.assertEqual(len(set(parsed)), len(parsed))


if __name__ == "__main__":
    unittest.main()
