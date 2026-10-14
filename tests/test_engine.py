import builtins
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emx import cli, core, engine
from emx.tmux import AGENT_OPT, SESSION_OPT, Pane, TmuxError

INTERRUPT = Path(__file__).parent / "fixtures" / "transcripts-interactive" / "interactive-interrupt.jsonl"
KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CLAUDE_SID = "11111111-1111-4111-8111-111111111111"


class FakeTmux:
    def __init__(self, panes=None, fail_window=False):
        self.windows, self.options, self.killed = [], [], []
        self.listed = panes or []
        self.fail_window = fail_window

    def new_hidden_window(self, command, cwd=None):
        if self.fail_window:
            raise TmuxError("no such session")
        self.windows.append((command, cwd))
        return "%7"

    def set_pane_option(self, pane, name, value):
        self.options.append((pane, name, value))

    def kill_pane(self, pane):
        self.killed.append(pane)

    def panes(self):
        return self.listed


class StoreCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.state = os.path.join(self.dir, "state", "emx")
        self.folder = os.path.join(self.dir, "work")
        os.mkdir(self.folder)
        self.home = os.path.join(self.dir, "claude-home")
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.home, "EMX_SHOW_TEST_SESSIONS": ""})
        env.start()
        self.addCleanup(env.stop)

    def key_dir(self, key=KEY):
        return os.path.join(self.state, "sessions", key)

    def write_record(self, key=KEY, **over):
        os.makedirs(self.key_dir(key), exist_ok=True)
        record = {"key": key, "agent": "claude", "name": "alpha", "cwd": self.folder, "session_id": key, "started_at": 1.0,
                  "run_at": 0}
        record.update(over)
        with open(os.path.join(self.key_dir(key), "session.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def write_events(self, *events, key=KEY):
        with open(os.path.join(self.key_dir(key), "events.jsonl"), "a", encoding="utf-8") as fh:
            for rec in events:
                fh.write(json.dumps(rec) + "\n")

    def live(self, key=KEY):
        return Pane("%1", "@1", False, "", key, 80, 80, agent="claude")

    def only_session(self):
        sessions, error = engine.list_sessions(FakeTmux([self.live()]), self.state)
        self.assertIsNone(error)
        (session,) = sessions
        return session


class EngineStore(StoreCase):
    def start(self, tmux=None, folder=None, message="hello"):
        tmux = tmux or FakeTmux()
        return tmux, engine.start(tmux, "claude", self.folder if folder is None else folder, message, state=self.state)

    def test_start_writes_the_record_and_a_private_settings_file(self):
        tmux, (session, error) = self.start()
        self.assertIsNone(error)
        where = self.key_dir(session.id)
        with open(os.path.join(where, "session.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(
            (record["key"], record["agent"], record["cwd"], record["session_id"]),
            (session.id, "claude", self.folder, session.id),
        )
        self.assertIsInstance(record["started_at"], int)
        self.assertEqual(record["run_at"], record["started_at"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(where, "settings.json")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(where).st_mode), 0o700)
        self.assertEqual(os.listdir(where).count("settings.json"), 1)
        self.assertFalse([n for n in os.listdir(where) if n.endswith(".tmp")])

    def test_every_hook_command_names_this_sessions_folder(self):
        _, (session, _) = self.start()
        with open(os.path.join(self.key_dir(session.id), "settings.json"), encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]
        self.assertEqual(set(hooks), set(core.HOOK_EVENTS))
        for event, groups in hooks.items():
            command = groups[0]["hooks"][0]["command"]
            self.assertTrue(command.endswith(" >/dev/null 2>&1; exit 0"), event)
            words = shlex.split(command[: -len(" >/dev/null 2>&1; exit 0")])
            self.assertEqual(words[1], "-I", event)
            self.assertEqual(words[2:], [cli.BIN, "hook", self.state, session.id], event)

    def test_a_hook_command_exits_0_silently_when_the_script_is_gone(self):
        with mock.patch.object(engine, "BIN", os.path.join(self.dir, "missing", "emx")):
            command = engine.hook_command(self.state, KEY)
        done = subprocess.run(["sh", "-c", command], input="{}", capture_output=True, text=True, timeout=30)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_start_opens_one_window_in_the_folder_and_tags_the_pane(self):
        tmux, (session, _) = self.start(message="fix it")
        ((command, cwd),) = tmux.windows
        self.assertEqual(cwd, self.folder)
        words = shlex.split(command)
        self.assertEqual(words[:4], ["exec", "claude", "--session-id", session.id])
        self.assertEqual(words[4:6], ["--settings", os.path.join(self.key_dir(session.id), "settings.json")])
        self.assertEqual(words[-2:], ["--", "fix it"])
        self.assertEqual(tmux.options, [("%7", SESSION_OPT, session.id), ("%7", AGENT_OPT, "claude")])

    def test_the_returned_session_is_running_and_starting(self):
        _, (session, _) = self.start()
        self.assertEqual((session.running, session.state, session.turned), (True, "starting", False))
        self.assertEqual(core.category(session), core.NEEDS_INPUT)
        self.assertEqual(session.cwd, self.folder)

    def test_a_missing_folder_is_an_error_and_writes_nothing(self):
        tmux, (session, error) = self.start(folder=os.path.join(self.dir, "nope"))
        self.assertIsNone(session)
        self.assertIn("doesn't exist", error)
        self.assertEqual(tmux.windows, [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state")))

    def test_a_tmux_failure_removes_what_was_written(self):
        tmux, (session, error) = self.start(tmux=FakeTmux(fail_window=True))
        self.assertIsNone(session)
        self.assertIn("Couldn't start", error)
        self.assertEqual(os.listdir(os.path.join(self.state, "sessions")), [])

    def test_a_tagging_failure_kills_the_window_and_removes_the_record(self):
        class Untaggable(FakeTmux):
            def set_pane_option(self, pane, name, value):
                raise TmuxError("gone")

        tmux, (session, error) = self.start(tmux=Untaggable())
        self.assertIsNone(session)
        self.assertEqual(tmux.killed, ["%7"])
        self.assertEqual(os.listdir(os.path.join(self.state, "sessions")), [])

    def test_the_default_state_folder_follows_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.dir, "xdg")}):
            session, _ = engine.start(FakeTmux(), "claude", self.folder, "")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "xdg", "emx", "sessions", session.id, "session.json")))

    def test_a_test_session_gets_its_name_from_the_env_flag(self):
        with mock.patch.dict(os.environ, {"EMX_SHOW_TEST_SESSIONS": "1"}):
            tmux, (session, _) = self.start()
        self.assertRegex(session.name, r"^emx-test-[0-9a-f]{6}$")
        self.assertIn("-n", shlex.split(tmux.windows[0][0]))

    def test_forget_removes_only_that_sessions_folder(self):
        self.write_record(KEY)
        self.write_record(OTHER)
        Path(self.state, "keep.txt").write_text("x")
        os.makedirs(os.path.join(self.home, "projects", "p"))
        transcript = Path(self.home, "projects", "p", f"{KEY}.jsonl")
        transcript.write_text("{}\n")
        self.assertEqual(engine.forget(KEY, state=self.state), (True, None))
        self.assertFalse(os.path.exists(self.key_dir(KEY)))
        self.assertTrue(os.path.isfile(os.path.join(self.key_dir(OTHER), "session.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "keep.txt")))
        self.assertTrue(transcript.is_file())

    def test_forget_refuses_a_key_that_is_not_a_session_id_and_forgets_a_missing_one(self):
        self.write_record(KEY)
        for key in ("..", "../x", "", "sessions"):
            ok, error = engine.forget(key, state=self.state)
            self.assertFalse(ok, key)
            self.assertTrue(error)
        self.assertTrue(os.path.isdir(self.key_dir(KEY)))
        self.assertEqual(engine.forget(OTHER, state=self.state), (True, None))

    def test_list_sessions_with_nothing_started_is_an_empty_list(self):
        self.assertEqual(engine.list_sessions(FakeTmux(), self.state), ([], None))

    def test_list_sessions_on_an_unreadable_state_folder_is_an_error_never_empty(self):
        os.makedirs(self.state)
        Path(self.state, "sessions").write_text("not a folder")
        sessions, error = engine.list_sessions(FakeTmux(), self.state)
        self.assertIsNone(sessions)
        self.assertIn("Couldn't read", error)

    def test_list_sessions_with_tmux_failing_is_an_error(self):
        class Down(FakeTmux):
            def panes(self):
                raise TmuxError("no server")

        self.write_record()
        self.assertEqual(engine.list_sessions(Down(), self.state), (None, "no server"))

    def test_list_sessions_reads_the_registry_hooks_and_panes(self):
        self.write_record(KEY, started_at=2.0)
        self.write_record(OTHER, started_at=1.0, name="beta")
        self.write_events(
            {"event": "UserPromptSubmit", "at": 5, "session_id": CLAUDE_SID, "transcript_path": "/x"},
            {"event": "Stop", "at": 6, "session_id": CLAUDE_SID}, key=KEY,
        )
        sessions, error = engine.list_sessions(FakeTmux([self.live(KEY)]), self.state)
        self.assertIsNone(error)
        self.assertEqual([(s.id, s.state, s.running) for s in sessions], [(OTHER, "stopped", False), (KEY, "done", True)])
        self.assertEqual(sessions[1].session_id, CLAUDE_SID)

    def test_a_corrupt_record_is_skipped_and_the_rest_listed(self):
        self.write_record(KEY)
        os.makedirs(self.key_dir(OTHER))
        Path(self.key_dir(OTHER), "session.json").write_text("{not json")
        Path(self.state, "sessions", "junk").mkdir()
        sessions, error = engine.list_sessions(FakeTmux(), self.state)
        self.assertEqual((error, [s.id for s in sessions]), (None, [KEY]))

    def test_a_long_event_file_is_read_from_its_end_without_the_cut_line(self):
        path = os.path.join(self.dir, "long.jsonl")
        Path(path).write_text("".join(f"line-{n:05d}-" + "x" * 90 + "\n" for n in range(2000)))
        lines = engine._tail_lines(path)
        self.assertTrue(lines[0].startswith("line-") and lines[0].endswith("x" * 90))
        self.assertEqual(lines[-1][:10], "line-01999")
        self.assertLess(len(lines), 2000)
        self.assertEqual(engine._tail_lines(os.path.join(self.dir, "missing.jsonl")), [])

    def test_a_complete_first_line_is_kept_when_the_window_starts_on_a_line_boundary(self):
        path = os.path.join(self.dir, "aligned.jsonl")
        Path(path).write_text("".join(f"{n:09d}\n" for n in range(100)))
        lines = engine._tail_lines(path, limit=50)
        self.assertEqual(lines, [f"{n:09d}" for n in range(95, 100)])
        lines = engine._tail_lines(path, limit=45)
        self.assertEqual(lines, [f"{n:09d}" for n in range(96, 100)])

    def test_the_state_comes_from_the_end_of_a_long_event_file(self):
        self.write_record()
        filler = {"event": "Notification", "at": 1, "session_id": CLAUDE_SID, "message": "x" * 150}
        self.write_events(*([filler] * 400), {"event": "Stop", "at": 9, "session_id": CLAUDE_SID})
        self.assertGreater(os.path.getsize(os.path.join(self.key_dir(), "events.jsonl")), engine.READ_TAIL_BYTES)
        self.assertEqual(self.only_session().state, "done")

    def transcript_in_projects(self):
        path = os.path.join(self.home, "projects", "-work", f"{CLAUDE_SID}.jsonl")
        os.makedirs(os.path.dirname(path))
        shutil.copy(INTERRUPT, path)
        return path

    def interrupted_session(self, path):
        at = core._iso_ms("2026-10-14T01:53:35.000Z")
        self.write_record()
        self.write_events({"event": "UserPromptSubmit", "at": at, "session_id": CLAUDE_SID, "transcript_path": path})
        return self.only_session()

    def test_an_interrupt_in_the_transcript_reads_idle(self):
        session = self.interrupted_session(self.transcript_in_projects())
        self.assertEqual((session.state, session.status), ("working", "idle"))

    def test_a_transcript_outside_claude_projects_is_never_opened(self):
        outside = os.path.join(self.dir, "elsewhere", f"{CLAUDE_SID}.jsonl")
        os.makedirs(os.path.dirname(outside))
        shutil.copy(INTERRUPT, outside)
        sneaky = os.path.join(self.home, "projects", "..", "..", "elsewhere", f"{CLAUDE_SID}.jsonl")
        opened = []
        real_open = builtins.open

        def recording(file, *args, **kwargs):
            opened.append(os.fspath(file))
            return real_open(file, *args, **kwargs)

        for path in (outside, sneaky, os.path.join(self.home, "projects", "x", "notes.txt"),
                     os.path.join(self.home, "projects", "x", "not-a-uuid.jsonl")):
            with mock.patch("builtins.open", recording):
                session = self.interrupted_session(path)
            self.assertEqual((session.state, session.status), ("working", ""), path)
            self.assertFalse([o for o in opened if o.endswith(".jsonl") and not o.endswith("events.jsonl")], path)
            opened.clear()
            shutil.rmtree(self.state)

    def test_a_symlink_out_of_projects_is_not_followed(self):
        outside = os.path.join(self.dir, "elsewhere")
        os.makedirs(outside)
        shutil.copy(INTERRUPT, os.path.join(outside, f"{CLAUDE_SID}.jsonl"))
        os.makedirs(os.path.join(self.home, "projects"))
        os.symlink(outside, os.path.join(self.home, "projects", "link"))
        session = self.interrupted_session(os.path.join(self.home, "projects", "link", f"{CLAUDE_SID}.jsonl"))
        self.assertEqual(session.status, "")

    def test_resume_command_continues_the_latest_claude_session_and_rewrites_the_settings(self):
        self.write_record()
        self.write_events({"event": "SessionStart", "at": 5, "session_id": CLAUDE_SID, "source": "clear"})
        settings = os.path.join(self.key_dir(), "settings.json")
        Path(settings).write_text("stale")
        command, error = engine.resume_command(KEY, "claude", state=self.state)
        self.assertIsNone(error)
        self.assertEqual(shlex.split(command), ["exec", "claude", "--resume", CLAUDE_SID, "--settings", settings])
        with open(settings, encoding="utf-8") as fh:
            self.assertEqual(set(json.load(fh)["hooks"]), set(core.HOOK_EVENTS))
        with open(os.path.join(self.key_dir(), "session.json"), encoding="utf-8") as fh:
            self.assertGreater(json.load(fh)["run_at"], 0)

    def test_resume_command_falls_back_to_the_record_session_id(self):
        self.write_record()
        command, _ = engine.resume_command(KEY, "claude", state=self.state)
        self.assertEqual(shlex.split(command)[3], KEY)

    def test_what_the_last_run_reported_does_not_count_after_a_resume(self):
        self.write_record()
        self.write_events({"event": "Stop", "at": 5, "session_id": KEY})
        engine.resume_command(KEY, "claude", state=self.state)
        self.assertEqual(self.only_session().state, "starting")

    def test_resume_command_for_an_unknown_key_is_an_error(self):
        self.assertIsNone(engine.resume_command(KEY, "claude", state=self.state)[0])
        self.assertIsNone(engine.resume_command("../x", "claude", state=self.state)[0])

    def test_set_name_updates_the_record_only(self):
        self.write_record()
        self.assertEqual(engine.set_name(KEY, "renamed", state=self.state), (True, None))
        with open(os.path.join(self.key_dir(), "session.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual((record["name"], record["cwd"]), ("renamed", self.folder))
        self.assertFalse(engine.set_name(OTHER, "x", state=self.state)[0])
        self.assertFalse(engine.set_name("..", "x", state=self.state)[0])
