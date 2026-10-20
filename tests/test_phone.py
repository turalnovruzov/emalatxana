import contextlib
import http.client
import io
import json
import os
import subprocess
import threading
import unittest
from pathlib import Path

from emx import phone

FIXTURES = Path(__file__).parent / "fixtures" / "tailscale"
OWNER = 4242
NODE = phone.Node("127.0.0.1", OWNER, "devbox.example-tailnet.ts.net", "devbox")


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
        return (200, {"ok": True, "state": "secret-session-data"}) if path == "/api/state" else None

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
        for host in (f"devbox.example-tailnet.ts.net:{self.port}", f"DevBox.Example-Tailnet.TS.net:{self.port}", f"devbox:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request(host=host)[0], 200)

    def test_methods_other_than_get_and_post_are_405(self):
        for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method)[0], 405)

    def test_an_unknown_path_is_404_only_after_the_guard(self):
        self.assertEqual(self.request(path="/nope")[0], 404)
        self.assertEqual(self.request(path="/nope", host="evil.example")[0], 400)


class Posts(ServerCase):
    def test_a_post_needs_this_pages_own_origin(self):
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/close", {"id": "x"}, headers={"Origin": "null"})[0], 403)
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


if __name__ == "__main__":
    unittest.main()
