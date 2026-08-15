#!/usr/bin/env python3
"""Regression test: check_task_claim_age must make a LEAKED task-handler claim
loud while it is leaking, instead of only at watcher exit.

The gap it covers: `watch-tasks-stream.sh` takes a claim in
state/task-event-handler-claims/ before dispatching a task to
$SUTANDO_TASK_EVENT_HANDLER and releases it on completion. A claim that is
never released takes NO error path, so nothing is logged and every other probe
reads healthy. It surfaces only when the watcher exits, where
fallback_outstanding_handlers() publishes one user-visible terminal failure per
held claim — so a slow leak's first and only symptom is a flood at restart.

Measured 2026-08-14: 34 claims accumulated over 21h, oldest 31.2h, and drained
as 34 Discord messages in two seconds on restart. The retired watcher's entire
captured stderr was 228 lines — 194 task events plus those 34 shutdown lines,
and nothing else. Zero handler failures. Nothing could have reported the leak.

Run: python3 tests/health-check-task-claim-age.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_claim_age_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestTaskClaimAge(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.claims = self.ws / "state" / "task-event-handler-claims"

    def tearDown(self):
        self._tmp.cleanup()

    def _claim(self, name: str, age_s: float) -> Path:
        self.claims.mkdir(parents=True, exist_ok=True)
        # The task must EXIST: these cases are about a bounded handler running
        # too long, not about a claim whose task was already archived.
        tasks = self.ws / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        task = tasks / name
        task.write_text("task\n")
        path = self.claims / name
        path.write_text("%d\nwatcher-id\n%s\nmust-handle\n" % (os.getpid(), task))
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    # --- the assertions that must FAIL in the broken state -------------------

    def test_leaked_claim_reports_down(self):
        """A 31h claim — the measured 2026-08-14 case — must read `down`."""
        self._claim("task-1786641305509.txt", 31.2 * 3600)
        out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down")
        self.assertIn("31.2h", out["detail"])

    def test_aging_claim_reports_warn(self):
        """Past any bounded handler run (codex-bounded caps at 240s) but not yet
        `down`: the window where the leak is still cheap to catch."""
        self._claim("task-1786641305509.txt", 45 * 60)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "warn"
        )

    def test_oldest_claim_decides_not_the_count(self):
        """Many fresh claims must not average away one old one — a busy handler
        would otherwise mask the leak exactly when it is worst."""
        for i in range(12):
            self._claim(f"task-fresh-{i}.txt", 5)
        self._claim("task-old.txt", 9 * 3600)
        out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down")
        self.assertIn("13 held claim(s)", out["detail"])
        self.assertIn("task-old.txt", out["detail"])

    # --- and the clean states, so the probe cannot be trivially always-down ---

    def test_in_flight_claim_is_ok(self):
        """A claim younger than one handler run is normal operation."""
        self._claim("task-1786641305509.txt", 30)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_empty_claims_dir_is_ok(self):
        self.claims.mkdir(parents=True, exist_ok=True)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_absent_claims_dir_is_ok(self):
        """A host that never dispatched to the handler has no directory, and
        that is not a fault — an absent-is-warn probe warns forever and gets
        ignored, which is how the alarm this exists to raise would be lost."""
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_non_claim_files_are_ignored(self):
        """Release/retire use `.stale-*` and `.claim-*` temporaries in the same
        directory; only published `task-*.txt` claims are held work."""
        self.claims.mkdir(parents=True, exist_ok=True)
        stale = self.claims / ".stale-watcher-task-1.txt"
        stale.write_text("x")
        old = time.time() - 40 * 3600
        os.utime(stale, (old, old))
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_probe_is_registered_in_the_report(self):
        """A probe that exists but is never appended to `checks` cannot fail."""
        source = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_task_claim_age())", source)

    # --- thresholds track the HANDLER's configured bound, not a constant -----
    # Claims wrap session-worker.py, whose hard limit is SUTANDO_TIER_HARD_TIMEOUT
    # (default 900s, explicitly configurable). A fixed threshold pages on live work
    # the moment a deployment raises that timeout.

    def test_raised_hard_timeout_does_not_page_on_an_in_flight_handler(self):
        """The reviewer's control on #2906: hard timeout 7200s, a 1900s live claim.

        Against a fixed 1800s warn this returned `warn` for a handler still well
        inside its permitted run. It must stay `ok`."""
        self._claim("task-live.txt", 1900)
        with mock.patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "7200"}):
            out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "ok")

    def test_raised_hard_timeout_still_pages_past_its_own_multiple(self):
        """Deriving from the bound must not disable the probe — 7200s bound still
        warns past 2x and goes down past 8x."""
        self._claim("task-stuck.txt", 7200 * 3)
        with mock.patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "7200"}):
            self.assertEqual(
                self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "warn"
            )

    def test_lowered_hard_timeout_tightens_the_threshold(self):
        """A 60s bound makes a 300s claim (5x) late — a fixed 1800s would miss it."""
        self._claim("task-late.txt", 300)
        with mock.patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": "60"}):
            self.assertEqual(
                self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "warn"
            )

    def test_unusable_hard_timeout_falls_back_to_the_handler_default(self):
        """session-worker rejects non-positive/unparseable values too, so the
        handler is not running with them either — 900s is the honest assumption.
        Never fail toward a threshold that pages on a permitted run."""
        self._claim("task-x.txt", 1000)          # < 2*900 warn, > 2*60 if misparsed as small
        for bad in ("", "abc", "0", "-5"):
            with mock.patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": bad}):
                self.assertEqual(
                    self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok",
                    f"unusable value {bad!r} must fall back to the 900s default",
                )

    def test_unreadable_claim_is_skipped_not_fatal(self):
        """A claim released mid-scan makes stat() raise. The probe must skip that
        entry and still judge the rest — a release racing the health check is
        normal operation, not an error."""
        self._claim("task-old.txt", 9 * 3600)
        real_stat = Path.stat

        def flaky(self_path, *a, **kw):
            if self_path.name == "task-gone.txt":
                raise OSError(2, "No such file or directory")
            return real_stat(self_path, *a, **kw)

        self._claim("task-gone.txt", 10)
        with mock.patch.object(Path, "stat", flaky):
            out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down")
        self.assertIn("1 held claim(s)", out["detail"])   # the vanished one is not counted

    def test_all_claims_unreadable_reads_as_empty_not_broken(self):
        """Every entry racing at once is indistinguishable from an empty dir, and
        an empty dir is the honest report — the next run sees the truth.

        Raises only for claim FILES, never blanket-on-Path.stat: CPython 3.12's
        `Path.is_dir()` calls stat(), so a global raise breaks the probe's own
        directory check and the test exercises the wrong failure. It passed on
        3.14 locally and errored on 3.12 in CI for exactly that reason. The
        suffix matters too — the claims DIRECTORY is `task-event-handler-claims`,
        which also starts with `task-`.
        """
        self._claim("task-gone.txt", 10)
        real_stat = Path.stat

        def flaky(self_path, *a, **kw):
            if self_path.name.startswith("task-") and self_path.name.endswith(".txt"):
                raise OSError(2, "No such file or directory")
            return real_stat(self_path, *a, **kw)

        with mock.patch.object(Path, "stat", flaky):
            out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "ok")




class TestClaimExecutionContract(unittest.TestCase):
    """The task file is the progress signal; count and age cannot condemn."""

    def setUp(self):
        self.hc = _load_health_check()

    def _claim(self, ws, name, pid, disposition, age_h, task_exists):
        cd = ws / "state" / "task-event-handler-claims"
        cd.mkdir(parents=True, exist_ok=True)
        td = ws / "tasks"
        td.mkdir(parents=True, exist_ok=True)
        tp = td / name
        if task_exists:
            tp.write_text("task\n")
        f = cd / name
        f.write_text("%s\nWID\n%s\n%s\n" % (pid, tp, disposition))
        t = time.time() - age_h * 3600
        os.utime(f, (t, t))

    def _claim_in(self, ws, name, pid, disposition, age_h, task_exists):
        cd = ws / "state" / "task-event-handler-claims"
        cd.mkdir(parents=True, exist_ok=True)
        td = ws / "tasks"
        td.mkdir(parents=True, exist_ok=True)
        tp = td / name
        if task_exists:
            tp.write_text("task\n")
        f = cd / name
        f.write_text("%s\nWID\n%s\n%s\n" % (pid, tp, disposition))
        t = time.time() - age_h * 3600
        os.utime(f, (t, t))
        return f

    def _run(self, spec):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            for i, (pid, disp, age, exists) in enumerate(spec):
                self._claim(ws, "task-%d.txt" % i, pid, disp, age, exists)
            return self.hc.check_task_claim_age(ws)

    def test_a_burst_of_queued_claims_never_alarms(self):
        # Six fresh fallback claims with their tasks still present. The watcher
        # claims before queueing and runs 2 workers, so this is a normal burst.
        r = self._run([(os.getpid(), "fallback", 0.01, True)] * 6)
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_queued_claims_do_not_alarm_on_age_either(self):
        r = self._run([(os.getpid(), "fallback", 5.0, True)] * 6)
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_the_founding_leak_is_still_caught(self):
        # 34 claims whose tasks are already archived: the work finished and
        # nothing released them.
        r = self._run([(os.getpid(), "fallback", 31.0 - i * 0.5, False) for i in range(34)])
        self.assertEqual(r["status"], "down", r["detail"])
        self.assertIn("task already archived", r["detail"])

    def test_a_sync_refreshed_mtime_cannot_hide_a_stale_bounded_claim(self):
        # A sync replay rewrites claim mtime; the execution lifetime is
        # unchanged and must still alarm.
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            subprocess.run(["git", "init", "-q", str(ws)], check=False)
            self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            first = self.hc.check_task_claim_age(ws)
            self.assertEqual(first["status"], "down", first["detail"])
            f = ws / "state" / "task-event-handler-claims" / "task-1.txt"
            now = time.time()
            os.utime(f, (now, now))          # the sync replay
            after = self.hc.check_task_claim_age(ws)
            self.assertEqual(after["status"], "down",
                             "a refreshed mtime pushed a stale bounded claim "
                             "back under its threshold: " + after["detail"])
            self.assertIn("40.0h", after["detail"])

    def test_the_observation_registry_is_outside_the_synced_tree(self):
        # state/ is carryable by a vault.sync include; the git dir never is.
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            subprocess.run(["git", "init", "-q", str(ws)], check=False)
            p = self.hc._claim_observations_path(ws)
            self.assertIn(".git", str(p),
                          "observations must live under the git dir: " + str(p))

    def test_archive_before_claim_release_is_not_an_outage(self):
        # The reviewer's case: handler published, bridge archived the task,
        # and finish_handler_task has not processed HANDLER_DONE yet.
        r = self._run([(os.getpid(), "fallback", 0.01, False)])
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_a_stranded_old_claim_with_no_task_is_still_caught(self):
        r = self._run([(os.getpid(), "fallback", 2.0, False)])
        self.assertEqual(r["status"], "down", r["detail"])
        self.assertIn("task already archived", r["detail"])

    def test_the_grace_is_a_deliberate_interval_not_zero(self):
        self.assertGreaterEqual(self.hc._TASK_CLAIM_ARCHIVE_GRACE_S, 60)

    def test_a_single_leaked_claim_is_caught_at_any_age(self):
        # Strictly better than the count rule this replaced, which needed six.
        r = self._run([(os.getpid(), "fallback", 0.2, False)])
        self.assertEqual(r["status"], "down", r["detail"])

    def test_a_long_owner_session_is_not_leaked(self):
        r = self._run([(os.getpid(), "fallback", 31.2, True)])
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_a_dead_owner_is_leaked_even_with_the_task_present(self):
        r = self._run([(999999, "fallback", 0.1, True)])
        self.assertEqual(r["status"], "down", r["detail"])
        self.assertIn("owner process gone", r["detail"])

    def test_a_bounded_claim_past_its_hard_timeout_is_down(self):
        r = self._run([(os.getpid(), "must-handle", 31.2, True)])
        self.assertEqual(r["status"], "down", r["detail"])

    def test_a_young_bounded_claim_is_ok(self):
        self.assertEqual(self._run([(os.getpid(), "must-handle", 0.05, True)])["status"], "ok")

    def test_pid_liveness_edge_cases(self):
        with mock.patch.object(self.hc.os, "kill", side_effect=PermissionError):
            self.assertTrue(self.hc._pid_alive(1))
        for exc in (OverflowError, ValueError, OSError):
            with mock.patch.object(self.hc.os, "kill", side_effect=exc):
                self.assertFalse(self.hc._pid_alive(1))

    def test_an_unreadable_claim_yields_no_owner(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "task-x.txt"
            f.write_text("1\nWID\n/tmp/t\nfallback\n")
            with mock.patch.object(Path, "read_text", side_effect=OSError):
                self.assertEqual(self.hc._claim_owner(f), (None, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
