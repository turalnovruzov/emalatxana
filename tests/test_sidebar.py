import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from emx import core, sidebar


def git(*args):
    subprocess.run(
        ["git", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    )


class GitPlacement(unittest.TestCase):
    """Runs the real git against throwaway repos in a temp folder."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(cls.tmp.name)
        cls.repo = os.path.join(base, "proj-alpha")
        cls.worktree = os.path.join(base, "alpha-feat-x")
        cls.empty = os.path.join(base, "fresh")
        cls.plain = os.path.join(base, "notes")
        os.makedirs(os.path.join(cls.repo, "sub"))
        os.makedirs(cls.plain)
        git("init", "-q", "-b", "main", cls.repo)
        git("-C", cls.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git("-C", cls.repo, "worktree", "add", "-q", cls.worktree, "-b", "feat-x")
        git("init", "-q", "-b", "main", cls.empty)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_main_checkout_and_its_subfolder(self):
        self.assertEqual(sidebar.git_placement(self.repo), core.Placement(self.repo))
        self.assertEqual(sidebar.git_placement(os.path.join(self.repo, "sub")), core.Placement(self.repo))

    def test_worktree_groups_under_the_main_repo(self):
        self.assertEqual(sidebar.git_placement(self.worktree), core.Placement(self.repo, "feat-x"))

    def test_repo_with_no_commits_yet(self):
        self.assertEqual(sidebar.git_placement(self.empty), core.Placement(self.empty))

    def test_non_git_and_missing_folders_fall_back_to_the_cwd(self):
        gone = os.path.join(self.plain, "deleted")
        for cwd in (self.plain, gone, ""):
            with self.subTest(cwd=cwd):
                self.assertEqual(sidebar.git_placement(cwd), core.Placement(cwd))


class RunGitNeverRaises(unittest.TestCase):
    def test_launch_failures_become_an_error_line(self):
        for exc in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5), PermissionError("no")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                    out, error = sidebar.run_git(tempfile.gettempdir(), "status")
                self.assertIsNone(out)
                self.assertTrue(error)
                with mock.patch.object(sidebar.subprocess, "run", side_effect=exc):
                    self.assertEqual(sidebar.git_placement(tempfile.gettempdir()), core.Placement(tempfile.gettempdir()))


class Listing(unittest.TestCase):
    def test_lists_with_all_so_stopped_sessions_stay(self):
        done = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with mock.patch.object(sidebar.subprocess, "run", return_value=done) as run:
            self.assertEqual(sidebar.list_rows("claude"), ([], None))
        self.assertEqual(run.call_args.args[0], ["claude", "agents", "--json", "--all"])

    def test_poller_resolves_each_folder_once(self):
        rows = [{"id": i, "kind": "background", "cwd": "/a", "pid": 1} for i in ("x", "y")]
        listed = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
        poller = sidebar.Poller("claude")
        with mock.patch.object(sidebar.subprocess, "run", return_value=listed), \
                mock.patch.object(sidebar, "git_placement", return_value=core.Placement("/r")) as placement:
            poller.poll()
            sessions, placements, error = poller.poll()
        self.assertEqual(placement.call_count, 1)
        self.assertEqual((len(sessions), placements, error), (2, {"/a": core.Placement("/r")}, None))


if __name__ == "__main__":
    unittest.main()
