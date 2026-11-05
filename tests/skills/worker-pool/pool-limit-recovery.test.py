"""Usage-limit reset parsing and bounded recovery decisions, without a live pane."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
import pool_supervise as sup  # noqa: E402

ps = sup.ps
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()


class LimitRecovery(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("TMUX", "CLAUDE_CONFIG_DIR") and not k.startswith("SUTANDO_")}
        self.env = patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def observation(self, reset, work=True):
        text = f"  ⎿  You've hit your weekly limit{reset}\n❯ \n"
        self.assertEqual(sup.classify_pane_text(text), ps.PANE_LIMIT)
        return ps.Observation(ps.STALE, True, watcher_beat=ps.ABSENT, watcher_held=False,
                              work_outstanding=work, pane=ps.PANE_LIMIT,
                              limit_reset_at=sup.pl.reset_at(text, NOW))

    def tick(self, state, obs, now):
        state, decisions = ps.evaluate(state, {"worker": obs}, now, expected_period_s=100000)
        return state, decisions["worker"]

    def test_future_reset_waits_even_without_a_watcher(self):
        obs = self.observation(" · resets Nov 5 at 2pm (UTC)")
        state, action = self.tick(ps.SupervisionState(), obs, NOW)
        self.assertEqual(action, ps.NOTHING)
        state, action = self.tick(state, obs, NOW + 3600)
        self.assertEqual(action, ps.NOTHING)
        state, action = self.tick(state, obs, NOW + 7200)
        self.assertEqual(action, ps.RETRY_LIMIT)

    def test_past_reset_recovers_once_then_backs_off_and_stops(self):
        obs = self.observation(" · resets Nov 5 at 11am (UTC)")
        state, action = self.tick(ps.SupervisionState(), obs, NOW)
        self.assertEqual(action, ps.RETRY_LIMIT)
        state, action = self.tick(state, obs, NOW + 1)
        self.assertEqual(action, ps.NOTHING)
        for delay in (900, 1800, 3600):
            due = state.workers["worker"].limit_next_at
            before = state.last_sample_at
            self.assertEqual(due - (NOW if before == NOW + 1 else before), delay)
            state, action = self.tick(state, obs, due)
            self.assertEqual(action, ps.RETRY_LIMIT)
        state, action = self.tick(state, obs, state.workers["worker"].limit_next_at)
        self.assertEqual(action, ps.ESCALATE)
        state, action = self.tick(state, obs, state.last_sample_at + 10000)
        self.assertEqual(action, ps.NOTHING)

    def test_unknown_reset_waits_and_idle_queue_does_nothing(self):
        obs = self.observation("")
        state, action = self.tick(ps.SupervisionState(), obs, NOW)
        self.assertEqual(action, ps.NOTHING)
        state, action = self.tick(state, obs, NOW + 900)
        self.assertEqual(action, ps.RETRY_LIMIT)
        _, action = self.tick(ps.SupervisionState(), self.observation(" · resets 11am (UTC)", False), NOW)
        self.assertEqual(action, ps.NOTHING)

    def test_state_survives_restart_and_pause(self):
        obs = self.observation(" · resets 11am (UTC)")
        state, _ = self.tick(ps.SupervisionState(), obs, NOW)
        with tempfile.TemporaryDirectory() as directory:
            sup.save_state(directory, state)
            self.assertEqual(sup.load_state(directory), state)
        paused = ps.Observation(ps.STALE, True, paused=True, pane=ps.PANE_LIMIT,
                                work_outstanding=True, limit_reset_at=NOW - 1)
        _, action = self.tick(state, paused, NOW + 900)
        self.assertEqual(action, ps.NOTHING)

    def test_rearm_uses_real_routed_delivery_without_a_pane_send(self):
        import pool_remedy as remedy
        import pool_ask
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            wid = sup.wi.new_worker_id()
            sup.wi.create_worker(ws, runtime="claude", cwd=str(ws), host="fixture",
                session_id="11111111-1111-4111-8111-111111111111", worker_id=wid)
            sup.pr.register_worker(ws, wid, "fixture-worker")
            pool_ask.ask(ws, wid, "Outstanding fixture work")
            def refuse_process(*args, **kwargs):
                raise AssertionError("recovery must not send keys or spawn a process")
            result = remedy.apply(ws, REPO, {wid: remedy.ps.RETRY_LIMIT}, runner=refuse_process,
                                  spawn=refuse_process)
            delivered = result["rearms"][wid]
            self.assertEqual(delivered["outcome"], "rearm-queued")
            self.assertTrue(sup.pd.find(ws, wid, delivered["task_id"]))
            payload = sup.pd.read_payload(ws, delivered["task_id"])
            self.assertIn("Re-arm your session-role task watcher", payload)
            sup.set_paused(ws, wid, True)
            self.assertEqual(remedy.retry_limit(ws, wid)["outcome"], "no-work-or-paused")


if __name__ == "__main__":
    unittest.main()
