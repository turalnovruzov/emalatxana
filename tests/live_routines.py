#!/usr/bin/env python3
"""Builds the throwaway data for a live check of the Routines view: `live_routines.py build <dir> <now>`.

Under <dir> it writes `here/` (this user's systemd fixture: no routines), `worker/`
(a user `worker`'s fixture: a long-running `loop.service` and a `daily-digest`
timer), `worker-claude/` (that user's Claude home with the sessions behind them),
`home/.config/systemd/user/loop.service` and `bin/` (systemctl, journalctl,
systemd-analyze linked to fake_systemd.py, claude to fake_claude.py). Every name
is a placeholder. `<now>` is epoch seconds.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SLUG = "-work-loop"
SESSIONS = {
    "yesterday": "11111111-aaaa-4aaa-8aaa-111111111111",
    "pick": "22222222-bbbb-4bbb-8bbb-222222222222",
    "build": "33333333-cccc-4ccc-8ccc-333333333333",
    "digest": "44444444-dddd-4ddd-8ddd-444444444444",
}
START, DONE = "7d4958e842da4a758f6c1cdc7b36dcc5", "39f53479d3a045ac8e11786248231fbf"


def stamp(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(at))


def message(kind: str, at: float, content, **more) -> dict:
    return {"type": kind, "timestamp": stamp(at), "isSidechain": False, "cwd": "/work/loop",
            "message": {"role": kind, "content": content}, **more}


def write(path: str, text: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def session(home: str, sid: str, title: str, begin: float, entries: list) -> None:
    lines = [{"type": "ai-title", "aiTitle": title}] + entries
    last = max(float(row.get("at", begin)) for row in entries)
    write(f"{home}/projects/{SLUG}/{sid}.jsonl", "".join(json.dumps({k: v for k, v in row.items() if k != "at"}) + "\n" for row in lines), last)


def cycle(home: str, sid: str, title: str, begin: float, prompt: str, ending: str) -> None:
    """One session: a prompt, a failing tool call, then `ending`, which is a reply or the usage limit."""
    first = message("user", begin, prompt, entrypoint="sdk-cli")
    first["at"] = begin
    call = message("assistant", begin + 20, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make check"}}])
    call["at"] = begin + 20
    result = message("user", begin + 30, [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                           "content": "Exit code 1\nerror: check failed\nmake: *** [check] Error 1"}])
    result["at"] = begin + 30
    if ending == "limit":
        last = message("assistant", begin + 60, [{"type": "text", "text": "You've hit your usage limit."}],
                       isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429,
                       apiErrorParams={"rate_limit_info": {"resetsAt": int(begin + 3600)}})
    else:
        last = message("assistant", begin + 60, [{"type": "text", "text": "Done: the check passes now."}])
    last["at"] = begin + 60
    session(home, sid, title, begin, [first, call, result, last])


def build(root: str, now: float) -> None:
    here, worker, claude = f"{root}/here", f"{root}/worker", f"{root}/worker-claude"
    names = ("timers.list", "timers.files", "timers.show", "timers.service.show", "services.list", "services.files",
             "services.show", "journal", "analyze")
    for name in names:
        write(f"{here}/{name}", "")
    for name in names:
        write(f"{worker}/{name}", "")
    started = now - 3 * 86400
    unit = f"{root}/home/.config/systemd/user/loop.service"
    write(unit, "[Service]\nExecStart=/bin/true\n")
    write(f"{worker}/services.list", "loop.service loaded active running Loop\n")
    write(f"{worker}/services.files", "loop.service enabled enabled\n")
    write(f"{worker}/services.show", (
        f"Id=loop.service\nFragmentPath={unit}\nActiveState=active\nSubState=running\nResult=success\n"
        f"ExecMainStartTimestamp=@{int(started)}\nExecMainExitTimestamp=\nActiveEnterTimestamp=@{int(started)}\n"
        "TriggeredBy=\nWorkingDirectory=\n"))
    yesterday = now - 86400 - 3600
    cycle(claude, SESSIONS["yesterday"], "Triage inbox", yesterday, "Triage the inbox.", "reply")
    today = now - 1800
    cycle(claude, SESSIONS["pick"], "pick", today, "Pick the next ticket.", "reply")
    cycle(claude, SESSIONS["build"], "build", today + 100, "Build TICKET-1.", "limit")

    begin, end = now - 2400, now - 2300
    write(f"{worker}/timers.list", "daily-digest.timer loaded active waiting Digest\n")
    write(f"{worker}/timers.files", "daily-digest.timer enabled enabled\n")
    write(f"{worker}/timers.show", (
        "Id=daily-digest.timer\nLoadState=loaded\nActiveState=active\nUnit=daily-digest.service\n"
        f"TimersCalendar={{ OnCalendar=*-*-* 09:10:00 ; next_elapse=@{int(now + 7200)} }}\n"
        f"NextElapseUSecRealtime=@{int(now + 7200)}\nActiveEnterTimestamp=@{int(started)}\n"))
    write(f"{worker}/timers.service.show", (
        "Id=daily-digest.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
        f"ExecMainStartTimestamp=@{int(begin)}\nExecMainExitTimestamp=@{int(end)}\nWorkingDirectory=\n"))
    write(f"{worker}/journal", "".join(json.dumps({
        "__REALTIME_TIMESTAMP": str(int(at * 1e6)), "MESSAGE_ID": message_id, "USER_UNIT": "daily-digest.service",
        "USER_INVOCATION_ID": "inv1", "JOB_RESULT": "done"}) + "\n" for at, message_id in ((begin, START), (end, DONE))))
    cycle(claude, SESSIONS["digest"], "Write the digest", begin + 5, "Write the daily digest.", "reply")

    bin_folder = f"{root}/bin"
    os.makedirs(bin_folder, exist_ok=True)
    for tool in ("systemctl", "journalctl", "systemd-analyze"):
        os.symlink(f"{HERE}/fake_systemd.py", f"{bin_folder}/{tool}")
    os.symlink(f"{HERE}/fake_claude.py", f"{bin_folder}/claude")


def main(argv: list) -> int:
    if len(argv) != 3 or argv[0] != "build":
        sys.stderr.write("usage: live_routines.py build <dir> <now>\n")
        return 2
    build(os.path.realpath(argv[1]), float(argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
