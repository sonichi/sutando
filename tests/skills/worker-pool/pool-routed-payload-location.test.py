#!/usr/bin/env python3
"""A routed task's payload lives beside its sentinel, not in tasks/ (#4836).

The core's readers list tasks/*.txt and treat every entry as the core's; a
payload left there after routing is adopted, answered, and the outbox item is
spent before the worker's answer. So: body first, then sentinel, then the tasks/
copy goes; the resolver hands the worker the body; a finished body is archived
where history readers look; a withdrawn hold puts the body back in tasks/.
"""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))
pd, rt, pr, rie = (importlib.import_module(m) for m in
                   ("pool_delivery", "pool_router", "pool_roster", "resolve_inbox_entry"))

A = "a" * 32
B = "b" * 32
ROOM = "!bound:example.test"
TEXT = "id: task-1\nsource: ag2space\nchannel_id: !bound:example.test\ntask: hello\n"


def roster(bindings):
    return {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}}, "bindings": bindings}


class T(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir()
        (self.ws / "tasks" / "task-1.txt").write_text(TEXT)
        self.task = {"id": "task-1", "source": "ag2space", "channel_id": ROOM}

    def tearDown(self):
        self.td.cleanup()

    def test_routed_payload_leaves_tasks_and_sits_beside_the_sentinel(self):
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        body = pd.body_path(self.ws, A, "task-1")
        self.assertTrue(body.is_file(), "body beside the sentinel")
        self.assertEqual(body.read_text(), TEXT)
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel")
        self.assertEqual(sorted(p.name for p in (self.ws / "tasks").glob("task-*.txt")), [],
                         "no reader of tasks/*.txt sees a routed task")

    def test_core_target_keeps_the_payload_in_tasks(self):
        out = rt.route(self.ws, self.task, roster({}))
        self.assertEqual(out["targets"], [pr.CORE])
        self.assertTrue((self.ws / "tasks" / "task-1.txt").is_file())
        self.assertFalse(list((self.ws / "deliveries").glob("*/task-1.body")))

    def test_fan_out_gives_every_recipient_a_body_before_the_copy_goes(self):
        out = rt.route(self.ws, self.task, roster({ROOM: [A, B]}))
        self.assertEqual(sorted(out["delivered"]), sorted([A, B]))
        for w in (A, B):
            self.assertEqual(pd.body_path(self.ws, w, "task-1").read_text(), TEXT)
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def test_replay_after_a_crash_between_body_and_sentinel_delivers_once(self):
        # A pass died after staging the body: no sentinel, no tasks/ copy either
        # (a later pass may have retired it). The replay must still deliver.
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        pd.body_path(self.ws, A, "task-1").write_text(TEXT)
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        self.assertEqual(out["skipped"], [])
        out2 = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out2["already"], [A])
        self.assertEqual(len(list(folder.glob("task-1.*"))), 2, "one body, one sentinel")

    def test_no_payload_anywhere_is_still_no_payload(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["skipped"], [A])
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_resolver_hands_the_worker_the_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        entry = str(self.ws / "deliveries" / A / "task-1.txt")
        got = rie.resolve(entry)
        self.assertEqual(got, Path(os.path.abspath(pd.body_path(self.ws, A, "task-1"))))
        self.assertEqual(got.read_text(), TEXT)
        p = subprocess.run([sys.executable, str(SCRIPTS / "resolve_inbox_entry.py"), entry],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), os.path.abspath(pd.body_path(self.ws, A, "task-1")))

    def test_resolver_still_finds_a_pre_move_payload_in_tasks(self):
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        (folder / "task-1.txt").touch()
        got = rie.resolve(str(folder / "task-1.txt"))
        self.assertEqual(got, Path(os.path.abspath(self.ws / "tasks" / "task-1.txt")))

    def test_read_payload_and_residue_see_the_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(pd.read_payload(self.ws, "task-1"), TEXT)
        self.assertNotEqual(pd.residue(self.ws, A, "task-1"), "stale-sentinel")

    def test_finished_body_is_archived_by_prune_and_the_sentinel_retired(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        (self.ws / "results" / "task-1.txt").write_text("done\n")
        pd.mark_done(self.ws, A, "task-1", published=True)
        acts = pd.prune_spent(self.ws, A)
        self.assertIn("task-1", acts["retired"])
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertEqual(pd.archived_payload(self.ws, "task-1").read_text(), TEXT)
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_withdrawn_hold_returns_the_body_to_the_core(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        pd.clear_pending(self.ws, A, "task-1")
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertEqual((self.ws / "tasks" / "task-1.txt").read_text(), TEXT)
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_body_is_never_a_sentinel(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(rt.holders(self.ws, "task-1"), [A])
        names = pd.sentinel_names("task-1")
        self.assertNotIn("task-1" + pd.BODY_SUFFIX, names)
        self.assertIsNone(pd.parse_sentinel("task-1" + pd.BODY_SUFFIX))


if __name__ == "__main__":
    unittest.main(verbosity=1)
