#!/usr/bin/env python3
"""Progress notifies carry the same worker stamp as results.

An unstamped notify renders with no attribution at all client-side, which
read as "the stripe disappeared" to the owner — so the stamp on this path is
load-bearing, not cosmetic.

Run: python3 tests/task-progress-notify-stamp.test.py
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "task-progress" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import notify  # noqa: E402

ROOM = "!r:ag2.space"
_GW_ENV = {"REMOTE_TASK_URL": "https://gw.example", "REMOTE_TASK_TOKEN": "tok"}


class NotifyWorkerStampTests(unittest.TestCase):
    def _payload(self, extra_env, **kw):
        sent = []

        def fake_post(url, payload, headers):
            sent.append({"url": url, "payload": payload})
            return True

        env = {**_GW_ENV, **extra_env}
        with mock.patch.object(notify, "_post", fake_post), \
                mock.patch.dict(os.environ, env, clear=False):
            for k in ("SUTANDO_WORKER_ID", "SUTANDO_CORE_ID"):
                if k not in extra_env:
                    os.environ.pop(k, None)
            ok = notify.send_remote_gateway("local-ag2space", ROOM, "on it", **kw)
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        return sent[0]["payload"]

    def test_worker_id_env_stamps_the_message(self):
        p = self._payload({"SUTANDO_WORKER_ID": "core-7"})
        self.assertEqual(p["extra_content"], {"space.ag2.worker": {"id": "core-7"}})

    def test_core_id_env_derives_the_stamp(self):
        p = self._payload({"SUTANDO_CORE_ID": "3", "SUTANDO_WORKER_ID": ""})
        self.assertEqual(p["extra_content"], {"space.ag2.worker": {"id": "worker-3"}})

    def test_no_worker_env_sends_no_stamp(self):
        p = self._payload({})
        self.assertNotIn("extra_content", p)
        self.assertEqual(p["body"], "on it")


class NotifyThreadRootTests(unittest.TestCase):
    def _send(self, argv):
        sent = []

        def fake_post(url, payload, headers):
            sent.append(payload)
            return True

        with mock.patch.object(notify, "_post", fake_post), \
                mock.patch.dict(os.environ, _GW_ENV, clear=False), \
                mock.patch.object(sys, "argv", ["notify.py", "--source", "local-ag2space",
                                                "--channel-id", ROOM, "--message", "on it", *argv]):
            for k in ("SUTANDO_WORKER_ID", "SUTANDO_CORE_ID", "SUTANDO_WORKER_SEAT"):
                os.environ.pop(k, None)
            rc = notify.main()
        return rc, sent

    def test_thread_root_flag_threads_the_message(self):
        rc, sent = self._send(["--thread-root", "$root123"])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it",
                                 "thread_root": "$root123"}])

    def test_no_flag_payload_is_unchanged(self):
        rc, sent = self._send([])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_empty_thread_root_posts_unthreaded(self):
        for empty in ("", "   "):
            rc, sent = self._send(["--thread-root", empty])
            self.assertEqual(rc, 0, empty)
            self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_malformed_thread_root_is_refused_without_posting(self):
        rc, sent = self._send(["--thread-root", "root123"])
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
