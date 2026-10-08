import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from emx import core

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

    def test_summary_line_uses_short_words_before_cutting(self):
        self.assertEqual(self.text(core.summary_line(self.SESSIONS, 60)), "1 waiting · 2 working · 1 done · 1 stopped")
        self.assertEqual(self.text(core.summary_line(self.SESSIONS, 38)), "1 wait · 2 work · 1 done · 1 stop")
        self.assertEqual(self.text(core.summary_line(self.SESSIONS, 28)), "1 wait · 2 work · 1 done · …")
        self.assertEqual(self.text(core.summary_line([], 28)), "no sessions")

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

    def test_complete_dir(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("alpha", "alps", "beta", ".hidden"):
                os.mkdir(os.path.join(d, n))
            Path(d, "alfile").write_text("")
            self.assertEqual(core.complete_dir("be", d), ("beta/", ["beta"]))
            self.assertEqual(core.complete_dir("al", d), ("alp", ["alpha", "alps"]))
            self.assertEqual(core.complete_dir("zz", d), ("zz", []))
            self.assertEqual(core.complete_dir("", d)[1], ["alpha", "alps", "beta"])
            self.assertEqual(core.complete_dir(".", d)[1], [".hidden"])
            self.assertEqual(core.complete_dir(d + "/be", "/nowhere"), (d + "/beta/", ["beta"]))
            self.assertEqual(core.complete_dir("nodir/x", d), ("nodir/x", []))
        self.assertEqual(core.complete_dir("~", "/b"), ("~/", []))


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
    """Ctrl+X twice on one row stops a running session or removes a stopped one; anything else in between cancels."""

    def setUp(self):
        self.running = mk(id="a", name="emx-test-a")
        self.stopped = mk(id="a", name="emx-test-a", state="stopped", running=False)
        self.on_a = ("session", "a")

    def test_which_command_for_which_state(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        self.assertEqual((armed.action, core.close_args("claude", armed)), (core.STOP, ["claude", "stop", "a"]))
        self.assertEqual(core.confirm_label(armed), "ctrl+x again to stop")
        armed, _ = core.press_close(None, self.on_a, self.stopped, 0.0)
        self.assertEqual((armed.action, core.close_args("claude", armed)), (core.REMOVE, ["claude", "rm", "a"]))
        self.assertEqual(core.confirm_label(armed), "ctrl+x again to remove")

    def test_a_done_session_whose_process_has_gone_is_removed(self):
        armed, _ = core.press_close(None, self.on_a, mk(id="a", state="done", running=False), 0.0)
        self.assertEqual(armed.action, core.REMOVE)

    def test_the_remove_never_discards_or_forces(self):
        armed, _ = core.press_close(None, self.on_a, self.stopped, 0.0)
        self.assertFalse(any(a.startswith("--") for a in core.close_args("claude", armed)))

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

    def test_a_group_header_or_no_row_does_nothing(self):
        armed, _ = core.press_close(None, self.on_a, self.running, 0.0)
        for cursor, row in ((("group", "/p"), None), (None, None), (self.on_a, None), (("session", "b"), self.running)):
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
        self.assertEqual(rearmed.action, core.REMOVE)

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
        self.assertEqual(core.close_blocked(False, busy), "Still stopping emx-test-a…")

    def test_a_close_runs_only_if_a_fresh_listing_still_shows_the_row_as_he_saw_it(self):
        stop, _ = core.press_close(None, self.on_a, self.running, 0.0)
        remove, _ = core.press_close(None, self.on_a, self.stopped, 0.0)
        self.assertTrue(core.close_still_applies(stop, self.running, True))
        self.assertTrue(core.close_still_applies(remove, self.stopped, True))
        self.assertFalse(core.close_still_applies(remove, self.running, True))
        self.assertIn("running again", core.close_refusal(remove, self.running, True))
        self.assertFalse(core.close_still_applies(stop, self.stopped, True))
        self.assertIn("no longer running", core.close_refusal(stop, self.stopped, True))
        self.assertFalse(core.close_still_applies(stop, None, True))
        self.assertIn("isn't listed", core.close_refusal(stop, None, True))
        self.assertFalse(core.close_still_applies(stop, self.running, False))
        self.assertIn("Couldn't list", core.close_refusal(stop, None, False))

    def test_notices(self):
        stop, _ = core.press_close(None, self.on_a, self.running, 0.0)
        remove, _ = core.press_close(None, self.on_a, self.stopped, 0.0)
        self.assertEqual((core.closing_text(stop), core.closed_text(stop)), ("Stopping emx-test-a…", "Stopped emx-test-a."))
        self.assertEqual((core.closing_text(remove), core.closed_text(remove)), ("Removing emx-test-a…", "Removed emx-test-a."))

    def test_one_line_keeps_every_line_of_a_refusal(self):
        self.assertEqual(core.one_line("\x1b[31mrefused:\x1b[0m\n\n  2 unpushed commits\n"), "refused: 2 unpushed commits")
        self.assertEqual(core.one_line(""), "")


class ErrorLine(unittest.TestCase):
    def test_first_line_of_message(self):
        self.assertEqual(core.error_line(RuntimeError("first\nsecond")), "first")

    def test_empty_message_falls_back_to_type(self):
        self.assertEqual(core.error_line(KeyError()), "KeyError")


if __name__ == "__main__":
    unittest.main()
