"""What emx reads of Claude's sessions at a place: one script, sent as `python3 -I -c <source>` through the place runner, since another user can't read emx's install folder and a server has no emx of this version. It imports nothing from emx.

It opens only `<claude home>/projects/<slug>/<uuid>.jsonl`, found by listing `projects` and each `<slug>` folder; the claude home itself is never listed, and credentials, settings, `sessions/` and subagent files are never opened. Symlinks are skipped.

    list <since epoch>   one JSON line per session file changed since, with its first and last times and the end of it
    stat <session id>    {"size", "mtime"} of that file
    read <session id>    the file's last 8 MiB, after {"emx_probe_cut": N} when cut
"""

import datetime
import json
import os
import pwd
import re
import stat as filemode
import sys

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 256 * 1024
READ_BYTES = 8 * 1024 * 1024
TAIL_ENTRIES = 4
TEXT_MAX = 2000
SLUG = re.compile(r"[A-Za-z0-9._-]+")
SESSION = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
SESSION_FILE = re.compile(SESSION.pattern + r"\.jsonl")
NO_SUCH = 3


def _read_self():
    try:
        with open(__file__, encoding="utf-8") as fh:
            return fh.read()
    except NameError:
        return ""


SOURCE = _read_self()


def command(*args):
    return ["python3", "-I", "-c", SOURCE, *args]


def claude_home():
    """`$CLAUDE_CONFIG_DIR`, else the account's own home from the password database; `$HOME` may be the invoking user's under sudo."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude")


def _is_regular(path):
    try:
        return filemode.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _projects(home):
    projects = os.path.join(home, "projects")
    return projects if os.path.isdir(projects) else None


def _slug_folders(projects):
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        return
    for name in names:
        if SLUG.fullmatch(name) is None:
            continue
        folder = os.path.join(projects, name)
        try:
            if filemode.S_ISDIR(os.lstat(folder).st_mode):
                yield name, folder
        except OSError:
            continue


def session_files(home):
    """(slug, session id, path) of every session file exactly two levels under `projects`."""
    projects = _projects(home)
    if projects is None:
        return
    for slug, folder in _slug_folders(projects):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if SESSION_FILE.fullmatch(name) is None:
                continue
            path = os.path.join(folder, name)
            if _is_regular(path):
                yield slug, name[:-len(".jsonl")], path


def find_session(home, sid):
    """The path of `<sid>.jsonl` in some slug folder, or None. Nothing is listed below `projects`."""
    projects = _projects(home)
    if projects is None or SESSION.fullmatch(sid) is None:
        return None
    for _, folder in _slug_folders(projects):
        path = os.path.join(folder, sid + ".jsonl")
        if _is_regular(path):
            return path
    return None


def _ms(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return int(round(moment.timestamp() * 1000))


def _entries(data, drop_first, drop_last):
    lines = data.decode("utf-8", errors="replace").split("\n")
    if drop_first and lines:
        lines = lines[1:]
    if drop_last and lines:
        lines = lines[:-1]
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _scalar(value):
    return value if value is None or isinstance(value, (bool, int, float, str)) else None


def _block(block):
    kind = block.get("type")
    if kind == "text" and isinstance(block.get("text"), str):
        return {"type": "text", "text": block["text"][:TEXT_MAX]}
    if kind == "tool_use" and isinstance(block.get("name"), str):
        return {"type": "tool_use", "name": block["name"][:200]}
    if kind == "tool_result":
        return {"type": "tool_result", "is_error": block.get("is_error") is True}
    return None


def _tail_entry(entry):
    out = {"type": entry.get("type"), "timestamp": _scalar(entry.get("timestamp"))}
    for key in ("isApiErrorMessage", "error", "apiErrorStatus"):
        if key in entry:
            out[key] = _scalar(entry[key])
    params = entry.get("apiErrorParams")
    info = params.get("rate_limit_info") if isinstance(params, dict) else None
    if isinstance(info, dict) and "resetsAt" in info:
        out["apiErrorParams"] = {"rate_limit_info": {"resetsAt": _scalar(info["resetsAt"])}}
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks = [_block(b) for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    out["message"] = {"content": [b for b in blocks if b is not None]}
    return out


def _title(entries, titles):
    for entry in entries:
        if entry.get("type") == "custom-title" and isinstance(entry.get("customTitle"), str):
            titles["custom"] = " ".join(entry["customTitle"].split())
        elif entry.get("type") == "ai-title" and isinstance(entry.get("aiTitle"), str):
            titles["ai"] = " ".join(entry["aiTitle"].split())


def summary(slug, sid, path):
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size <= HEAD_BYTES + TAIL_BYTES:
            data = fh.read()
            head = tail = list(_entries(data, False, False))
        else:
            head = list(_entries(fh.read(HEAD_BYTES), False, True))
            fh.seek(size - TAIL_BYTES)
            tail = list(_entries(fh.read(), True, False))
    titles = {}
    _title(head, titles)
    _title(tail, titles)
    stamps = [_ms(e.get("timestamp")) for e in head]
    last_stamps = [_ms(e.get("timestamp")) for e in tail]
    keep = [e for e in tail if e.get("type") in ("user", "assistant") and e.get("isSidechain") is not True]
    return {
        "id": sid,
        "slug": slug,
        "cwd": next((e["cwd"] for e in head if isinstance(e.get("cwd"), str)), None),
        "first": next((s for s in stamps if s is not None), None),
        "last": next((s for s in reversed(last_stamps) if s is not None), None),
        "entrypoint": next((e["entrypoint"] for e in head if isinstance(e.get("entrypoint"), str)), None),
        "title": titles.get("custom") or titles.get("ai") or "",
        "tail": [_tail_entry(e) for e in keep[-TAIL_ENTRIES:]],
    }


def do_list(home, since, out):
    for slug, sid, path in session_files(home):
        try:
            if os.lstat(path).st_mtime < since:
                continue
            out.write(json.dumps(summary(slug, sid, path)) + "\n")
        except OSError:
            continue
    return 0


def do_stat(home, sid, out):
    path = find_session(home, sid)
    if path is None:
        sys.stderr.write("no such session\n")
        return NO_SUCH
    info = os.lstat(path)
    out.write(json.dumps({"size": info.st_size, "mtime": info.st_mtime}) + "\n")
    return 0


def do_read(home, sid, out):
    path = find_session(home, sid)
    if path is None:
        sys.stderr.write("no such session\n")
        return NO_SUCH
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        cut = max(0, size - READ_BYTES)
        fh.seek(cut)
        data = fh.read()
    if cut:
        out.write(json.dumps({"emx_probe_cut": cut}) + "\n")
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    out.write(data.decode("utf-8", errors="replace"))
    return 0


def main(argv, out=None):
    out = out or sys.stdout
    try:
        if len(argv) == 2 and argv[0] == "list":
            return do_list(claude_home(), float(argv[1]), out)
        if len(argv) == 2 and argv[0] == "stat":
            return do_stat(claude_home(), argv[1], out)
        if len(argv) == 2 and argv[0] == "read":
            return do_read(claude_home(), argv[1], out)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(" ".join(str(exc).split()) + "\n")
        return 1
    sys.stderr.write("usage: list <since> | stat <session id> | read <session id>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
