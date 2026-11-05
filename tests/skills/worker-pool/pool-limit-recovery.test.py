"""Usage-limit reset parsing and bounded recovery decisions, without a live pane."""
import sys
import subprocess
import shutil
import time
import shlex
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
import pool_supervise as sup  # noqa: E402

ps = sup.ps
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()


class LimitRecovery(unittest.TestCase):
    def observation(self, reset, work=True):
        text = f"  ⎿  You've hit your weekly limit{reset}\n❯ \n"
        self.assertEqual(sup.classify_pane_text(text), ps.PANE_LIMIT)
        return ps.Observation(ps.STALE, True, watcher_beat=ps.ABSENT, watcher_held=False,
                              work_outstanding=work, pane=ps.PANE_LIMIT,
                              limit_reset_at=sup.pl.reset_at(text, NOW))

    def tick(self, state, obs, now):
        state, decisions = ps.evaluate(state, {"worker": obs}, now, expected_period_s=100000)
        return state, decisions["worker"]

    def test_reset_parser_fallbacks_and_calendar_dates(self):
        self.assertIsNone(sup.pl.reset_at("resets 2pm (Invalid/Timezone)", NOW))
        self.assertIsNone(sup.pl.reset_at("resets someday", NOW))
        self.assertEqual(sup.pl.reset_at("resets tomorrow 2pm (UTC)", NOW), NOW + 26 * 3600)
        self.assertEqual(sup.pl.reset_at("resets 2026-11-05 14:00 (UTC)", NOW), NOW + 7200)

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

    def working(self):
        return ps.Observation(ps.STALE, True, watcher_beat=ps.ABSENT, watcher_held=False,
                              work_outstanding=True, pane=ps.PANE_WORKING, pane_id="spin")

    def test_transient_working_between_refusals_keeps_backoff_and_cap(self):
        limit = self.observation(" · resets Nov 5 at 11am (UTC)")
        state, retries, escalations = ps.SupervisionState(), 0, 0
        for step in range(20):
            state, action = self.tick(state, limit if step % 2 == 0 else self.working(), NOW + 15 * step)
            retries += action == ps.RETRY_LIMIT
        self.assertEqual(retries, 1)
        self.assertEqual(state.workers["worker"].limit_attempts, 1)
        now = NOW + 300
        for _ in range(800):
            now += 15
            state, action = self.tick(state, limit, now)
            retries += action == ps.RETRY_LIMIT
            escalations += action == ps.ESCALATE
            now += 15
            state, _ = self.tick(state, self.working(), now)
        self.assertEqual((retries, escalations), (ps.LIMIT_MAX_ATTEMPTS, 1))

    def test_only_a_finished_turn_ends_the_limit_episode(self):
        state, _ = self.tick(ps.SupervisionState(), self.observation(" · resets Nov 5 at 11am (UTC)"), NOW)
        idle = ps.Observation(ps.LIVE, True, watcher_beat=ps.LIVE, work_outstanding=True, pane=ps.PANE_IDLE)
        state, _ = self.tick(state, idle, NOW + 15)
        ev = state.workers["worker"]
        self.assertEqual((ev.limit_next_at, ev.limit_attempts, ev.limit_reset_at), (None, 0, None))

    def test_yearless_reset_rolls_into_the_next_year(self):
        eve = datetime(2026, 12, 31, 12, tzinfo=timezone.utc).timestamp()
        self.assertEqual(sup.pl.reset_at("resets Jan 1 at 2pm (UTC)", eve),
                         datetime(2027, 1, 1, 14, tzinfo=timezone.utc).timestamp())
        new_year = datetime(2027, 1, 1, 9, tzinfo=timezone.utc).timestamp()
        self.assertEqual(sup.pl.reset_at("resets Dec 31 at 11pm (UTC)", new_year),
                         datetime(2026, 12, 31, 23, tzinfo=timezone.utc).timestamp())
        self.assertEqual(sup.pl.reset_at("resets Nov 5 at 11am (UTC)", NOW), NOW - 3600)
        autumn = datetime(2028, 9, 1, tzinfo=timezone.utc).timestamp()
        self.assertIsNone(sup.pl.reset_at("resets Feb 29 at 2pm (UTC)", autumn))

    def test_unchanged_relative_banner_keeps_its_first_deadline_across_midnight(self):
        text = "  ⎿  You've hit your weekly limit · resets tomorrow 2pm (UTC)\n❯ \n"
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, text, "")
        first = datetime(2026, 11, 5, 23, tzinfo=timezone.utc).timestamp()
        due = datetime(2026, 11, 6, 14, tzinfo=timezone.utc).timestamp()
        state = ps.SupervisionState()
        with patch.object(sup, "_open_tmux", return_value=("fixture.sock", "fixture")):
            for now in (first, first + 3600, first + 7200, due):
                kind, frame, reset = sup.observe_pane("/nonexistent", "worker", runner=runner, now=now)
                obs = ps.Observation(ps.STALE, True, watcher_beat=ps.ABSENT, watcher_held=False,
                                     work_outstanding=True, pane=kind, pane_id=frame,
                                     limit_reset_at=reset.at, limit_banner=reset.banner)
                before = state.workers.get("worker")
                state, action = self.tick(state, obs, now)
                if now < due:
                    self.assertEqual((action, state.workers["worker"].limit_next_at), (ps.NOTHING, due))
        self.assertEqual(before.limit_next_at, due)
        self.assertEqual(action, ps.RETRY_LIMIT)

    def test_rearm_routes_then_uses_guarded_line_sender(self):
        import pool_remedy as remedy
        import pool_ask
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            wid = sup.wi.new_worker_id()
            sup.wi.create_worker(ws, runtime="claude", cwd=str(ws), host="fixture",
                session_id="11111111-1111-4111-8111-111111111111", worker_id=wid)
            sup.pr.register_worker(ws, wid, "fixture-worker")
            pool_ask.ask(ws, wid, "Outstanding fixture work")
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "tmux":
                    return subprocess.CompletedProcess(argv, 0, "You've hit your weekly limit\n❯ \n", "")
                self.assertEqual(argv[:2], ["bash", str(REPO / "scripts/tmux-send-line.sh")])
                self.assertIn("--refuse-if-pending", argv)
                self.assertIn("--socket", argv)
                return subprocess.CompletedProcess(argv, 0, "sent", "")
            with patch.object(remedy.sup, "_open_tmux", return_value=("fixture.sock", "fixture-worker")):
                result = remedy.apply(ws, REPO, {wid: remedy.ps.RETRY_LIMIT}, runner=runner)
            delivered = result["rearms"][wid]
            self.assertEqual(delivered["outcome"], "rearm-sent")
            self.assertEqual(len(calls), 2)
            self.assertTrue(sup.pd.find(ws, wid, delivered["task_id"]))
            payload = sup.pd.read_payload(ws, delivered["task_id"])
            self.assertIn("Re-arm your session-role task watcher", payload)
            for frame in ("You've hit your weekly limit\n❯ owner draft\n", "❯ \n", ""):
                def changed(argv, **kwargs):
                    self.assertEqual(argv[0], "tmux", "changed panes must never send")
                    return subprocess.CompletedProcess(argv, 0, frame, "")
                with patch.object(remedy.sup, "_open_tmux", return_value=("fixture.sock", "fixture-worker")):
                    refused = remedy.retry_limit(ws, REPO, wid, runner=changed)
                self.assertEqual(refused["outcome"], "pane-changed-or-pending")
            with patch.object(remedy.sup, "_open_tmux", return_value=(None, "fixture-worker")):
                self.assertEqual(remedy.retry_limit(ws, REPO, wid)["outcome"], "unsupported-or-no-session")
            with patch.object(remedy.sup, "_open_tmux", return_value=("fixture.sock", "fixture-worker")):
                with patch.object(remedy.subprocess, "run", side_effect=OSError("capture failed")):
                    self.assertEqual(remedy.retry_limit(ws, REPO, wid)["outcome"], "capture-failed")
                ask = remedy._sibling("pool_ask")
                with patch.object(ask, "ask", side_effect=ValueError("routing failed")):
                    self.assertEqual(remedy.retry_limit(ws, REPO, wid, runner=runner)["outcome"], "delivery-failed")
                with patch.object(remedy.sup, "is_paused", side_effect=[False, True]):
                    self.assertEqual(remedy.retry_limit(ws, REPO, wid, runner=runner)["outcome"], "rearm-queued")
                def failed_sender(argv, **kwargs):
                    if argv[0] == "tmux":
                        return runner(argv, **kwargs)
                    raise subprocess.TimeoutExpired(argv, 60)
                self.assertEqual(remedy.retry_limit(ws, REPO, wid, runner=failed_sender)["outcome"], "wake-failed")
            sup.set_paused(ws, wid, True)
            self.assertEqual(remedy.retry_limit(ws, REPO, wid)["outcome"], "no-work-or-paused")

    @unittest.skipUnless(shutil.which("tmux"), "tmux required")
    def test_real_guarded_sender_wakes_an_isolated_limit_pane(self):
        import pool_remedy as remedy
        import pool_ask
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            socket = str(ws / "tmux.sock")
            received = ws / "received.txt"
            script = ws / "fixture.py"
            script.write_text("from pathlib import Path\n"
                              "print(\"You've hit your weekly limit\")\n"
                              f"Path({str(received)!r}).write_text(input('❯ '))\n"
                              "import time; time.sleep(30)\n")
            cmd = ["tmux", "-S", socket]
            subprocess.run(cmd + ["-f", "/dev/null", "new-session", "-d", "-s", "fixture",
                f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"], check=True)
            try:
                for _ in range(100):
                    frame = subprocess.run(cmd + ["capture-pane", "-p", "-t", "fixture"],
                                           capture_output=True, text=True).stdout
                    if "❯" in frame:
                        break
                    time.sleep(0.05)
                self.assertIn("❯", frame)
                wid = sup.wi.new_worker_id()
                sup.wi.create_worker(ws, runtime="claude", cwd=str(ws), host="fixture",
                    session_id="11111111-1111-4111-8111-111111111111", worker_id=wid)
                sup.pr.register_worker(ws, wid, "fixture-worker")
                pool_ask.ask(ws, wid, "Outstanding fixture work")
                with patch.object(remedy.sup, "_open_tmux", return_value=(socket, "fixture")):
                    result = remedy.retry_limit(ws, REPO, wid)
                self.assertEqual(result["outcome"], "rearm-sent", result)
                for _ in range(100):
                    if received.exists():
                        break
                    time.sleep(0.05)
                self.assertIn(result["task_id"], received.read_text())
                self.assertTrue(sup.pd.find(ws, wid, result["task_id"]))
            finally:
                subprocess.run(cmd + ["kill-server"], capture_output=True)

    @unittest.skipUnless(shutil.which("tmux"), "tmux required")
    def test_sender_refuses_a_pane_that_turned_busy_after_routing(self):
        import pool_remedy as remedy
        import pool_ask
        frames = {"working": "✳ Moonwalking… (15s · esc to interrupt)",
                  "dialog": None}
        for name, above in frames.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                ws = Path(directory)
                socket = str(ws / "tmux.sock")
                received = ws / "received.txt"
                script = ws / "fixture.py"
                if above is None:
                    body = ("import sys\nsys.stdout.write('❯ \\n Do you want to make this edit?\\n'"
                            "' Enter to confirm · Esc to cancel\\n')\nsys.stdout.flush()\n"
                            f"from pathlib import Path\nPath({str(received)!r}).write_text(sys.stdin.readline())\n")
                else:
                    body = (f"print({above!r})\nfrom pathlib import Path\n"
                            f"Path({str(received)!r}).write_text(input('❯ '))\n")
                script.write_text(body + "import time; time.sleep(30)\n")
                cmd = ["tmux", "-S", socket]
                subprocess.run(cmd + ["-f", "/dev/null", "new-session", "-d", "-s", "fixture",
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"], check=True)
                try:
                    for _ in range(100):
                        frame = subprocess.run(cmd + ["capture-pane", "-p", "-t", "fixture"],
                                               capture_output=True, text=True).stdout
                        if "❯" in frame:
                            break
                        time.sleep(0.05)
                    self.assertNotEqual(sup.classify_pane_text(frame), ps.PANE_LIMIT)
                    wid = sup.wi.new_worker_id()
                    sup.wi.create_worker(ws, runtime="claude", cwd=str(ws), host="fixture",
                        session_id="11111111-1111-4111-8111-111111111111", worker_id=wid)
                    sup.pr.register_worker(ws, wid, "fixture-worker")
                    pool_ask.ask(ws, wid, "Outstanding fixture work")
                    def stale_capture(argv, **kwargs):
                        if argv[0] == "tmux":
                            return subprocess.CompletedProcess(argv, 0, "You've hit your weekly limit\n❯ \n", "")
                        return subprocess.run(argv, **kwargs)
                    with patch.object(remedy.sup, "_open_tmux", return_value=(socket, "fixture")):
                        result = remedy.retry_limit(ws, REPO, wid, runner=stale_capture)
                    self.assertEqual((result["outcome"], result["send_code"]), ("wake-refused", 8), result)
                    time.sleep(0.3)
                    self.assertFalse(received.exists() and received.read_text().strip())
                finally:
                    subprocess.run(cmd + ["kill-server"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
