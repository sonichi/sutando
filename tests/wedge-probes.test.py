#!/usr/bin/env python3
"""src/wedge_probes.py: each signature fires on its wedge, clears on recovery, and stays silent when it
cannot look or when the core is merely slow. Process tables are injected, so no real `ps` runs."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import wedge_probes as wp  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wedge.")).resolve()
        self.addCleanup(shutil.rmtree, self.ws, True)
        (self.ws / "tasks").mkdir()
        (self.ws / "state").mkdir()
        self.inbox, self.state = str(self.ws / "tasks"), str(self.ws / "state")
        self.argv = {}

    def proc(self, pid: int, *args: str) -> str:
        self.argv[str(pid)] = list(args)
        return f"  {pid} 1 {' '.join(args)}\n"

    def session(self, pid=100) -> str:
        return self.proc(pid, "bash", "src/watch-tasks-stream.sh", "--role", "session", "--inbox", self.inbox)

    def standby(self, pid=200) -> str:
        return self.proc(pid, "bash", "src/watch-tasks-stream.sh", self.inbox, "--role", "standby",
                         "--inbox", self.inbox)

    def stamp(self, pid) -> None:
        (self.ws / "state" / "watch-tasks-stream.pid").write_text(f"{pid}\n")

    def probe(self, ps: str, core_alive=True):
        return wp.watcher_not_ready(self.inbox, self.state, core_alive, ps,
                                    argv_vector=lambda pid: self.argv.get(str(pid)))

    def write(self, name: str, data, age: float = 0.0) -> Path:
        p = self.ws / "state" / name
        p.write_text(json.dumps(data))
        t = time.time() - age
        os.utime(p, (t, t))
        return p


class WatcherNotReady(Fixture):
    def test_a_ready_watcher_is_no_wedge(self):
        self.stamp(100)
        self.assertIsNone(self.probe(self.session()))

    def test_a_running_watcher_whose_sentinel_lapsed_fires_and_clears_once_restamped(self):
        ps = self.session()
        self.stamp(999)  # the sentinel names someone else: the lapse the core reported
        sig = self.probe(ps)
        self.assertEqual(sig.kind, wp.WATCHER_NOT_READY)
        self.assertIn("running but not ready", sig.reason)
        self.assertEqual((sig.subject, sig.unblock), (self.inbox, "re-arm the core's streaming task watcher"))
        self.stamp(100)
        self.assertIsNone(self.probe(ps), "recovery clears it")

    def test_no_watcher_at_all_fires(self):
        sig = self.probe(self.proc(300, "python3", "something-else.py"))
        self.assertIn("no task watcher is running", sig.reason)

    def test_a_standby_covering_the_inbox_is_no_wedge(self):
        self.assertIsNone(self.probe(self.standby()))

    def test_a_dead_core_expects_no_watcher(self):
        self.assertIsNone(self.probe("", core_alive=False))

    def test_an_unreadable_process_table_claims_nothing(self):
        with unittest.mock.patch.object(wp, "_ps", return_value=None):
            self.assertIsNone(wp.watcher_not_ready(self.inbox, self.state, True))

    def test_another_inbox_is_not_ours(self):
        other = self.proc(400, "bash", "src/watch-tasks-stream.sh", "--role", "session", "--inbox", "/elsewhere")
        self.assertIn("no task watcher is running", self.probe(other).reason)


class CoreStatusStale(Fixture):
    def test_running_past_the_threshold_fires_dated_by_its_last_ts(self):
        ts = time.time() - 900
        self.write("core-status.json", {"status": "running", "step": "triage", "ts": ts})
        sig = wp.core_status_stale(self.ws)
        self.assertEqual((sig.kind, sig.since), (wp.CORE_STATUS_STALE, ts))
        self.assertIn("'triage' for 15 min", sig.reason)

    def test_a_slow_but_moving_pass_is_no_wedge(self):
        self.write("core-status.json", {"status": "running", "step": "triage", "ts": time.time() - 300})
        self.assertIsNone(wp.core_status_stale(self.ws))

    def test_idle_however_old_is_no_wedge(self):
        self.write("core-status.json", {"status": "idle", "ts": time.time() - 86400})
        self.assertIsNone(wp.core_status_stale(self.ws))

    def test_missing_or_malformed_claims_nothing(self):
        self.assertIsNone(wp.core_status_stale(self.ws))
        (self.ws / "state" / "core-status.json").write_text("{half")
        self.assertIsNone(wp.core_status_stale(self.ws))
        for ts in (None, True, "1"):
            self.assertIsNone(wp.core_status_verdict({"status": "running", "ts": ts}, time.time()))

    def test_health_check_reports_the_same_rule(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hc_wedge", REPO / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        with unittest.mock.patch.object(hc, "WORKSPACE_DIR", self.ws):
            self.write("core-status.json", {"status": "running", "step": "s", "ts": time.time() - 900})
            self.assertEqual(hc.check_core_proactive_loop()["status"], "warn")
            self.write("core-status.json", {"status": "running", "step": "s", "ts": time.time() - 30})
            self.assertEqual(hc.check_core_proactive_loop()["status"], "ok")
            for state in sorted(wp.NEEDS_USER):
                self.write("core-supervisor.json", {"state": state})
                self.assertIn("core needs you", hc.check_core_supervisor()["detail"])


class PanePromptBlocked(Fixture):
    def test_a_fresh_prompt_verdict_fires_with_its_excerpt_and_clears(self):
        self.write("core-supervisor.json", {"state": "blocked-human", "prompt": "Allow edit?\n 1. Yes",
                                            "session": "sutando-core"})
        sig = wp.pane_prompt_blocked(self.ws)
        self.assertEqual(sig.kind, wp.PANE_PROMPT_BLOCKED)
        self.assertEqual(sig.reason, "sutando-core is waiting on a prompt: Allow edit?")
        self.write("core-supervisor.json", {"state": "running", "session": "sutando-core"})
        self.assertIsNone(wp.pane_prompt_blocked(self.ws), "recovery clears it")

    def test_signed_out_fires_with_its_own_remedy(self):
        self.write("core-supervisor.json", {"state": "logged-out", "session": "sutando-core"})
        self.assertIn("/login", wp.pane_prompt_blocked(self.ws).unblock)

    def test_a_prompt_with_no_text_uses_the_detail(self):
        self.write("core-supervisor.json", {"state": "blocked-human", "detail": "selection prompt"})
        self.assertEqual(wp.pane_prompt_blocked(self.ws).reason, "the core is waiting on a prompt: selection prompt")

    def test_a_stale_verdict_or_none_claims_nothing(self):
        self.assertIsNone(wp.pane_prompt_blocked(self.ws))
        self.write("core-supervisor.json", {"state": "blocked-human"}, age=wp.SUPERVISOR_FRESH_S + 5)
        self.assertIsNone(wp.pane_prompt_blocked(self.ws), "an old verdict says nothing about the pane now")
        self.write("core-supervisor.json", ["not", "an", "object"])
        self.assertIsNone(wp.pane_prompt_blocked(self.ws))


class ProbeAllAndReport(Fixture):
    def test_one_pass_finds_every_wedge_quickly(self):
        self.write("core-status.json", {"status": "running", "step": "s", "ts": time.time() - 900})
        self.write("core-supervisor.json", {"state": "logged-out"})
        start = time.monotonic()
        found = wp.probe_all(self.ws, core_alive=True, ps_output="")
        self.assertLess(time.monotonic() - start, 1.0, "cheap enough for a 15 s detector cadence")
        self.assertEqual(sorted(s.kind for s in found),
                         sorted([wp.WATCHER_NOT_READY, wp.CORE_STATUS_STALE, wp.PANE_PROMPT_BLOCKED]))

    def test_a_dead_core_reports_only_what_the_supervisor_says(self):
        self.write("core-status.json", {"status": "running", "step": "s", "ts": time.time() - 900})
        self.assertEqual(wp.probe_all(self.ws, core_alive=False, ps_output=""), [])

    def test_core_liveness_defaults_to_health_checks_rule(self):
        with unittest.mock.patch.object(wp, "_core_alive_default", return_value=False) as alive:
            self.assertEqual(wp.probe_all(self.ws, ps_output=""), [])
        alive.assert_called_once_with(self.ws)
        self.assertFalse(wp._core_alive_default(self.ws), "no heartbeat in a fresh workspace")

    def test_report_hands_each_signature_to_the_detector_and_nothing_else(self):
        sig = wp.Signature("k", "r", "u", "s", since=1.0, task_id="t")
        got = []
        self.assertEqual(wp.report([sig], publisher=got.append), 1)
        self.assertEqual(got, [sig.as_dict()])
        with unittest.mock.patch.dict(sys.modules, {"wedge_detector": None}):
            self.assertEqual(wp.report([sig]), 0, "with no detector installed nothing is published")
        fake = unittest.mock.Mock()
        with unittest.mock.patch.dict(sys.modules, {"wedge_detector": fake}):
            self.assertEqual(wp.report([sig]), 1)
        fake.publish_signature.assert_called_once_with(sig.as_dict())

    def test_the_cli_prints_and_exits_by_what_it_found(self):
        sig = wp.Signature("k", "r", "u", "s")
        for found, code, text in (([], 0, ""), ([sig], 1, "k: r -- u\n")):
            out = io.StringIO()
            with unittest.mock.patch.object(wp, "probe_all", return_value=found), contextlib.redirect_stdout(out):
                self.assertEqual(wp.main(["--workspace", str(self.ws)]), code)
            self.assertEqual(out.getvalue(), text)
        out = io.StringIO()
        with unittest.mock.patch.object(wp, "probe_all", return_value=[sig]), contextlib.redirect_stdout(out):
            wp.main(["--workspace", str(self.ws), "--json"])
        self.assertEqual(json.loads(out.getvalue())["kind"], "k")

    def test_the_real_process_table_is_read_once_per_question(self):
        class Done:
            returncode, stdout = 0, ""
        with unittest.mock.patch.object(wp.subprocess, "run", return_value=Done()) as run:
            self.assertEqual(wp._ps(), "")
        run.assert_called_once()
        Done.returncode = 1
        with unittest.mock.patch.object(wp.subprocess, "run", return_value=Done()):
            self.assertIsNone(wp._ps())
        with unittest.mock.patch.object(wp.subprocess, "run", side_effect=OSError):
            self.assertIsNone(wp._ps())


if __name__ == "__main__":
    unittest.main()
