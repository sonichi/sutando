#!/usr/bin/env python3
"""A proactive file a pool worker's session writes is sent by that worker's own lane, as its agent.

One rule, `proactive_routing.lane_claims_proactive(name, lane_worker)`, reached by every claimer:

- The worker's session stamps the name (`proactive-<ts>.by-<worker>[.to-<channel>].txt`), through
  `result_publish.py` or by writing it so; the core's session publishes unstamped names unchanged.
- A worker's lane claims exactly its worker's stamped files; a core lane never claims a stamped file,
  and the shared bridge rules (Discord, Slack, Telegram, their fallbacks) refuse one too.
- The gateway's drain hands an owner-directed file a non-primary lane names as its own
  (`PROACTIVE_OWN_FILE`) to that lane; the host wires that hook to the same rule.

Run: python3 tests/proactive-worker-lane.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import proactive_routing as pr  # noqa: E402
import result_publish  # noqa: E402

W, OTHER = "7c700e11" + "0" * 24, "9d9d9d9d" + "0" * 24
STAMPED, OTHERS, PLAIN = pr.proactive_filename(1, worker=W), pr.proactive_filename(2, worker=OTHER), pr.proactive_filename(3)


class Grammar(unittest.TestCase):
    def test_the_stamp_sits_before_the_destination_and_both_read_back(self):
        name = pr.proactive_filename(7, "ag2space", W)
        self.assertEqual(name, f"proactive-7.by-{W}.to-ag2space.txt")
        self.assertEqual((pr.proactive_worker(name), pr.proactive_destination(name)), (W, "ag2space"))
        self.assertEqual((pr.proactive_worker(STAMPED), pr.proactive_destination(STAMPED)), (W, None))
        self.assertIsNone(pr.proactive_worker(PLAIN))
        self.assertIsNone(pr.proactive_worker("proactive-1.to-ag2space.txt"))

    def test_about_task_names_carry_the_stamp_and_still_name_their_task(self):
        name = pr.about_task_filename("task-alpha~b1", 9, "ag2space", W)
        self.assertEqual((pr.about_task(name), pr.about_task_ts(name), pr.proactive_worker(name)), ("task-alpha~b1", 9, W))
        bare = pr.about_task_filename("task-alpha~b1", 9, worker=W)
        self.assertEqual((pr.about_task(bare), pr.proactive_destination(bare)), ("task-alpha~b1", None))

    def test_stamp_worker_is_idempotent_and_leaves_results_and_core_names_alone(self):
        self.assertEqual(pr.stamp_worker("proactive-5.txt", W), f"proactive-5.by-{W}.txt")
        self.assertEqual(pr.stamp_worker("proactive-5.to-ag2space.txt", W), f"proactive-5.by-{W}.to-ag2space.txt")
        self.assertEqual(pr.stamp_worker(f"proactive-5.by-{W}.txt", W), f"proactive-5.by-{W}.txt")
        self.assertEqual(pr.stamp_worker(f"proactive-5.by-{OTHER}.txt", W), f"proactive-5.by-{OTHER}.txt")
        self.assertEqual(pr.stamp_worker("task-5.txt", W), "task-5.txt")
        self.assertEqual(pr.stamp_worker("proactive-5.txt", ""), "proactive-5.txt")
        self.assertEqual(pr.stamp_worker("/x/results/proactive-5.txt", W), f"proactive-5.by-{W}.txt")

    def test_a_malformed_worker_id_is_refused_not_spelled(self):
        for bad in ("Upper", "has space", "a" * 33, "-lead"):
            with self.assertRaises(ValueError):
                pr.proactive_filename(1, worker=bad)
            with self.assertRaises(ValueError):
                pr.stamp_worker("proactive-1.txt", bad)


class TheRule(unittest.TestCase):
    def test_a_stamped_file_is_its_workers_lanes_alone(self):
        self.assertTrue(pr.lane_claims_proactive(STAMPED, W))
        self.assertFalse(pr.lane_claims_proactive(STAMPED))
        self.assertFalse(pr.lane_claims_proactive(STAMPED, ""))
        self.assertFalse(pr.lane_claims_proactive(STAMPED, OTHER))
        self.assertTrue(pr.lane_claims_proactive(pr.proactive_filename(1, "ag2space", W), W))

    def test_an_unstamped_file_is_never_a_worker_lanes(self):
        self.assertTrue(pr.lane_claims_proactive(PLAIN))
        self.assertTrue(pr.lane_claims_proactive("proactive-3.to-ag2space.txt", ""))
        self.assertFalse(pr.lane_claims_proactive(PLAIN, W))
        self.assertFalse(pr.lane_claims_proactive("proactive-3.to-ag2space.txt", W))


class EveryBridgeRule(unittest.TestCase):
    """The shared claim functions the Discord, Slack, Telegram and gateway bridges call."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proactive-worker-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "last-owner-activity.json"

    def test_activity_routing_never_claims_a_stamped_file(self):
        for channel in sorted(pr.BRIDGE_CHANNELS):
            self.state.write_text(json.dumps({"channel": channel}))
            tagged = pr.proactive_filename(1, channel, W)
            with self.subTest(channel=channel):
                self.assertTrue(pr.should_claim_proactive_file(PLAIN, self.state, channel))
                self.assertFalse(pr.should_claim_proactive_file(STAMPED, self.state, channel))
                self.assertFalse(pr.should_claim_proactive_file(tagged, self.state, channel))
                self.assertFalse(pr.claims_unless_routed_elsewhere(STAMPED, self.state, channel))
                self.assertFalse(pr.claims_unless_routed_elsewhere(tagged, self.state, channel))
                self.assertFalse(pr.fallback_claims_name(STAMPED, channel))
                self.assertFalse(pr.fallback_claims_name(tagged, channel))
                self.assertTrue(pr.fallback_claims_name(PLAIN, channel))

    def test_a_body_addressed_to_the_bridge_does_not_override_the_stamp(self):
        self.state.write_text(json.dumps({"channel": "discord"}))
        body = "[channel: 123456789012345678]\nhello\n"
        self.assertTrue(pr.should_claim_proactive_peeked(PLAIN, body, self.state, "discord"))
        self.assertFalse(pr.should_claim_proactive_peeked(STAMPED, body, self.state, "discord"))
        self.assertFalse(pr.claims_unless_routed_elsewhere(STAMPED, self.state, "discord", body=body))


def _load_shim():
    spec = importlib.util.spec_from_file_location("rgb_proactive_worker", ROOT / "src" / "remote-gateway-bridge.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["rgb_proactive_worker"] = m
    spec.loader.exec_module(m)
    return m


class TheGatewayHost(unittest.TestCase):
    """The loader's gates, and the canonical drain's owner-directed decision they feed."""

    @classmethod
    def setUpClass(cls):
        cls.rgb = _load_shim()

    def setUp(self):
        rgb = self.rgb
        self.tmp = Path(tempfile.mkdtemp(prefix="proactive-worker-lane-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ("results", "state"):
            (self.tmp / d).mkdir()
        self.saved = {k: getattr(rgb, k) for k in ("WS", "RESULTS_DIR", "TASK_ADDRESSEE", "PROACTIVE_CLAIM_GATE",
                                                   "PROACTIVE_OWN_FILE", "OWNS_UNADDRESSED", "_log")}
        rgb.WS, rgb.RESULTS_DIR = self.tmp, self.tmp / "results"
        rgb._log = lambda *a: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.rgb, k, v)

    def file(self, name, body="note\n"):
        p = self.tmp / "results" / name
        p.write_text(body)
        return p

    def test_a_core_lane_never_claims_a_workers_file_whatever_else_says_so(self):
        gate = self.rgb._ag2space_proactive_claim_gate
        self.assertFalse(gate(self.file(pr.proactive_filename(1, "ag2space", W))))
        self.assertFalse(gate(self.file(STAMPED, "[channel: !room:alpha-fixture.net]\nnote\n")))
        self.assertTrue(gate(self.file("proactive-3.to-ag2space.txt")))

    def test_a_worker_lane_claims_and_owns_exactly_its_workers_files(self):
        rgb = self.rgb
        rgb._install_worker_lane(W)
        self.assertEqual(rgb.TASK_ADDRESSEE(), W)
        for gate in (rgb.PROACTIVE_CLAIM_GATE, rgb.PROACTIVE_OWN_FILE):
            self.assertTrue(gate(self.file(STAMPED)))
            self.assertTrue(gate(self.file(pr.proactive_filename(1, "ag2space", W))))
            self.assertFalse(gate(self.file(OTHERS)))
            self.assertFalse(gate(self.file(PLAIN)))
            self.assertFalse(gate(self.file("proactive-4.to-ag2space.txt", "[channel: !r:alpha-fixture.net]\n")))

    def test_the_drain_hands_an_owner_directed_file_to_the_lane_that_names_it_its_own(self):
        rgb = self.rgb
        rgb.OWNS_UNADDRESSED = lambda: False
        rgb.PROACTIVE_OWN_FILE = None
        self.assertFalse(rgb.owner_directed_is_mine(self.file(PLAIN)), "standalone: only the primary")
        rgb._install_worker_lane(W)
        self.assertTrue(rgb.owner_directed_is_mine(self.file(STAMPED)))
        self.assertFalse(rgb.owner_directed_is_mine(self.file(PLAIN)))
        self.assertFalse(rgb.owner_directed_is_mine(self.file(OTHERS)))
        rgb.PROACTIVE_OWN_FILE = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertFalse(rgb.owner_directed_is_mine(self.file(STAMPED)), "an unanswerable host keeps the primary-only rule")
        rgb.OWNS_UNADDRESSED = lambda: True
        self.assertTrue(rgb.owner_directed_is_mine(self.file(PLAIN)), "the primary delivers every one")


class ThePublisher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="proactive-publish-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "results").mkdir()

    def publish(self, name, worker):
        env = {"SUTANDO_WORKER_ID": worker} if worker else {}
        err = io.StringIO()
        with mock.patch.dict("os.environ", env, clear=False), redirect_stderr(err), \
                mock.patch.object(sys, "stdin", io.StringIO("[no-send] note\n")):
            if not worker:
                import os
                os.environ.pop("SUTANDO_WORKER_ID", None)
            rc = result_publish.main(["result_publish.py", str(self.tmp / "results" / name)])
        return rc, sorted(p.name for p in (self.tmp / "results").glob("*.txt")), err.getvalue()

    def test_a_worker_session_publishes_its_proactive_file_stamped(self):
        rc, names, err = self.publish("proactive-11.txt", W)
        self.assertEqual((rc, names), (0, [f"proactive-11.by-{W}.txt"]))
        self.assertIn(f"proactive-11.by-{W}.txt", err)
        rc, names, _ = self.publish("proactive-12.to-ag2space.txt", W)
        self.assertEqual((rc, names[-1]), (0, f"proactive-12.by-{W}.to-ag2space.txt"))

    def test_the_cores_session_and_a_task_result_keep_their_names(self):
        rc, names, err = self.publish("proactive-21.txt", "")
        self.assertEqual((rc, names, err), (0, ["proactive-21.txt"], ""))
        rc, names, _ = self.publish("task-22.txt", W)
        self.assertEqual((rc, names[-1]), (0, "task-22.txt"))
        self.assertEqual(result_publish.worker_published_path("/r/proactive-1.txt", {"SUTANDO_WORKER_ID": "Not Valid"}),
                         Path("/r/proactive-1.txt"), "a seat variable that is no worker id stamps nothing")


if __name__ == "__main__":
    unittest.main(verbosity=1)
