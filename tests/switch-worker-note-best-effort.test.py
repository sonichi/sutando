#!/usr/bin/env python3
"""A switch whose worker started is not reported as failed when its pid cannot be put on record."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "account-switch" / "scripts"))
sys.path.insert(0, str(REPO / "skills" / "runtime-switch" / "scripts"))

import switch_worker  # noqa: E402


class _Proc:
    pid = 4242


class NoteIsBestEffort(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="switch-worker-"))

    def test_note_swallows_a_failed_write(self):
        rec = self.ws / "state" / "x-running.json"
        with mock.patch.object(switch_worker.holder_lock, "start_time", return_value="Mon 1"), \
                mock.patch.object(switch_worker, "write_whole", side_effect=OSError("disk full")):
            switch_worker.note(rec, "a1", 4242)
        self.assertFalse(switch_worker.path(rec).exists())
        self.assertFalse(switch_worker.dead(rec, "a1"))

    def test_note_records_when_it_can(self):
        rec = self.ws / "state" / "x-running.json"
        rec.parent.mkdir(parents=True)
        with mock.patch.object(switch_worker.holder_lock, "start_time", return_value="Mon 1"):
            switch_worker.note(rec, "a1", 4242)
        self.assertEqual(json.loads(switch_worker.path(rec).read_text())["pid"], 4242)

    def test_a_worker_on_record_for_another_attempt_is_not_this_ones(self):
        rec = self.ws / "state" / "x-running.json"
        rec.parent.mkdir(parents=True)
        switch_worker.path(rec).write_text(json.dumps({"attempt": "a0", "pid": 1, "start": "Mon 1"}))
        self.assertFalse(switch_worker.dead(rec, "a1"))
        switch_worker.path(rec).write_text("[]")
        self.assertFalse(switch_worker.dead(rec, "a1"))

    def _start(self, mod, *args, **kw):
        with mock.patch.object(switch_worker.holder_lock, "start_time", return_value="Mon 1"), \
                mock.patch.object(switch_worker, "write_whole", side_effect=OSError("disk full")):
            return mod.start_switch(self.ws, *args, popen=lambda *a, **k: _Proc(), **kw)

    def test_account_start_switch_keeps_the_spawned_pid(self):
        import account_card
        self.assertEqual(self._start(account_card, "!dm:x", "k1", "a1"), 4242)

    def test_runtime_start_switch_keeps_the_spawned_pid(self):
        import runtime_card
        self.assertEqual(self._start(runtime_card, "!dm:x", "codex", "a1"), 4242)


if __name__ == "__main__":
    unittest.main()
