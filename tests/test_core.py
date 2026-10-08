import json
import os
import tempfile
import unittest
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


class StateLabel(unittest.TestCase):
    def mk(self, **kw):
        base = dict(id="i", name="n", cwd="", state="", status="", waiting_for="")
        base.update(kw)
        return core.Session(**base)

    def test_known_states(self):
        self.assertEqual(core.state_label(self.mk(state="working")), "working")
        self.assertEqual(core.state_label(self.mk(state="blocked")), "waiting for you")
        self.assertEqual(core.state_label(self.mk(state="done")), "done")

    def test_fallbacks(self):
        self.assertEqual(core.state_label(self.mk(state="weird")), "weird")
        self.assertEqual(core.state_label(self.mk(status="st")), "st")
        self.assertEqual(core.state_label(self.mk()), "unknown")

    def test_waiting_for_appended(self):
        self.assertEqual(core.state_label(self.mk(state="blocked", waiting_for="x")), "waiting for you: x")


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

    def test_wrap_degenerate(self):
        self.assertEqual(core.wrap("abc", 0), [])
        self.assertEqual(core.wrap("abc", -1), [])
        self.assertEqual(core.wrap("", 5), [""])

    def test_strip_ansi_and_first_message(self):
        self.assertEqual(core.strip_ansi("\x1b[1;31mred\x1b[0m"), "red")
        self.assertEqual(core.first_message("\n  \x1b[31mboom\x1b[0m\nsecond"), "boom")
        self.assertEqual(core.first_message(""), "")
        self.assertEqual(core.first_message("\n \n"), "")

    def test_folder_tail(self):
        self.assertEqual(core.folder_tail("", "/h"), "")
        self.assertEqual(core.folder_tail("/h", "/h"), "~")
        self.assertEqual(core.folder_tail("/h/", "/h"), "~")
        self.assertEqual(core.folder_tail("/a/b/c/", "/h"), "c")
        self.assertEqual(core.folder_tail("/", "/h"), "/")


class Layout(unittest.TestCase):
    def test_main_capped_at_reading_width(self):
        lay = core.compute_layout(300, 40, 100)
        self.assertEqual((lay.panel, lay.main), (40, 100))
        self.assertEqual(lay.panel + lay.main + lay.spacer + 2, 300)

    def test_spacer_takes_remainder(self):
        lay = core.compute_layout(200, 40, 90)
        self.assertEqual(lay.spacer, 200 - 2 - 40 - 90)

    def test_widths_always_positive_and_fit(self):
        for ww in range(0, 400):
            for pw in (1, 16, 40, 500):
                for mw in (1, 20, 100, 500):
                    lay = core.compute_layout(ww, pw, mw)
                    with self.subTest(ww=ww, pw=pw, mw=mw):
                        self.assertGreaterEqual(lay.panel, 1)
                        self.assertGreaterEqual(lay.main, 1)
                        self.assertGreaterEqual(lay.spacer, 1)
                        if ww >= 5:
                            self.assertLessEqual(lay.panel + lay.main + lay.spacer + 2, ww)

    def test_narrow_terminal_keeps_min_main(self):
        lay = core.compute_layout(60, 40, 100)
        self.assertGreaterEqual(lay.main, core.MIN_MAIN)
        self.assertLessEqual(lay.panel + lay.main + lay.spacer + 2, 60)

    def test_width_action(self):
        self.assertEqual(core.width_action(None, (200, 40, 100)), "apply")
        self.assertEqual(core.width_action((200, 40, 100), (180, 40, 100)), "apply")
        self.assertEqual(core.width_action((200, 40, 100), (200, 45, 100)), "adopt-panel")
        self.assertEqual(core.width_action((200, 40, 100), (200, 40, 90)), "adopt-main")
        self.assertIsNone(core.width_action((200, 40, 100), (200, 40, 100)))


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
        prefs = {"side": "left", "main_width": 80, "panel_width": 30, "last_folder": "/x"}
        core.save_prefs(self.path, prefs)
        self.assertEqual(core.load_prefs(self.path), prefs)
        self.assertFalse(self.path.with_name("prefs.json.tmp").exists())

    def test_normalize_rejects_bad_values(self):
        got = core.normalize_prefs({"side": "up", "main_width": "9", "panel_width": True, "last_folder": ""})
        self.assertEqual(got, core.DEFAULT_PREFS)

    def test_normalize_clamps(self):
        got = core.normalize_prefs({"main_width": -5, "panel_width": 10**9})
        self.assertEqual(got["main_width"], core.MIN_MAIN)
        self.assertEqual(got["panel_width"], core.MAX_WIDTH)

    def test_normalize_non_dict(self):
        for raw in (None, [], "s", 3):
            self.assertEqual(core.normalize_prefs(raw), core.DEFAULT_PREFS)

    def test_normalize_does_not_mutate_defaults(self):
        core.normalize_prefs({"side": "left", "last_folder": "/q"})
        self.assertEqual(core.DEFAULT_PREFS, {"side": "right", "main_width": 100, "panel_width": 40})

    def test_prefs_path_xdg(self):
        self.assertEqual(core.prefs_path({"XDG_CONFIG_HOME": "/cfg"}), Path("/cfg/emx/prefs.json"))

    def test_prefs_path_ignores_empty_or_relative_xdg(self):
        home = os.path.expanduser("~")
        want = Path(home) / ".config" / "emx" / "prefs.json"
        self.assertEqual(core.prefs_path({}), want)
        self.assertEqual(core.prefs_path({"XDG_CONFIG_HOME": ""}), want)
        self.assertEqual(core.prefs_path({"XDG_CONFIG_HOME": "rel/dir"}), want)

    def test_adjust_width_clamped(self):
        p = dict(core.DEFAULT_PREFS)
        self.assertEqual(core.adjust_width(p, "main_width", -1000)["main_width"], core.MIN_MAIN)
        self.assertEqual(core.adjust_width(p, "panel_width", -1000)["panel_width"], core.MIN_PANEL)
        self.assertEqual(core.adjust_width(p, "panel_width", 10**6)["panel_width"], core.MAX_WIDTH)
        self.assertEqual(core.adjust_width(p, "main_width", 5)["main_width"], 105)
        self.assertEqual(p, core.DEFAULT_PREFS)

    def test_flip_side(self):
        p = dict(core.DEFAULT_PREFS)
        self.assertEqual(core.flip_side(p)["side"], "left")
        self.assertEqual(core.flip_side(core.flip_side(p))["side"], "right")
        self.assertEqual(p["side"], "right")


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


if __name__ == "__main__":
    unittest.main()
