"""The phone page: emx's sessions on a web page that only his own Tailscale devices can reach (standard library only, ADR 0011).

The page lives in the primary panel's process. It binds this machine's
Tailscale IPv4 and nothing else, and every request is checked before anything
is read or done: the peer must be this node's owner according to
`tailscale whois`, the `Host` must be one of this node's own names, and a POST
must come from this page's own origin as a small JSON object.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import parse_qs

import emx
from emx import core, routines, sidebar
from emx.cli import read_changelog
from emx.engine import AGENT, bridge_link
from emx.tmux import TmuxError

MISSING, DOWN, TAGGED = "missing", "down", "tagged"
MAX_BODY = 16 * 1024
WHOIS_FRESH_SECONDS = 60
WHOIS_REFUSED_SECONDS = 5
TAILSCALE_TIMEOUT = 5
REQUEST_TIMEOUT = 10
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; manifest-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
)


class Node(NamedTuple):
    ip: str
    user_id: int
    dns_name: str
    short_name: str


def _object(text: str):
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _is_int(value) -> bool:
    return type(value) is int


def _ipv4(text) -> bool:
    try:
        return isinstance(text, str) and isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address)
    except ValueError:
        return False


def parse_status(text: str) -> tuple:
    """(Node, None) from `tailscale status --json`, else (None, DOWN or TAGGED). A tagged node has no user, so emx can't tell whose it is."""
    data = _object(text)
    me = data.get("Self") if data else None
    if not isinstance(me, dict) or data.get("BackendState") != "Running":
        return None, DOWN
    ip = next((a for a in me.get("TailscaleIPs") or [] if _ipv4(a)), None)
    dns = me.get("DNSName")
    if ip is None or not _is_int(me.get("UserID")) or not isinstance(dns, str) or not dns:
        return None, DOWN
    if me.get("Tags"):
        return None, TAGGED
    name = dns.lower().rstrip(".")
    return Node(ip, me["UserID"], name, name.split(".")[0]), None


def parse_whois(text: str) -> int | None:
    """The Tailscale user a peer belongs to, from `tailscale whois --json`: only when the node's user and the profile agree and the node is not tagged."""
    data = _object(text)
    node = data.get("Node") if data else None
    profile = data.get("UserProfile") if data else None
    if not isinstance(node, dict) or not isinstance(profile, dict) or node.get("Tags"):
        return None
    user = node.get("User")
    if not _is_int(user) or not _is_int(profile.get("ID")) or user != profile["ID"]:
        return None
    return user


def run_tailscale(args: list) -> str:
    done = subprocess.run(
        ["tailscale", *args], capture_output=True, text=True, timeout=TAILSCALE_TIMEOUT,
        stdin=subprocess.DEVNULL, check=True,
    )
    return done.stdout


class Tailnet:
    """This machine's Tailscale identity and who a peer is, from the `tailscale` command; emx never installs or changes it."""

    def __init__(self, run: Callable = run_tailscale, which: Callable = shutil.which, clock: Callable = time.monotonic):
        self.run, self.which, self.clock = run, which, clock
        self.known = {}
        self.lock = threading.Lock()

    def node(self) -> tuple:
        if self.which("tailscale") is None:
            return None, MISSING
        try:
            return parse_status(self.run(["status", "--json"]))
        except FileNotFoundError:
            return None, MISSING
        except (OSError, subprocess.SubprocessError):
            return None, DOWN

    def peer_user(self, ip: str) -> int | None:
        if not _ipv4(ip):
            return None
        now = self.clock()
        with self.lock:
            user, until = self.known.get(ip, (None, 0.0))
            if now < until:
                return user
        try:
            user = parse_whois(self.run(["whois", "--json", ip]))
        except (OSError, subprocess.SubprocessError):
            user = None
        with self.lock:
            self.known[ip] = (user, now + (WHOIS_FRESH_SECONDS if user is not None else WHOIS_REFUSED_SECONDS))
        return user


def _proc_address(text: str) -> tuple | None:
    """(ip, port) of a `/proc/net/tcp` or `tcp6` address, which is each 32-bit word in host (little-endian) byte order and the port big-endian: `0100007F:1F90` is 127.0.0.1:8080. Only IPv4 and its IPv4-mapped form are read."""
    host, _, port = text.partition(":")
    try:
        if len(host) == 8:
            word = host
        elif len(host) == 32 and host[:24].upper() == "0000000000000000FFFF0000":
            word = host[24:]
        else:
            return None
        return str(ipaddress.IPv4Address(int(word, 16).to_bytes(4, "little"))), int(port, 16)
    except ValueError:
        return None


def local_peer_uid(peer: tuple, server: tuple, proc_text: str) -> int | None:
    """The uid owning the connection a peer on this machine made to `server`, from the text of `/proc/net/tcp` and `tcp6`; None when no row matches.

    The matching row is the peer's own socket: its local address is the peer's
    and its remote address the server's. Only asked when the peer's IP is this
    node's own, which is the one case `tailscale whois` can't tell apart from
    another user of this machine.
    """
    for line in proc_text.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 8:
            continue
        if _proc_address(columns[1]) == peer and _proc_address(columns[2]) == server:
            try:
                return int(columns[7])
            except ValueError:
                return None
    return None


def read_proc_net() -> str:
    """`/proc/net/tcp` and `tcp6` as one text; empty where there is no /proc, which makes every own-machine request refused."""
    text = ""
    for name in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{name}", encoding="ascii", errors="replace") as fh:
                text += fh.read() + "\n"
        except OSError:
            pass
    return text


def allowed_folder(path: str, home: str, known) -> bool:
    """Whether the page may offer or start a session in `path`: under $HOME, or one of `known` (session, recent and favourite folders) or below one. Symlinks are resolved first, so a link under $HOME to elsewhere does not count."""
    real = os.path.realpath(path)
    roots = [os.path.realpath(home)] + [os.path.realpath(k) for k in known if k]
    return any(r != "/" and (real == r or real.startswith(r + "/")) for r in roots)


WEB = Path(__file__).parent / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MANIFEST_TYPE = "application/manifest+json; charset=utf-8"
CHROME_INDEX = {"ground": 234, "raised": 235, "selected": 236, "border": 238, "plain": 252}


def tokens_css() -> str:
    """The page's colours, all from the panel's own tone indexes, so they cannot drift from it."""
    colours = {tone: core.TONE_HEX[tone] for tone in ("needs", "done", "idle", "stopped", "muted", "path", "accent", "error", "copper")}
    colours.update({name: core.xterm256_hex(index) for name, index in CHROME_INDEX.items()})
    return ":root{" + "".join(f"--{name}:{value};" for name, value in colours.items()) + "}\n"


def manifest_json() -> str:
    """The home-screen app's manifest; its colours are the page's ground tone, like tokens.css."""
    ground = core.xterm256_hex(CHROME_INDEX["ground"])
    return json.dumps({"name": "emx", "short_name": "emx", "start_url": "/", "scope": "/", "display": "standalone",
                       "background_color": ground, "theme_color": ground})


TRUST_QUESTION = re.compile(r"trust this folder|do you trust", re.IGNORECASE)
TRUST_DETAIL = "waiting to trust this folder: answer it in a terminal"
COUNT_WORDS = {core.NEEDS_INPUT: "needs", core.WORKING: "working", core.DONE: "done", "idle": "idle"}
FOLDER_ROWS = 8
FOLDER_CANDIDATES = 60
ROUTINES_FRESH_SECONDS = 60
ROUTINES_WAIT_SECONDS = 10
STILL_FRAME = 2
FIND_MAX = 200
PEEK_TEXT_MAX = 4000
GROUP_MAX = 200
NAME_OFFERED_TEXT_MAX = 200
NOT_A_SESSION = "That is not a session of emx's."
GONE = sidebar.OPEN_GONE
FOLDER_CHANGED = "That folder changed; look again."
PINNED_FOLDER_REFUSAL = "Pinned can't be closed as a folder; close its sessions one at a time."
FOLDER_GONE = "That folder isn't listed any more."
FIND_NONE = sidebar.FIND_NONE
NAME_BUSY = "Still finishing an earlier Haiku call; tap Suggest again in a moment."
NAME_UNFIT_PAGE = "Haiku's name didn't fit; tap Suggest for another."


class RoutinesCache:
    """Routines read on a thread of their own: three systemd tools can be slow, and the page asks every few seconds. The first call waits for the first answer."""

    def __init__(self, read: Callable, clock: Callable = time.time):
        self.read, self.clock = read, clock
        self.lock = threading.Lock()
        self.answer, self.at, self.thread = None, 0.0, None

    def get(self) -> tuple:
        with self.lock:
            if self.answer is None or self.clock() - self.at >= ROUTINES_FRESH_SECONDS:
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self.refresh, daemon=True)
                    self.thread.start()
            first, thread = self.answer is None, self.thread
        if first:
            thread.join(ROUTINES_WAIT_SECONDS)
        with self.lock:
            return self.answer or (None, "Still reading the routines.")

    def refresh(self) -> None:
        try:
            answer = self.read(self.clock())
        except Exception as exc:
            answer = (None, core.error_line(exc))
        with self.lock:
            self.answer, self.at = answer, self.clock()


class PanelSources(NamedTuple):
    """What the primary panel knows that the page shows too, read by callable; every field answers empty where there is no panel."""

    transcripts: Callable = dict
    placements: Callable = dict
    transcript_path: Callable = lambda sid: None
    history: Callable = list
    major: Callable = lambda: None
    claude: str = ""


class PhoneApi:
    """What the page asks for and does, through emx's own listing, close and rename. It decides nothing about what a state means."""

    def __init__(self, engine, home: str, state, prefs_files, read_routines: Callable = routines.read,
                 wait_running: Callable = sidebar.wait_until_running, sources: PanelSources = PanelSources(),
                 namer: Callable = sidebar.Namer, closer: Callable = sidebar.Closer, changelog: Callable = read_changelog):
        self.engine, self.home, self.state, self.prefs_files = engine, home, Path(state), prefs_files
        self.sources, self.namer, self.closer, self.changelog = sources, namer, closer, changelog
        self.name_lock = threading.Lock()
        self.routines = RoutinesCache(read_routines)
        self.wait_running = wait_running
        self.action_lock = threading.Lock()
        self.count_lock = threading.Lock()
        self.acting = 0

    def busy(self) -> bool:
        """True while a POST runs, so the panel does not relaunch for an update under it."""
        return self.acting > 0

    def get(self, path: str, query: str):
        params = parse_qs(query)
        if path == "/tokens.css":
            return 200, tokens_css().encode(), "text/css; charset=utf-8"
        if path == "/manifest.webmanifest":
            return 200, manifest_json().encode(), MANIFEST_TYPE
        if path in STATIC:
            name, kind = STATIC[path]
            body = (WEB / name).read_bytes()
            if name == "index.html":
                body = body.replace(b"{{ground}}", core.xterm256_hex(CHROME_INDEX["ground"]).encode())
            return 200, body, kind
        first = lambda name: (params.get(name) or [""])[0]
        if path == "/api/state":
            return 200, self.snapshot(first("find"))
        if path == "/api/folders":
            return 200, {"folders": self.folders(first("q"))}
        if path == "/api/routines":
            return 200, self.routines_view()
        if path == "/api/peek":
            return self.peek(first("id"))
        if path == "/api/notifications":
            return 200, self.notifications()
        if path == "/api/whats-new":
            return self.whats_new(first("since"))
        return None

    def post(self, path: str, body: dict):
        actions = {
            "/api/new": self.new, "/api/rename": self.rename, "/api/close": self.close, "/api/resume": self.resume,
            "/api/pin": self.pin, "/api/close-group": self.close_group,
        }
        action = actions.get(path)
        if action is None and path != "/api/suggest-name":
            return None
        with self.count_lock:
            self.acting += 1
        try:
            if action is None:
                return self.suggest_name(body)
            with self.action_lock:
                return action(body)
        finally:
            with self.count_lock:
                self.acting -= 1

    def summaries(self) -> dict:
        try:
            with open(self.state / sidebar.SUMMARIES_FILE, encoding="utf-8") as fh:
                return core.summaries_from(json.load(fh))
        except (OSError, ValueError):
            return {}

    def waits_to_trust(self, session, pane) -> bool:
        if session.state != "starting" or pane is None:
            return False
        try:
            return TRUST_QUESTION.search(self.engine.tmux.capture(pane.id)) is not None
        except TmuxError:
            return False

    def row(self, s, panes, summaries, transcripts: dict, now_ms: float, placement=None, pinned: bool = False) -> dict:
        pane = core.live_viewer(panes, s.id) if s.running else None
        link = core.app_link(bridge_link(pane.pid, s.session_id)) if pane is not None and pane.pid > 1 else None
        stopped = core.category(s) == core.STOPPED
        if stopped:
            detail = ""
        elif self.waits_to_trust(s, pane):
            detail = TRUST_DETAIL
        else:
            detail = core.row_detail(s, transcripts.get(s.id), summaries.get(s.id))
        return {
            "id": s.id, "name": s.name, "word": core.state_word(s), "tone": core.session_tone(s),
            "glyph": core.session_glyph(s, STILL_FRAME), "glyph_tone": core.glyph_tone(s), "spin": core.spins(s),
            "stopped": stopped, "status": not stopped, "detail": detail if detail.strip() else "",
            "age": core.session_age(s, now_ms), "running": s.running, "link": link, "app": core.app_line(AGENT, link),
            "worktree": placement.worktree if placement is not None else "", "pinned": pinned,
        }

    def grouped(self, sessions, find: str, prefs: dict) -> list:
        """The panel's groups for the sessions that match `find`: by repository, Pinned on top."""
        shown = core.find_matches(sessions, find, self.home)
        return core.group_sessions(shown, self.sources.placements(), core.PROJECT, core.pinned_ids(prefs))

    def snapshot(self, find: str = "") -> dict:
        find = find.strip()[:FIND_MAX]
        sessions, error = self.engine.list_sessions()
        if sessions is None:
            return {"error": error, "counts": [], "groups": [], "unread": 0, "version": emx.__version__}
        try:
            panes = self.engine.tmux.panes()
        except TmuxError:
            panes = []
        prefs = core.load_prefs(self.prefs_files)
        placements = self.sources.placements()
        pinned = core.pinned_ids(prefs)
        summaries, transcripts, now_ms = self.summaries(), self.sources.transcripts(), time.time() * 1000
        panel_groups, groups = self.grouped(sessions, find, prefs), []
        for g in panel_groups:
            start = core.default_folder(("group", g.key), panel_groups, sessions, core.PROJECT, prefs.get("last_folder", ""))
            groups.append({
                "key": g.key, "pinned": g.key == core.PINNED, "closable": core.group_closable(core.PROJECT, g.key),
                "label": core.PINNED_LABEL if g.key == core.PINNED else core.tilde_path(g.key, self.home),
                "base": core.PINNED_LABEL if g.key == core.PINNED else os.path.basename(g.key.rstrip("/")) or g.key,
                "new_folder": core.tilde_path(start, self.home) if start else None,
                "sessions": [
                    self.row(s, panes, summaries, transcripts, now_ms, placements.get(s.cwd), s.id in pinned) for s in g.sessions
                ],
            })
        return {
            "error": None, "finding": bool(find), "version": emx.__version__,
            "look": {"frames": core.WORKING_FRAMES, "frame_ms": round(sidebar.FRAME_SECONDS * 1000),
                     "fold": [core.UNFOLDED, core.FOLDED], "closing": core.CLOSING_LABEL, "worktree_mark": core.WORKTREE_MARK,
                     "find_none": FIND_NONE, "open_gone": GONE},
            "counts": [{"key": COUNT_WORDS[k], "number": n, "tone": core.BAR_COUNT_TONES[k]} for k, n in core.bar_counts(sessions)],
            "unread": core.history_unread(self.sources.history()),
            "groups": groups,
        }

    def known_folders(self, sessions) -> tuple:
        """(folders with sessions, recent, favourites) as the panel's picker takes them; sessions newest first."""
        prefs = core.load_prefs(self.prefs_files)
        newest = sorted(sessions or [], key=lambda s: -(s.started_at or 0.0))
        return (
            core.session_folders(newest, [], core.PROJECT),
            prefs.get("recent_folders", []), core.favourite_folders(prefs),
        )

    def allowed_roots(self, sessions) -> list:
        """Every folder the page may start in or below: the picker's three sources and the repository each session's folder belongs to, so a worktree's header can start a session in its repo."""
        placements = self.sources.placements()
        repos = [placements[s.cwd].repo for s in sessions or [] if s.cwd in placements]
        return [*(f for group in self.known_folders(sessions) for f in group), *repos]

    def folders(self, text: str) -> list:
        """Up to eight folders for the typed text, found as the panel's picker finds them (a bare word matches any part of a path), minus any the page may not use."""
        sessions, _ = self.engine.list_sessions()
        in_use, recent, favourites = self.known_folders(sessions)
        known = self.allowed_roots(sessions)
        roots = core.repo_roots(in_use + recent, self.sources.placements())
        text = text.strip()[:200]
        if "$" in text:
            return []
        listed = {}
        for folder in core.folders_to_list(text, in_use, recent, self.home, roots):
            if "/" in text and not allowed_folder(folder, self.home, known):
                continue
            names = sidebar.list_subdirs(folder)
            if names is not None:
                listed[folder] = names
        rows = core.folder_suggestions(
            text, in_use, recent, listed, self.home, limit=FOLDER_CANDIDATES, roots=roots, favourites=favourites,
        )
        shown = [r for r in rows if allowed_folder(r.path, self.home, known) and os.path.isdir(r.path)]
        return [
            {"path": r.path, "shown": r.shown, "base": os.path.basename(r.path.rstrip("/")) or r.path}
            for r in shown[:FOLDER_ROWS]
        ]

    def listed(self, sid: str):
        sessions, _ = self.engine.list_sessions()
        return core.checked_row(sessions, sid)

    def resolve(self, text: str) -> str:
        """The folder typed, with `~` and relative paths taken from home. Variables are not expanded: the server's environment is not his to read through a folder field."""
        text = text.strip()
        if text == "~" or text.startswith("~/"):
            text = self.home.rstrip("/") + text[1:]
        return os.path.normpath(os.path.join(self.home, text))

    def new(self, body: dict) -> tuple:
        text = lambda key: body.get(key) if isinstance(body.get(key), str) else ""
        name, message = text("name").strip(), text("message")
        if not text("folder").strip():
            return 200, {"ok": False, "message": "Pick a folder."}
        folder = self.resolve(text("folder"))
        sessions, _ = self.engine.list_sessions()
        if not os.path.isdir(folder):
            return 200, {"ok": False, "message": "That isn't a folder on this machine."}
        if not allowed_folder(folder, self.home, self.allowed_roots(sessions)):
            return 200, {"ok": False, "message": "emx only starts sessions in your home folder or in folders it already knows."}
        if not all(core.name_char_allowed(c) for c in name):
            return 200, {"ok": False, "message": "A name can't hold control characters or line breaks."}
        if core.typed_length(name) > core.RENAME_MAX:
            return 200, {"ok": False, "message": f"A name can be at most {core.RENAME_MAX} characters."}
        started, error = self.engine.start(folder, message, name)
        if started is None:
            return 200, {"ok": False, "message": error or "Couldn't start the session."}
        session, error = self.wait_running(self.engine, started.id)
        if session is None:
            return 200, {"ok": False, "id": started.id, "message": error}
        try:
            self.update_prefs(lambda prefs: (core.remember_folder(prefs, folder), None))
        except OSError:
            pass
        return 200, {"ok": True, "id": started.id, "message": f"Started {started.name}."}

    def rename(self, body: dict) -> tuple:
        sid, name = body.get("id"), body.get("name")
        if not core.is_session_id(sid) or not isinstance(name, str):
            return 400, {"ok": False, "message": "That is not a session of emx's."}
        session, fresh = self.listed(sid)
        if not fresh or session is None:
            return 200, {"ok": False, "message": "That session isn't listed any more."}
        name = name.strip()
        rename, error = core.rename_request(sid, session.name, core.test_prefixed(name) if name else name)
        if error:
            return 200, {"ok": False, "message": error}
        if rename is None:
            return 200, {"ok": True, "message": "Unchanged."}
        done, message, _ = sidebar.Renamer(self.engine, self.engine.tmux, rename, None).attempt()
        return 200, {"ok": done, "message": message}

    def close(self, body: dict) -> tuple:
        sid, running = body.get("id"), body.get("running")
        if not core.is_session_id(sid) or type(running) is not bool:
            return 400, {"ok": False, "message": "That is not a session of emx's."}
        session, fresh = self.listed(sid)
        if not fresh or session is None:
            return 200, {"ok": False, "message": "That session isn't listed any more."}
        confirm = core.Confirm(sid, session.name, running, time.time())
        closer = sidebar.Closer(self.engine, (confirm,), clear_dead_panes=True)
        closed, message = closer.close_one(confirm)
        return 200, {"ok": closed, "message": message}

    def resume(self, body: dict) -> tuple:
        sid = body.get("id")
        if not core.is_session_id(sid):
            return 400, {"ok": False, "message": "That is not a session of emx's."}
        session, _ = self.listed(sid)
        if session is None:
            return 200, {"ok": False, "message": "That session isn't listed any more."}
        try:
            held = core.live_or_dead_agent(self.engine.tmux.panes(), sid)
        except TmuxError as exc:
            return 200, {"ok": False, "message": core.error_line(exc)}
        if session.running or (held is not None and not held.dead):
            return 200, {"ok": True, "message": f"{session.name} is already running."}
        if not core.attach_allowed(session, core.OPEN, True):
            return 200, {"ok": False, "message": core.refusal(session.name, core.OPEN, True, session)}
        try:
            _, error = sidebar.resume_pane(self.engine, self.engine.tmux, sid, session.cwd or None, held)
        except TmuxError as exc:
            return 200, {"ok": False, "message": core.error_line(exc)}
        if error:
            return 200, {"ok": False, "message": error}
        running, _ = self.wait_running(self.engine, sid)
        if running is None:
            return 200, {"ok": False, "message": f"emx resumed {session.name}, but it isn't listed running yet."}
        return 200, {"ok": True, "message": f"Resumed {session.name}."}

    def update_prefs(self, change: Callable):
        """`change(prefs)` returns (new prefs, answer); it is read, changed and written under the lock the panel's own saves hold, so neither drops the other's change."""
        result = []

        def apply(prefs):
            new, answer = change(prefs)
            result.append(answer)
            return new

        with sidebar.PREFS_LOCK:
            core.change_prefs(self.prefs_files, apply)
        return result[0]

    def peek(self, sid: str) -> tuple:
        if not core.is_session_id(sid):
            return 400, {"ok": False, "message": NOT_A_SESSION}
        session, _ = self.listed(sid)
        if session is None:
            return 200, {"ok": False, "message": GONE}
        peek = core.peek_content(session, self.sources.transcripts().get(sid), time.time() * 1000)
        cut = lambda text: text if len(text) <= PEEK_TEXT_MAX else text[:PEEK_TEXT_MAX - 1] + "…"
        return 200, {
            "ok": True, "title": peek.title,
            "tone": "muted" if core.category(session) == core.STOPPED else core.session_tone(session),
            "lines": [{"text": cut(text), "tone": sidebar.PEEK_TONES[style], "dim": style == "dim", "code": style == "code"}
                      for text, style in peek.lines],
        }

    def pin(self, body: dict) -> tuple:
        sid = body.get("id")
        if not core.is_session_id(sid):
            return 400, {"ok": False, "message": NOT_A_SESSION}
        session, _ = self.listed(sid)
        if session is None:
            return 200, {"ok": False, "message": GONE}

        def toggle(prefs):
            new = core.toggle_pin(prefs, sid)
            return new, (core.pin_notice(new, session), sid in core.pinned_ids(new))

        try:
            message, pinned = self.update_prefs(toggle)
        except OSError as exc:
            return 200, {"ok": False, "message": f"Couldn't save settings: {exc}"}
        return 200, {"ok": True, "pinned": pinned, "message": message}

    def close_group(self, body: dict) -> tuple:
        key, find, sent = body.get("key"), body.get("find"), body.get("sessions")
        valid = (
            isinstance(key, str) and isinstance(find, str) and len(find) <= FIND_MAX
            and isinstance(sent, list) and 0 < len(sent) <= GROUP_MAX
            and all(isinstance(e, dict) and core.is_session_id(e.get("id")) and type(e.get("running")) is bool for e in sent)
        )
        if not valid or len({e["id"] for e in sent}) != len(sent):
            return 400, {"ok": False, "message": NOT_A_SESSION}
        if not core.group_closable(core.PROJECT, key):
            return 200, {"ok": False, "message": PINNED_FOLDER_REFUSAL}
        sessions, error = self.engine.list_sessions()
        if sessions is None:
            return 200, {"ok": False, "message": error or "Couldn't list sessions."}
        group = next((g for g in self.grouped(sessions, find, core.load_prefs(self.prefs_files)) if g.key == key), None)
        if group is None:
            return 200, {"ok": False, "message": FOLDER_GONE}
        if {s.id for s in group.sessions} != {e["id"] for e in sent}:
            return 200, {"ok": False, "message": FOLDER_CHANGED}
        now = time.time()
        running = {e["id"]: e["running"] for e in sent}
        confirm = core.GroupConfirm(key, tuple(core.Confirm(s.id, s.name, running[s.id], now) for s in group.sessions), now)
        if not core.confirm_holds(confirm, ("group", key), group, now):
            return 200, {"ok": False, "message": FOLDER_CHANGED}
        closed, message, _ = self.closer(self.engine, core.close_targets(confirm), clear_dead_panes=True).attempt({})
        return 200, {"ok": closed, "message": message}

    def suggest_name(self, body: dict) -> tuple:
        sid, offered = body.get("id"), body.get("offered")
        valid = (
            core.is_session_id(sid) and isinstance(offered, list) and len(offered) <= core.NAME_OFFERED_MAX
            and all(isinstance(o, str) and len(o) <= NAME_OFFERED_TEXT_MAX for o in offered)
        )
        if not valid:
            return 400, {"ok": False, "message": NOT_A_SESSION}
        session, _ = self.listed(sid)
        if session is None:
            return 200, {"ok": False, "message": GONE}
        if not self.name_lock.acquire(blocking=False):
            return 200, {"ok": False, "message": NAME_BUSY}
        try:
            try:
                namer = self.namer(self.sources.claude, self.sources.transcript_path(sid), sid, session.name, core.unnamed(session), offered)
                name, error, _ = namer.ask()
            except Exception as exc:
                name, error = None, f"asking for a name failed: {core.error_line(exc)}"
        finally:
            self.name_lock.release()
        if name:
            return 200, {"ok": True, "name": name}
        return 200, {"ok": False, "message": NAME_UNFIT_PAGE if error == sidebar.NAME_UNFIT else error}

    def notifications(self) -> dict:
        history = self.sources.history()
        live = {s.id for s in (self.engine.list_sessions()[0] or [])}
        now = time.monotonic()
        return {
            "title": core.HISTORY_TITLE, "empty": sidebar.HISTORY_EMPTY, "unread": core.history_unread(history),
            "entries": [
                {"id": e.id, "title": e.title, "text": e.text, "mark": core.HISTORY_MARKS.get(e.kind, "·"),
                 "tone": sidebar.CATEGORY_TONE.get(e.kind, "plain"), "age": core.format_age(now - e.at),
                 "read": e.read, "listed": e.id in live}
                for e in history
            ],
        }

    def whats_new(self, since: str) -> tuple:
        if since and core.parse_version(since) is None:
            return 400, {"ok": False, "message": "That is not a version."}
        version = emx.__version__
        due = core.whats_new_due(since or None, version)
        major = self.sources.major()
        text = core.whats_new_since(self.changelog(), since, version) if due == core.SHOW_NEW else ""
        return 200, {
            "ok": True, "due": due, "show": bool(text), "record": due is not None and not text,
            "version": version, "heading": core.updated_heading(version), "text": text,
            "major": major, "major_line": core.major_line(major) if major else "",
        }

    def routines_view(self) -> dict:
        views, error = self.routines.get()
        now, out = time.time(), []
        for view in views or []:
            runs, upcoming = core.routine_words(view, now)
            out.append({
                "name": view.name, "tag": view.tag, "tone": view.tone, "error": view.error,
                "runs": runs, "next": upcoming or view.note,
            })
        return {"routines": out, "error": error, "title": sidebar.ROUTINES_TITLE, "none": sidebar.ROUTINES_NONE}


class PhoneServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple, handler, node: Node, tailnet, api, peer_uid: Callable = None):
        super().__init__(address, handler)
        self.node, self.tailnet, self.api = node, tailnet, api
        self.peer_uid = peer_uid or (lambda peer, server: local_peer_uid(peer, server, read_proc_net()))

    def handle_error(self, request, client_address) -> None:
        """The panel owns the terminal; nothing is printed."""


class Handler(BaseHTTPRequestHandler):
    server_version = "emx"
    timeout = REQUEST_TIMEOUT
    body: dict

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format, *args) -> None:
        pass

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def reply(self, status: int, data=None, body: bytes | None = None, kind: str = "application/json") -> None:
        if body is None:
            body = json.dumps(data if data is not None else {"ok": False}).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def refuse(self, status: int) -> int:
        self.close_connection = True
        return status

    def hosts(self) -> set:
        node, port = self.server.node, self.server.server_address[1]
        return {f"{name}:{port}" for name in (node.ip, node.dns_name, node.short_name)}

    def check(self, method: str) -> int | None:
        """The status to refuse the request with, or None to serve it. In order: who the peer is, which Host, then for a POST its origin, type, size and body."""
        node, tailnet = self.server.node, self.server.tailnet
        peer = self.client_address[:2]
        if tailnet.peer_user(peer[0]) != node.user_id:
            return self.refuse(403)
        if peer[0] == node.ip and self.server.peer_uid(peer, self.server.server_address[:2]) != os.getuid():
            return self.refuse(403)
        host = (self.headers.get("Host") or "").lower()
        name, _, port = host.rpartition(":")
        if (name.removesuffix(".") + ":" + port) not in self.hosts():
            return self.refuse(400)
        if method not in ("GET", "POST"):
            return self.refuse(405)
        if method == "GET":
            return None
        return self.check_post(host)

    def check_post(self, host: str) -> int | None:
        if (self.headers.get("Origin") or "").lower() != "http://" + host:
            return self.refuse(403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.refuse(415)
        if self.headers.get("Transfer-Encoding") is not None:
            return self.refuse(400)
        length = self.headers.get("Content-Length")
        if length is None:
            return self.refuse(411)
        if not length.isascii() or not length.isdigit():
            return self.refuse(400)
        if int(length) > MAX_BODY:
            return self.refuse(413)
        try:
            body = json.loads(self.rfile.read(int(length)))
        except (ValueError, RecursionError):
            return self.refuse(400)
        if not isinstance(body, dict):
            return self.refuse(400)
        self.body = body
        return None

    def dispatch(self) -> None:
        refused = self.check(self.command)
        if refused is not None:
            self.reply(refused, {"ok": False, "message": "Refused."})
            return
        path, _, query = self.path.partition("?")
        if self.command == "GET":
            answer = self.server.api.get(path, query)
        else:
            answer = self.server.api.post(path, self.body)
        if answer is None:
            self.reply(404, {"ok": False, "message": "Not found."})
        elif len(answer) == 3:
            status, body, kind = answer
            self.reply(status, body=body, kind=kind)
        else:
            status, data = answer
            self.reply(status, data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = dispatch


BUSY = "busy"
DEFAULT_PORT = core.DEFAULT_PHONE_PORT
PHONE_FILE = "phone.json"
CHECK_SECONDS = 30
BIND_TRIES = 3
BIND_WAIT_SECONDS = 1.0
PROBLEM_NOTICE_SECONDS = 15
PROBLEM_LINES = {
    MISSING: "Phone page off: Tailscale isn't installed here. emx docs phone says how to set it up.",
    DOWN: "Phone page off: Tailscale isn't connected. Run: sudo tailscale up",
    TAGGED: "Phone page off: this machine is a tagged Tailscale node, so emx can't tell it is yours.",
    BUSY: "Phone page off: port {port} is taken. Set another with c in the panel (Port), or in emx config.",
}


def phone_port(prefs=None) -> int:
    return core.phone_port(prefs or {})


def problem_line(problem: str, port: int) -> str:
    return PROBLEM_LINES[problem].format(port=port)


def read_phone_file(state) -> dict:
    try:
        with open(Path(state) / PHONE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class PhoneService(threading.Thread):
    """Serves the page while Tailscale is up, from a thread of the primary panel: checks every `CHECK_SECONDS`, binds this node's Tailscale IPv4 when it can, stops when Tailscale goes away or changes.

    Each change in what it serves, or why it doesn't, is written to
    `phone.json` and queued as one notice line for the panel, unless that file
    already said the same (a relaunched panel does not announce it again).
    """

    def __init__(self, engine, port: int, state, home: str, prefs_files, tailnet=None, interval: float = CHECK_SECONDS,
                 sleep: Callable = time.sleep, sources: PanelSources | None = None):
        super().__init__(daemon=True)
        self.port, self.state, self.interval, self.sleep = port, Path(state), interval, sleep
        self.tailnet = tailnet or Tailnet()
        self.api = PhoneApi(engine, home, state, prefs_files, sources=sources or PanelSources())
        self.notices, self.lock = [], threading.Lock()
        self.stopping = threading.Event()
        self.server, self.node, self.recorded = None, None, None

    def busy(self) -> bool:
        return self.api.busy()

    def take_notice(self) -> tuple | None:
        """(line, seconds it stays up or None for until replaced), oldest first. A problem is said for a while; the address stays until another notice replaces it, since nobody may be attached when it first appears."""
        with self.lock:
            return self.notices.pop(0) if self.notices else None

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.stopping.wait(self.interval)
        self.stop_serving()

    def stop(self) -> None:
        self.stopping.set()

    def tick(self) -> None:
        node, problem = self.tailnet.node()
        if node is None:
            self.stop_serving()
            self.record(None, problem)
            return
        if self.server is not None and node != self.node:
            self.stop_serving()
        if self.server is None:
            self.bind(node)

    def bind(self, node: Node) -> None:
        server = None
        for attempt in range(BIND_TRIES):
            try:
                server = PhoneServer((node.ip, self.port), Handler, node, self.tailnet, self.api)
                break
            except OSError:
                if attempt + 1 < BIND_TRIES:
                    self.sleep(BIND_WAIT_SECONDS)
        if server is None:
            self.record(None, BUSY)
            return
        self.server, self.node = server, node
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        self.record(f"http://{node.dns_name}:{server.server_address[1]}/", None)

    def stop_serving(self) -> None:
        server, self.server, self.node = self.server, None, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def record(self, url: str | None, problem: str | None) -> None:
        if (url, problem) == self.recorded:
            return
        self.recorded = (url, problem)
        said = read_phone_file(self.state)
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            tmp = self.state / (PHONE_FILE + ".tmp")
            tmp.write_text(json.dumps({"url": url, "problem": problem}), encoding="utf-8")
            os.replace(tmp, self.state / PHONE_FILE)
        except OSError:
            pass
        if (said.get("url"), said.get("problem")) == (url, problem):
            return
        with self.lock:
            self.notices.append((f"Phone page: {url}", None) if url else (problem_line(problem, self.port), PROBLEM_NOTICE_SECONDS))
