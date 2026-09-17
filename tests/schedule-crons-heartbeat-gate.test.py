#!/usr/bin/env python3
"""Step 5.5's gate decides by the record's CONTENT: a fresh .alive kept by a previous session's
writer (which records the NEW pane's pid) must not skip the start. Run:
  python3 tests/schedule-crons-heartbeat-gate.test.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GATE = ROOT / "skills" / "schedule-crons" / "scripts" / "heartbeat-gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("heartbeat_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GateDecision(unittest.TestCase):
    def setUp(self):
        self.gate = _load_gate()
        self.hb = self.gate.hb
        self.tmp = Path(tempfile.mkdtemp(prefix="hb-gate-"))
        self.alive = self.tmp / "state" / "cores" / "host.alive"
        self.alive.parent.mkdir(parents=True)
        self.patches = [patch.object(self.hb, "_alive_path", lambda: self.alive),
                        patch.object(self.hb, "_pidfile", lambda: self.alive.with_suffix(".heartbeat.pid"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, pid, heartbeat_pid, age_s=5.0):
        self.alive.write_text(json.dumps({"pid": pid, "heartbeat_pid": heartbeat_pid, "schema_version": 4}))
        t = time.time() - age_s
        os.utime(self.alive, (t, t))

    def _old_mtime_rule(self):
        """Step 5.5 before this gate: fresh mtime alone → skip."""
        return "skip" if time.time() - self.alive.stat().st_mtime <= 90 else "start"

    def test_fresh_but_another_panes_pid_starts_and_the_old_rule_would_have_skipped(self):
        self._record(pid=777, heartbeat_pid=4242, age_s=5)
        with patch.object(self.gate, "current_core_pid", return_value=(4243, True)), \
             patch.object(self.gate, "writer_state", return_value="own"):
            word, reason, live = self.gate.decide()
        self.assertEqual(self._old_mtime_rule(), "skip")
        self.assertEqual(word, "start", reason)
        self.assertEqual(live, 4242, "the live foreign writer is named so it is stopped first")

    def test_fresh_same_pane_writer_alive_skips(self):
        self._record(pid=4243, heartbeat_pid=4242, age_s=5)
        with patch.object(self.gate, "current_core_pid", return_value=(4243, True)), \
             patch.object(self.gate, "writer_state", return_value="own"):
            word, reason, live = self.gate.decide()
        self.assertEqual(word, "skip", reason)
        self.assertIsNone(live)

    def test_the_incident_shape_fresh_same_pane_but_another_checkouts_writer_starts(self):
        # The orphan beat every 30 s and had already recorded the NEW pane's pid: pane equality alone
        # reads skip here, so the writer's own script must be this checkout's too.
        self._record(pid=4243, heartbeat_pid=4242, age_s=5)
        with patch.object(self.gate, "current_core_pid", return_value=(4243, True)), \
             patch.object(self.gate, "writer_state", return_value="foreign"):
            word, reason, live = self.gate.decide()
        self.assertEqual(self._old_mtime_rule(), "skip")
        self.assertEqual(word, "start", reason)
        self.assertEqual(live, 4242, "the live foreign writer is named so it is stopped first")

    def test_fresh_same_pane_but_writer_dead_starts(self):
        self._record(pid=4243, heartbeat_pid=4242, age_s=5)
        with patch.object(self.gate, "current_core_pid", return_value=(4243, True)), \
             patch.object(self.gate, "writer_state", return_value="dead"):
            word, reason, live = self.gate.decide()
        self.assertEqual(word, "start", reason)
        self.assertIsNone(live)

    def test_stale_starts_without_consulting_tmux(self):
        self._record(pid=4243, heartbeat_pid=4242, age_s=600)
        with patch.object(self.gate, "current_core_pid", side_effect=AssertionError("tmux consulted")), \
             patch.object(self.gate, "writer_state", return_value="own"):
            word, reason, live = self.gate.decide()
        self.assertEqual(word, "start", reason)
        self.assertEqual(live, 4242)

    def test_missing_file_starts(self):
        self.assertEqual(self.gate.decide()[0], "start")

    def test_unreadable_ps_is_unknown(self):
        self._record(pid=4243, heartbeat_pid=4242, age_s=5)
        with patch.object(self.gate.subprocess, "run", side_effect=OSError("no ps")):
            word, reason, live = self.gate.decide()
        self.assertEqual(word, "unknown", reason)

    def test_unobserved_tmux_is_unknown(self):
        self._record(pid=4243, heartbeat_pid=4242, age_s=5)
        with patch.object(self.gate, "current_core_pid", return_value=(None, False)), \
             patch.object(self.gate, "writer_state", return_value="own"):
            word, reason, live = self.gate.decide()
        self.assertEqual(word, "unknown", reason)

    def test_writer_state_uses_the_writers_own_argv_rule_and_its_recorded_script(self):
        def answering(rc, args):
            return lambda cmd, **kw: type("R", (), {"returncode": rc, "stdout": args})()
        own = self.gate.HB_SCRIPT
        pidfile = self.alive.with_suffix(".heartbeat.pid")
        with patch.object(self.gate.subprocess, "run", side_effect=answering(0, f"/usr/bin/python3 {own} --interval 30")):
            self.assertEqual(self.gate.writer_state(4242), "own")
        with patch.object(self.gate.subprocess, "run",
                          side_effect=answering(0, "/usr/bin/python3 /elsewhere/src/core_heartbeat.py --interval 30")):
            self.assertEqual(self.gate.writer_state(4242), "foreign", "another checkout's writer is live but not ours")
        with patch.object(self.gate.subprocess, "run", side_effect=answering(0, "python3 src/core_heartbeat.py")):
            self.assertEqual(self.gate.writer_state(4242), "foreign", "a relative argv with no record is not provably ours")
            pidfile.write_text(f"4242 {own}\n")
            self.assertEqual(self.gate.writer_state(4242), "own", "the writer's own record resolves a relative argv")
            pidfile.write_text("4243 /elsewhere/src/core_heartbeat.py\n")
            self.assertEqual(self.gate.writer_state(4242), "foreign", "another pid's record is not this pid's")
        with patch.object(self.gate.subprocess, "run", side_effect=answering(0, "bash -c sleep 60")):
            self.assertEqual(self.gate.writer_state(4242), "dead", "a recycled pid is not a writer")
        with patch.object(self.gate.subprocess, "run", side_effect=answering(1, "")):
            self.assertEqual(self.gate.writer_state(4242), "dead")
        self.assertEqual(self.gate.writer_state(None), "dead")
        self.assertEqual(self.gate.writer_state(1), "dead")


class GateEdges(unittest.TestCase):
    """The branches a stubbed happy path never reaches: the real pane-pid resolution,
    an own-checkout comparison that raises, an unreadable .alive, a --stop that raises."""

    def setUp(self):
        self.gate = _load_gate()
        self.hb = self.gate.hb
        self.tmp = Path(tempfile.mkdtemp(prefix="hb-gate-"))
        self.alive = self.tmp / "state" / "cores" / "host.alive"
        self.alive.parent.mkdir(parents=True)
        self.patches = [patch.object(self.hb, "_alive_path", lambda: self.alive),
                        patch.object(self.hb, "_pidfile", lambda: self.alive.with_suffix(".heartbeat.pid"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_current_core_pid_resolves_through_the_writers_own_functions(self):
        seen = {}

        def core_pid(sock, session):
            seen["args"] = (sock, session)
            return 4243
        with patch.object(self.hb, "_socket_path", return_value="/tmp/x.sock"), \
             patch.object(self.hb, "_observed_session", return_value="real"), \
             patch.object(self.hb, "core_pid", side_effect=core_pid):
            self.assertEqual(self.gate.current_core_pid(), (4243, True))
        self.assertEqual(seen["args"], ("/tmp/x.sock", "real"), "the same socket + session write_beat uses")

    def test_current_core_pid_reports_unobserved_when_the_probe_says_so(self):
        def core_pid(sock, session):
            self.hb._LAST_SESSION_PROBE = None
            return None
        with patch.object(self.hb, "_socket_path", return_value="/tmp/x.sock"), \
             patch.object(self.hb, "_observed_session", return_value=None), \
             patch.object(self.hb, "core_pid", side_effect=core_pid):
            self.assertEqual(self.gate.current_core_pid(), (None, False))

    def test_a_recorded_script_that_cannot_be_resolved_reads_foreign_not_a_raise(self):
        argv = f"/usr/bin/python3 {self.gate.HB_SCRIPT}"
        ps = type("R", (), {"returncode": 0, "stdout": argv, "stderr": ""})()
        with patch.object(self.gate.subprocess, "run", return_value=ps), \
             patch.object(self.gate, "_recorded_script", return_value="/abs/\x00bad"):
            self.assertEqual(self.gate.writer_state(4242), "foreign")

    def test_an_unreadable_alive_starts_and_names_the_error(self):
        self.alive.write_text("{not json")
        word, reason, live = self.gate.decide()
        self.assertEqual(word, "start")
        self.assertIn("unreadable .alive (JSONDecodeError)", reason)
        self.assertIsNone(live)

    def test_a_stop_that_raises_is_a_failed_stop_exit_3(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.gate, "decide", return_value=("start", "fresh, foreign writer", 4242)), \
             patch.object(self.gate.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="stop", timeout=30)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.gate.main([])
        self.assertEqual(rc, 3)
        self.assertIn("would not stop", out.getvalue())


class GateMain(unittest.TestCase):
    """main() stops a live foreign writer through core_heartbeat.py --stop before saying `start`."""

    def setUp(self):
        self.gate = _load_gate()

    def _run_main(self, decision, stop_rc=0, alive_after="dead", argv=None):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"returncode": stop_rc, "stdout": "", "stderr": ""})()
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.gate, "decide", return_value=decision), \
             patch.object(self.gate, "writer_state", return_value=alive_after), \
             patch.object(self.gate.subprocess, "run", side_effect=fake_run), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.gate.main(argv or [])
        return rc, out.getvalue(), err.getvalue(), calls

    def test_start_with_a_live_writer_invokes_stop_with_the_writers_own_script(self):
        rc, out, err, calls = self._run_main(("start", "foreign pane", 4242))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "start")
        self.assertEqual(calls, [[sys.executable, self.gate.HB_SCRIPT, "--stop"]])
        self.assertTrue(calls[0][1].endswith(os.path.join("src", "core_heartbeat.py")))
        self.assertIn("stopped writer 4242", err)

    def test_start_without_a_live_writer_stops_nothing(self):
        rc, out, err, calls = self._run_main(("start", "no .alive", None))
        self.assertEqual((rc, out.strip(), calls), (0, "start", []))

    def test_skip_stops_nothing(self):
        rc, out, err, calls = self._run_main(("skip", "fresh, this pane", None))
        self.assertEqual((rc, out.strip(), calls), (0, "skip", []))

    def test_unknown_exits_2(self):
        rc, out, err, calls = self._run_main(("unknown", "tmux could not answer", None))
        self.assertEqual((rc, out.strip(), calls), (2, "unknown", []))

    def test_a_writer_that_survives_stop_exits_3_and_prints_the_command(self):
        rc, out, err, calls = self._run_main(("start", "foreign pane", 4242), alive_after="foreign")
        self.assertEqual(rc, 3)
        self.assertIn(f"{self.gate.HB_SCRIPT} --stop", out)
        self.assertNotIn("\nstart\n", "\n" + out)

    def test_no_stop_prints_the_command_and_leaves_the_writer(self):
        rc, out, err, calls = self._run_main(("start", "foreign pane", 4242), argv=["--no-stop"])
        self.assertEqual((rc, out.strip(), calls), (0, "start", []))
        self.assertIn("--stop", err)


class GateIsTheStep(unittest.TestCase):
    def test_skill_step_5_5_calls_the_gate_not_the_mtime_rule(self):
        text = (ROOT / "skills" / "schedule-crons" / "SKILL.md").read_text()
        step = text[text.index("5.5. **Ensure the core heartbeat"):text.index("5.6. ")]
        self.assertIn("skills/schedule-crons/scripts/heartbeat-gate.py", step)
        self.assertNotIn("older than 90 seconds", step)
        for word in ("`start`", "`skip`", "`unknown`"):
            self.assertIn(word, step)

    def test_gate_runs_from_the_cli(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "SUTANDO_WORKSPACE": d, "SUTANDO_TEST_MODE": "1", "SUTANDO_HOST_LABEL": "host"}
            r = subprocess.run([sys.executable, str(GATE), "--no-stop"], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "start"), r.stderr)
        self.assertIn("no .alive", r.stderr)


if __name__ == "__main__":
    unittest.main()
