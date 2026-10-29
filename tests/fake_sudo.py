#!/usr/bin/env python3
"""A stand-in for `sudo -n -u <login>` in tests and live checks: drops the options and the login, points the command at the other user's fixtures, and runs the rest.

$FAKE_SUDO_SYSTEMD_DIR becomes FAKE_SYSTEMD_DIR and $FAKE_SUDO_CLAUDE_HOME becomes
CLAUDE_CONFIG_DIR, when set. It never asks for or uses a real user.
"""

import os
import sys


def main(argv: list) -> int:
    rest = list(argv)
    while rest and rest[0] in ("-n", "-u"):
        rest.pop(0)
    if rest:
        rest.pop(0)
    if not rest:
        return 2
    env = dict(os.environ)
    for source, target in (("FAKE_SUDO_SYSTEMD_DIR", "FAKE_SYSTEMD_DIR"), ("FAKE_SUDO_CLAUDE_HOME", "CLAUDE_CONFIG_DIR")):
        if source in env:
            env[target] = env[source]
    os.execvpe(rest[0], rest, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
