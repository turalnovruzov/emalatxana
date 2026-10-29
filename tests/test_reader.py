import curses
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from emx import core, reader
from emx.core import FINISHED, GOING, ERRORED, LIMIT

FIXTURE = Path(__file__).parent / "fixtures" / "reader" / "run.jsonl"
STAMP = 1_800_042_180.0


def pin_zone(case):
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()

    def restore():
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    case.addCleanup(restore)


def entry(kind, at, **more):
    return json.dumps({"type": kind, "timestamp": f"2027-01-15T19:{at // 60:02d}:{at % 60:02d}.000Z", "isSidechain": False, **more})


def many_replies(n):
    return "\n".join(
        entry("assistant", i, message={"role": "assistant", "content": [{"type": "text", "text": f"reply number {i}"}]})
        for i in range(n))


class Steps(unittest.TestCase):
    def setUp(self):
        pin_zone(self)
        self.text = FIXTURE.read_text()

    def test_steps_from_a_fixture(self):
        steps = core.reader_steps(self.text)
        self.assertEqual([s.kind for s in steps], ["prompt", "tool", "tool", "error", "reply"])
        self.assertEqual(steps[0].text, "Prompt: Build TICKET-12.\nRun the tests when done.")
        self.assertEqual((steps[1].text, steps[1].mark), ("▸ Bash git status --short", "✓"))
        self.assertEqual((steps[2].text, steps[2].mark, steps[2].tone), ("▸ Bash make test", "✗ exit 1", "error"))
        self.assertEqual((steps[3].text, steps[3].tone), ("! 401 Unauthorized: token expired", "error"))
        self.assertEqual(steps[4].text, "Claude: The tests failed with an authorization error, so I stopped.")

    def test_sidechain_and_meta_skipped(self):
        joined = " ".join(s.text for s in core.reader_steps(self.text))
        self.assertNotIn("subagent chatter", joined)
        self.assertNotIn("meta text", joined)

    def test_a_failed_tool_without_an_exit_code_is_a_plain_cross(self):
        text = "\n".join([
            entry("assistant", 1, message={"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/x/y.txt"}}]}),
            entry("user", 2, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": [{"type": "text", "text": "File not found"}]}]}),
        ])
        steps = core.reader_steps(text)
        self.assertEqual([(s.kind, s.text, s.mark) for s in steps], [("tool", "▸ Read y.txt", "✗"), ("error", "! File not found", "")])

    def test_a_prompt_is_cut_after_six_lines(self):
        prompt = "\n".join(f"line {i}" for i in range(1, 10))
        steps = core.reader_steps(entry("user", 1, message={"role": "user", "content": prompt}))
        self.assertEqual(steps[0].text.splitlines()[-2:], ["line 6", "…"])
        self.assertEqual(len(steps[0].text.splitlines()), 7)

    def test_an_api_error_is_an_error_step(self):
        text = entry("assistant", 1, isApiErrorMessage=True, error="rate_limit",
                     message={"role": "assistant", "content": [{"type": "text", "text": "You've hit your limit"}]})
        self.assertEqual([(s.kind, s.text) for s in core.reader_steps(text)], [("error", "! You've hit your limit")])

    def test_garbage_lines_are_skipped(self):
        self.assertEqual(core.reader_steps('not json\n[1]\n{"type": "user"}\n'), [])

    def test_earlier_count_from_cut_marker(self):
        text = json.dumps({"emx_probe_cut": 1234}) + "\n" + many_replies(3)
        steps = core.reader_steps(text)
        self.assertEqual([s.kind for s in steps], ["cut", "reply", "reply", "reply"])
        rows = core.reader_rows(steps, ("r", "read-only"), None, 60)
        self.assertEqual(rows[2][3], 0)
        self.assertIn("earlier steps weren't loaded", rows[2][0])

    def test_title_prefers_the_name_over_the_generated_title(self):
        self.assertEqual(core.reader_title(self.text), "Build TICKET-12")
        named = self.text + json.dumps({"type": "custom-title", "customTitle": "  build "}) + "\n"
        self.assertEqual(core.reader_title(named), "build")


class Endings(unittest.TestCase):
    def setUp(self):
        pin_zone(self)

    def test_reader_ending_for_each_kind(self):
        final = entry("assistant", 60, message={"role": "assistant", "content": [{"type": "text", "text": "Done."}]})
        self.assertEqual(core.reader_ending(final, STAMP)[0], FINISHED)
        failed = entry("assistant", 60, isApiErrorMessage=True, error="unknown", message={"role": "assistant", "content": [{"type": "text", "text": "x"}]})
        self.assertEqual(core.reader_ending(failed, STAMP)[0], ERRORED)
        limited = entry("assistant", 60, isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429,
                        apiErrorParams={"rate_limit_info": {"resetsAt": 1_800_072_000}},
                        message={"role": "assistant", "content": [{"type": "text", "text": "You've hit your limit"}]})
        self.assertEqual(core.reader_ending(limited, STAMP)[::2], (LIMIT, "till 04:00"))
        midway = entry("assistant", 60, message={"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}]})
        at = core._iso_ms("2027-01-15T19:01:00.000Z") / 1000
        self.assertEqual(core.reader_ending(midway, at + 30)[0], GOING)
        self.assertEqual(core.reader_ending(midway, at + 3600)[::2], (ERRORED, "ended mid-step"))
        self.assertIsNone(core.reader_ending("", STAMP))

    def test_end_line_for_each_ending(self):
        steps = core.reader_steps(many_replies(1))
        for ending, want in (
            ((FINISHED, STAMP, ""), "■ stopped here · 19:43 · the run ended: finished"),
            ((ERRORED, STAMP, ""), "■ stopped here · 19:43 · the run ended: failed"),
            ((LIMIT, STAMP, "till 21:40"), "■ stopped here · 19:43 · the run ended: usage limit till 21:40"),
            ((GOING, STAMP, ""), "… still going · 19:43"),
        ):
            with self.subTest(ending=ending[0]):
                text, tone, _, step = core.reader_rows(steps, ("r", "read-only"), ending, 80)[-1]
                self.assertEqual((text, step), (want, -1))
                self.assertEqual(tone, "plain" if ending[0] == GOING else "needs")

    def test_no_end_line_without_an_ending(self):
        rows = core.reader_rows(core.reader_steps(many_replies(1)), ("r", "read-only"), None, 80)
        self.assertEqual(len(rows), 3)


class Rows(unittest.TestCase):
    def setUp(self):
        pin_zone(self)
        self.steps = core.reader_steps(FIXTURE.read_text())

    def test_the_header_is_bold_label_and_muted_note_over_a_rule(self):
        rows = core.reader_rows(self.steps, ("worker · 19:42 run · TICKET-12", "read-only · session 2 of 2 (build)"), None, 120)
        self.assertEqual(rows[0][0], "worker · 19:42 run · TICKET-12  read-only · session 2 of 2 (build)")
        self.assertEqual(rows[0][2], ((0, 30, "bold"), (30, 66, "muted")))
        self.assertEqual(rows[1], ("─" * 120, "muted", (), -1))

    def test_hanging_indent_and_width(self):
        long_reply = entry("assistant", 5, message={"role": "assistant", "content": [{"type": "text", "text": "word " * 60}]})
        steps = self.steps + core.reader_steps(long_reply)
        for width in (40, 120):
            with self.subTest(width=width):
                rows = core.reader_rows(steps, ("worker · 19:42 run", "read-only · session 1 of 1"), (FINISHED, STAMP, ""), width)
                for text, *_ in rows:
                    self.assertLessEqual(core.display_width(text), width, text)
                reply = [r for r in rows if r[3] == len(steps) - 1]
                self.assertGreater(len(reply), 1)
                self.assertTrue(reply[0][0].startswith("19:00  Claude: word"))
                for text, *_ in reply[1:]:
                    self.assertTrue(text.startswith(" " * 9 + "word"), text)

    def test_tool_rows_colour_the_name_the_arguments_and_the_mark(self):
        rows = core.reader_rows(self.steps, ("r", "n"), None, 80)
        text, tone, spans, step = rows[5]
        self.assertEqual(text, "19:42  ▸ Bash make test  ✗ exit 1")
        self.assertEqual(spans[:3], ((0, 5, "muted"), (7, 13, "accent"), (13, len(text), "muted")))
        self.assertEqual(spans[-1], (len(text) - len("✗ exit 1"), len(text), "error"))

    def test_error_rows_sit_under_their_tool_in_the_error_tone(self):
        rows = core.reader_rows(self.steps, ("r", "n"), None, 80)
        self.assertEqual(rows[6][:2], (" " * 9 + "! 401 Unauthorized: token expired", "error"))

    def test_control_characters_never_reach_a_row(self):
        step = core.Step(0, "reply", "Claude: a\tb\x1b[31mred")
        text = core.reader_rows([step], ("r", "n"), None, 80)[2][0]
        self.assertTrue(text.isprintable(), text)


class Scr:
    def __init__(self, height=8, width=60, keys=()):
        self.size, self.keys, self.cells = (height, width), list(keys), {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.cells = {}

    def addstr(self, y, x, text, attr=0):
        self.cells[(y, x)] = text

    def refresh(self):
        pass

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        return self.keys.pop(0)

    def lines(self):
        grid = {}
        for (y, x), text in self.cells.items():
            row = grid.setdefault(y, {})
            row.update({x + k: ch for k, ch in enumerate(text)})
        return ["".join(grid[y].get(x, " ") for x in range(max(grid[y]) + 1)).rstrip() for y in sorted(grid)]


class Palette:
    def attr(self, tone, bar=None):
        return 0


class Place:
    def __init__(self, host=None):
        self.host, self.login, self.name = host, None, "p"


class Fetcher:
    def __init__(self, texts):
        self.texts, self.fetched, self.statted, self.stamp = texts, [], [], (10, 1.0)

    def fetch(self, sid):
        self.fetched.append(sid)
        return self.texts[sid], None

    def stat(self, sid):
        self.statted.append(sid)
        return self.stamp, None


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ReaderKeys(unittest.TestCase):
    A, B = "a" * 8 + "-1111-2222-3333-" + "4" * 12, "b" * 8 + "-1111-2222-3333-" + "4" * 12

    def setUp(self):
        pin_zone(self)
        self.final = entry("assistant", 59, message={"role": "assistant", "content": [{"type": "text", "text": "Done."}]})
        self.fetcher = Fetcher({self.A: many_replies(30) + "\n" + self.final, self.B: many_replies(2) + "\n" + self.final})
        self.clock = Clock()
        self.shown = []

    def make(self, sids=None, host=None, keys=(), texts=None, now=STAMP):
        if texts:
            self.fetcher.texts.update(texts)
        scr = Scr(keys=keys)
        view = reader.Reader(scr, Place(host), "worker · 19:42 run", sids or [self.A, self.B], self.fetcher.fetch, self.fetcher.stat,
                             Palette(), self.shown.append, self.clock, lambda: now)
        view.load()
        view.draw()
        return view, scr

    def test_opens_at_the_end(self):
        view, scr = self.make()
        self.assertEqual(self.fetcher.fetched, [self.B])
        self.assertEqual(view.at, 1)
        self.assertEqual(self.shown, [self.B])
        text = scr.lines()
        self.assertIn("■ stopped here", text[-1])
        self.assertTrue(text[0].startswith("worker · 19:42 run  read-only · session 2 of 2"))
        view, scr = self.make(sids=[self.A])
        self.assertIn("■ stopped here", scr.lines()[-1])
        self.assertTrue(view.pinned)

    def test_scroll_keys_and_the_earlier_count(self):
        view, scr = self.make(sids=[self.A])
        self.assertEqual(scr.lines()[2], "  ↑ 27 earlier steps")
        view.key("g")
        view.draw()
        self.assertEqual(view.top, 0)
        self.assertNotIn("earlier", " ".join(scr.lines()))
        view.key("j")
        view.draw()
        self.assertEqual(scr.lines()[2], "  ↑ 1 earlier step")
        view.key(curses.KEY_NPAGE)
        view.draw()
        self.assertEqual(view.top, 1 + 4)
        view.key(curses.KEY_PPAGE)
        view.key("k")
        view.key(curses.KEY_UP)
        self.assertEqual(view.top, 0)
        view.key("G")
        view.draw()
        self.assertTrue(view.pinned)
        self.assertIn("■ stopped here", scr.lines()[-1])
        view.key(curses.KEY_DOWN)
        self.assertTrue(view.pinned)

    def test_tab_switches_session_and_header_says_2_of_2(self):
        view, scr = self.make()
        self.assertIn("session 2 of 2", scr.lines()[0])
        self.fetcher.fetched.clear()
        view.key("\t")
        view.draw()
        self.assertEqual(self.fetcher.fetched, [self.A])
        self.assertIn("session 1 of 2", scr.lines()[0])
        self.assertEqual(self.shown[-1], self.A)
        view.key("\t")
        self.assertEqual(view.at, 1)

    def test_other_keys_change_nothing(self):
        view, scr = self.make()
        before = (view.top, view.at, view.pinned, list(self.fetcher.fetched), scr.lines())
        boom = AssertionError("a key started a process")
        with mock.patch("subprocess.run", side_effect=boom), mock.patch("subprocess.Popen", side_effect=boom):
            for key in ("x", "\x18", "\n", "\r", curses.KEY_ENTER, curses.KEY_RIGHT, curses.KEY_LEFT, " ", "q", "n", "R", "y"):
                self.assertTrue(view.key(key))
        view.draw()
        self.assertEqual((view.top, view.at, view.pinned, list(self.fetcher.fetched), scr.lines()), before)

    def test_esc_exits_zero(self):
        view, scr = self.make(keys=["x", "\x1b"])
        self.assertEqual(view.loop(), 0)
        self.assertEqual(scr.keys, [])

    def test_ssh_place_reads_once(self):
        midway = many_replies(2) + "\n" + entry("assistant", 60, message={"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}]})
        view, _ = self.make(host="srv2", texts={self.B: midway}, now=core._iso_ms("2027-01-15T19:01:10.000Z") / 1000)
        self.assertEqual(view.doc.ending[0], GOING)
        self.clock.t += 10
        view.tick()
        self.assertEqual((self.fetcher.fetched, self.fetcher.statted), ([self.B], []))

    def test_local_live_run_rereads(self):
        midway = many_replies(2) + "\n" + entry("assistant", 60, message={"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "ls"}}]})
        view, scr = self.make(texts={self.B: midway}, now=core._iso_ms("2027-01-15T19:01:10.000Z") / 1000)
        self.assertEqual(view.doc.ending[0], GOING)
        self.assertIn("… still going", scr.lines()[-1])
        self.fetcher.fetched.clear()
        view.tick()
        self.assertEqual(self.fetcher.fetched, [])
        self.clock.t += reader.READER_SECONDS
        view.tick()
        self.assertEqual(self.fetcher.fetched, [], "the file did not change")
        self.fetcher.stamp = (11, 2.0)
        self.fetcher.texts[self.B] = midway + "\n" + self.final
        self.clock.t += reader.READER_SECONDS
        view.tick()
        self.assertEqual(self.fetcher.fetched, [self.B])
        view.draw()
        self.assertIn("■ stopped here", scr.lines()[-1])
        self.fetcher.statted.clear()
        self.clock.t += reader.READER_SECONDS * 10
        view.tick()
        self.assertEqual(self.fetcher.statted, [], "a finished run is not watched")

    def test_a_fetch_error_shows_one_line_and_esc_still_exits(self):
        self.fetcher.fetch = lambda sid: (None, "Can't reach srv2: Connection refused")
        view = reader.Reader(Scr(keys=["\x1b"]), Place("srv2"), "worker", [self.A], self.fetcher.fetch, self.fetcher.stat, Palette(),
                             None, self.clock, lambda: STAMP)
        view.load()
        view.draw()
        self.assertEqual(view.scr.lines()[-1], "Can't reach srv2: Connection refused")
        self.assertEqual(view.loop(), 0)

    def test_the_timeout_is_a_minute_and_the_runner_uses_it(self):
        self.assertEqual(reader.READER_TIMEOUT, 60)
        with mock.patch.object(reader.places, "run", return_value=("x", None)) as ran:
            fetch, stat = reader.runner(Place(), {})
            fetch(self.A)
        self.assertEqual(ran.call_args.kwargs["timeout"], 60)
        self.assertEqual(ran.call_args.args[1][-2:], ["read", self.A])

    def test_stat_reads_size_and_mtime(self):
        with mock.patch.object(reader.places, "run", return_value=('{"size": 5, "mtime": 2.5}\n', None)):
            self.assertEqual(reader.runner(Place(), {})[1](self.A), ((5, 2.5), None))
        with mock.patch.object(reader.places, "run", return_value=(None, "no such session")):
            self.assertEqual(reader.runner(Place(), {})[1](self.A), (None, "no such session"))

    def test_a_place_gone_from_the_config_says_so(self):
        fetch, stat = reader.runner(None, {})
        self.assertEqual(fetch(self.A), (None, reader.GONE))


if __name__ == "__main__":
    unittest.main()
