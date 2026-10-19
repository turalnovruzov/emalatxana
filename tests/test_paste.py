import io
import os
import shlex
import shutil
import signal
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from emx import BIN, cli, core, remote
from emx.tmux import RELAY_PASTE_OPT, SESSION_OPT

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_remote import FAKE_SSH, StubNotifier, StubVoice  # noqa: E402
from test_tmux import BRACKETED_RECORDER, PrivateServer  # noqa: E402

FIXTURE_PNG = core.PNG_SIGNATURE + b"\x00throwaway picture\xff"
SID = "0b6f4f0c-6b0a-4c4e-9a53-2f0e3c1d7a11"


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class CtrlVEndToEnd(PrivateServer):
    """Ctrl+V on a private server whose `emx follow` is read by `remote.connect` over a fake ssh, with the real `emx paste` and `emx paste-image`."""

    recorder_source = BRACKETED_RECORDER

    def setUp(self):
        super().setUp()
        self.tmux.set_pane_option(self.main, SESSION_OPT, SID)
        self.state = Path(self.dir) / "state"
        self.state.mkdir()
        self.images = self.state / "emx" / "images"
        self.flag = Path(self.dir) / "leave"
        self.pid_file = Path(self.dir) / "follow.pid"
        self.picture = Path(self.dir) / "picture.png"
        self.picture.write_bytes(FIXTURE_PNG)
        self.addCleanup(self.stop_follow)
        # Fake ssh drops the environment, and without these the server commands would land on his socket and state.
        wrapper = Path(self.dir) / "wrapper.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"export EMX_SOCKET={shlex.quote(self.tmux.socket)} TMUX_TMPDIR={shlex.quote(self.dir)}\n"
            f"export XDG_STATE_HOME={shlex.quote(str(self.state))} EMX_CONFIG_HOME={shlex.quote(self.dir)} EMX_SHOW_TEST_SESSIONS=1\n"
            f"EMX={shlex.quote(BIN)}\n"
            'case "$1" in\n'
            "  update) echo 'emx-update current 0.0.0 0.0.0 -';;\n"
            f"  follow) echo $$ > {shlex.quote(str(self.pid_file))}; exec \"$EMX\" \"$@\";;\n"
            "  --version|paste-image) exec \"$EMX\" \"$@\";;\n"
            f"  *) while [ ! -e {shlex.quote(str(self.flag))} ]; do sleep 0.1; done;;\n"
            "esac\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        env = {
            "EMX_SSH": FAKE_SSH, "EMX_REMOTE_COMMAND": str(wrapper), "EMX_SOCKET": self.tmux.socket,
            "XDG_STATE_HOME": str(self.state), "EMX_SHOW_TEST_SESSIONS": "1", "EMX_SAY": "true",
            "EMX_CLIPBOARD_IMAGE": f"cp {shlex.quote(str(self.picture))}",
        }
        for patch in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(remote, "control_dir", lambda: "/tmp/emx-test-ctl/1"),
            mock.patch.object(remote, "NOTE_SECONDS", 0),
            mock.patch.object(remote.sys, "stdin", io.StringIO()),
            mock.patch.object(
                remote.Delivery, "started",
                classmethod(lambda cls, socket, paste=None: remote.Delivery(StubVoice(), StubNotifier(), paste)),
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.relay = core.relay_path(core.state_path())
        self.tmux.bind_paste("C-v", cli.self_command("paste", self.tmux.socket, str(self.relay)))
        self.thread = threading.Thread(target=self.connect, daemon=True)
        self.addCleanup(self.leave)

    def connect(self):
        with redirect_stderr(io.StringIO()):
            remote.connect("emx-test-host")

    def leave(self):
        self.flag.touch()
        if self.thread.is_alive():
            self.thread.join(15)

    def stop_follow(self):
        try:
            os.kill(int(self.pid_file.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass

    def listen(self):
        self.thread.start()
        self.wait_for(lambda: self.tmux.global_option(RELAY_PASTE_OPT), "the laptop's stamp")

    def press(self):
        self.tmux.run("send-keys", "-K", "-c", self.client, "C-v")

    def saved(self):
        return list(self.images.glob("*.png")) if self.images.exists() else []

    def test_ctrl_v_pastes_the_laptops_image_as_a_bracketed_path(self):
        self.listen()
        self.press()
        deadline = time.monotonic() + 10
        while not self.typed("main").endswith(b"\x1b[201~") and time.monotonic() < deadline:
            time.sleep(0.05)
        typed = self.typed("main")
        self.assertTrue(typed.startswith(b"\x1b[200~") and typed.endswith(b"\x1b[201~"), typed)
        path = Path(typed[len(b"\x1b[200~"):-len(b"\x1b[201~")].decode())
        self.assertEqual(path.parent, self.images)
        self.assertEqual(path.read_bytes(), FIXTURE_PNG)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.images.stat().st_mode & 0o777, 0o700)

    def test_no_image_types_nothing_and_says_so(self):
        os.environ["EMX_CLIPBOARD_IMAGE"] = "false"
        self.listen()
        self.press()
        seen = b""
        deadline = time.monotonic() + 5
        while b"no image in clipboard" not in seen and time.monotonic() < deadline:
            try:
                seen += os.read(self.client_fd, 65536)
            except OSError:
                break
        self.assertIn(b"no image in clipboard", seen)
        time.sleep(0.5)
        self.assertEqual(self.typed("main"), b"")
        self.assertEqual(self.saved(), [])

    def test_a_stale_stamp_passes_through(self):
        self.listen()
        self.tmux.set_global_option(RELAY_PASTE_OPT, str(time.time() - 20))
        before = self.relay.read_text() if self.relay.exists() else ""
        self.press()
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), b"\x16")
        self.assertEqual(self.relay.read_text() if self.relay.exists() else "", before)

    def test_a_pane_without_a_session_passes_through(self):
        self.listen()
        self.tmux.run("set-option", "-p", "-u", "-t", self.main, SESSION_OPT)
        self.press()
        self.wait_for(lambda: self.typed("main"), "the key in main")
        self.assertEqual(self.typed("main"), b"\x16")
        time.sleep(0.5)
        self.assertEqual(self.saved(), [])


if __name__ == "__main__":
    unittest.main()
