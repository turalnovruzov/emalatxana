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
from typing import Callable, NamedTuple

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
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
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
        if host not in self.hosts():
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
