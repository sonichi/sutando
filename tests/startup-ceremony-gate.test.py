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


def _workspace(root: Path, *, started_at: float, stamp_ts: float | None) -> Path:
    ws = root / "workspace"
    cfg = ws / "hosts" / HOST / "crons.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps(ENTRIES))
    state = ws / "state"
    state.mkdir()
    (state / "session-starts.log").write_text(
        json.dumps({"host": HOST, "session_started_at": started_at}) + "\n"
    )
    if stamp_ts is not None:
        (cfg.parent / "schedule-crons-stamp.json").write_text(
            json.dumps({"ts": stamp_ts, "registered": 1, "config_total": 1})
        )
    return ws


def _run(ws: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, SUTANDO_HOST_LABEL=HOST)
    return subprocess.run(
        [sys.executable, str(GATE), "--workspace", str(ws), "--host-label", HOST],
        capture_output=True, text=True, env=env, timeout=60,
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
            env = dict(os.environ, SUTANDO_HOST_LABEL=HOST)
            r = subprocess.run([sys.executable, str(dst), "--workspace", str(ws), "--host-label", HOST],
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 2, f"rc={r.returncode}\n{r.stderr}")
            self.assertIn("probe unavailable", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
