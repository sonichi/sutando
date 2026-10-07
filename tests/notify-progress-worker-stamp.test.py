#!/usr/bin/env python3
"""Progress notifies carry the same worker stamp as results.

An unstamped notify renders with no attribution at all client-side, which
read as "the stripe disappeared" to the owner — so the stamp on this path is
load-bearing, not cosmetic.

Run: python3 tests/task-progress-notify-stamp.test.py
"""
import contextlib
import io
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
        rc, sent = self._send(["--thread-root", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [{"op": "message", "room_id": ROOM, "body": "on it"}])

    def test_malformed_thread_root_is_refused_without_posting(self):
        for bad in ("root123", "   ", "$"):
            rc, sent = self._send(["--thread-root", bad])
            self.assertEqual(rc, 1, repr(bad))
            self.assertEqual(sent, [], repr(bad))


class EventIdParityTests(unittest.TestCase):
    """notify.py keeps its own copy of the event-id check (agent-room-ops is optional),
    so both copies must give the same answer for every non-empty input."""

    CASES = ("$ok", " $ok ", "$", "root", "   ", "e$vt", "\t$x\n")

    def test_notify_and_relations_agree(self):
        sys.path.insert(0, str(_SCRIPTS.parents[1] / "agent-room-ops"))
        import relations  # noqa: E402
        for value in self.CASES:
            try:
                expected = relations._event_id(value, "thread_root")
            except relations.RelationError:
                expected = None
            sent, err = [], io.StringIO()
            with mock.patch.object(notify, "_post", lambda url, payload, headers: sent.append(payload) or True), \
                    mock.patch.dict(os.environ, _GW_ENV, clear=False), \
                    contextlib.redirect_stderr(err):
                ok = notify.send_remote_gateway("local-ag2space", ROOM, "x", thread_root=value)
            if expected is None:
                self.assertFalse(ok, repr(value))
                self.assertEqual(sent, [], repr(value))
                self.assertIn(repr(value), err.getvalue(), repr(value))
            else:
                self.assertTrue(ok, repr(value))
                self.assertEqual(sent[0]["thread_root"], expected, repr(value))

if __name__ == "__main__":
    unittest.main(verbosity=1)
