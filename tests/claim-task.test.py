#!/usr/bin/env python3
"""Tests for the claim primitive (src/claim_task.py).

Run: python3 tests/claim-task.test.py
Exit: 0 on pass, 1 on fail.

Covers:
  1. Happy path — claim an existing task, file gets renamed.
  2. Race — two processes both try to claim the same task, exactly one wins.
  3. Missing task — claim a task that doesn't exist returns None.
  4. Already claimed — second claim attempt on the same task returns None.
  5. Different core_id format — claim files distinguish by core id.
  6. Input validation — empty / dotted / path-separator inputs rejected.
"""
import multiprocessing as mp
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claim_task import claim_plain as claim  # noqa: E402


def _try_claim(ws_str: str, task_id: str, core_id: str, q: "mp.Queue[str]") -> None:
    """Subprocess entry point — try to claim and post the result on the
    queue. Used by the race test to fork two true OS processes that both
    attempt the rename concurrently. We can't use threads here because the
    `os.rename` atomicity guarantee is per-process via the kernel, and
    threads in CPython would serialize via the GIL and obscure the race."""
    sys.path.insert(0, str(Path(ws_str).parent / "src"))  # safety
    sys.path.insert(0, str(ROOT / "src"))
    from claim_task import claim_plain as _claim

    result = _claim(task_id, core_id, workspace=Path(ws_str))
    q.put(f"{core_id}:{'won' if result is not None else 'lost'}:{result}")


class ClaimTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_task(self, task_id: str, content: str = "body\n") -> Path:
        p = self.ws / "tasks" / f"task-{task_id}.txt"
        p.write_text(content)
        return p

    def test_happy_path_claim_renames_file(self):
        original = self._write_task("abc123", "hello\n")
        result = claim("abc123", "1", workspace=self.ws)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "task-abc123.claimed-worker-1.txt")
        self.assertTrue(result.exists())
        self.assertFalse(original.exists())
        # Content preserved through rename.
        self.assertEqual(result.read_text(), "hello\n")

    def test_missing_task_returns_none(self):
        result = claim("doesnotexist", "1", workspace=self.ws)
        self.assertIsNone(result)

    def test_double_claim_second_returns_none(self):
        self._write_task("dup")
        first = claim("dup", "1", workspace=self.ws)
        second = claim("dup", "2", workspace=self.ws)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        # First claim file still present, no second claim file.
        self.assertTrue((self.ws / "tasks" / "task-dup.claimed-worker-1.txt").exists())
        self.assertFalse((self.ws / "tasks" / "task-dup.claimed-worker-2.txt").exists())

    def test_race_exactly_one_wins(self):
        """Two subprocesses race to claim the same task — exactly one wins.

        This is the core correctness guarantee. If multiple sessions in the
        pool ever both claim the same task, the down-stream done-flag gate
        is the only thing preventing duplicate side effects — but that gate
        is for crash recovery, not for live race-loss. The claim itself
        MUST be exclusive.
        """
        self._write_task("race")
        ctx = mp.get_context("fork")
        q: "mp.Queue[str]" = ctx.Queue()
        p1 = ctx.Process(target=_try_claim, args=(str(self.ws), "race", "1", q))
        p2 = ctx.Process(target=_try_claim, args=(str(self.ws), "race", "2", q))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)
        self.assertFalse(p1.is_alive())
        self.assertFalse(p2.is_alive())
        outcomes = [q.get_nowait(), q.get_nowait()]
        won = [o for o in outcomes if ":won:" in o]
        lost = [o for o in outcomes if ":lost:" in o]
        self.assertEqual(len(won), 1, f"expected exactly 1 win, got {outcomes!r}")
        self.assertEqual(len(lost), 1, f"expected exactly 1 loss, got {outcomes!r}")
        # The won-by core's claim file exists, the other's doesn't.
        won_core = won[0].split(":")[0]
        lost_core = lost[0].split(":")[0]
        self.assertTrue(
            (self.ws / "tasks" / f"task-race.claimed-worker-{won_core}.txt").exists()
        )
        self.assertFalse(
            (self.ws / "tasks" / f"task-race.claimed-worker-{lost_core}.txt").exists()
        )

    def test_different_core_ids_distinguish_claim_files(self):
        self._write_task("a")
        self._write_task("b")
        a = claim("a", "1", workspace=self.ws)
        b = claim("b", "2", workspace=self.ws)
        self.assertEqual(a.name, "task-a.claimed-worker-1.txt")
        self.assertEqual(b.name, "task-b.claimed-worker-2.txt")

    def test_validation_rejects_path_traversal(self):
        # hostile task_id with / or .. must not escape tasks/
        # (py/path-injection class; same shape as the attach allowlist)
        for bad in ["../etc", "a/b", "..", ".", "", ".dotfile"]:
            with self.assertRaises(ValueError, msg=f"should reject {bad!r}"):
                claim(bad, "1", workspace=self.ws)
        for bad in ["../1", "a/1", ""]:
            with self.assertRaises(ValueError, msg=f"should reject core_id {bad!r}"):
                claim("ok", bad, workspace=self.ws)

    def test_validation_allows_realistic_ids(self):
        self._write_task("1779170589673")
        result = claim("1779170589673", "1", workspace=self.ws)
        self.assertIsNotNone(result)


# Channel-affinity tests (#884)

from claim_task import claim_with_affinity, ALIVE_THRESHOLD_SEC  # noqa: E402


class ChannelAffinityTests(unittest.TestCase):
    """Sticky-handler semantics: same channel → same core within IDLE_THRESHOLD;
    dead handler → auto-rebalance within ALIVE_THRESHOLD; idle channel →
    race-claim."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "state" / "cores").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_task(self, task_id: str) -> Path:
        p = self.ws / "tasks" / f"task-{task_id}.txt"
        p.write_text("body\n")
        return p

    def _touch_alive(self, core_id: str, age_sec: float = 0) -> None:
        """Write a fresh heartbeat for core <core_id>. age_sec lets the test
        backdate the mtime to simulate a dead heartbeat."""
        p = self.ws / "state" / "cores" / f"worker-{core_id}.alive"
        p.write_text(f'{{"core_id": "{core_id}"}}')
        if age_sec > 0:
            past = os.path.getmtime(p) - age_sec
            os.utime(p, (past, past))

    def test_first_task_claims_and_writes_handler(self):
        self._write_task("a")
        self._touch_alive("1")
        result = claim_with_affinity("a", "1", "ch-X", workspace=self.ws)
        self.assertIsNotNone(result)
        # Handler should now point to worker-1
        handler_p = self.ws / "state" / "cores" / "channel-ch-X.handler"
        self.assertTrue(handler_p.exists())
        import json as _json
        data = _json.loads(handler_p.read_text())
        self.assertEqual(data["worker"], "worker-1")
        self.assertIn("last_handled_at", data)

    def test_subsequent_task_same_channel_sticks_to_handler(self):
        # worker-1 handles first task in channel ch-X.
        self._write_task("a")
        self._touch_alive("1")
        self._touch_alive("2")
        claim_with_affinity("a", "1", "ch-X", workspace=self.ws)
        # Now task-b arrives in same channel. worker-2 attempts → should be
        # refused (handler-respect); worker-1 should win.
        self._write_task("b")
        c2_attempt = claim_with_affinity("b", "2", "ch-X", workspace=self.ws)
        self.assertIsNone(c2_attempt, "worker-2 should not claim while worker-1 is fresh handler")
        c1_attempt = claim_with_affinity("b", "1", "ch-X", workspace=self.ws)
        self.assertIsNotNone(c1_attempt, "worker-1 should win as fresh handler")

    def test_different_channel_does_not_steal_handler(self):
        # worker-1 handles ch-X. New task in ch-Y → worker-2 should be free
        # to claim it (different channel).
        self._write_task("a")
        self._touch_alive("1")
        self._touch_alive("2")
        claim_with_affinity("a", "1", "ch-X", workspace=self.ws)
        self._write_task("b")
        result = claim_with_affinity("b", "2", "ch-Y", workspace=self.ws)
        self.assertIsNotNone(result, "worker-2 should claim ch-Y (different channel)")
        # ch-Y handler now points to worker-2
        handler_p = self.ws / "state" / "cores" / "channel-ch-Y.handler"
        import json as _json
        self.assertEqual(_json.loads(handler_p.read_text())["worker"], "worker-2")

    def test_dead_handler_releases_to_other_core(self):
        # worker-1 handles ch-X, then dies. worker-2 should be able to claim
        # the next ch-X task once worker-1's heartbeat goes stale.
        self._write_task("a")
        self._touch_alive("1")
        self._touch_alive("2")
        claim_with_affinity("a", "1", "ch-X", workspace=self.ws)
        # Backdate worker-1's heartbeat — simulate crash.
        self._touch_alive("1", age_sec=ALIVE_THRESHOLD_SEC + 30)
        self._touch_alive("2")  # worker-2 still fresh
        self._write_task("b")
        result = claim_with_affinity("b", "2", "ch-X", workspace=self.ws)
        self.assertIsNotNone(result, "worker-2 should claim ch-X after handler dies")
        # Handler now points to worker-2
        handler_p = self.ws / "state" / "cores" / "channel-ch-X.handler"
        import json as _json
        self.assertEqual(_json.loads(handler_p.read_text())["worker"], "worker-2")

    def test_idle_channel_releases_for_race(self):
        # worker-1 handles ch-X, then the channel goes idle past IDLE_THRESHOLD.
        # Next task → race-claim, any core can win.
        import json as _json
        self._write_task("a")
        self._touch_alive("1")
        self._touch_alive("2")
        claim_with_affinity("a", "1", "ch-X", workspace=self.ws)
        # Manually age the handler beyond IDLE_THRESHOLD.
        handler_p = self.ws / "state" / "cores" / "channel-ch-X.handler"
        data = _json.loads(handler_p.read_text())
        data["last_handled_at"] = time.time() - (31 * 60)  # 31 min ago
        handler_p.write_text(_json.dumps(data))
        # Now worker-2 should be able to claim (race-claim path).
        self._write_task("b")
        result = claim_with_affinity("b", "2", "ch-X", workspace=self.ws)
        self.assertIsNotNone(result, "worker-2 should claim after channel idle expires")

    def test_no_channel_id_falls_back_to_plain_claim(self):
        # channel_id=None: voice tasks etc. Plain race-claim, no handler tracking.
        self._write_task("a")
        result = claim_with_affinity("a", "1", None, workspace=self.ws)
        self.assertIsNotNone(result)
        # No handler file should be created
        handlers = list((self.ws / "state" / "cores").glob("channel-*.handler"))
        self.assertEqual(handlers, [], "no handler file for channel-less task")

    def test_channel_id_validation_rejects_traversal(self):
        self._write_task("a")
        self._touch_alive("1")
        for bad in ["../etc", "a/b", "..", ".", "", ".dotfile"]:
            with self.assertRaises(ValueError, msg=f"should reject channel_id {bad!r}"):
                claim_with_affinity("a", "1", bad, workspace=self.ws)

    def test_malformed_handler_falls_back_to_race(self):
        # If handler file is corrupted (not valid JSON), treat as no handler.
        self._write_task("a")
        self._touch_alive("1")
        self._touch_alive("2")
        handler_p = self.ws / "state" / "cores" / "channel-ch-X.handler"
        handler_p.parent.mkdir(parents=True, exist_ok=True)
        handler_p.write_text("not valid json {{{")
        # worker-2 should be able to claim despite the malformed handler
        result = claim_with_affinity("a", "2", "ch-X", workspace=self.ws)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
