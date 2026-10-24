import contextlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import emx
from emx import core, engine, phone, sidebar
from emx.tmux import Pane, TmuxError

FIXTURES = Path(__file__).parent / "fixtures" / "tailscale"
OWNER = 4242
NODE = phone.Node("127.0.0.1", OWNER, "devbox.example-tailnet.ts.net", "devbox")


def setUpModule():
    tmp = os.path.realpath(tempfile.mkdtemp())
    unittest.addModuleCleanup(shutil.rmtree, tmp, True)
    patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(tmp, "config"), "XDG_STATE_HOME": os.path.join(tmp, "state")})
    patch.start()
    unittest.addModuleCleanup(patch.stop)
    os.environ.pop("EMX_CONFIG_HOME", None)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class StubTailnet:
    def __init__(self, user=OWNER):
        self.user = user

    def peer_user(self, ip):
        return self.user


class StubApi:
    def __init__(self):
        self.posts = []

    def get(self, path, query):
        if path == "/api/state":
            return 200, {"ok": True, "state": "secret-session-data"}
        return phone.PhoneApi.get(self, path, query) if not path.startswith("/api/") else None

    def post(self, path, body):
        self.posts.append((path, body))
        return (200, {"ok": True}) if path == "/api/close" else None


class ServerCase(unittest.TestCase):
    tailnet = None
    uid = None

    def setUp(self):
        self.api = StubApi()
        self.tailnet = self.tailnet or StubTailnet()
        self.server = phone.PhoneServer(("127.0.0.1", 0), phone.Handler, NODE, self.tailnet, self.api, self.peer_uid)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def peer_uid(self, peer, server):
        return os.getuid() if self.uid is None else self.uid

    def request(self, method="GET", path="/api/state", body=None, headers=None, host=None, origin=True, raw=None):
        send = {"Host": host or f"127.0.0.1:{self.port}"}
        if method == "POST":
            send["Content-Type"] = "application/json"
            if origin:
                send["Origin"] = "http://" + send["Host"]
        send.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in send.items():
            if value is not None:
                conn.putheader(name, value)
        if data is not None and "Content-Length" not in send:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        response = conn.getresponse()
        self.addCleanup(conn.close)
        return response.status, response.read(), response


class Guard(ServerCase):
    def test_the_owner_gets_the_state(self):
        status, body, _ = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"secret-session-data", body)

    def test_another_tailscale_user_is_refused_with_no_session_data(self):
        self.tailnet = StubTailnet(user=7777)
        self.setUp()
        status, body, _ = self.request()
        self.assertEqual(status, 403)
        self.assertNotIn(b"secret", body)

    def test_a_peer_whois_cannot_place_is_refused(self):
        self.tailnet = StubTailnet(user=None)
        self.setUp()
        self.assertEqual(self.request()[0], 403)

    def test_a_peer_on_this_machine_must_be_this_unix_user(self):
        self.uid = os.getuid() + 1
        self.setUp()
        self.assertEqual(self.request()[0], 403)
        self.uid = os.getuid()
        self.setUp()
        self.assertEqual(self.request()[0], 200)

    def test_a_peer_on_this_machine_with_no_socket_row_is_refused(self):
        self.uid = -1
        self.setUp()
        self.assertEqual(self.request()[0], 403)

    def test_the_host_must_be_one_of_this_nodes_names(self):
        self.assertEqual(self.request(host="evil.example")[0], 400)
        self.assertEqual(self.request(host=f"evil.example:{self.port}")[0], 400)
        self.assertEqual(self.request(host=None, headers={"Host": None})[0], 400)
        for host in (f"devbox.example-tailnet.ts.net:{self.port}", f"DevBox.Example-Tailnet.TS.net:{self.port}", f"devbox:{self.port}",
                     f"devbox.example-tailnet.ts.net.:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request(host=host)[0], 200)
        self.assertEqual(self.request(host=f"devbox..:{self.port}")[0], 400)

    def test_methods_other_than_get_and_post_are_405(self):
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method)[0], 405)

    def test_an_unknown_path_is_404_only_after_the_guard(self):
        self.assertEqual(self.request(path="/nope")[0], 404)
        self.assertEqual(self.request(path="/nope", host="evil.example")[0], 400)


class WebAppFeel(ServerCase):
    def page(self, path):
        status, body, response = self.request(path=path)
        self.assertEqual(status, 200)
        return body.decode(), response

    def test_the_page_cannot_be_zoomed_and_is_a_home_screen_app(self):
        html, _ = self.page("/")
        viewport = re.search(r'<meta name="viewport" content="([^"]*)"', html).group(1)
        for part in ("width=device-width", "maximum-scale=1", "user-scalable=no", "viewport-fit=cover"):
            self.assertIn(part, viewport)
        for name, value in (("apple-mobile-web-app-capable", "yes"), ("mobile-web-app-capable", "yes"),
                            ("apple-mobile-web-app-status-bar-style", "black-translucent"), ("apple-mobile-web-app-title", "emx"),
                            ("theme-color", core.xterm256_hex(phone.CHROME_INDEX["ground"]))):
            self.assertIn(f'<meta name="{name}" content="{value}">', html)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)

    def test_the_page_file_holds_no_hex_colour(self):
        source = (phone.WEB / "index.html").read_text()
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", source), [])

    def test_the_stylesheet_fixes_the_shell_and_scrolls_only_inside(self):
        css, _ = self.page("/app.css")
        flat = re.sub(r"\s+", " ", css)
        for declaration in ("overflow: hidden", "overscroll-behavior: none", "touch-action: manipulation", "height: 100dvh",
                            "overscroll-behavior: contain", "-webkit-overflow-scrolling: touch", "-webkit-touch-callout: none",
                            "-webkit-user-select: none", "env(safe-area-inset-top)", "env(safe-area-inset-bottom)",
                            "-webkit-tap-highlight-color: transparent"):
            self.assertIn(declaration, flat)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", re.sub(r"rgba\(0, 0, 0, [0-9.]+\)", "", css)))

    def test_every_input_is_at_least_16px_so_focus_does_not_zoom(self):
        css, _ = self.page("/app.css")
        field = re.search(r"\.field \{([^}]*)\}", css).group(1)
        self.assertGreaterEqual(int(re.search(r"font-size: (\d+)px", field).group(1)), 16)
        html, _ = self.page("/")
        controls = re.findall(r"<(?:input|textarea)\b[^>]*>", html)
        self.assertEqual(len(controls), 5)
        for control in controls:
            self.assertRegex(control, r'class="field[ "]')

    def test_the_script_stops_the_pinch_gestures(self):
        js, _ = self.page("/app.js")
        self.assertIn("gesturestart", js)
        self.assertIn("preventDefault", js)

    def test_the_manifest_is_served_as_a_standalone_app_and_only_through_the_guard(self):
        body, response = self.page("/manifest.webmanifest")
        manifest = json.loads(body)
        self.assertEqual((manifest["name"], manifest["display"], manifest["start_url"]), ("emx", "standalone", "/"))
        self.assertEqual(manifest["theme_color"], core.xterm256_hex(phone.CHROME_INDEX["ground"]))
        self.assertTrue(response.getheader("Content-Type").startswith("application/manifest+json"))
        self.assertIn("manifest-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(self.request(path="/manifest.webmanifest", host="evil.example")[0], 400)
        self.tailnet = StubTailnet(user=7777)
        self.setUp()
        self.assertEqual(self.request(path="/manifest.webmanifest")[0], 403)


class Posts(ServerCase):
    def test_a_post_needs_this_pages_own_origin(self):
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.api.posts, [])

    def test_resume_is_refused_without_this_pages_origin(self):
        self.assertEqual(self.request("POST", "/api/resume", {"id": KEY}, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/resume", {"id": KEY}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.api.posts, [])

    def test_a_post_must_be_json(self):
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, headers={"Content-Type": "application/json; charset=utf-8"})[0], 200)
        self.assertEqual(len(self.api.posts), 1)

    def test_a_post_needs_a_content_length_and_no_transfer_encoding(self):
        status, _, _ = self.request("POST", "/api/close", raw=b"{}", headers={"Content-Length": None})
        self.assertEqual(status, 411)
        status, _, _ = self.request("POST", "/api/close", raw=b"0\r\n\r\n", headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)

    def test_a_post_over_the_cap_is_refused_without_being_read(self):
        status, _, _ = self.request("POST", "/api/close", raw=b"{" + b" " * (17 * 1024) + b"}")
        self.assertEqual(status, 413)
        self.assertEqual(self.api.posts, [])

    def test_a_post_body_must_be_a_json_object(self):
        for raw in (b"[1]", b"7", b"nope", b"", b'"x"'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/api/close", raw=raw)[0], 400)

    def test_a_bad_content_length_is_400(self):
        self.assertEqual(self.request("POST", "/api/close", raw=b"{}", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request("POST", "/api/close", raw=b"{}", headers={"Content-Length": "1e3"})[0], 400)

    def test_the_guard_runs_before_any_post_is_read(self):
        self.tailnet.user = 7777
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"})[0], 403)
        self.assertEqual(self.api.posts, [])


class Headers(ServerCase):
    def test_every_response_carries_no_store_and_the_csp(self):
        cases = [self.request(), self.request(host="evil.example"), self.request("PUT"), self.request(path="/nope"),
                 self.request("POST", "/api/close", raw=b"nope")]
        for status, _, response in cases:
            with self.subTest(status=status):
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
                self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
                self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))

    def test_a_response_does_not_name_the_python_version(self):
        _, _, response = self.request()
        self.assertEqual(response.getheader("Server"), "emx")

    def test_nothing_is_written_to_the_terminal(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.request()
            self.request("PUT")
            self.request("POST", "/api/close", raw=b"nope")
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.request("GET", "/", headers={"Host": "evil.example"})
            conn.getresponse().read()
            raw = __import__("socket").create_connection(("127.0.0.1", self.port), timeout=2)
            raw.sendall(b"\x00\x01garbage\r\n\r\n")
            raw.close()
            self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


class FakeRunner:
    """A `tailscale` that answers `status` and `whois` from fixture files."""

    def __init__(self, status="status.json", whois="whois-owner.json", edit=None):
        self.status, self.whois, self.edit, self.calls = status, whois, edit, []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "status":
            return fixture(self.status)
        text = fixture(self.whois)
        return self.edit(text) if self.edit else text


class OverFixtures(unittest.TestCase):
    """The real parse-and-compare chain on the fixtures' JSON, not a stub of it."""

    def serve(self, runner):
        tailnet = phone.Tailnet(run=runner, which=lambda name: "/usr/bin/tailscale")
        node, problem = tailnet.node()
        self.assertIsNone(problem)
        node = node._replace(ip="127.0.0.1")
        server = phone.PhoneServer(("127.0.0.1", 0), phone.Handler, node, tailnet, StubApi(), lambda peer, srv: os.getuid())
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/state", headers={"Host": f"127.0.0.1:{server.server_address[1]}"})
        self.addCleanup(conn.close)
        return conn.getresponse().status

    def test_the_owner_is_let_in(self):
        self.assertEqual(self.serve(FakeRunner()), 200)

    def test_another_user_a_tagged_node_and_a_mismatch_are_refused(self):
        def mismatch(text):
            data = json.loads(text)
            data["Node"]["User"] = 7777
            return json.dumps(data)

        for label, runner in (
            ("other", FakeRunner(whois="whois-other.json")),
            ("tagged", FakeRunner(whois="whois-tagged.json")),
            ("user and profile differ", FakeRunner(edit=mismatch)),
        ):
            with self.subTest(label):
                self.assertEqual(self.serve(runner), 403)


class Parsing(unittest.TestCase):
    def test_status_gives_the_node_with_a_lowercase_name_and_no_trailing_dot(self):
        node, problem = phone.parse_status(fixture("status.json"))
        self.assertEqual((problem, node), (None, phone.Node("100.64.0.10", OWNER, "devbox.example-tailnet.ts.net", "devbox")))

    def test_status_gives_each_problem(self):
        self.assertEqual(phone.parse_status(fixture("status-down.json")), (None, phone.DOWN))
        self.assertEqual(phone.parse_status(fixture("status-no-ip.json")), (None, phone.DOWN))
        self.assertEqual(phone.parse_status(fixture("status-tagged.json")), (None, phone.TAGGED))
        for text in ("", "nope", "[]", "{}", '{"BackendState": "Running"}'):
            with self.subTest(text=text):
                self.assertEqual(phone.parse_status(text), (None, phone.DOWN))

    def test_whois_gives_the_owner_and_nothing_for_a_tagged_node(self):
        self.assertEqual(phone.parse_whois(fixture("whois-owner.json")), OWNER)
        self.assertEqual(phone.parse_whois(fixture("whois-other.json")), 7777)
        self.assertIsNone(phone.parse_whois(fixture("whois-tagged.json")))
        for text in ("", "nope", "{}", '{"Node": {"User": "4242"}, "UserProfile": {"ID": "4242"}}'):
            with self.subTest(text=text):
                self.assertIsNone(phone.parse_whois(text))

    def test_a_missing_tailscale_is_a_problem_not_an_exception(self):
        def gone(args):
            raise FileNotFoundError("tailscale")

        self.assertEqual(phone.Tailnet(run=gone, which=lambda name: "/x").node(), (None, phone.MISSING))
        self.assertEqual(phone.Tailnet(run=gone, which=lambda name: None).node(), (None, phone.MISSING))

    def test_a_failing_tailscale_reads_as_down_and_a_failing_whois_as_nobody(self):
        def fails(args):
            raise subprocess.CalledProcessError(1, "tailscale")

        tailnet = phone.Tailnet(run=fails, which=lambda name: "/x")
        self.assertEqual(tailnet.node(), (None, phone.DOWN))
        self.assertIsNone(tailnet.peer_user("100.64.0.20"))

    def test_whois_answers_are_cached_longer_for_a_known_peer_than_a_refused_one(self):
        now = [0.0]
        runner = FakeRunner()
        tailnet = phone.Tailnet(run=runner, which=lambda name: "/x", clock=lambda: now[0])
        self.assertEqual(tailnet.peer_user("100.64.0.20"), OWNER)
        now[0] = 59
        tailnet.peer_user("100.64.0.20")
        self.assertEqual(len(runner.calls), 1)
        now[0] = 61
        tailnet.peer_user("100.64.0.20")
        self.assertEqual(len(runner.calls), 2)
        runner.whois = "whois-tagged.json"
        now[0] = 200
        self.assertIsNone(tailnet.peer_user("100.64.0.20"))
        now[0] = 204
        tailnet.peer_user("100.64.0.20")
        self.assertEqual(len(runner.calls), 3)
        now[0] = 206
        tailnet.peer_user("100.64.0.20")
        self.assertEqual(len(runner.calls), 4)

    def test_whois_is_never_asked_about_something_that_is_not_an_ip(self):
        runner = FakeRunner()
        tailnet = phone.Tailnet(run=runner, which=lambda name: "/x")
        for text in ("--help", "", "a b", "::1", "1.2.3"):
            self.assertIsNone(tailnet.peer_user(text))
        self.assertEqual(runner.calls, [])


class ProcNet(unittest.TestCase):
    TCP = fixture("proc-net-tcp.txt")
    TCP6 = fixture("proc-net-tcp6.txt")

    def test_an_address_decodes_with_its_words_little_endian_and_the_port_big_endian(self):
        self.assertEqual(phone._proc_address("0100007F:1F90"), ("127.0.0.1", 8080))
        self.assertEqual(phone._proc_address("0000000000000000FFFF00000100007F:8368"), ("127.0.0.1", 0x8368))
        self.assertIsNone(phone._proc_address("C04C0A2A891A0101FF2C8F78B9A93AFE:E3AE"))

    def test_the_uid_is_the_one_owning_the_peers_socket_not_the_servers(self):
        self.assertEqual(phone.local_peer_uid(("127.0.0.1", 0x835C), ("127.0.0.1", 8080), self.TCP), 4321)

    def test_a_mapped_ipv6_socket_is_found_too(self):
        text = self.TCP + self.TCP6
        self.assertEqual(phone.local_peer_uid(("127.0.0.1", 0x8368), ("127.0.0.1", 8080), text), 4322)

    def test_no_matching_row_is_none(self):
        self.assertIsNone(phone.local_peer_uid(("127.0.0.1", 1), ("127.0.0.1", 8080), self.TCP + self.TCP6))
        self.assertIsNone(phone.local_peer_uid(("127.0.0.1", 0x835C), ("127.0.0.1", 9999), self.TCP))
        self.assertIsNone(phone.local_peer_uid(("127.0.0.1", 0x835C), ("127.0.0.1", 8080), ""))
        self.assertIsNone(phone.local_peer_uid(("127.0.0.1", 0x835C), ("127.0.0.1", 8080), "garbage\nmore garbage"))


class ProcGuard(ServerCase):
    def peer_uid(self, peer, server):
        return phone.local_peer_uid(peer, server, ProcNet.TCP)

    def test_the_guard_refuses_a_peer_with_no_row(self):
        self.assertEqual(self.request()[0], 403)


KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def session(n, name, cwd, state, status="", started=1000.0, running=True):
    sid = f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{n * 12}"
    return core.Session(sid, name, cwd, state, status, "", started, running, session_id=sid)


def every_state(folder):
    return [
        session("1", "finished", folder, "done"), session("2", "asking", folder, "blocked"),
        session("3", "paused", folder, "working", "idle"), session("4", "busy", folder, "working"),
        session("5", "ended", folder, "stopped", running=False), session("6", "bg", folder, "working", "background"),
        session("7", "odd", folder, "surprise"), session("8", "booting", folder, "starting"),
        session("9", "cold", folder, "done", running=False),
    ]


class FakeTmux:
    def __init__(self, panes=(), screen=""):
        self.listed, self.screen = list(panes), screen
        self.writes = []
        self.tag_error = False

    def panes(self):
        return self.listed

    def capture(self, pane):
        return self.screen

    def respawn(self, pane, command, cwd=None):
        self.writes.append(("respawn", pane, command, cwd))

    def new_hidden_window(self, command, cwd=None):
        self.writes.append(("window", command, cwd))
        return "%42"

    def set_pane_option(self, pane, name, value):
        if self.tag_error:
            raise TmuxError("no such pane")
        self.writes.append(("tag", pane, name, value))

    def kill_pane(self, pane):
        self.writes.append(("kill", pane))


class FakeEngine:
    def __init__(self, sessions, tmux=None):
        self.sessions, self.tmux = sessions, tmux or FakeTmux()
        self.started = []

    def list_sessions(self):
        return self.sessions, None

    def resume_command(self, sid):
        return f"claude --resume {sid}", None

    def start(self, folder, message, name=""):
        self.started.append((folder, message, name))
        return session("9", name or "new", folder, "starting"), None


class ApiCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.home = os.path.join(self.dir, "home")
        self.state = os.path.join(self.dir, "state")
        self.prefs = core.PrefsFiles(Path(self.dir) / "config", Path(self.dir) / "remembered.json", Path(self.dir) / "prefs.json")
        for folder in ("Workspace/budget-tracker", "Workspace/emalatxana", "other/place"):
            os.makedirs(os.path.join(self.home, folder))
        os.makedirs(self.state)
        env = mock.patch.dict(os.environ, {"EMX_SHOW_TEST_SESSIONS": ""})
        env.start()
        self.addCleanup(env.stop)

    def saved(self):
        return tuple(f.read_bytes() if f.exists() else None for f in (self.prefs.config, self.prefs.state))

    def api(self, engine_, **kwargs):
        kwargs.setdefault("wait_running", lambda eng, sid: (object(), None))
        return phone.PhoneApi(engine_, self.home, self.state, self.prefs, **kwargs)

    def path(self, *parts):
        return os.path.join(self.home, *parts)


class StateView(ApiCase):
    def test_each_kind_of_session_has_emxs_word_and_tone(self):
        folder = self.path("Workspace", "emalatxana")
        sessions = [
            session("1", "finished", folder, "done"), session("2", "asking", folder, "blocked"),
            session("3", "paused", folder, "working", "idle"), session("4", "busy", folder, "working"),
            session("5", "ended", folder, "stopped", running=False),
        ]
        data = self.api(FakeEngine(sessions)).get("/api/state", "")[1]
        (group,) = data["groups"]
        rows = {r["name"]: (r["word"], r["tone"], r["glyph"]) for r in group["sessions"]}
        self.assertEqual(rows, {
            "finished": ("Done", "done", "✻"), "asking": ("Needs input", "needs", "✻"), "paused": ("Idle", "idle", "✻"),
            "busy": ("Working", "plain", "✳"), "ended": ("Stopped", "stopped", "∙"),
        })
        self.assertEqual((group["label"], group["base"]), ("~/Workspace/emalatxana", "emalatxana"))

    def test_each_rows_look_is_the_panels(self):
        folder = self.path("Workspace", "emalatxana")
        sessions = every_state(folder)
        with open(os.path.join(self.state, "summaries.json"), "w") as fh:
            json.dump({s.id: {"text": "Said something", "state": core.summary_state(s, None)} for s in sessions}, fh)
        data = self.api(FakeEngine(sessions)).get("/api/state", "")[1]
        rows = {r["id"]: r for r in data["groups"][0]["sessions"]}
        self.assertEqual(len(rows), len(sessions))
        for s in sessions:
            row = rows[s.id]
            lines = core.row_lines(s.name, "", row["word"], row["detail"], row["age"], 1000, core.category(s) == core.STOPPED)
            with self.subTest(state=s.state, status=s.status, running=s.running):
                self.assertEqual(row["glyph_tone"], core.glyph_tone(s))
                self.assertEqual(row["spin"], core.spins(s))
                self.assertEqual(len(lines) > 1, row["status"])
                if row["status"]:
                    self.assertEqual(lines[1][0][1], row["word"])
                    self.assertEqual(lines[-1][-1][1], row["detail"])
                else:
                    self.assertEqual((row["detail"], row["stopped"]), ("", True))

    def test_a_working_rows_detail_comes_from_its_transcript(self):
        folder = self.path("Workspace", "emalatxana")
        busy = session("4", "busy", folder, "working")
        transcript = core.Transcript(latest="tool", tool="Bash: npm test", tool_at=1.0)
        with open(os.path.join(self.state, "summaries.json"), "w") as fh:
            json.dump({busy.id: {"text": "A Done line", "state": "done"}}, fh)
        api = self.api(FakeEngine([busy]), sources=phone.PanelSources(transcripts=lambda: {busy.id: transcript}))
        row = api.get("/api/state", "")[1]["groups"][0]["sessions"][0]
        self.assertEqual(row["detail"], core.session_summary(busy, transcript))
        self.assertIn("npm test", row["detail"])

    def test_the_look_carries_the_panels_frames_rate_arrows_and_closing_label(self):
        look = self.api(FakeEngine([])).get("/api/state", "")[1]["look"]
        self.assertEqual(look, {"frames": core.WORKING_FRAMES, "frame_ms": round(sidebar.FRAME_SECONDS * 1000),
                                "fold": [core.UNFOLDED, core.FOLDED], "closing": core.CLOSING_LABEL,
                                "worktree_mark": core.WORKTREE_MARK, "find_none": "No session matches.",
                                "open_gone": sidebar.OPEN_GONE})

    def test_counts_are_the_bars_and_leave_out_stopped_sessions(self):
        folder = self.path("Workspace", "emalatxana")
        sessions = [session("1", "a", folder, "done"), session("2", "b", folder, "blocked"), session("3", "c", folder, "working"),
                    session("4", "d", folder, "working"), session("5", "e", folder, "stopped", running=False)]
        counts = self.api(FakeEngine(sessions)).get("/api/state", "")[1]["counts"]
        self.assertEqual([(c["key"], c["number"], c["tone"]) for c in counts],
                         [("needs", 1, "needs"), ("working", 2, "muted"), ("done", 1, "done")])

    def test_sessions_group_by_folder_in_name_order_oldest_first(self):
        a, b = self.path("Workspace", "emalatxana"), self.path("Workspace", "budget-tracker")
        sessions = [session("1", "late", a, "done", started=3000.0), session("2", "other", b, "done"), session("3", "early", a, "done", started=2000.0)]
        groups = self.api(FakeEngine(sessions)).get("/api/state", "")[1]["groups"]
        self.assertEqual([(g["base"], [r["name"] for r in g["sessions"]]) for g in groups],
                         [("budget-tracker", ["other"]), ("emalatxana", ["early", "late"])])

    def test_a_failed_listing_is_an_error_the_page_can_show(self):
        eng = FakeEngine(None)
        eng.list_sessions = lambda: (None, "tmux is not answering")
        self.assertEqual(self.api(eng).get("/api/state", "")[1]["error"], "tmux is not answering")

    def test_a_summary_for_the_rows_state_is_its_detail_and_one_for_another_state_is_not(self):
        folder = self.path("Workspace", "emalatxana")
        sessions = [session("1", "a", folder, "done"), session("2", "b", folder, "blocked")]
        with open(os.path.join(self.state, "summaries.json"), "w") as fh:
            json.dump({sessions[0].id: {"text": "Fixed the rounding", "state": "done"},
                       sessions[1].id: {"text": "Stale line", "state": "done"}}, fh)
        rows = {r["name"]: r["detail"] for r in self.api(FakeEngine(sessions)).get("/api/state", "")[1]["groups"][0]["sessions"]}
        self.assertEqual(rows, {"a": "Fixed the rounding", "b": ""})


class Grouping(ApiCase):
    """The page groups, finds and labels as the panel does: its own core functions over the primary panel's placements."""

    def setUp(self):
        super().setUp()
        self.repo = self.path("Workspace", "emalatxana")
        self.tree = self.path("Workspace", "emalatxana-67")
        os.makedirs(self.tree)
        self.plain = self.path("other", "place")
        self.sessions = [
            session("1", "in-repo", self.repo, "done", started=1000.0),
            session("2", "in-tree", self.tree, "blocked", started=2000.0),
            session("3", "plain", self.plain, "working", started=3000.0),
        ]
        self.placements = {
            self.repo: core.Placement(self.repo), self.tree: core.Placement(self.repo, "feature/67"),
            self.plain: core.Placement(self.plain),
        }

    def api(self, engine_=None, **kwargs):
        kwargs.setdefault("sources", phone.PanelSources(placements=lambda: self.placements))
        return super().api(engine_ or FakeEngine(self.sessions), **kwargs)

    def snapshot(self, query=""):
        return self.api().get("/api/state", query)[1]

    def test_groups_are_the_panels_with_pinned_first(self):
        with open(self.prefs.legacy, "w") as fh:
            json.dump({"pinned": [self.sessions[2].id]}, fh)
        panel = core.group_sessions(self.sessions, self.placements, core.PROJECT, frozenset({self.sessions[2].id}))
        groups = self.snapshot()["groups"]
        self.assertEqual([g["key"] for g in groups], [g.key for g in panel])
        self.assertEqual(groups[0]["key"], core.PINNED)
        self.assertEqual((groups[0]["label"], groups[0]["pinned"]), ("Pinned", True))
        self.assertEqual([r["id"] for r in groups[0]["sessions"]], [self.sessions[2].id])
        self.assertTrue(groups[0]["sessions"][0]["pinned"])
        self.assertEqual([r["name"] for r in groups[1]["sessions"]], ["in-repo", "in-tree"])

    def test_a_worktree_row_carries_its_branch_and_the_mark(self):
        data = self.snapshot()
        rows = {r["name"]: r for g in data["groups"] for r in g["sessions"]}
        self.assertEqual(data["look"]["worktree_mark"], "⎇ ")
        self.assertEqual((rows["in-tree"]["worktree"], rows["in-repo"]["worktree"], rows["plain"]["worktree"]), ("feature/67", "", ""))

    def test_find_filters_as_the_panel_does(self):
        everything = self.snapshot()
        found = self.snapshot("find=emalatxana-6")
        want = [s.id for s in core.find_matches(self.sessions, "emalatxana-6", self.home)]
        self.assertEqual(sorted(r["id"] for g in found["groups"] for r in g["sessions"]), sorted(want))
        self.assertEqual(len(want), 1)
        self.assertTrue(found["finding"])
        self.assertFalse(everything["finding"])
        self.assertEqual(found["counts"], everything["counts"])

    def test_a_find_that_matches_nothing_has_no_groups(self):
        self.assertEqual(self.snapshot("find=zzz")["groups"], [])

    def test_a_header_new_folder_is_default_folder(self):
        with open(self.prefs.legacy, "w") as fh:
            json.dump({"pinned": [self.sessions[2].id], "last_folder": self.tree}, fh)
        by_key = {g["key"]: g for g in self.snapshot()["groups"]}
        self.assertEqual(by_key[self.repo]["new_folder"], "~/Workspace/emalatxana")
        self.assertEqual(by_key[core.PINNED]["new_folder"], "~/Workspace/emalatxana-67")
        self.assertEqual({k: g["closable"] for k, g in by_key.items()}, {self.repo: True, core.PINNED: False})

    def test_pinned_without_a_last_folder_has_no_new_folder(self):
        with open(self.prefs.legacy, "w") as fh:
            json.dump({"pinned": [self.sessions[2].id]}, fh)
        pinned = self.snapshot()["groups"][0]
        self.assertIsNone(pinned["new_folder"])

    def test_the_snapshot_carries_the_unread_count_and_the_version(self):
        history = [core.Entry("a", core.NOTICE_DONE, "t", "x", 1.0), core.Entry("b", core.NOTICE_DONE, "t", "x", 2.0, read=True)]
        api = self.api(sources=phone.PanelSources(placements=lambda: self.placements, history=lambda: history))
        data = api.get("/api/state", "")[1]
        self.assertEqual((data["unread"], data["version"]), (1, emx.__version__))


class RealListing(ApiCase):
    """Over emx's own registry, so the panel's hiding and `starting` rules are the ones the page gets."""

    def record(self, key, name, cwd):
        where = engine.session_folder(Path(self.state), key)
        os.makedirs(where)
        with open(os.path.join(where, "session.json"), "w", encoding="utf-8") as fh:
            json.dump({"key": key, "agent": "claude", "name": name, "cwd": cwd, "session_id": key, "started_at": 1.0, "run_at": 0}, fh)

    def real(self, panes=(), screen=""):
        return engine.Engine(FakeTmux(panes, screen), "claude", Path(self.state))

    def names(self, api):
        return [r["name"] for g in api.get("/api/state", "")[1]["groups"] for r in g["sessions"]]

    def test_a_test_session_in_a_temp_folder_is_hidden_unless_the_flag_is_set(self):
        self.record(KEY, "emx-test-a", tempfile.gettempdir())
        self.record(OTHER, "real", self.path("Workspace"))
        self.assertEqual(self.names(self.api(self.real())), ["real"])
        with mock.patch.dict(os.environ, {"EMX_SHOW_TEST_SESSIONS": "1"}):
            self.assertEqual(sorted(self.names(self.api(self.real()))), ["emx-test-a", "real"])

    def test_a_session_waiting_at_claudes_trust_question_says_so(self):
        self.record(KEY, "new", self.path("Workspace"))
        pane = Pane("%9", "@9", False, "", KEY, 80, 80, agent="claude", pid=0)
        for screen, detail in (("Do you trust the files in this folder?\n 1. Yes, I trust this folder", phone.TRUST_DETAIL),
                               ("Welcome", "")):
            with self.subTest(screen=screen):
                row = self.api(self.real([pane], screen)).get("/api/state", "")[1]["groups"][0]["sessions"][0]
                self.assertEqual((row["word"], row["detail"]), ("Needs input", detail))


class Folders(ApiCase):
    def setUp(self):
        super().setUp()
        self.engine = FakeEngine([session("1", "a", self.path("Workspace", "emalatxana"), "done")])

    def find(self, text):
        return [f["path"] for f in self.api(self.engine).get("/api/folders", "q=" + text)[1]["folders"]]

    def test_any_part_of_a_folder_name_finds_it(self):
        self.assertEqual(self.find("budget")[0], self.path("Workspace", "budget-tracker"))
        self.assertEqual(self.find("Budget")[0], self.path("Workspace", "budget-tracker"))
        for text in ("emx", "EMX", "emalatxana"):
            with self.subTest(text=text):
                self.assertEqual(self.find(text)[0], self.path("Workspace", "emalatxana"))

    def test_a_folder_row_has_its_path_a_tilde_form_and_its_last_part(self):
        (row,) = [f for f in self.api(self.engine).get("/api/folders", "q=budget")[1]["folders"]][:1]
        self.assertEqual((row["shown"], row["base"]), ("~/Workspace/budget-tracker", "budget-tracker"))

    def test_nothing_typed_offers_the_session_folder_first(self):
        self.assertEqual(self.find("")[0], self.path("Workspace", "emalatxana"))

    def test_a_path_outside_home_and_known_folders_lists_nothing(self):
        self.assertEqual(self.find("/etc/"), [])
        self.assertEqual(self.find("/etc/ss"), [])
        for text in ("/", "~/../", "/" + self.dir.split("/")[1] + "/"):
            with self.subTest(text=text):
                self.assertTrue(all(p.startswith(self.home + "/") for p in self.find(text)), self.find(text))

    def test_a_variable_in_the_text_is_never_expanded(self):
        with mock.patch.dict(os.environ, {"EMX_TEST_SECRET": self.home}):
            self.assertEqual(self.find("$EMX_TEST_SECRET/"), [])
            body = self.api(self.engine).post("/api/new", {"folder": "$EMX_TEST_SECRET/Workspace"})[1]
        self.assertFalse(body["ok"])
        self.assertNotIn(self.home, body["message"])

    def test_at_most_eight_folders(self):
        for n in range(12):
            os.makedirs(self.path("Workspace", f"many-{n}"))
        self.assertEqual(len(self.find("many")), 8)

    def test_allowed_folder_is_home_known_folders_and_what_is_below_them(self):
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "sub"))
        self.assertFalse(phone.allowed_folder("/etc", self.home, []))
        self.assertFalse(phone.allowed_folder("/", self.home, ["/"]))
        self.assertTrue(phone.allowed_folder(self.path("Workspace"), self.home, []))
        self.assertFalse(phone.allowed_folder(elsewhere, self.home, []))
        self.assertTrue(phone.allowed_folder(elsewhere, self.home, [elsewhere]))
        self.assertTrue(phone.allowed_folder(os.path.join(elsewhere, "sub"), self.home, [elsewhere]))
        self.assertFalse(phone.allowed_folder(elsewhere + "-sibling", self.home, [elsewhere]))

    def test_a_link_under_home_that_points_outside_is_not_allowed(self):
        os.symlink("/etc", self.path("link"))
        self.assertFalse(phone.allowed_folder(self.path("link"), self.home, []))
        self.assertFalse(phone.allowed_folder(self.path("link", "ssl"), self.home, []))

    def test_a_worktree_repo_root_is_allowed(self):
        repo = os.path.join(self.dir, "elsewhere", "repo")
        tree = os.path.join(self.dir, "elsewhere", "repo-tree")
        os.makedirs(repo)
        os.makedirs(tree)
        placements = {tree: core.Placement(repo, "b")}
        engine_ = FakeEngine([session("2", "t", tree, "done")])
        api = self.api(engine_, sources=phone.PanelSources(placements=lambda: placements))
        status, body = api.post("/api/new", {"folder": repo})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(engine_.started[-1][0], repo)
        found = [f["path"] for f in api.get("/api/folders", "q=repo")[1]["folders"]]
        self.assertIn(repo, found)

    def test_a_recent_folder_outside_home_is_offered_and_allowed(self):
        elsewhere = os.path.join(self.dir, "elsewhere", "proj-x")
        os.makedirs(elsewhere)
        with open(self.prefs.legacy, "w") as fh:
            json.dump({"recent_folders": [elsewhere]}, fh)
        self.assertEqual(self.find("proj-x"), [elsewhere])


class Actions(ApiCase):
    def setUp(self):
        super().setUp()
        self.folder = self.path("Workspace", "emalatxana")
        self.target = session("1", "alpha", self.folder, "done")
        self.engine = FakeEngine([self.target])

    def post(self, path, body):
        return self.api(self.engine).post(path, body)

    def test_new_starts_in_a_folder_the_page_may_use_with_the_name_and_message(self):
        status, body = self.post("/api/new", {"folder": "~/Workspace/budget-tracker", "name": " Budget ", "message": "hi"})
        self.assertEqual((status, body["ok"], body["id"]), (200, True, "9" * 8 + "-9999-4999-8999-" + "9" * 12))
        self.assertEqual(self.engine.started, [(self.path("Workspace", "budget-tracker"), "hi", "Budget")])

    def test_new_refuses_folders_it_may_not_use_and_never_starts_there(self):
        for folder in ("/etc", "/nonexistent-folder", "", "../../etc", "~/../.."):
            with self.subTest(folder=folder):
                body = self.post("/api/new", {"folder": folder, "name": ""})[1]
                self.assertFalse(body["ok"])
        self.assertEqual(self.engine.started, [])

    def test_new_refuses_a_bad_name(self):
        for name in ("a\nb", "a\x1b[0m", "x" * 51):
            with self.subTest(name=name):
                self.assertFalse(self.post("/api/new", {"folder": self.folder, "name": name})[1]["ok"])
        self.assertEqual(self.engine.started, [])

    def test_new_reports_a_session_that_did_not_come_up(self):
        api = self.api(self.engine, wait_running=lambda eng, sid: (None, "Session never ran"))
        body = api.post("/api/new", {"folder": self.folder})[1]
        self.assertEqual((body["ok"], body["message"]), (False, "Session never ran"))

    def test_rename_goes_through_the_renamer_and_keeps_the_test_prefix_under_the_flag(self):
        made = []

        class Renamer:
            def __init__(self, eng, tmux, rename, panel):
                made.append((rename, panel))

            def attempt(self):
                return True, "Renamed.", True

        with mock.patch.object(phone.sidebar, "Renamer", Renamer):
            self.assertEqual(self.post("/api/rename", {"id": self.target.id, "name": "beta"})[1], {"ok": True, "message": "Renamed."})
            with mock.patch.dict(os.environ, {"EMX_SHOW_TEST_SESSIONS": "1"}):
                self.post("/api/rename", {"id": self.target.id, "name": "x"})
        self.assertEqual([(r.id, r.new, panel) for r, panel in made], [(self.target.id, "beta", None), (self.target.id, "emx-test-x", None)])

    def test_rename_refuses_what_the_panel_refuses(self):
        for name in ("", "a\nb", "x" * 51, "ends\\"):
            with self.subTest(name=name):
                self.assertFalse(self.post("/api/rename", {"id": self.target.id, "name": name})[1]["ok"])
        self.assertEqual(self.post("/api/rename", {"id": self.target.id, "name": "alpha"})[1]["message"], "Unchanged.")

    def test_close_and_rename_need_a_session_id_and_close_needs_a_real_running_flag(self):
        for path, body in (
            ("/api/close", {"id": "../x", "running": True}), ("/api/close", {"id": self.target.id, "running": "yes"}),
            ("/api/close", {"id": self.target.id}), ("/api/close", {"id": 5, "running": True}),
            ("/api/rename", {"id": "../x", "name": "n"}), ("/api/rename", {"id": self.target.id, "name": 7}),
        ):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.post(path, body)[0], 400)

    def test_close_goes_through_the_closer_with_what_the_page_showed(self):
        made = []

        class Closer:
            def __init__(self, eng, targets, wait_for=(), panel="", clear_dead_panes=False):
                made.append((targets, clear_dead_panes))

            def close_one(self, confirm):
                return True, f"Closed {confirm.name}."

        with mock.patch.object(phone.sidebar, "Closer", Closer):
            body = self.post("/api/close", {"id": self.target.id, "running": True})[1]
        self.assertEqual(body, {"ok": True, "message": "Closed alpha."})
        ((targets, clear),) = made
        self.assertEqual([(t.id, t.name, t.running) for t in targets], [(self.target.id, "alpha", True)])
        self.assertTrue(clear)

    def stopped_session(self):
        stopped = session("5", "ended", self.folder, "stopped", running=False)
        self.engine = FakeEngine([stopped])
        return stopped

    def test_resume_of_a_stopped_session_with_a_dead_pane_respawns_it_there(self):
        stopped = self.stopped_session()
        self.engine.tmux = FakeTmux([Pane("%7", "@7", True, "", stopped.id, 80, 80, agent="claude")])
        body = self.post("/api/resume", {"id": stopped.id})[1]
        self.assertEqual(body, {"ok": True, "message": "Resumed ended."})
        self.assertEqual(self.engine.tmux.writes, [("respawn", "%7", f"claude --resume {stopped.id}", self.folder)])

    def test_resume_with_no_pane_makes_a_window_tagged_agent_then_key(self):
        stopped = self.stopped_session()
        self.assertTrue(self.post("/api/resume", {"id": stopped.id})[1]["ok"])
        self.assertEqual(self.engine.tmux.writes, [
            ("window", f"claude --resume {stopped.id}", self.folder),
            ("tag", "%42", phone.sidebar.AGENT_OPT, phone.sidebar.AGENT),
            ("tag", "%42", phone.sidebar.SESSION_OPT, stopped.id),
        ])

    def test_resume_never_touches_a_live_pane_or_a_running_session(self):
        stopped = self.stopped_session()
        self.engine.tmux = FakeTmux([Pane("%7", "@7", False, "", stopped.id, 80, 80, agent="claude")])
        body = self.post("/api/resume", {"id": stopped.id})[1]
        self.assertEqual(body, {"ok": True, "message": "ended is already running."})
        self.engine = FakeEngine([self.target])
        self.assertTrue(self.post("/api/resume", {"id": self.target.id})[1]["ok"])
        self.assertEqual(self.engine.tmux.writes, [])
        self.assertEqual(self.engine.tmux.listed, [])

    def test_resume_of_a_session_no_longer_listed_or_a_failed_listing_starts_nothing(self):
        stopped = self.stopped_session()
        self.assertFalse(self.post("/api/resume", {"id": OTHER})[1]["ok"])
        self.engine.list_sessions = lambda: (None, "tmux is not answering")
        self.assertFalse(self.post("/api/resume", {"id": stopped.id})[1]["ok"])
        self.engine.list_sessions = lambda: ([stopped], None)
        self.engine.tmux.panes = mock.Mock(side_effect=TmuxError("gone"))
        self.assertFalse(self.post("/api/resume", {"id": stopped.id})[1]["ok"])
        self.assertEqual(self.engine.tmux.writes, [])

    def test_resume_needs_a_session_id(self):
        for body in ({"id": "../x"}, {}, {"id": 5}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/resume", body)[0], 400)
        self.assertEqual(self.engine.tmux.writes, [])

    def test_resume_reports_one_that_did_not_come_up(self):
        stopped = self.stopped_session()
        api = self.api(self.engine, wait_running=lambda eng, sid: (None, "never"))
        body = api.post("/api/resume", {"id": stopped.id})[1]
        self.assertEqual(body, {"ok": False, "message": "emx resumed ended, but it isn't listed running yet."})

    def test_an_untaggable_window_is_killed(self):
        stopped = self.stopped_session()
        self.engine.tmux.tag_error = True
        body = self.post("/api/resume", {"id": stopped.id})[1]
        self.assertFalse(body["ok"])
        self.assertEqual(self.engine.tmux.writes[-1], ("kill", "%42"))

    def test_close_of_a_session_no_longer_listed_does_nothing(self):
        body = self.post("/api/close", {"id": OTHER, "running": True})[1]
        self.assertFalse(body["ok"])

    def test_an_unknown_post_path_is_not_found(self):
        self.assertIsNone(self.post("/api/nope", {}))

    def test_busy_is_true_only_while_a_post_runs(self):
        api = self.api(self.engine)
        seen = []
        api.rename = lambda body: seen.append(api.busy()) or (200, {})
        api.post("/api/rename", {})
        self.assertEqual((seen, api.busy()), ([True], False))


class Routines(ApiCase):
    NOW = 1_800_000_000.0

    def test_routines_carry_their_tag_tone_runs_and_next_time(self):
        passed = core.RoutineView("backup-nightly", "passed", "done", ((self.NOW - 600, core.PASSED, ""),), self.NOW + 7200)
        failed = core.RoutineView("sync", "failed · exit 2", "error", ((self.NOW - 900, core.FAILED, "2"),), None, note="no calendar schedule")
        api = self.api(FakeEngine([]), read_routines=lambda now: ([passed, failed], None))
        data = api.get("/api/routines", "")[1]
        one, two = data["routines"]
        self.assertEqual((one["name"], one["tag"], one["tone"]), ("backup-nightly", "passed", "done"))
        self.assertRegex(one["runs"][0], r"^\d\d:\d\d ✓$")
        self.assertRegex(one["next"], r"^next ")
        self.assertEqual((two["tone"], two["next"]), ("error", "no calendar schedule"))
        self.assertRegex(two["runs"][0], r"✗2$")
        self.assertIsNone(data["error"])

    def test_the_panels_lines_for_the_same_views_are_unchanged(self):
        view = core.RoutineView("backup-nightly", "passed", "done", ((self.NOW - 600, core.PASSED, ""),), self.NOW + 7200)
        rows = core.routine_lines(view, 40, self.NOW, local=time.gmtime)
        self.assertEqual(rows[1][0], "  " + time.strftime("%H:%M", time.gmtime(self.NOW - 600)) + " ✓")

    def test_an_error_from_the_reader_reaches_the_page(self):
        api = self.api(FakeEngine([]), read_routines=lambda now: (None, "systemd isn't here"))
        self.assertEqual(api.get("/api/routines", "")[1], {
            "routines": [], "error": "systemd isn't here", "title": sidebar.ROUTINES_TITLE, "none": sidebar.ROUTINES_NONE})

    def test_a_reader_that_raises_is_an_error_not_a_crash(self):
        def boom(now):
            raise RuntimeError("no systemctl")

        self.assertEqual(self.api(FakeEngine([]), read_routines=boom).get("/api/routines", "")[1]["error"], "no systemctl")

    def test_the_reader_runs_once_a_minute_not_per_request(self):
        calls = []
        api = self.api(FakeEngine([]), read_routines=lambda now: calls.append(now) or ([], None))
        api.get("/api/routines", "")
        api.get("/api/routines", "")
        self.assertEqual(len(calls), 1)


class Page(ServerCase):
    def setUp(self):
        super().setUp()
        self.server.api = phone.PhoneApi(FakeEngine([]), "/nonexistent-home", "/nonexistent-state", "/nonexistent-prefs")

    def text(self, path):
        status, body, response = self.request(path=path)
        self.assertEqual(status, 200, path)
        return body.decode(), response

    def test_the_page_is_html_that_names_its_script_and_style_and_carries_none_inline(self):
        html, response = self.text("/")
        self.assertTrue(response.getheader("Content-Type").startswith("text/html"))
        self.assertIn('src="/app.js"', html)
        self.assertIn('href="/app.css"', html)
        self.assertIn('href="/tokens.css"', html)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(html, r"<script[^>]*>\s*[^\s<]")
        self.assertNotRegex(html, r"\sstyle=")
        self.assertNotRegex(html, r"\son[a-z]+=")

    def test_the_tokens_are_the_panels_colours(self):
        css, response = self.text("/tokens.css")
        self.assertTrue(response.getheader("Content-Type").startswith("text/css"))
        for want in ("--needs:#ffd75f", "--done:#87d787", "--idle:#8787af", "--stopped:#626262", "--muted:#8a8a8a",
                     "--accent:#5fafff", "--error:#ff5f5f", "--copper:#d7875f", "--ground:#1c1c1c", "--selected:#303030"):
            self.assertIn(want, css)

    def test_the_stylesheet_has_no_colour_literal_of_its_own(self):
        css, _ = self.text("/app.css")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,6}\b", css), [])
        self.assertEqual(re.findall(r"\b(?:rgb|hsl)a?\((?!0, 0, 0, 0\.55\))", css), [])
        used = set(re.findall(r"var\(--([a-z]+)\)", css))
        defined = set(re.findall(r"--([a-z]+):", self.text("/tokens.css")[0]))
        self.assertLessEqual(used, defined)

    def test_the_stopped_row_has_no_detail_line_and_the_close_window_matches_the_panel(self):
        css, _ = self.text("/app.css")
        js, _ = self.text("/app.js")
        self.assertNotIn(".stopped .detail-text", css)
        self.assertEqual(int(re.search(r"CONFIRM_MS = (\d+)", js).group(1)), int(core.CONFIRM_SECONDS * 1000))

    def test_the_script_holds_no_state_rule_of_its_own(self):
        js, _ = self.text("/app.js")
        for mark in "✢✳✶✻∙▸▾":
            self.assertNotIn(mark, js)
        self.assertNotIn("CLOSING_LABEL", js)
        self.assertNotIn(core.CLOSING_LABEL, js)
        self.assertEqual(re.findall(r'"(?:stopped|working|done|needs|idle)"', js), [])

    def test_the_glyph_tone_is_data_not_a_css_special_case(self):
        css, _ = self.text("/app.css")
        self.assertNotRegex(css, r"\.glyph\.tone-")
        folder = "/w"
        tones = {core.glyph_tone(s) for s in every_state(folder)}
        self.assertEqual(tones, {"idle", "muted", "needs", "done", "stopped"})
        for tone in tones:
            self.assertRegex(css, r"\.tone-%s\b" % tone)

    def test_every_tone_class_the_script_can_write_has_a_css_rule(self):
        css, _ = self.text("/app.css")
        js, _ = self.text("/app.js")
        written = set(re.findall(r'"tone-([a-z]+)', js))
        sent = set(core.CATEGORY_TONE.values()) | {core.glyph_tone(s) for s in every_state("/w")} | {"path", "error", "muted", "accent", "needs", "idle"}
        for tone in written | sent:
            self.assertRegex(css, r"\.tone-%s\b[^{]*\{[^}]*color: var\(--%s\)" % (tone, tone), tone)

    def test_the_branch_label_is_written_as_text_and_only_the_branch_truncates(self):
        css, _ = self.text("/app.css")
        js, _ = self.text("/app.js")
        rule = lambda selector: re.search(re.escape(selector) + r" \{([^}]*)\}", css).group(1)
        self.assertNotIn("flex: 1", rule(".row .nameline .name"))
        self.assertIn("flex: 1 1 0", rule(".row .nameline .branch"))
        self.assertIn("text-overflow: ellipsis", rule(".row .nameline .branch"))
        self.assertRegex(js, r'"branch tone-path", glyphText\(')
        self.assertEqual(len(re.findall(r'"more", glyphText\("⋯"\)', js)), 2)

    def test_the_script_respects_reduced_motion(self):
        js, _ = self.text("/app.js")
        self.assertIn("prefers-reduced-motion", js)
        css, _ = self.text("/app.css")
        self.assertIn("prefers-reduced-motion", css)

    def test_the_new_controls_are_at_least_44px_and_stars_are_text_glyphs(self):
        css, _ = self.text("/app.css")
        js, _ = self.text("/app.js")
        for selector in (r"\.star", r"\.folder-bar \.more", r"\.findbar \.plain-button"):
            rule = re.search(selector + r" \{([^}]*)\}", css).group(1)
            self.assertRegex(rule, r"(?:min-)?(?:width|height): 44px")
        self.assertRegex(js, r'glyphText\(folder\.favourite \? "★" : "☆"\)')

    def test_the_script_holds_no_grouping_rule(self):
        js, _ = self.text("/app.js")
        self.assertNotIn(":pinned", js)
        self.assertNotIn("Pinned", js)

    def test_the_pull_listens_without_passive_and_only_on_the_list(self):
        js, _ = self.text("/app.js")
        moves = re.findall(r'(\S+)\.addEventListener\("touchmove", [^;]*;', js)
        self.assertEqual(moves, ['$("list")'])
        self.assertRegex(js, r'\$\("list"\)\.addEventListener\("touchmove", \w+, \{ passive: false \}\)')

    def test_the_sheet_has_resume(self):
        html, _ = self.text("/")
        self.assertRegex(html, r'<button class="item accent" id="sheet-resume"[^>]*hidden>Resume</button>')

    def test_glyphs_are_written_as_text_not_emoji(self):
        js, _ = self.text("/app.js")
        function = re.search(r"function glyphText\(\w+\) \{(.*?)\n\}", js, re.S).group(1)
        self.assertIn("\\uFE0E", function)
        writes = [line for line in js.splitlines() if re.search(r'"glyph tone-|"muted fold"|pull-glyph"\)\.textContent', line)]
        self.assertGreaterEqual(len(writes), 3)
        for line in writes:
            self.assertIn("glyphText(", line)
        self.assertNotIn("\uFE0E", js)

    def test_the_new_button_never_shrinks_and_the_brand_wraps(self):
        css, _ = self.text("/app.css")
        rule = lambda selector: re.search(re.escape(selector) + r" \{([^}]*)\}", css).group(1)
        for declaration in ("flex-shrink: 0", "white-space: nowrap"):
            self.assertIn(declaration, rule(".button"))
        self.assertIn("flex-wrap: wrap", rule(".brand"))
        self.assertNotIn("white-space", rule(".counts"))
        self.assertIn("white-space: nowrap", rule(".counts span"))

    def test_peek_and_names_are_written_as_text(self):
        js, _ = self.text("/app.js")
        for name in ("loadPeek", "suggestName", "noticeRow", "askUpdate", "openRoutineSheet"):
            body = re.search(r"function %s\(.*?\n\}\n" % name, js, re.S).group(0)
            self.assertNotRegex(body, r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(")
        self.assertRegex(js, r"el\(\"div\", \"peek-line[^\n]*line\.text\)")
        self.assertIn('$("rename-field").value = answer.name', js)

    def test_the_script_never_writes_html_and_the_inputs_turn_off_ios_capitals(self):
        js, _ = self.text("/app.js")
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("outerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("eval(", js)
        html, _ = self.text("/")
        for field in ("new-folder", "new-name", "rename-field"):
            tag = re.search(r'<input[^>]*id="%s"[^>]*>' % field, html).group(0)
            for attribute in ('autocapitalize="none"', 'autocorrect="off"', 'spellcheck="false"'):
                self.assertIn(attribute, tag, field)

    def test_a_script_gets_the_page_only_through_the_guard(self):
        self.assertEqual(self.request(path="/app.js", host="evil.example")[0], 400)


class ScriptedTailnet(StubTailnet):
    """A tailnet whose node() gives what the test sets."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def node(self):
        return self.answer


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def refused(port):
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


class Service(ApiCase):
    def service(self, answer, port=None, **kwargs):
        self.tailnet = ScriptedTailnet(answer)
        service = phone.PhoneService(
            FakeEngine([]), free_port() if port is None else port, self.state, self.home, self.prefs,
            tailnet=self.tailnet, sleep=lambda seconds: None, **kwargs,
        )
        self.addCleanup(service.stop_serving)
        return service

    def said(self, service):
        out = []
        while (notice := service.take_notice()) is not None:
            out.append(notice[0])
        return out

    def test_a_working_tailscale_binds_on_the_first_tick_and_writes_the_address(self):
        service = self.service((NODE, None))
        service.tick()
        port = service.server.server_address[1]
        self.assertFalse(refused(port))
        url = f"http://devbox.example-tailnet.ts.net:{port}/"
        self.assertEqual(phone.read_phone_file(self.state), {"url": url, "problem": None})
        self.assertEqual(self.said(service), [f"Phone page: {url}"])

    def test_the_address_notice_has_no_end_and_a_problem_notice_has_one(self):
        service = self.service((NODE, None))
        service.tick()
        self.assertIsNone(service.take_notice()[1])
        service.tailnet.answer = (None, phone.DOWN)
        service.tick()
        self.assertEqual(service.take_notice()[1], phone.PROBLEM_NOTICE_SECONDS)

    def test_a_problem_is_written_and_said_once_until_it_changes(self):
        service = self.service((None, phone.DOWN))
        service.tick()
        self.assertEqual(phone.read_phone_file(self.state), {"url": None, "problem": "down"})
        self.assertEqual(self.said(service), [phone.PROBLEM_LINES[phone.DOWN]])
        service.tick()
        self.assertEqual(self.said(service), [])
        service.tailnet.answer = (None, phone.MISSING)
        service.tick()
        self.assertEqual(self.said(service), [phone.PROBLEM_LINES[phone.MISSING]])

    def test_a_later_down_stops_serving(self):
        service = self.service((NODE, None))
        service.tick()
        port = service.server.server_address[1]
        service.tailnet.answer = (None, phone.DOWN)
        service.tick()
        self.assertTrue(refused(port))
        self.assertIsNone(service.server)

    def test_a_changed_node_is_served_again_on_its_new_name(self):
        service = self.service((NODE, None), port=free_port())
        service.tick()
        service.tailnet.answer = (NODE._replace(dns_name="other.example-tailnet.ts.net", short_name="other"), None)
        service.tick()
        self.assertIn("other.example-tailnet.ts.net", phone.read_phone_file(self.state)["url"])
        self.assertEqual(len(self.said(service)), 2)

    def test_a_port_that_is_taken_is_said_with_its_number(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            service = self.service((NODE, None), port=port)
            service.tick()
        self.assertEqual(phone.read_phone_file(self.state), {"url": None, "problem": "busy"})
        self.assertEqual(self.said(service), [f"Phone page off: port {port} is taken. Set EMX_PHONE_PORT to another."])

    def test_a_relaunched_panel_does_not_say_again_what_the_file_already_says(self):
        port = free_port()
        first = self.service((NODE, None), port=port)
        first.tick()
        self.said(first)
        first.stop_serving()
        second = self.service((NODE, None), port=port)
        second.tick()
        self.assertEqual(self.said(second), [])
        self.assertFalse(refused(port))

    def test_the_service_is_busy_while_a_post_runs(self):
        service = self.service((None, phone.DOWN))
        self.assertFalse(service.busy())
        service.api.acting = 1
        self.assertTrue(service.busy())

    def test_the_thread_ticks_until_stopped(self):
        service = self.service((None, phone.DOWN), interval=0.01)
        service.start()
        deadline = time.monotonic() + 5
        while not phone.read_phone_file(self.state) and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        service.join(5)
        self.assertFalse(service.is_alive())
        self.assertEqual(phone.read_phone_file(self.state)["problem"], "down")

    def test_a_tick_that_raises_does_not_end_the_thread(self):
        service = self.service((None, phone.DOWN), interval=0.01)
        calls = []

        def tick():
            calls.append(1)
            raise RuntimeError("boom")

        service.tick = tick
        service.start()
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        service.join(5)
        self.assertGreaterEqual(len(calls), 3)


class Port(unittest.TestCase):
    def test_the_port_is_8787_unless_the_setting_is_a_valid_unprivileged_port(self):
        for value, want in (("", 8787), ("abc", 8787), ("80", 8787), ("1023", 8787), ("70000", 8787), ("-5", 8787),
                            ("1024", 1024), ("9000", 9000), ("65535", 65535)):
            with self.subTest(value=value):
                self.assertEqual(phone.phone_port({"EMX_PHONE_PORT": value}), want)
        self.assertEqual(phone.phone_port({}), 8787)


class Peek(ApiCase):
    def setUp(self):
        super().setUp()
        self.folder = self.path("Workspace", "emalatxana")
        self.done = session("1", "alpha", self.folder, "done")

    def peek(self, sid, transcripts=None, sessions=None):
        sources = phone.PanelSources(transcripts=lambda: transcripts or {})
        api = self.api(FakeEngine(sessions if sessions is not None else [self.done]), sources=sources)
        return api.get("/api/peek", "id=" + sid)

    def test_peek_is_peek_content(self):
        transcript = core.Transcript(reply="All done, the tests pass.", answered=True, reply_at=time.time() * 1000 - 120000)
        status, body = self.peek(self.done.id, {self.done.id: transcript})
        want = core.peek_content(self.done, transcript, time.time() * 1000)
        self.assertEqual(status, 200)
        self.assertEqual((body["ok"], body["title"]), (True, want.title))
        self.assertEqual([l["text"] for l in body["lines"]], [text for text, _ in want.lines])
        self.assertEqual([l["tone"] for l in body["lines"]], [sidebar.PEEK_TONES[style] for _, style in want.lines])

    def test_a_code_line_is_flagged_for_the_page(self):
        waiting = session("2", "asking", self.folder, "blocked")
        transcript = core.Transcript(latest="tool", pending=True, tool="Bash: npm test", tool_lines=("Run the tests?", "npm test"),
                                     tool_about="Run the tests?", tool_at=1.0)
        body = self.peek(waiting.id, {waiting.id: transcript}, [waiting])[1]
        self.assertEqual([(l["text"], l["code"]) for l in body["lines"][:2]], [("Run the tests?", False), ("npm test", True)])

    def test_peek_text_is_capped(self):
        transcript = core.Transcript(reply="x" * 10000, answered=True, reply_at=1.0)
        (line,) = self.peek(self.done.id, {self.done.id: transcript})[1]["lines"]
        self.assertEqual(len(line["text"]), 4000)
        self.assertTrue(line["text"].endswith("…"))

    def test_peek_needs_a_session_id_and_a_listed_session(self):
        self.assertEqual(self.peek("not-an-id")[0], 400)
        self.assertEqual(self.peek("")[0], 400)
        status, body = self.peek("b" * 8 + "-bbbb-4bbb-8bbb-" + "b" * 12)
        self.assertEqual((status, body), (200, {"ok": False, "message": sidebar.OPEN_GONE}))


class Pins(ApiCase):
    def setUp(self):
        super().setUp()
        self.target = session("1", "alpha", self.path("Workspace", "emalatxana"), "done")
        self.engine = FakeEngine([self.target])

    def post(self, body):
        return self.api(self.engine).post("/api/pin", body)

    def test_pin_toggles_the_panels_pref_and_keeps_other_prefs(self):
        core.save_prefs(self.prefs, {**core.DEFAULT_PREFS, "side": "left", "favourite_folders": ["/a"]})
        status, body = self.post({"id": self.target.id})
        saved = core.load_prefs(self.prefs)
        self.assertEqual((status, body["ok"], body["pinned"], body["message"]), (200, True, True, "Pinned alpha."))
        self.assertEqual((saved["pinned"], saved["side"], saved["favourite_folders"]), ([self.target.id], "left", ["/a"]))
        body = self.post({"id": self.target.id})[1]
        saved = core.load_prefs(self.prefs)
        self.assertEqual((body["pinned"], body["message"], saved.get("pinned", [])), (False, "Unpinned alpha.", []))
        self.assertEqual((saved["side"], saved["favourite_folders"]), ("left", ["/a"]))

    def test_pin_of_an_unlisted_session_writes_nothing(self):
        core.save_prefs(self.prefs, {**core.DEFAULT_PREFS, "side": "left"})
        before = self.saved()
        body = self.post({"id": "b" * 8 + "-bbbb-4bbb-8bbb-" + "b" * 12})[1]
        self.assertEqual((body["ok"], body["message"]), (False, sidebar.OPEN_GONE))
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.post({"id": "nope"})[0], 400)
        self.assertEqual(self.saved(), before)


class Names(ApiCase):
    def setUp(self):
        super().setUp()
        self.target = session("1", "alpha", self.path("Workspace", "emalatxana"), "done")
        self.engine = FakeEngine([self.target])
        self.made = []

    def namer(self, answer=("Fresh name", None, None), wait=None):
        made = self.made

        class Stub:
            def __init__(self, claude, path, sid, current, unnamed, offered):
                made.append((claude, path, sid, current, unnamed, offered))

            def ask(self):
                if wait:
                    wait()
                return answer

        return Stub

    def api(self, namer=None, **kwargs):
        sources = phone.PanelSources(transcript_path=lambda sid: "/t/" + sid[:8], claude="claude")
        return super().api(self.engine, namer=namer or self.namer(), sources=sources, **kwargs)

    def test_suggest_name_goes_through_the_namer_with_what_the_page_offered(self):
        status, body = self.api().post("/api/suggest-name", {"id": self.target.id, "offered": ["One", "Two"]})
        self.assertEqual((status, body), (200, {"ok": True, "name": "Fresh name"}))
        self.assertEqual(self.made, [("claude", "/t/11111111", self.target.id, "alpha", core.unnamed(self.target), ["One", "Two"])])
        self.assertEqual(self.engine.tmux.writes, [])

    def test_a_namer_failure_is_a_message_worded_for_the_page(self):
        api = self.api(self.namer((None, sidebar.NAME_UNFIT, None)))
        body = api.post("/api/suggest-name", {"id": self.target.id, "offered": []})[1]
        self.assertEqual((body["ok"], body["message"]), (False, "Haiku's name didn't fit; tap Suggest for another."))
        api = self.api(self.namer((None, sidebar.NAME_NO_REQUESTS, None)))
        self.assertEqual(api.post("/api/suggest-name", {"id": self.target.id, "offered": []})[1]["message"], sidebar.NAME_NO_REQUESTS)

    def test_suggest_name_refuses_a_bad_offered_list(self):
        api = self.api()
        for offered in (["x"] * 11, [1], "name", None, ["y" * 201]):
            with self.subTest(offered=offered):
                self.assertEqual(api.post("/api/suggest-name", {"id": self.target.id, "offered": offered})[0], 400)
        self.assertEqual(api.post("/api/suggest-name", {"id": "bad", "offered": []})[0], 400)
        self.assertEqual(self.made, [])

    def test_suggest_name_of_an_unlisted_session_asks_nothing(self):
        body = self.api().post("/api/suggest-name", {"id": "b" * 8 + "-bbbb-4bbb-8bbb-" + "b" * 12, "offered": []})[1]
        self.assertEqual(body["message"], sidebar.OPEN_GONE)
        self.assertEqual(self.made, [])

    def test_suggest_name_does_not_hold_the_action_lock(self):
        other = session("2", "beta", self.path("Workspace", "emalatxana"), "done", running=False)
        self.engine.sessions = [self.target, other]
        release, started = threading.Event(), threading.Event()

        def wait():
            started.set()
            release.wait(5)

        api = self.api(self.namer(wait=wait))
        api.closer = lambda eng, targets, clear_dead_panes: mock.Mock(attempt=lambda finished: (True, "ok", {}))
        thread = threading.Thread(target=api.post, args=("/api/suggest-name", {"id": self.target.id, "offered": []}), daemon=True)
        thread.start()
        self.assertTrue(started.wait(5))
        self.assertTrue(api.busy())
        done = []
        closer = threading.Thread(target=lambda: done.append(api.post("/api/rename", {"id": other.id, "name": other.name})), daemon=True)
        closer.start()
        closer.join(3)
        self.assertEqual(len(done), 1)
        busy = api.post("/api/suggest-name", {"id": self.target.id, "offered": []})[1]
        self.assertEqual(busy["message"], "Still finishing an earlier Haiku call; tap Suggest again in a moment.")
        release.set()
        thread.join(5)
        self.assertFalse(api.busy())


class Folder(ApiCase):
    def setUp(self):
        super().setUp()
        self.folder = self.path("Workspace", "emalatxana")
        self.sessions = [
            session("1", "alpha", self.folder, "done", running=False, started=1000.0),
            session("2", "beta", self.folder, "working", started=2000.0),
            session("3", "gamma", self.path("other", "place"), "done", started=3000.0),
        ]
        self.engine = FakeEngine(self.sessions)
        self.closed = []

    def closer(self, ok=True):
        closed = self.closed

        class Stub:
            def __init__(self, engine_, targets, wait_for=(), panel="", clear_dead_panes=False):
                closed.append((list(targets), clear_dead_panes))
                self.targets = targets

            def attempt(self, finished):
                return ok, "Closed them.", {t.id: ok for t in self.targets}

        return Stub

    def api(self, **kwargs):
        return super().api(self.engine, closer=self.closer(), **kwargs)

    def body(self, sessions=None, **over):
        sent = sessions if sessions is not None else [{"id": s.id, "running": s.running} for s in self.sessions[:2]]
        return {"key": self.folder, "find": "", "sessions": sent, **over}

    def test_close_group_closes_what_the_page_showed_through_the_closer(self):
        status, body = self.api().post("/api/close-group", self.body())
        self.assertEqual((status, body), (200, {"ok": True, "message": "Closed them."}))
        ((targets, clear),) = self.closed
        self.assertEqual([(t.id, t.name, t.running) for t in targets], [(s.id, s.name, s.running) for s in self.sessions[:2]])
        self.assertTrue(clear)

    def test_close_group_refuses_pinned_a_changed_set_and_a_changed_running_state(self):
        api = self.api()
        pinned = api.post("/api/close-group", self.body(key=core.PINNED))[1]
        self.assertEqual((pinned["ok"], pinned["message"]), (False, "Pinned can't be closed as a folder; close its sessions one at a time."))
        added = self.sessions + [session("4", "delta", self.folder, "done", started=4000.0)]
        self.engine.sessions = added
        self.assertEqual(api.post("/api/close-group", self.body())[1]["message"], "That folder changed; look again.")
        self.engine.sessions = self.sessions[1:]
        self.assertEqual(api.post("/api/close-group", self.body())[1]["message"], "That folder changed; look again.")
        self.engine.sessions = self.sessions
        flipped = [{"id": self.sessions[0].id, "running": True}, {"id": self.sessions[1].id, "running": True}]
        self.assertFalse(api.post("/api/close-group", self.body(flipped))[1]["ok"])
        self.assertFalse(api.post("/api/close-group", self.body(key=self.path("nowhere")))[1]["ok"])
        self.assertEqual(self.closed, [])

    def test_close_group_with_find_closes_only_the_matching_rows(self):
        api = self.api()
        only_alpha = [{"id": self.sessions[0].id, "running": False}]
        self.assertTrue(api.post("/api/close-group", self.body(only_alpha, find="alpha"))[1]["ok"])
        self.assertEqual([t.id for t in self.closed[0][0]], [self.sessions[0].id])
        self.assertFalse(api.post("/api/close-group", self.body(find="alpha"))[1]["ok"])
        self.assertEqual(len(self.closed), 1)

    def test_close_group_validates_its_body(self):
        api = self.api()
        one = {"id": self.sessions[0].id, "running": False}
        bad = [
            {"find": "", "sessions": [one]},
            self.body(key=7), self.body(find=3), self.body(find="x" * 201), self.body(sessions="nope"), self.body([]),
            self.body([{"id": self.sessions[0].id, "running": 1}]), self.body([{"id": "bad", "running": False}]),
            self.body([one, one]), self.body([{"id": f"{i:08d}-0000-4000-8000-{i:012d}", "running": False} for i in range(201)]),
        ]
        for body in bad:
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(api.post("/api/close-group", body)[0], 400)
        self.assertEqual(self.closed, [])

    def test_a_failed_close_is_reported_not_hidden(self):
        api = super().api(self.engine, closer=self.closer(ok=False))
        body = api.post("/api/close-group", self.body())[1]
        self.assertEqual(body["ok"], False)


class Favourites(ApiCase):
    def setUp(self):
        super().setUp()
        self.engine = FakeEngine([session("1", "a", self.path("Workspace", "emalatxana"), "done")])
        self.budget = self.path("Workspace", "budget-tracker")

    def post(self, folder):
        return self.api(self.engine).post("/api/favourite", {"folder": folder})

    def test_favourite_toggles_the_panels_pref(self):
        core.save_prefs(self.prefs, {**core.DEFAULT_PREFS, "side": "left"})
        body = self.post(self.budget)[1]
        self.assertEqual((body["ok"], body["on"], body["message"]), (True, True, "★ ~/Workspace/budget-tracker is a favourite"))
        saved = core.load_prefs(self.prefs)
        self.assertEqual((saved["favourite_folders"], saved["side"]), ([self.budget], "left"))
        rows = self.api(self.engine).get("/api/folders", "q=budget")[1]["folders"]
        self.assertEqual([r["favourite"] for r in rows if r["path"] == self.budget], [True])
        body = self.post("~/Workspace/budget-tracker")[1]
        self.assertEqual((body["on"], body["message"]), (False, "~/Workspace/budget-tracker is no longer a favourite"))
        saved = core.load_prefs(self.prefs)
        self.assertNotIn("favourite_folders", saved)
        self.assertEqual(saved["side"], "left")

    def test_favourite_refuses_a_folder_the_page_may_not_use(self):
        outside = os.path.join(self.dir, "outside")
        os.makedirs(outside)
        core.save_prefs(self.prefs, dict(core.DEFAULT_PREFS))
        before = self.saved()
        for folder in (outside, "/etc", self.path("missing")):
            with self.subTest(folder=folder):
                self.assertFalse(self.post(folder)[1]["ok"])
        self.assertEqual(self.post(1)[0], 400)
        self.assertEqual(self.post("x" * 4097)[0], 400)
        self.assertEqual(self.saved(), before)
        core.save_prefs(self.prefs, {**core.DEFAULT_PREFS, "favourite_folders": [outside]})
        body = self.post(outside)[1]
        self.assertEqual((body["ok"], body["on"]), (True, False))

    def test_new_records_the_folder_as_the_panel_does(self):
        core.save_prefs(self.prefs, {**core.DEFAULT_PREFS, "recent_folders": ["/old"]})
        self.assertTrue(self.api(self.engine).post("/api/new", {"folder": self.budget})[1]["ok"])
        saved = core.load_prefs(self.prefs)
        self.assertEqual((saved["last_folder"], saved["recent_folders"][:2]), (self.budget, [self.budget, "/old"]))

    def test_a_start_that_failed_records_nothing(self):
        api = self.api(self.engine, wait_running=lambda eng, sid: (None, "never ran"))
        api.post("/api/new", {"folder": self.budget})
        self.assertFalse(self.prefs.config.exists())


class Notifications(ApiCase):
    def setUp(self):
        super().setUp()
        self.sessions = [session("1", "a", self.path("Workspace", "emalatxana"), "done"), session("2", "b", self.path("Workspace", "emalatxana"), "blocked")]
        now = time.monotonic()
        self.history = [
            core.Entry(self.sessions[1].id, core.NOTICE_NEEDS, "b · emalatxana", "Asks which file", now - 30),
            core.Entry("c" * 8 + "-cccc-4ccc-8ccc-" + "c" * 12, core.NOTICE_DONE, "gone · emalatxana", "Finished", now - 600),
            core.Entry(self.sessions[0].id, core.NOTICE_DONE, "a · emalatxana", "All done", now - 3000, read=True),
        ]

    def api(self, **kwargs):
        sources = phone.PanelSources(history=lambda: self.history, **kwargs)
        return super().api(FakeEngine(self.sessions), sources=sources)

    def test_notifications_are_the_panels_history_newest_first(self):
        data = self.api().get("/api/notifications", "")[1]
        self.assertEqual((data["title"], data["unread"], data["empty"]), (core.HISTORY_TITLE, 2, sidebar.HISTORY_EMPTY))
        self.assertEqual([e["id"] for e in data["entries"]], [e.id for e in self.history])
        self.assertEqual([(e["read"], e["listed"]) for e in data["entries"]], [(False, True), (False, False), (True, True)])
        self.assertEqual([(e["mark"], e["tone"]) for e in data["entries"]], [("●", "needs"), ("✓", "done"), ("✓", "done")])
        self.assertEqual([e["title"] for e in data["entries"]][0], "b · emalatxana")
        self.assertEqual(data["entries"][0]["age"], core.format_age(30))

    def test_reading_notifications_changes_nothing(self):
        before = list(self.history)
        listed = self.history
        self.api().get("/api/notifications", "")
        self.api().get("/api/state", "")
        self.assertIs(self.history, listed)
        self.assertEqual(self.history, before)

    def test_no_history_is_the_panels_empty_line(self):
        self.history = []
        data = self.api().get("/api/notifications", "")[1]
        self.assertEqual((data["entries"], data["unread"]), ([], 0))


class WhatsNew(ApiCase):
    CHANGELOG = "# Changelog\n\n## 99.0.0 — soon\n\n- Newest line\n\n## 2.14.0 — then\n\n- Older line\n\n## 2.13.0\n\n- Oldest line\n"

    def get(self, since, **kwargs):
        sources = phone.PanelSources(**kwargs)
        return super().api(FakeEngine([]), sources=sources, changelog=lambda: self.CHANGELOG).get("/api/whats-new", "since=" + since)

    def test_whats_new_is_the_panels_text_since_the_version_given(self):
        with mock.patch.object(emx, "__version__", "99.0.0"):
            status, body = self.get("2.14.0")
        self.assertEqual(status, 200)
        self.assertEqual(body["heading"], "emx updated to 99.0.0.")
        self.assertEqual(body["text"], core.whats_new_since(self.CHANGELOG, "2.14.0", "99.0.0"))
        self.assertIn("Newest line", body["text"])
        self.assertNotIn("Older line", body["text"])
        self.assertEqual((body["show"], body["record"]), (True, False))

    def test_a_first_visit_records_and_says_nothing(self):
        body = self.get("")[1]
        self.assertEqual((body["show"], body["record"], body["text"]), (False, True, ""))

    def test_the_same_or_a_newer_version_asks_for_nothing(self):
        body = self.get(emx.__version__)[1]
        self.assertEqual((body["show"], body["record"]), (False, False))
        self.assertEqual(self.get("999.0.0")[1]["due"], None)

    def test_a_since_that_is_not_a_version_is_refused(self):
        for since in ("x", "1.2", "1.2.3; drop", "../etc"):
            with self.subTest(since=since):
                self.assertEqual(self.get(since)[0], 400)

    def test_a_waiting_major_is_said_as_the_panel_says_it(self):
        body = self.get("", major=lambda: "999.0.0")[1]
        self.assertEqual((body["major"], body["major_line"]), ("999.0.0", core.major_line("999.0.0")))
        self.assertEqual(self.get("")[1]["major_line"], "")


class RoutineWords(ApiCase):
    def test_routines_carry_the_panels_title_and_empty_line(self):
        data = self.api(FakeEngine([]), read_routines=lambda now: ([], None)).get("/api/routines", "")[1]
        self.assertEqual((data["title"], data["none"]), (sidebar.ROUTINES_TITLE, sidebar.ROUTINES_NONE))


class NewRoutesAreGuarded(ServerCase):
    GETS = ("/api/peek?id=" + KEY, "/api/notifications", "/api/whats-new?since=2.0.0")
    POSTS = ("/api/pin", "/api/favourite", "/api/close-group", "/api/suggest-name")

    def test_another_tailscale_user_is_refused_everywhere_with_no_session_data(self):
        self.tailnet = StubTailnet(user=7777)
        self.setUp()
        for path in self.GETS:
            with self.subTest(path=path):
                status, body, _ = self.request(path=path)
                self.assertEqual((status, b"secret" in body), (403, False))
        for path in self.POSTS:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, {"id": KEY})[0], 403)
        self.assertEqual(self.api.posts, [])

    def test_a_post_without_this_pages_origin_is_refused_and_never_acts(self):
        for path in self.POSTS:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, {"id": KEY}, origin=False)[0], 403)
                self.assertEqual(self.request("POST", path, {"id": KEY}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.api.posts, [])


if __name__ == "__main__":
    unittest.main()
