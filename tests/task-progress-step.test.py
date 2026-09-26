#!/usr/bin/env python3
"""step.py posts one browser step into a conversation: a text line through the
same gateway sender as notify.py, then the screenshot through the gateway's
media route under the `[file:]` allowlist plus the screenshot dir. A
`--screenshot <path>` is the live page the working session captured; a
`--capture <url>` is a fresh load through src/browser.mjs and says so.

Run: python3 tests/task-progress-step.test.py
"""
import base64
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "task-progress" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import notify  # noqa: E402
import step  # noqa: E402

ROOM = "!r:ag2.space"
_GW_ENV = {"REMOTE_TASK_URL": "https://gw.example", "REMOTE_TASK_TOKEN": "tok"}


class StepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="step-test-")
        self.shots = os.path.join(self.tmp, "shots")
        os.makedirs(self.shots)
        self.png = os.path.join(self.shots, "browser-1.png")
        with open(self.png, "wb") as f:
            f.write(b"\x89PNG-bytes")
        self.sent = []
        self.post_ok = True

        def fake_post(url, payload, headers, timeout=10):
            self.sent.append({"url": url, "payload": payload, "timeout": timeout})
            return self.post_ok

        self.env = {**_GW_ENV, "SUTANDO_SCREENSHOT_DIR": self.shots}
        self.patches = [
            mock.patch.object(notify, "_post", fake_post),
            mock.patch.dict(os.environ, self.env, clear=False),
        ]
        for p in self.patches:
            p.start()
        for k in ("SUTANDO_WORKER_ID", "SUTANDO_CORE_ID", "SUTANDO_WORKER_SEAT"):
            os.environ.pop(k, None)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _run(self, *argv, source="local-ag2space", channel=ROOM):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = step.run(["--source", source, "--channel-id", channel, *argv])
        return rc, err.getvalue()

    def test_text_only_step_posts_one_room_message(self):
        rc, _ = self._run("--message", "Opened the checkout page")
        self.assertEqual(rc, 0)
        self.assertEqual([s["url"] for s in self.sent], ["https://gw.example/v1/room"])
        self.assertEqual(self.sent[0]["payload"]["body"], "Opened the checkout page")
        self.assertEqual(self.sent[0]["payload"]["room_id"], ROOM)

    def test_screenshot_path_uploads_through_the_media_route(self):
        rc, err = self._run("--message", "Filled the form", "--screenshot", self.png)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 2)
        media = self.sent[1]
        self.assertEqual(media["url"], "https://gw.example/v1/rooms/%21r%3Aag2.space/media")
        self.assertEqual(media["payload"]["filename"], "browser-1.png")
        self.assertEqual(base64.b64decode(media["payload"]["content_b64"]), b"\x89PNG-bytes")
        self.assertEqual(media["timeout"], 60)
        self.assertNotIn("fresh load", err, "a session screenshot is the live page; no caveat")

    def test_path_outside_the_allowlist_is_refused_but_the_text_still_lands(self):
        stray = os.path.join(self.tmp, "elsewhere.png")
        with open(stray, "wb") as f:
            f.write(b"x")
        rc, err = self._run("--message", "Step", "--screenshot", stray)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1, "text only")
        self.assertIn("not allowlisted", err)

    def test_capture_loads_the_url_fresh_via_browser_mjs_and_says_so(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=f"noise\n{self.png}\n", stderr="")

        with mock.patch.object(subprocess, "run", fake_run):
            rc, err = self._run("--message", "Searching flights", "--capture", "https://x.example")
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][1:], [str(step.BROWSER_MJS), "https://x.example", "screenshot",
                                        "--timeout=60000"])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1]["url"].endswith("/media"))
        self.assertIn("fresh load of https://x.example — not the live tab", err)

    def test_screenshot_and_capture_are_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run("--message", "Step", "--screenshot", self.png, "--capture", "https://x")
        self.assertEqual(self.sent, [])

    def test_capture_failure_keeps_the_text_step(self):
        cases = {
            "browser exited": lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error: timeout"),
            "no output": lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr=""),
            "no such file": lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0, stdout=os.path.join(self.shots, "missing.png"), stderr=""),
            "node missing": mock.Mock(side_effect=OSError("node: not found")),
            "hung": mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="node", timeout=90)),
        }
        expect = {"browser exited": "Error: timeout", "no output": "exit 0", "no such file": "returned no file",
                  "node missing": "node: not found", "hung": "timed out"}
        for name, fake_run in cases.items():
            with self.subTest(name):
                self.sent.clear()
                with mock.patch.object(subprocess, "run", fake_run):
                    rc, err = self._run("--message", "Step", "--capture", "https://x")
                self.assertEqual(rc, 0)
                self.assertEqual(len(self.sent), 1, "the text line still lands")
                self.assertIn("screenshot skipped", err)
                self.assertIn(expect[name], err)

    def test_long_message_is_refused_before_anything_is_sent(self):
        rc, err = self._run("--message", "x" * 300, "--screenshot", self.png)
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent, [])
        self.assertIn("too long", err)

    def test_a_missing_channel_is_refused_before_anything_is_sent(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = step.run(["--source", "local-ag2space", "--message", "Step"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent, [])
        self.assertIn("--channel-id", err.getvalue())

    def test_unsent_text_is_exit_1_and_no_picture_follows(self):
        self.post_ok = False
        rc, err = self._run("--message", "Step", "--screenshot", self.png)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.sent), 1, "no media upload after a failed text line")
        self.assertIn("step text not sent", err)

    def test_slack_discord_telegram_steps_send_text_only(self):
        senders = {"slack": ("send_slack", "C1"), "discord": ("send_discord", "123"),
                   "telegram": ("send_telegram", "456")}
        for source, (fn, channel) in senders.items():
            with self.subTest(source):
                with mock.patch.object(notify, fn, return_value=True) as sender:
                    rc, err = self._run("--message", "Step", "--screenshot", self.png,
                                        source=source, channel=channel)
                self.assertEqual(rc, 0)
                sender.assert_called_once()
                self.assertEqual(sender.call_args.args[:2], (channel, "Step"))
                self.assertEqual(self.sent, [])
                self.assertIn(f"not supported on {source}", err)

    def test_telegram_chat_id_is_the_channel(self):
        with mock.patch.object(notify, "send_telegram", return_value=True) as sender:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = step.run(["--source", "telegram", "--chat-id", "789", "--message", "Step"])
        self.assertEqual(rc, 0)
        self.assertEqual(sender.call_args.args[0], "789")

    def test_oversized_file_is_refused(self):
        with mock.patch.object(notify, "MAX_MEDIA_BYTES", 4):
            rc, err = self._run("--message", "Step", "--screenshot", self.png)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("exceeds", err)

    def test_an_unreadable_file_is_a_reason_not_a_crash(self):
        with mock.patch.object(os.path, "getsize", side_effect=OSError("gone")):
            rc, err = self._run("--message", "Step", "--screenshot", self.png)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("read failed: gone", err)

    def test_no_gateway_config_means_no_upload(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp}), \
                contextlib.redirect_stderr(io.StringIO()):
            for k in ("REMOTE_TASK_URL", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
                os.environ.pop(k, None)
            ok, reason = notify.upload_room_media("nogateway", ROOM, self.png)
        self.assertEqual((ok, reason), (False, "no gateway config"))
        self.assertEqual(self.sent, [])

    def test_an_invalid_source_slug_is_no_gateway(self):
        with contextlib.redirect_stderr(io.StringIO()):
            ok, reason = notify.upload_room_media("Bad Source!", ROOM, self.png)
        self.assertEqual((ok, reason), (False, "no gateway config"))

    def test_the_media_post_carries_its_own_timeout(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return Resp(b'{"event_id": "$1"}')

        for p in self.patches[:1]:
            p.stop()
        try:
            with mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen):
                self.assertTrue(notify._post("https://gw.example/v1/rooms/r/media", {"a": 1}, {}, timeout=60))
        finally:
            self.patches[0].start()
        self.assertEqual(seen["timeout"], 60)

    def test_an_unimportable_attachment_policy_fails_closed(self):
        with mock.patch.dict(sys.modules, {"policy.egress.attachment": None}):
            stub = notify._load_attachment_policy()
        self.assertFalse(stub(self.png, (self.shots,)), "no policy, nothing is sendable")
        self.assertFalse(stub(self.png))


if __name__ == "__main__":
    unittest.main(verbosity=1)
