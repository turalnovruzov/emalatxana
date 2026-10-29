import shlex
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from emx import core, places, probe

SSH = "/opt/fake/ssh"
BOT = core.Place("worker", None, "bot")
FAR = core.Place("far", "srv2", "bot")
OTHER_SERVER = core.Place("server-2", "srv2", None)
SYSTEMCTL = ["systemctl", "--user", "show"]


def parse(line):
    place, why = core.parse_place(line)
    assert place is not None, why
    return place


def prefs_from(text):
    values, _, problems = core.read_config(text, "/home/x")
    assert not problems, problems
    return values


class Argv(unittest.TestCase):
    def test_this_user_runs_the_command_as_is(self):
        self.assertEqual(places.argv(places.HERE, SYSTEMCTL, "sudo -n -u", SSH), SYSTEMCTL)

    def test_another_user_here(self):
        line = places.argv(parse("worker user bot"), SYSTEMCTL, "sudo -n -u", SSH)
        self.assertEqual(line[:9], ["sudo", "-n", "-u", "bot", "sh", "-c", places.RUNTIME_WRAP, "sh", "systemctl"])

    def test_my_login_on_another_server(self):
        line = places.argv(parse("server-2 server srv2"), SYSTEMCTL, "sudo -n -u", SSH)
        self.assertEqual(line[0], SSH)
        self.assertIn("BatchMode=yes", line)
        self.assertEqual(line[-2], "srv2")
        self.assertEqual(line[-1], shlex.join(["sh", "-c", places.RUNTIME_WRAP, "sh", *SYSTEMCTL]))
        self.assertNotIn("sudo", line[-1])

    def test_another_user_on_another_server(self):
        line = places.argv(parse("far server srv2 user bot"), SYSTEMCTL, "sudo -n -u", SSH)
        self.assertEqual(line[-2], "srv2")
        self.assertTrue(line[-1].startswith("sudo -n -u bot sh -c"))

    def test_user_command_is_configurable(self):
        line = places.argv(BOT, SYSTEMCTL, "/usr/bin/env python3 /x/fake_sudo.py", SSH)
        self.assertEqual(line[:4], ["/usr/bin/env", "python3", "/x/fake_sudo.py", "bot"])

    def test_runtime_dir_is_set_for_every_other_place(self):
        for place in (BOT, OTHER_SERVER, FAR):
            with self.subTest(place=place.name):
                text = " ".join(places.argv(place, SYSTEMCTL, "sudo -n -u", SSH))
                self.assertIn("XDG_RUNTIME_DIR", text)
                self.assertLess(text.index("XDG_RUNTIME_DIR"), text.index("systemctl"))


class Parsing(unittest.TestCase):
    def test_word_order_is_free(self):
        self.assertEqual(parse("far user bot server srv2"), parse("far server srv2 user bot"))
        self.assertEqual(parse("far server srv2 user bot"), core.Place("far", "srv2", "bot"))
        place, why = core.parse_place("x ssh srv2")
        self.assertIsNone(place)
        self.assertIn('unknown word "ssh"', why)

    def test_moving_a_place_is_one_line(self):
        here = places.from_prefs(prefs_from("place = worker user bot\n"))
        moved = places.from_prefs(prefs_from("place = worker server srv2 user bot\n"))
        self.assertEqual([p.name for p in here], [p.name for p in moved])
        self.assertEqual((here[1].host, moved[1].host), (None, "srv2"))
        self.assertEqual((here[1].login, here[0]), (moved[1].login, moved[0]))

    def test_this_user_is_built_in_and_first(self):
        self.assertEqual(places.from_prefs({})[0], places.HERE)
        self.assertEqual(places.HERE.name, "this user")


class Running(unittest.TestCase):
    def run_with(self, fake, place=OTHER_SERVER):
        with mock.patch.object(places.subprocess, "run", fake):
            return places.run(place, SYSTEMCTL, {}, {"EMX_SSH": SSH, "PATH": "/bin"})

    def test_unreachable_place_is_one_line(self):
        err = "ssh: connect to host srv2 port 22: Connection refused\n"
        fake = mock.Mock(return_value=SimpleNamespace(stdout="", stderr=err, returncode=255))
        self.assertEqual(self.run_with(fake), (None, "Can't reach srv2: ssh: connect to host srv2 port 22: Connection refused"))
        slow = mock.Mock(side_effect=subprocess.TimeoutExpired("ssh", 10))
        self.assertEqual(self.run_with(slow), (None, f"{SSH} took longer than 10 seconds"))
        missing = mock.Mock(side_effect=FileNotFoundError(2, "no", SSH))
        self.assertEqual(self.run_with(missing)[0], None)
        odd = mock.Mock(side_effect=ValueError("bad"))
        self.assertEqual(self.run_with(odd)[0], None)

    def test_a_missing_sudo_password_is_named(self):
        fake = mock.Mock(return_value=SimpleNamespace(stdout="", stderr="sudo: a password is required\n", returncode=1))
        self.assertEqual(self.run_with(fake, BOT), (None, "Can't run as bot: sudo: a password is required"))

    def test_success_and_plain_failure(self):
        ok = mock.Mock(return_value=SimpleNamespace(stdout="out", stderr="", returncode=0))
        self.assertEqual(self.run_with(ok, places.HERE), ("out", None))
        bad = mock.Mock(return_value=SimpleNamespace(stdout="", stderr="\nboom\nmore", returncode=1))
        self.assertEqual(self.run_with(bad, places.HERE), (None, "boom"))

    def test_the_ssh_binary_and_user_command_come_from_the_environment_and_prefs(self):
        ok = mock.Mock(return_value=SimpleNamespace(stdout="", stderr="", returncode=0))
        with mock.patch.object(places.subprocess, "run", ok):
            places.run(BOT, SYSTEMCTL, {"place_user_command": "/x/sudo -u"}, {"PATH": "/bin"})
        self.assertEqual(ok.call_args.args[0][:3], ["/x/sudo", "-u", "bot"])


class PrivateFiles(unittest.TestCase):
    def test_no_command_names_a_private_file(self):
        reads = [
            ["systemctl", "--user", "list-units", "--type=service", "--all", "--plain", "--no-legend", "--no-pager"],
            ["systemctl", "--user", "show", "--timestamp=unix", "-p", "Id", "a.timer"],
            ["journalctl", "--user", "-o", "json", "--no-pager", "-u", "a.service"],
            ["systemd-analyze", "calendar", "hourly"],
        ]
        for place in (places.HERE, BOT, OTHER_SERVER, FAR):
            for cmd in reads:
                with self.subTest(place=place.name, cmd=cmd[0]):
                    self.assert_clean(places.argv(place, cmd, "sudo -n -u", SSH))

    def test_probe_commands_name_no_private_file_outside_their_source(self):
        sid = "11111111-2222-3333-4444-555555555555"
        for place in (places.HERE, BOT, OTHER_SERVER, FAR):
            for args in (("list", "0"), ("read", sid), ("stat", sid)):
                with self.subTest(place=place.name, probe=args[0]):
                    line = places.argv(place, probe.command(*args), "sudo -n -u", SSH)
                    outside = [e for e in line if probe.SOURCE not in e and shlex.quote(probe.SOURCE) not in e]
                    self.assertLess(len(outside), len(line))
                    self.assert_clean(outside)

    def assert_clean(self, line):
        for element in line:
            for private in (".credentials", "settings.json", "/sessions/", ".key"):
                self.assertNotIn(private, element)
            self.assertNotIn(".claude", element)


if __name__ == "__main__":
    unittest.main()
