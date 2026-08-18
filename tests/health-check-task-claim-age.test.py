#!/usr/bin/env python3
"""check_task_claim_age must make a LEAKED handler claim loud while it is leaking,
    not only at watcher exit. Run: python3 tests/health-check-task-claim-age.test.py"""
from __future__ import annotations
import importlib.util
import json
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

    def test_a_leaked_claim_never_renders_an_unknown_age_as_zero(self):
        """`age or 0.0` turned an untrusted ledger into "oldest 0.0h" inside a
        `down` verdict — the sibling branches count `age_unknown` for a reason."""
        tasks = self.ws / "tasks"; tasks.mkdir(parents=True, exist_ok=True)
        self.claims.mkdir(parents=True, exist_ok=True)
        (self.claims / "task-leaked.txt").write_text(
            "999999\nwatcher-id\n%s\nmust-handle\n" % (tasks / "task-gone.txt"))
        with mock.patch.object(self.hc, "_claim_ages", return_value=({}, False)):
            out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down", out["detail"])
        self.assertIn("age unknown", out["detail"])
        self.assertNotIn("0.0h", out["detail"])

    def test_a_fresh_leak_reads_in_seconds_not_zero_hours(self):
        """Leak detection is age-INDEPENDENT — the owner pid is gone — so a
        seconds-old leak is the common case, not a corner. `_claim` uses a LIVE
        pid, which takes the grace-gated branch instead; this needs a dead one."""
        tasks = self.ws / "tasks"; tasks.mkdir(parents=True, exist_ok=True)
        self.claims.mkdir(parents=True, exist_ok=True)
        (self.claims / "task-fresh.txt").write_text(
            "999999\nwatcher-id\n%s\nmust-handle\n" % (tasks / "task-gone.txt"))
        out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down", out["detail"])
        self.assertNotIn("0.0h", out["detail"])
        self.assertRegex(out["detail"], r"oldest \d+s")

    def test_the_named_leak_is_the_OLDEST_not_the_youngest(self):
        """Review finding on the first draft: sorting ascending to put `None`
        first also reversed the known ages, so the line named the youngest leak
        "oldest". A single-claim fixture cannot see it — every ordering agrees."""
        tasks = self.ws / "tasks"; tasks.mkdir(parents=True, exist_ok=True)
        self.claims.mkdir(parents=True, exist_ok=True)
        for name in ("task-old.txt", "task-new.txt"):
            (self.claims / name).write_text(
                "999999\nwatcher-id\n%s\nmust-handle\n" % (tasks / "task-gone.txt"))
        ages = {"task-old.txt": 6 * 3600.0, "task-new.txt": 60.0}
        with mock.patch.object(self.hc, "_claim_ages", return_value=(ages, True)):
            out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down", out["detail"])
        self.assertIn("oldest 6.0h", out["detail"])
        self.assertIn("task-old.txt", out["detail"])
        self.assertNotIn("task-new.txt", out["detail"])

    def test_an_unknown_age_still_outranks_a_known_one(self):
        """Unknown sorts first even against an older known age: nothing can
        vouch for it, so it is the one the operator must look at."""
        tasks = self.ws / "tasks"; tasks.mkdir(parents=True, exist_ok=True)
        self.claims.mkdir(parents=True, exist_ok=True)
        for name in ("task-known.txt", "task-unknown.txt"):
            (self.claims / name).write_text(
                "999999\nwatcher-id\n%s\nmust-handle\n" % (tasks / "task-gone.txt"))
        with mock.patch.object(self.hc, "_claim_ages",
                               return_value=({"task-known.txt": 9 * 3600.0}, False)):
            out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "down", out["detail"])
        self.assertIn("age unknown", out["detail"])
        self.assertIn("task-unknown.txt", out["detail"])

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

    def test_a_running_bounded_claim_is_not_counted_as_zero_running(self):
        """The measured live shape: two `must-handle` claims on tasks that exist,
        both inside the bound. The count must not read the leak condition."""
        self._claim("task-43f5aca3ff8332f997.txt", 30)
        self._claim("task-d70e2596cf7044645a.txt", 30)
        out = self.hc.check_task_claim_age(workspace_dir=self.ws)
        self.assertEqual(out["status"], "ok", out["detail"])
        self.assertNotIn("0 still queued or running", out["detail"])
        self.assertIn("2 held claim(s), 2 still queued or running", out["detail"])

    def test_empty_claims_dir_is_ok(self):
        self.claims.mkdir(parents=True, exist_ok=True)
        self.assertEqual(
            self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok"
        )

    def test_absent_claims_dir_is_ok(self):
        """A host that never dispatched has no directory; an absent-is-warn probe warns
        forever and gets ignored, losing the alarm this exists to raise."""
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

    # Thresholds track SUTANDO_TIER_HARD_TIMEOUT (configurable), not a constant:
    # a fixed threshold pages on live work as soon as a deployment raises it.

    def test_raised_hard_timeout_does_not_page_on_an_in_flight_handler(self):
        """Hard timeout 7200s with a 1900s live claim returned warn against a fixed 1800s
        bound. A handler still inside its permitted run must stay ok."""
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
        """session-worker rejects the same values, so the handler is not running with them
        either — never fail toward a threshold that pages on a permitted run."""
        self._claim("task-x.txt", 1000)          # < 2*900 warn, > 2*60 if misparsed as small
        for bad in ("", "abc", "0", "-5"):
            with mock.patch.dict(os.environ, {"SUTANDO_TIER_HARD_TIMEOUT": bad}):
                self.assertEqual(
                    self.hc.check_task_claim_age(workspace_dir=self.ws)["status"], "ok",
                    f"unusable value {bad!r} must fall back to the 900s default",
                )

    def test_unreadable_claim_is_skipped_not_fatal(self):
        """A release racing the scan makes stat() raise; that is normal operation, so the
        probe must skip the entry and still judge the rest."""
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
        """Raises only for claim FILES: Path.is_dir() calls stat(), so a blanket raise breaks
        the probe's own directory check and exercises the wrong failure."""
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

    def test_the_registry_is_outside_the_workspace_WITHOUT_git_initing_it(self):
        # The previous version ran `git init` on the temp workspace, so it
        # CONSTRUCTED the condition it verified. A plain dir is the real case.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)                      # deliberately NOT a git repo
            p = self.hc._claim_observations_path(ws)
            self.assertIsNotNone(p, "engine checkout should supply a store")
            self.assertNotIn(str(ws.resolve()), str(p),
                             "registry must not live under the workspace: " + str(p))

    def test_no_engine_git_dir_yields_NONE_never_a_workspace_path(self):
        # Discriminating: forces the branch the happy path never reaches, so a
        # reintroduced workspace-relative fallback turns this red.
        import subprocess as _sp
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            fail = _sp.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
            with mock.patch.object(self.hc.subprocess, "run", return_value=fail):
                p = self.hc._claim_observations_path(ws)
            self.assertIsNone(
                p, "with no engine git dir the store must be None, not a "
                   "workspace path that sync can carry: " + str(p))

    def test_age_is_reported_UNAVAILABLE_when_no_unsynced_store_exists(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            with mock.patch.object(self.hc, "_claim_observations_path",
                                   return_value=None):
                r = self.hc.check_task_claim_age(ws)
            self.assertEqual(r["status"], "warn", r["detail"])
            self.assertIn("unavailable", r["detail"])

    def test_a_corrupt_registry_does_not_reseed_from_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            p = self.hc._claim_observations_path(ws)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{ this is not json")
            r = self.hc.check_task_claim_age(ws)
            self.assertEqual(r["status"], "warn", r["detail"])
            self.assertIn("unavailable", r["detail"])

    def test_a_recorded_first_sighting_is_never_moved_FORWARD(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            f = self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            self.hc.check_task_claim_age(ws)           # records the sighting
            p = self.hc._claim_observations_path(ws)
            before = json.loads(p.read_text())["task-1.txt"]
            now = time.time()
            os.utime(f, (now, now))                    # the sync replay
            self.hc.check_task_claim_age(ws)
            after = json.loads(p.read_text())["task-1.txt"]
            self.assertEqual(before, after,
                             "a first sighting moved forward under an mtime refresh")

    def test_rev_parse_raising_yields_no_store(self):
        # subprocess itself blowing up (git missing) must not crash the probe.
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(self.hc.subprocess, "run",
                                   side_effect=OSError("no git")):
                self.assertIsNone(self.hc._claim_observations_path(Path(td)))

    def test_a_nonexistent_git_dir_yields_no_store(self):
        with tempfile.TemporaryDirectory() as td:
            ok = type("R", (), {"returncode": 0, "stdout": "definitely-not-here"})()
            with mock.patch.object(self.hc.subprocess, "run", return_value=ok):
                self.assertIsNone(self.hc._claim_observations_path(Path(td)))

    def test_an_unwritable_store_degrades_to_untrusted(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")):
                r = self.hc.check_task_claim_age(ws)
            self.assertEqual(r["status"], "warn", r["detail"])
            self.assertIn("unavailable", r["detail"])

    def test_a_registry_holding_a_non_dict_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            p = self.hc._claim_observations_path(ws)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('["a list, not a dict"]')
            r = self.hc.check_task_claim_age(ws)
            self.assertEqual(r["status"], "down", r["detail"])

    def test_a_flock_failure_degrades_to_untrusted(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._claim_in(ws, "task-1.txt", os.getpid(), "must-handle", 40.0, True)
            with mock.patch.object(self.hc.fcntl, "flock",
                                   side_effect=OSError("locking unsupported")):
                r = self.hc.check_task_claim_age(ws)
            self.assertEqual(r["status"], "warn", r["detail"])

    def test_untrusted_age_still_reports_a_DEAD_OWNER_as_down(self):
        # The reviewer's P1: a dead owner is knowable without any age, so an
        # unusable ledger must not downgrade it to warn.
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self._claim_in(ws, "task-1.txt", 999999, "fallback", 0.1, True)
            with mock.patch.object(self.hc, "_claim_ages", return_value=({}, False)):
                r = self.hc.check_task_claim_age(ws)
            self.assertEqual(r["status"], "down", r["detail"])
            self.assertIn("owner process gone", r["detail"])

    def test_the_probe_resolves_git_through_git_argv_not_a_bare_binary(self):
        # A bare `git` can raise the Xcode CLT dialog on a clean macOS box.
        src = (REPO / "src" / "health-check.py").read_text()
        i = src.index("def _claim_observations_path")
        body = src[i:src.index("def _claim_ages")]
        self.assertIn("git_argv(", body)
        self.assertNotIn('["git"', body)

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
