#!/usr/bin/env python3
"""step.py posts one browser step into the room: a text line through the
same gateway sender as notify.py, then the screenshot through the gateway's
media route under the `[file:]` allowlist plus the screenshot dir.

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

        def fake_post(url, payload, headers, timeout=10):
            self.sent.append({"url": url, "payload": payload, "timeout": timeout})
            return True

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

    def _run(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = step.run(["--source", "local-ag2space", "--channel-id", ROOM, *argv])
        return rc, err.getvalue()

    def test_text_only_step_posts_one_room_message(self):
        rc, _ = self._run("--message", "Opened the checkout page")
        self.assertEqual(rc, 0)
        self.assertEqual([s["url"] for s in self.sent], ["https://gw.example/v1/room"])
        self.assertEqual(self.sent[0]["payload"]["body"], "Opened the checkout page")
        self.assertEqual(self.sent[0]["payload"]["room_id"], ROOM)

    def test_screenshot_path_uploads_through_the_media_route(self):
        rc, _ = self._run("--message", "Filled the form", "--screenshot", self.png)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 2)
        media = self.sent[1]
        self.assertEqual(media["url"], "https://gw.example/v1/rooms/%21r%3Aag2.space/media")
        self.assertEqual(media["payload"]["filename"], "browser-1.png")
        self.assertEqual(base64.b64decode(media["payload"]["content_b64"]), b"\x89PNG-bytes")
        self.assertEqual(media["timeout"], 60)

    def test_path_outside_the_allowlist_is_refused_but_the_text_still_lands(self):
        stray = os.path.join(self.tmp, "elsewhere.png")
        with open(stray, "wb") as f:
            f.write(b"x")
        rc, err = self._run("--message", "Step", "--screenshot", stray)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1, "text only")
        self.assertIn("not allowlisted", err)

    def test_bare_screenshot_captures_the_url_via_browser_mjs(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=f"noise\n{self.png}\n", stderr="")

        with mock.patch.object(subprocess, "run", fake_run):
            rc, _ = self._run("--message", "Searching flights", "--url", "https://x.example",
                              "--screenshot")
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][1:], [str(step.BROWSER_MJS), "https://x.example", "screenshot",
                                        "--timeout=60000"])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1]["url"].endswith("/media"))

    def test_bare_screenshot_without_url_is_a_warning_not_a_failure(self):
        rc, err = self._run("--message", "Step", "--screenshot")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("no --url", err)

    def test_capture_failure_keeps_the_text_step(self):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error: timeout")

        with mock.patch.object(subprocess, "run", fake_run):
            rc, err = self._run("--message", "Step", "--url", "https://x", "--screenshot")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Error: timeout", err)

    def test_long_message_is_refused_before_anything_is_sent(self):
        rc, err = self._run("--message", "x" * 300, "--screenshot", self.png)
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent, [])
        self.assertIn("too long", err)

    def test_slack_step_sends_text_only(self):
        with mock.patch.object(notify, "send_slack", return_value=True) as slack:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = step.run(["--source", "slack", "--channel-id", "C1", "--message", "Step",
                               "--screenshot", self.png])
        self.assertEqual(rc, 0)
        slack.assert_called_once()
        self.assertEqual(self.sent, [])
        self.assertIn("not supported on slack", err.getvalue())

    def test_oversized_file_is_refused(self):
        with mock.patch.object(notify, "MAX_MEDIA_BYTES", 4):
            rc, err = self._run("--message", "Step", "--screenshot", self.png)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("exceeds", err)


if __name__ == "__main__":
    unittest.main(verbosity=1)
