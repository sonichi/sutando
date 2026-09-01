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
    def _payload(self, extra_env):
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
            ok = notify.send_remote_gateway("local-ag2space", ROOM, "on it")
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        return sent[0]["payload"]

    def test_worker_id_env_stamps_the_message(self):
        p = self._payload({"SUTANDO_WORKER_ID": "core-7"})
        self.assertEqual(p["extra_content"], {"space.ag2.worker": {"id": "core-7"}})

    def test_core_id_env_derives_the_stamp(self):
        p = self._payload({"SUTANDO_CORE_ID": "3", "SUTANDO_WORKER_ID": ""})
        self.assertEqual(p["extra_content"], {"space.ag2.worker": {"id": "core-3"}})

    def test_no_worker_env_sends_no_stamp(self):
        p = self._payload({})
        self.assertNotIn("extra_content", p)
        self.assertEqual(p["body"], "on it")


if __name__ == "__main__":
    unittest.main(verbosity=1)
