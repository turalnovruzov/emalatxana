#!/usr/bin/env python3
"""A stand-in for ssh in tests: runs the remote command on this machine through /bin/sh, as sshd would, and logs its argv to $FAKE_SSH_LOG."""

import json
import os
import sys

SKIP = {"-t", "-T", "-n", "-N", "-f", "-q"}
TAKES_VALUE = {"-o", "-O", "-S", "-p", "-l", "-i", "-F", "-E", "-J", "-c", "-m", "-b"}
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "TERM", "LANG", "TMPDIR")


def main(argv: list) -> int:
    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    if "-O" in argv or "-N" in argv:
        return 0
    rest, i = [], 0
    while i < len(argv):
        if argv[i] in SKIP:
            i += 1
        elif argv[i] in TAKES_VALUE:
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    if not rest:
        return 0
    command = " ".join(rest[1:])
    if not command:
        return 0
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env["SHELL"] = "/bin/sh"
    os.execve("/bin/sh", ["/bin/sh", "-c", command], env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
