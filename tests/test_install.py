import fcntl
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from emx import core, install

GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]


def run_git(repo, *args):
    subprocess.run(["git", *GIT_IDENTITY, "-C", repo, *args], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV}, timeout=60)


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def add_version(repo, version):
    write(os.path.join(repo, "bin", "emx"), f"#!/bin/sh\necho emx {version}\n", 0o755)
    write(os.path.join(repo, "emx", "__init__.py"), f'__version__ = "{version}"\n')
    write(os.path.join(repo, "CHANGELOG.md"), f"# Changelog\n\n## {version}\n\n- {version}\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", version)
    run_git(repo, "tag", f"v{version}")


def make_source(tmp, versions, name="source"):
    """A non-bare repo with one commit and one `vX.Y.Z` tag per version, in order."""
    repo = os.path.join(tmp, name)
    os.makedirs(repo)
    run_git(repo, "init", "-q")
    for version in versions:
        add_version(repo, version)
    return repo


def temp_homes(case):
    """Puts EMX_HOME, EMX_BIN_HOME and git's config in temp folders for the whole test, so nothing reaches his real install or his git config."""
    tmp = os.path.realpath(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, tmp, True)
    homes = {"EMX_HOME": os.path.join(tmp, "root"), "EMX_BIN_HOME": os.path.join(tmp, "bin"), **GIT_ENV}
    patcher = mock.patch.dict(os.environ, homes)
    patcher.start()
    case.addCleanup(patcher.stop)
    base = os.path.realpath(tempfile.gettempdir()) + os.sep
    for name in ("EMX_HOME", "EMX_BIN_HOME"):
        case.assertTrue(os.path.realpath(os.environ[name]).startswith(base), name)
    return tmp, homes["EMX_HOME"], homes["EMX_BIN_HOME"]


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.bin = temp_homes(self)

    def install(self, versions=("2.3.0", "2.3.1"), env=None):
        self.source = make_source(self.tmp, versions)
        out = io.StringIO()
        got = install.install(self.source, dict(os.environ) if env is None else env, out)
        return got, out.getvalue()

    def link(self):
        return os.readlink(os.path.join(self.root, "current"))


class Install(InstallCase):
    def test_install_makes_newest_version_and_links_bin(self):
        (version, error), _ = self.install()
        self.assertEqual((version, error), ("2.3.1", None))
        self.assertEqual(self.link(), os.path.join("versions", "2.3.1"))
        emx = os.path.join(self.bin, "emx")
        self.assertEqual(os.path.realpath(emx), os.path.join(self.root, "versions", "2.3.1", "bin", "emx"))
        self.assertTrue(os.access(emx, os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(self.root, "versions", "2.3.1", ".git")))
        self.assertEqual(subprocess.run([emx], capture_output=True, text=True, timeout=30).stdout, "emx 2.3.1\n")

    def test_install_refuses_when_no_tag_reaches_2_3_0(self):
        (version, error), _ = self.install(("2.2.1", "2.2.2"))
        self.assertIsNone(version)
        self.assertIn("v2.3.0", error)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "current")))
        self.assertFalse(os.path.lexists(os.path.join(self.bin, "emx")))

    def test_install_records_the_url_it_was_given(self):
        self.install()
        url, error = install.git(self.root, "config", "remote.origin.url")
        self.assertEqual((url.strip(), error), (self.source, None))

    def test_install_prints_path_line_only_when_bin_not_on_path(self):
        (_, error), said = self.install(env={**os.environ, "PATH": "/usr/bin:/bin"})
        self.assertIsNone(error)
        self.assertIn(f'export PATH="{self.bin}:$PATH"', said)
        out = io.StringIO()
        install.install(self.source, {**os.environ, "PATH": f"/usr/bin:{self.bin}"}, out)
        self.assertNotIn("export PATH", out.getvalue())

    def test_install_replaces_a_symlink_and_refuses_a_regular_file(self):
        os.makedirs(self.bin)
        emx = os.path.join(self.bin, "emx")
        os.symlink("/somewhere/else/emx", emx)
        (_, error), said = self.install()
        self.assertIsNone(error)
        self.assertIn("/somewhere/else/emx", said)
        self.assertEqual(os.readlink(emx), os.path.join(self.root, "current", "bin", "emx"))
        os.unlink(emx)
        write(emx, "mine\n", 0o755)
        version, error = install.install(self.source, dict(os.environ), io.StringIO())
        self.assertIsNone(version)
        self.assertIn(emx, error)
        with open(emx, "rb") as fh:
            self.assertEqual(fh.read(), b"mine\n")
        self.assertFalse(os.path.islink(emx))

    def test_a_failed_first_install_leaves_no_root_behind(self):
        version, error = install.install(os.path.join(self.tmp, "missing"), dict(os.environ), io.StringIO())
        self.assertIsNone(version)
        self.assertIn("couldn't clone", error)
        self.assertFalse(os.path.lexists(self.root))

    def test_a_failed_reinstall_keeps_the_existing_root(self):
        self.install()
        with mock.patch.object(install, "fetch", return_value=(None, "no network")):
            version, error = install.install(self.source, dict(os.environ), io.StringIO())
        self.assertEqual((version, error), (None, "no network"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "versions", "2.3.1")))

    def test_install_over_an_existing_root_says_the_url_was_ignored(self):
        self.install()
        other = make_source(self.tmp, ["2.3.0"], name="other")
        out = io.StringIO()
        version, error = install.install(other, dict(os.environ), out)
        self.assertEqual((version, error), ("2.3.1", None))
        self.assertIn(f"emx is already installed from {self.source}; updating from there.", out.getvalue())
        out = io.StringIO()
        install.install(self.source, dict(os.environ), out)
        self.assertNotIn("already installed", out.getvalue())

    def test_install_refuses_a_non_empty_foreign_emx_home(self):
        write(os.path.join(self.root, "notes.txt"), "his\n")
        (version, error), _ = self.install()
        self.assertIsNone(version)
        self.assertIn(self.root, error)
        self.assertEqual(os.listdir(self.root), ["notes.txt"])

    def test_a_relative_emx_home_or_bin_home_is_an_error_not_the_default(self):
        self.source = make_source(self.tmp, ["2.3.0"])
        for name in ("EMX_HOME", "EMX_BIN_HOME"):
            with self.subTest(name=name):
                version, error = install.install(self.source, {**os.environ, name: "rel"}, io.StringIO())
                self.assertIsNone(version)
                self.assertIn(name, error)
                self.assertFalse(os.path.exists(self.root))


class Update(InstallCase):
    def setUp(self):
        super().setUp()
        (version, error), _ = self.install()
        self.assertEqual((version, error), ("2.3.1", None))

    def test_update_flips_to_new_same_major_tag(self):
        add_version(self.source, "2.3.2")
        self.assertEqual(install.update(self.root, "2.3.1", allow_major=False), (("2.3.1", "2.3.2", None), None))
        self.assertEqual(install.current(self.root), "2.3.2")

    def test_update_with_nothing_new_changes_nothing(self):
        self.assertEqual(install.update(self.root, "2.3.1", allow_major=True), (("2.3.1", "2.3.1", None), None))

    def test_update_holds_a_major_unless_allowed(self):
        add_version(self.source, "3.0.0")
        self.assertEqual(install.update(self.root, "2.3.1", allow_major=False), (("2.3.1", "2.3.1", "3.0.0"), None))
        self.assertEqual(install.current(self.root), "2.3.1")
        self.assertEqual(install.update(self.root, "2.3.1", allow_major=True), (("2.3.1", "3.0.0", None), None))
        self.assertEqual(install.current(self.root), "3.0.0")

    def test_update_waits_for_the_panel_to_catch_up(self):
        add_version(self.source, "2.3.2")
        install.update(self.root, "2.3.1", allow_major=False)
        add_version(self.source, "2.3.3")
        got = install.update(self.root, "2.3.1", allow_major=False, apply_only_if_running_current=True)
        self.assertEqual(got, (("2.3.2", "2.3.2", None), None))
        self.assertEqual(install.current(self.root), "2.3.2")
        self.assertFalse(os.path.exists(os.path.join(self.root, "versions", "2.3.3")))

    def test_a_failed_extract_leaves_current_and_no_partial_folder(self):
        add_version(self.source, "2.3.2")
        with mock.patch.object(install, "untar", return_value=(None, "tar: broken")):
            value, error = install.update(self.root, "2.3.1", allow_major=False)
        self.assertIsNone(value)
        self.assertIn("tar: broken", error)
        self.assertEqual(install.current(self.root), "2.3.1")
        names = os.listdir(os.path.join(self.root, "versions"))
        self.assertNotIn("2.3.2", names)
        self.assertEqual([n for n in names if n.startswith(".tmp-")], [])

    def test_flip_is_a_rename_of_a_new_link(self):
        add_version(self.source, "2.3.2")
        install.fetch(self.root)
        install.prepare(self.root, "2.3.2")
        with mock.patch.object(install.os, "replace", wraps=os.replace) as replace:
            self.assertEqual(install.flip(self.root, "2.3.2"), ("2.3.2", None))
        replace.assert_called_once_with(os.path.join(self.root, ".current.tmp"), os.path.join(self.root, "current"))
        self.assertEqual(self.link(), os.path.join("versions", "2.3.2"))

    def test_flip_replaces_a_stale_current_tmp(self):
        os.symlink("versions/junk", os.path.join(self.root, ".current.tmp"))
        self.assertEqual(install.flip(self.root, "2.3.0"), (None, "emx 2.3.0 is not unpacked"))
        install.prepare(self.root, "2.3.0")
        self.assertEqual(install.flip(self.root, "2.3.0"), ("2.3.0", None))
        self.assertEqual(self.link(), os.path.join("versions", "2.3.0"))
        self.assertFalse(os.path.lexists(os.path.join(self.root, ".current.tmp")))

    def test_a_stale_temp_folder_is_removed_and_a_fresh_one_kept(self):
        versions = os.path.join(self.root, "versions")
        stale, fresh = os.path.join(versions, ".tmp-2.3.9-1"), os.path.join(versions, ".tmp-2.3.8-2")
        os.makedirs(stale)
        os.makedirs(fresh)
        old = os.path.getmtime(stale) - install.STALE_TMP_SECONDS - 10
        os.utime(stale, (old, old))
        add_version(self.source, "2.3.2")
        install.update(self.root, "2.3.1", allow_major=False)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_fetch_failure_is_an_error_line_not_an_exception(self):
        install.git(self.root, "remote", "set-url", "origin", os.path.join(self.tmp, "missing"))
        value, error = install.fetch(self.root)
        self.assertIsNone(value)
        self.assertIsInstance(error, str)
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn("does not appear to be a git repository", error)
        value, error = install.update(self.root, "2.3.1", allow_major=False)
        self.assertIsNone(value)
        self.assertIn("couldn't fetch", error)
        self.assertIn("does not appear to be a git repository", error)

    def test_an_ssh_failure_keeps_its_cause_not_gits_closing_advice(self):
        stderr = (
            "ssh: Could not resolve hostname no-such-host.invalid: nodename nor servname provided, or not known\n"
            "fatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\nand the repository exists.\n"
        )
        done = subprocess.CompletedProcess([], 128, stdout="", stderr=stderr)
        with mock.patch.object(install.subprocess, "run", return_value=done):
            value, error = install.fetch(self.root)
        self.assertIsNone(value)
        self.assertIn("Could not resolve hostname", error)
        self.assertNotIn("and the repository exists", error)


def make_versions(root, names, current):
    for name in names:
        write(os.path.join(root, "versions", name, "bin", "emx"), "", 0o755)
    os.makedirs(os.path.join(root, "source.git"), exist_ok=True)
    os.symlink(os.path.join("versions", current), os.path.join(root, "current"))


class Prune(unittest.TestCase):
    def setUp(self):
        _, self.root, _ = temp_homes(self)
        make_versions(self.root, ["2.3.0", "2.3.1", "2.4.0", "2.5.0"], "2.5.0")

    def on_disk(self):
        return sorted(os.listdir(os.path.join(self.root, "versions")))

    def test_prune_deletes_only_what_prune_plan_names(self):
        self.assertEqual(install.prune(self.root, "2.3.0"), (["2.3.1"], None))
        self.assertEqual(self.on_disk(), ["2.3.0", "2.4.0", "2.5.0"])
        with mock.patch.object(install.core, "prune_plan", return_value=["2.4.0"]) as plan:
            self.assertEqual(install.prune(self.root, "2.3.0"), (["2.4.0"], None))
        plan.assert_called_once_with(mock.ANY, "2.5.0", "2.3.0")
        self.assertEqual(self.on_disk(), ["2.3.0", "2.5.0"])

    def test_keep_lagging_keeps_the_version_before_the_callers_own_and_everything_above(self):
        self.assertEqual(install.prune(self.root, "2.4.0", keep_lagging=True), (["2.3.0"], None))
        self.assertEqual(self.on_disk(), ["2.3.1", "2.4.0", "2.5.0"])
        self.assertEqual(install.prune(self.root, "2.5.0", keep_lagging=True), (["2.3.1"], None))
        self.assertEqual(self.on_disk(), ["2.4.0", "2.5.0"])

    def test_prune_skips_while_the_lock_is_held(self):
        with install.locked(self.root, wait=True) as busy:
            self.assertIsNone(busy)
            self.assertEqual(install.prune(self.root, "2.5.0"), ([], None))
        self.assertEqual(self.on_disk(), ["2.3.0", "2.3.1", "2.4.0", "2.5.0"])


class Lock(unittest.TestCase):
    def setUp(self):
        _, self.root, _ = temp_homes(self)
        os.makedirs(self.root)

    def hold(self):
        fd = os.open(os.path.join(self.root, "lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_the_lock_skips_for_the_worker_while_held(self):
        self.hold()
        with install.locked(self.root, wait=False) as busy:
            self.assertEqual(busy, install.BUSY)
        self.assertEqual(install.update(self.root, "2.3.0", allow_major=False, wait=False), (None, install.BUSY))

    def test_the_cli_waits_then_fails(self):
        self.hold()
        with mock.patch.object(install, "LOCK_WAIT_SECONDS", 0.3):
            with install.locked(self.root, wait=True) as busy:
                self.assertEqual(busy, install.BUSY)

    def test_the_lock_is_free_again_after_use(self):
        with install.locked(self.root, wait=False) as busy:
            self.assertIsNone(busy)
        with install.locked(self.root, wait=False) as busy:
            self.assertIsNone(busy)


class Clone(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, _ = temp_homes(self)

    def test_clone_puts_the_url_after_double_dash(self):
        source = make_source(self.tmp, ["2.3.0"])
        with mock.patch.object(install.subprocess, "run", wraps=subprocess.run) as run:
            self.assertEqual(install.clone(self.root, source), (os.path.join(self.root, "source.git"), None))
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index(source) - 1], "--")
        shutil.rmtree(self.root)
        value, error = install.clone(self.root, "-oProxy=x")
        self.assertIsNone(value)
        self.assertIn("couldn't clone", error)
        self.assertFalse(os.path.exists(os.path.join(self.root, "source.git")))
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".source.git")], [])

    def test_a_tag_pushed_after_the_clone_is_fetched(self):
        source = make_source(self.tmp, ["2.3.0"])
        install.clone(self.root, source)
        add_version(source, "2.3.1")
        self.assertEqual(install.fetch(self.root), (True, None))
        self.assertEqual(install.versions(self.root), (["2.3.0", "2.3.1"], None))

    def test_a_missing_git_is_an_error_line(self):
        with mock.patch.object(install.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(install.fetch(self.root), (None, "couldn't fetch updates: git is not installed"))


class UpdatesSwitch(unittest.TestCase):
    def test_off_writes_the_marker_and_on_removes_it(self):
        _, root, _ = temp_homes(self)
        os.makedirs(root)
        self.assertTrue(install.updates_on(root))
        self.assertEqual(install.set_updates(root, False), (False, None))
        self.assertFalse(install.updates_on(root))
        self.assertEqual(install.set_updates(root, True), (True, None))
        self.assertTrue(install.updates_on(root))
        self.assertEqual(install.set_updates(root, True), (True, None))


class Uninstall(InstallCase):
    def setUp(self):
        super().setUp()
        (version, error), _ = self.install()
        self.assertIsNone(error)
        self.prefs = os.path.join(self.tmp, "config", "emx", "prefs.json")
        self.state = os.path.join(self.tmp, "state", "emx", "x")
        write(self.prefs, "{}")
        write(self.state, "")

    def test_uninstall_removes_root_and_our_link_but_not_a_foreign_link_or_prefs(self):
        install.set_updates(self.root, False)
        removed, error = install.uninstall(dict(os.environ), self.root)
        self.assertIsNone(error)
        self.assertIn(os.path.join(self.bin, "emx"), removed)
        self.assertFalse(os.path.exists(self.root))
        self.assertFalse(os.path.lexists(os.path.join(self.bin, "emx")))
        self.assertTrue(os.path.isfile(self.prefs))
        self.assertTrue(os.path.isfile(self.state))

        source = make_source(self.tmp, ["2.3.0"], name="again")
        self.assertEqual(install.install(source, dict(os.environ), io.StringIO())[1], None)
        emx = os.path.join(self.bin, "emx")
        os.unlink(emx)
        os.symlink("/opt/other/emx", emx)
        install.uninstall(dict(os.environ), self.root)
        self.assertEqual(os.readlink(emx), "/opt/other/emx")
        self.assertFalse(os.path.exists(self.root))

    def test_uninstall_leaves_files_emx_did_not_make(self):
        write(os.path.join(self.root, "stray.txt"), "his\n")
        removed, error = install.uninstall(dict(os.environ), self.root)
        self.assertIsNone(error)
        self.assertEqual(os.listdir(self.root), ["stray.txt"])
        self.assertNotIn(self.root, removed)

    def test_uninstall_of_a_checkout_does_nothing(self):
        self.assertEqual(install.uninstall(dict(os.environ), None), (None, "this emx is not installed"))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "source.git")))


if __name__ == "__main__":
    unittest.main()
