#!/usr/bin/env python3
"""A stand-in for interactive `claude` in tests.

Takes the arguments emx gives claude (`--session-id <id>` or `--resume <id>`, and
`--settings <file>`), runs the SessionStart hook command from that settings file
like Claude Code does, then reads one hook event name per line from the terminal
and runs that event's hook command with a payload on stdin. While a file named
`hold` is in the working folder it stays silent, like claude at its trust question.
"""

import json
import os
import subprocess
import sys
import time


def main(argv):
    settings = argv[argv.index("--settings") + 1]
    resuming = "--resume" in argv
    sid = argv[argv.index("--resume" if resuming else "--session-id") + 1]
    with open(settings, encoding="utf-8") as fh:
        hooks = json.load(fh)["hooks"]

    def fire(event, **extra):
        command = hooks[event][0]["hooks"][0]["command"]
        payload = {"hook_event_name": event, "session_id": sid, "cwd": os.getcwd(), **extra}
        subprocess.run(command, shell=True, input=json.dumps(payload), text=True, capture_output=True)

    while os.path.exists("hold"):
        time.sleep(0.02)
    fire("SessionStart", source="resume" if resuming else "startup")
    for line in sys.stdin:
        event = line.strip()
        if event in hooks:
            fire(event, **({"reason": "other"} if event == "SessionEnd" else {}))
        if event == "SessionEnd":
            return


if __name__ == "__main__":
    main(sys.argv[1:])
