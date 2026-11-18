#!/usr/bin/env python3
"""Tests for skills/startup/scripts/verify-ceremony.py — the /startup step-3 gate.

The failure it closes: cron registration done by hand (`CronCreate` from the crons.json list)
passes every cheap check and never writes `schedule-crons-stamp.json`, so the desktop app's
ceremony-health reads the session as never-completed and re-sends `/startup` every 10 minutes,
indefinitely. The gate runs the same stamp-vs-session-boundary probe in-session so `/startup`
cannot claim completion while the app would still call it diverged.

Black-box: drives the real CLI in a subprocess. `SUTANDO_HOST_LABEL` makes the fixture host read
as local, which is what lets the session boundary resolve from `state/session-starts.log`.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "skills" / "startup" / "scripts" / "verify-ceremony.py"
HOST = "gate-test-host"
ENTRIES = [{"name": "main-loop", "cron": "*/15 * * * *", "prompt_skill": "proactive-loop"}]

# Subprocess coverage is opt-in in this repo (see runtime-cli-surface.test.py): under the
# gate, run the child through `coverage run` so the repo-path arms measure the real file.
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]


def _workspace(root: Path, *, started_at: float, stamp_ts: float | None,
               entries: list | None = None, registered: int | None = None) -> Path:
    entries = ENTRIES if entries is None else entries
    ws = root / "workspace"
    cfg = ws / "hosts" / HOST / "crons.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps(entries))
    state = ws / "state"
    state.mkdir()
    (state / "session-starts.log").write_text(
        json.dumps({"host": HOST, "session_started_at": started_at}) + "\n"
    )
    if stamp_ts is not None:
        n = len(entries)
        (cfg.parent / "schedule-crons-stamp.json").write_text(
            json.dumps({"ts": stamp_ts, "registered": n if registered is None else registered,
                        "config_total": n})
        )
    return ws


def _run(ws: Path) -> subprocess.CompletedProcess:
    env = isolated_child_env(SUTANDO_HOST_LABEL=HOST)
    return subprocess.run(
        PYBASE + [str(GATE), "--workspace", str(ws), "--host-label", HOST],
        capture_output=True, text=True, env=env, timeout=60, cwd=REPO,
    )


class VerifyCeremonyGate(unittest.TestCase):
    def test_stale_stamp_refuses_completion(self):
        """The hand-rolled-registration case: stamp predates this session's launch."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=900_000.0)
            r = _run(ws)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("predates this session", r.stderr)
            self.assertIn("NOT complete", r.stderr)
            self.assertIn("/schedule-crons", r.stderr)
            self.assertNotIn("ok", r.stdout)

    def test_no_stamp_refuses_completion(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=None)
            r = _run(ws)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("never stamped", r.stderr)

    def test_fresh_stamp_passes(self):
        """Stamp written after the boundary — /schedule-crons completed this boot."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_300.0)
            r = _run(ws)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("verify-ceremony: ok", r.stdout)

    def test_stamp_write_flips_the_verdict(self):
        """One workspace, both verdicts: proves the gate reads the stamp, not a constant."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=900_000.0)
            self.assertEqual(_run(ws).returncode, 1)
            (ws / "hosts" / HOST / "schedule-crons-stamp.json").write_text(
                json.dumps({"ts": 1_000_300.0, "registered": 1, "config_total": 1})
            )
            self.assertEqual(_run(ws).returncode, 0)

    def test_empty_crons_with_stale_stamp_refuses_completion(self):
        """crons.json is `[]` (owner paused every job): the health probe calls a stale stamp ok
        because nothing is expected, but the desktop compares only stamp vs boundary and would
        re-send /startup every 10 minutes. The gate must hold the desktop's criterion."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=900_000.0, entries=[])
            r = _run(ws)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("predates this session", r.stderr)
            self.assertIn("NOT complete", r.stderr)

    def test_empty_crons_with_fresh_zero_stamp_passes(self):
        """A ceremony that legitimately registered nothing completes once it stamps."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_300.0, entries=[])
            r = _run(ws)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_empty_crons_without_stamp_passes(self):
        """No stamp at all is "no evidence" for the desktop too; never a re-send."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=None, entries=[])
            self.assertEqual(_run(ws).returncode, 0)

    def test_stamp_within_launch_skew_passes(self):
        """The desktop allows STAMP_SKEW_TOLERANCE_SECS of launcher skew; the gate must not be
        stricter, nor looser: one second past the tolerance is refused."""
        skew = _load_gate_module().STAMP_SKEW_TOLERANCE_SECS
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_000.0 - skew, entries=[])
            self.assertEqual(_run(ws).returncode, 0)
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_000.0 - skew - 1, entries=[])
            self.assertEqual(_run(ws).returncode, 1)

    def test_fallback_stamp_after_rerun_clears_the_desktop_resend(self):
        """Empty crons.json: /schedule-crons registers the fallback /proactive-loop and stamps
        `registered: 1` against 0 session-owned. The gate stays rc 1 (surplus), but the stamp is
        fresh, so the desktop criterion (stamp vs launch) no longer re-sends /startup. Repeated
        re-runs give the identical verdict: the gate cannot converge it, so the one-retry bound
        on /startup's rc-1 path is skill prose, not code."""
        with tempfile.TemporaryDirectory() as td:
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_300.0,
                            entries=[], registered=1)
            verdicts = []
            for rerun_ts in (1_000_300.0, 1_000_400.0, 1_000_500.0):
                (ws / "hosts" / HOST / "schedule-crons-stamp.json").write_text(
                    json.dumps({"ts": rerun_ts, "registered": 1, "config_total": 0}))
                r = _run(ws)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("only 0 are session-owned", r.stderr)
                self.assertNotIn("predates this session", r.stderr)
                verdicts.append(r.stderr)
            self.assertEqual(len(set(verdicts)), 1, verdicts)
            gate = _load_gate_module()
            hc = gate.load_health_check()
            from unittest import mock
            with mock.patch.dict(os.environ, {"SUTANDO_HOST_LABEL": HOST}):
                self.assertIsNone(gate.stamp_predates_launch(hc, ws, HOST))

    def test_missing_probe_is_rc2_not_rc1(self):
        """Copy-deployed tree: the script exists, src/health-check.py does not. Must be rc 2
        ("cannot answer"), never rc 1 — rc 1 tells the agent to run /schedule-crons and retry,
        which can never fix an unimportable probe and re-creates the re-send loop."""
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "copy"
            dst = tree / "skills" / "startup" / "scripts" / "verify-ceremony.py"
            dst.parent.mkdir(parents=True)
            dst.write_text(GATE.read_text())
            (tree / "src").mkdir()  # src/ present, health-check.py absent — the reviewer's second case
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_300.0)
            env = isolated_child_env(SUTANDO_HOST_LABEL=HOST)
            r = subprocess.run([sys.executable, str(dst), "--workspace", str(ws), "--host-label", HOST],
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 2, f"rc={r.returncode}\n{r.stderr}")
            self.assertIn("probe unavailable", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_probe_that_raises_at_import_is_rc2(self):
        """The other half of the fix: the probe file EXISTS but fails to import. Must be rc 2,
        no traceback — the same "cannot answer" verdict as a missing file."""
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td) / "copy"
            dst = tree / "skills" / "startup" / "scripts" / "verify-ceremony.py"
            dst.parent.mkdir(parents=True)
            dst.write_text(GATE.read_text())
            (tree / "src").mkdir()
            (tree / "src" / "health-check.py").write_text('raise RuntimeError("boom at import")\n')
            ws = _workspace(Path(td), started_at=1_000_000.0, stamp_ts=1_000_300.0)
            env = isolated_child_env(SUTANDO_HOST_LABEL=HOST)
            r = subprocess.run([sys.executable, str(dst), "--workspace", str(ws), "--host-label", HOST],
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 2, f"rc={r.returncode}\n{r.stderr}")
            self.assertIn("probe unavailable", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


def _load_gate_module():
    """Import the gate from its REPO path so coverage attributes the run to the real file —
    the subprocess arms above copy it to a temp tree, which measures nothing in-repo."""
    spec = importlib.util.spec_from_file_location("verify_ceremony", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ProbeContract(unittest.TestCase):
    def test_real_health_check_exports_every_name_the_gate_reads(self):
        """A rename in src/health-check.py must fail here, not degrade the gate in the field."""
        gate = _load_gate_module()
        hc = gate.load_health_check()
        self.assertIsNotNone(hc, "src/health-check.py failed to import")
        missing = [n for n in gate.REQUIRED_PROBE_NAMES if not hasattr(hc, n)]
        self.assertEqual(missing, [])

    def test_required_names_cover_the_helpers_the_gate_calls(self):
        """Guards the list itself: every `mod.<name>` the gate source reads is declared."""
        import re
        used = set(re.findall(r"\bmod\.([A-Za-z_]\w*)", GATE.read_text()))
        self.assertEqual(used - set(_load_gate_module().REQUIRED_PROBE_NAMES), set())


_OK_PROBE = (
    "def check_session_cron_registration(ws, host_label=None):\n"
    "    return {'status': 'ok', 'detail': 'stub'}\n"
    "WORKSPACE_DIR = None\n"
    "def _host_label():\n    return 'h'\n"
)


class VerifyCeremonyGateInProcess(unittest.TestCase):
    def _main_with_repo(self, repo: Path) -> tuple[int, str]:
        mod = _load_gate_module()
        mod.REPO = repo
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mod.main(["--workspace", str(repo / "ws"), "--host-label", HOST])
        return rc, err.getvalue()

    def test_missing_probe_rc2_in_process(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td); (repo / "src").mkdir()
            rc, err = self._main_with_repo(repo)
            self.assertEqual(rc, 2, err)
            self.assertIn("probe unavailable", err)

    def test_raising_probe_rc2_in_process(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td); (repo / "src").mkdir()
            (repo / "src" / "health-check.py").write_text('raise RuntimeError("boom at import")\n')
            rc, err = self._main_with_repo(repo)
            self.assertEqual(rc, 2, err)
            self.assertIn("probe unavailable", err)
            self.assertNotIn("Traceback", err)

    def test_probe_exiting_at_import_is_rc2_in_process(self):
        """`except SystemExit: pass` then no probe attribute → None → rc 2."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td); (repo / "src").mkdir()
            (repo / "src" / "health-check.py").write_text("raise SystemExit(0)\n")
            rc, err = self._main_with_repo(repo)
            self.assertEqual(rc, 2, err)

    def test_missing_private_helper_is_rc2_in_process(self):
        """A renamed `_last_core_launch_at` used to raise AttributeError → traceback → rc 1."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td); (repo / "src").mkdir()
            (repo / "src" / "health-check.py").write_text(_OK_PROBE)
            rc, err = self._main_with_repo(repo)
            self.assertEqual(rc, 2, err)
            self.assertIn("probe unavailable", err)

    def test_drifted_helper_signature_is_rc2_in_process(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td); (repo / "src").mkdir()
            (repo / "src" / "health-check.py").write_text(
                _OK_PROBE + "def _last_core_launch_at():\n    return None\n")
            rc, err = self._main_with_repo(repo)
            self.assertEqual(rc, 2, err)
            self.assertIn("launch-boundary check unavailable", err)

    def test_unloadable_spec_is_rc2_in_process(self):
        """spec_from_file_location → None (unknown suffix) is guarded too; force it."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td); (repo / "src").mkdir()
            (repo / "src" / "health-check.py").write_text("x = 1\n")
            mod = _load_gate_module(); mod.REPO = repo
            with mock.patch.object(mod.importlib.util, "spec_from_file_location", return_value=None):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = mod.main(["--workspace", str(repo / "ws"), "--host-label", HOST])
            self.assertEqual(rc, 2, err.getvalue())


if __name__ == "__main__":
    unittest.main()
