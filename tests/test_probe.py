import builtins
import datetime
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from emx import probe

UUID1 = "11111111-2222-3333-4444-555555555555"
UUID2 = "66666666-7777-8888-9999-000000000000"


def stamp(second, millis=0):
    return f"2026-10-08T09:43:{second:02d}.{millis:03d}Z"


def ms(second, millis=0):
    when = datetime.datetime(2026, 10, 8, 9, 43, second, millis * 1000, tzinfo=datetime.timezone.utc)
    return int(when.timestamp() * 1000)


ENTRIES = [
    {"type": "last-prompt"},
    {"type": "attachment", "timestamp": stamp(1, 151), "cwd": "/work/a", "attachment": {"type": "hook_success"}},
    {"type": "user", "timestamp": stamp(2, 5), "cwd": "/work/a", "entrypoint": "sdk-cli",
     "message": {"role": "user", "content": "Do the thing."}},
    {"type": "assistant", "timestamp": stamp(3), "message": {"role": "assistant", "content": [
        {"type": "text", "text": "On it."}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "timestamp": stamp(4), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "boom"}]}},
    {"type": "assistant", "isSidechain": True, "timestamp": stamp(5), "message": {"role": "assistant", "content": "side"}},
    {"type": "assistant", "timestamp": stamp(6), "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
     "apiErrorParams": {"rate_limit_info": {"resetsAt": 1_800_000_000, "other": "x"}},
     "message": {"role": "assistant", "content": [{"type": "text", "text": "X" * 5000}]}},
    {"type": "ai-title", "aiTitle": "A   title"},
    {"type": "assistant", "timestamp": stamp(7, 999), "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}},
]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        write(f"{self.home}/.credentials.json", "SECRET-TOKEN")
        write(f"{self.home}/settings.json", "{}")
        write(f"{self.home}/sessions/1.json", "{}")
        write(f"{self.home}/projects/-tmp-a/{UUID1}.jsonl", "".join(json.dumps(e) + "\n" for e in ENTRIES))
        write(f"{self.home}/projects/-tmp-a/{UUID1}/subagents/agent-x.jsonl", "{}\n")
        write(f"{self.home}/projects/-tmp-a/notes.txt", "note")
        os.symlink("../../.credentials.json", f"{self.home}/projects/-tmp-a/{UUID2}.jsonl")
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.home})
        env.start()
        self.addCleanup(env.stop)

    def call(self, *args):
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            code = probe.main(list(args), out)
        return code, out.getvalue()

    def listed(self, since="0"):
        code, text = self.call("list", since)
        self.assertEqual(code, 0)
        return [json.loads(line) for line in text.splitlines()]


class Allowlist(Home):
    def test_only_session_files_are_opened(self):
        opened, touched, listed = [], [], []

        def recording(real, into):
            def wrapper(path, *a, **k):
                into.append(os.fspath(path))
                return real(path, *a, **k)
            return wrapper

        allowed = re.compile("^" + re.escape(self.home) + r"/projects/[A-Za-z0-9._-]+/[0-9a-f-]{36}\.jsonl$")
        with mock.patch.object(builtins, "open", recording(builtins.open, opened)), \
                mock.patch.object(io, "open", recording(io.open, opened)), \
                mock.patch.object(os, "open", recording(os.open, opened)), \
                mock.patch.object(os, "stat", recording(os.stat, touched)), \
                mock.patch.object(os, "lstat", recording(os.lstat, touched)), \
                mock.patch.object(os, "scandir", recording(os.scandir, listed)), \
                mock.patch.object(os, "listdir", recording(os.listdir, listed)), \
                mock.patch.object(os, "walk", recording(os.walk, listed)):
            self.call("list", "0")
            self.call("stat", UUID1)
            self.call("read", UUID1)
            self.call("read", UUID2)
        self.assertTrue(opened)
        for path in opened:
            self.assertRegex(path, allowed)
        for path in opened + touched + listed:
            for private in (".credentials", "settings", "sessions", "subagents", "notes"):
                self.assertNotIn(private, path)
        self.assertEqual(sorted(set(listed)), [f"{self.home}/projects", f"{self.home}/projects/-tmp-a"])

    def test_subagent_and_symlinked_files_are_not_listed(self):
        rows = self.listed()
        self.assertEqual([r["id"] for r in rows], [UUID1])

    def test_a_symlinked_session_is_not_read_or_stated(self):
        self.assertNotEqual(self.call("read", UUID2)[0], 0)
        self.assertNotEqual(self.call("stat", UUID2)[0], 0)
        self.assertNotIn("SECRET", self.call("read", UUID2)[1])

    def test_read_refuses_a_non_uuid(self):
        code, text = self.call("read", "../../.credentials")
        self.assertNotEqual(code, 0)
        self.assertEqual(text, "")
        self.assertNotEqual(self.call("stat", "../.credentials")[0], 0)

    def test_an_unknown_command_or_missing_session_reads_nothing(self):
        self.assertEqual(self.call("cat", ".credentials.json")[0], 2)
        code, text = self.call("read", "12345678-1234-1234-1234-123456789012")
        self.assertEqual((code, text), (probe.NO_SUCH, ""))


class Listing(Home):
    def test_list_reports_first_last_cwd_entrypoint_title(self):
        (row,) = self.listed()
        self.assertEqual((row["first"], row["last"]), (ms(1, 151), ms(7, 999)))
        self.assertEqual((row["cwd"], row["entrypoint"], row["title"], row["slug"]), ("/work/a", "sdk-cli", "A title", "-tmp-a"))

    def test_a_session_whose_entries_carry_no_entrypoint_reports_null(self):
        plain = [e for e in ENTRIES if "entrypoint" not in e]
        write(f"{self.home}/projects/-tmp-a/{UUID1}.jsonl", "".join(json.dumps(e) + "\n" for e in plain))
        self.assertIsNone(self.listed()[0]["entrypoint"])

    def test_the_tail_keeps_the_last_four_non_sidechain_entries_trimmed(self):
        (row,) = self.listed()
        tail = row["tail"]
        self.assertEqual(len(tail), 4)
        self.assertEqual([e["timestamp"] for e in tail], [stamp(3), stamp(4), stamp(6), stamp(7, 999)])
        limit = tail[2]
        self.assertEqual((limit["isApiErrorMessage"], limit["error"], limit["apiErrorStatus"]), (True, "rate_limit", 429))
        self.assertEqual(limit["apiErrorParams"], {"rate_limit_info": {"resetsAt": 1_800_000_000}})
        self.assertEqual(len(limit["message"]["content"][0]["text"]), probe.TEXT_MAX)
        self.assertEqual(tail[0]["message"]["content"], [{"type": "text", "text": "On it."}, {"type": "tool_use", "name": "Bash"}])
        self.assertEqual(tail[1]["message"]["content"], [{"type": "tool_result", "is_error": True}])

    def test_since_leaves_out_older_files(self):
        path = f"{self.home}/projects/-tmp-a/{UUID1}.jsonl"
        os.utime(path, (1000, 1000))
        self.assertEqual(self.listed("500")[0]["id"], UUID1)
        self.assertEqual(self.listed("2000"), [])

    def test_a_big_file_is_read_from_both_ends_only(self):
        filler = json.dumps({"type": "attachment", "timestamp": stamp(30), "pad": "p" * 1000}) + "\n"
        head = [ENTRIES[1], ENTRIES[2]]
        body = "".join(json.dumps(e) + "\n" for e in head) + filler * 700 + "".join(json.dumps(e) + "\n" for e in ENTRIES[3:])
        write(f"{self.home}/projects/-tmp-a/{UUID1}.jsonl", body)
        self.assertGreater(len(body), probe.HEAD_BYTES + probe.TAIL_BYTES)
        (row,) = self.listed()
        self.assertEqual((row["first"], row["last"], row["entrypoint"]), (ms(1, 151), ms(7, 999), "sdk-cli"))
        self.assertEqual(row["title"], "A title")

    def test_claude_config_dir_moves_the_home(self):
        other = os.path.join(self.home, "other")
        write(f"{other}/projects/-x/{UUID2}.jsonl", json.dumps(ENTRIES[2]) + "\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": other}):
            self.assertEqual([r["id"] for r in self.listed()], [UUID2])

    def test_no_projects_folder_is_an_empty_listing(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.home, "nowhere")}):
            self.assertEqual(self.listed(), [])


class Reading(Home):
    def test_stat_gives_size_and_mtime(self):
        code, text = self.call("stat", UUID1)
        info = json.loads(text)
        path = f"{self.home}/projects/-tmp-a/{UUID1}.jsonl"
        self.assertEqual((code, info["size"]), (0, os.path.getsize(path)))

    def test_read_gives_the_file(self):
        code, text = self.call("read", UUID1)
        self.assertEqual(code, 0)
        self.assertEqual(text, "".join(json.dumps(e) + "\n" for e in ENTRIES))

    def test_a_long_file_is_cut_at_a_line_with_a_marker(self):
        lines = "".join(json.dumps({"n": i, "pad": "p" * 100}) + "\n" for i in range(200))
        write(f"{self.home}/projects/-tmp-a/{UUID1}.jsonl", lines)
        with mock.patch.object(probe, "READ_BYTES", 5000):
            code, text = self.call("read", UUID1)
        first, *rest = text.splitlines()
        self.assertEqual(json.loads(first)["emx_probe_cut"], len(lines) - 5000)
        self.assertTrue(rest)
        self.assertTrue(all(json.loads(line)["n"] is not None for line in rest))
        self.assertEqual(json.loads(rest[-1])["n"], 199)


class AsAScript(Home):
    def test_runs_as_a_script(self):
        env = {"PATH": os.environ["PATH"], "CLAUDE_CONFIG_DIR": self.home}
        done = subprocess.run(probe.command("list", "0"), env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["id"], UUID1)
        self.assertEqual(done.stdout, self.call("list", "0")[1])

    def test_the_source_is_the_file_and_imports_nothing_from_emx(self):
        self.assertIn("def main(", probe.SOURCE)
        self.assertIsNone(re.search(r"^\s*(import|from)\s+emx", probe.SOURCE, re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
