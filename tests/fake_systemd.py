#!/usr/bin/env python3
"""A stand-in for `systemctl`, `journalctl` and `systemd-analyze` in tests and live checks: link it under each of those names.

It prints the file of $FAKE_SYSTEMD_DIR that matches the command, and exits 1 with
`fake systemd: no fixture` when the variable or the file is missing. Files:
timers.list, timers.files, timers.show, timers.service.show (the timers' own
services), services.list, services.files, services.show (long-running
services), journal and analyze.
"""

import os
import sys


def fixture_name(program: str, argv: list) -> str:
    if program == "journalctl":
        return "journal"
    if program == "systemd-analyze":
        return "analyze"
    if "show" in argv:
        properties = argv[argv.index("-p") + 1] if "-p" in argv else ""
        if "FragmentPath" in properties:
            return "services.show"
        return "timers.service.show" if any(a.endswith(".service") for a in argv) else "timers.show"
    kind = "services" if "--type=service" in argv else "timers"
    return f"{kind}.files" if "list-unit-files" in argv else f"{kind}.list"


def main(argv: list) -> int:
    program = os.path.basename(argv[0])
    folder = os.environ.get("FAKE_SYSTEMD_DIR")
    path = os.path.join(folder, fixture_name(program, argv[1:])) if folder else ""
    if not path or not os.path.isfile(path):
        sys.stderr.write("fake systemd: no fixture\n")
        return 1
    with open(path, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
