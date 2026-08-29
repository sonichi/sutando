#!/usr/bin/env python3
"""The bridge loop's probe-failure branches, driven end to end.

The tri-state probe rules were pinned at the row/candidate boundary but the
bridge LOOP's own branches — pgrep raising, the per-pid ps read failing, the
log-content override under an armed pin, and the orphan-note append on a
healthy bridge — were reachable only in production. Each case here drives the
real run_all_checks() (or the real check_port) with only subprocess faked.

Run: python3 tests/health-check-bridge-probe-coverage.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="ccd-bridge-probe-"))
_ccd = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_ccd.mkdir(parents=True, exist_ok=True)
(_ccd / "access.json").write_text("{}")

REPO = Path(__file__).resolve().parents[1]
PID = "515151"
LSTART = "Mon Aug 25 00:00:00 2026"


def _load():
    spec = importlib.util.spec_from_file_location("hc_bpc", REPO / "src/health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_bridges(fake_run, *, pin_pid=PID, log_text="ok\n"):
    """run_all_checks() with the given subprocess fake; returns slack row."""
    with tempfile.TemporaryDirectory() as td:
        ws, repo = Path(td) / "ws", Path(td) / "repo"
        (ws / "state").mkdir(parents=True)
        (ws / "logs").mkdir(parents=True)
        (ws / "logs" / "slack-bridge.log").write_text(log_text)
        (repo / "src").mkdir(parents=True)
        src = repo / "src" / "slack-bridge.py"
        src.write_text("# bridge\n")
        os.utime(src, (1000000000, 1000000000))   # far older than any lstart
        pins = {"pins": [{"service": "slack-bridge", "pid": pin_pid,
                          "lstart": LSTART, "reason": "witness",
                          "expires_at": "2099-01-01T00:00:00Z"}]}
        (ws / "state" / "process-pins.json").write_text(json.dumps(pins))
        mod = _load()
        mod.WORKSPACE_DIR = ws
        mod.REPO_DIR = repo
        real = subprocess.run
        with mock.patch.object(mod.subprocess, "run", fake_run(real)):
            checks = mod.run_all_checks()
        return next(c for c in checks if c.get("name") == "slack-bridge")


class BridgeProbeBranches(unittest.TestCase):
    def test_pgrep_raising_emits_unknown_row_with_veto(self) -> None:
        def fake(real):
            def run(cmd, *a, **kw):
                joined = " ".join(str(x) for x in (cmd if isinstance(cmd, list) else [cmd]))
                if "pgrep" in joined and "slack-bridge" in joined:
                    raise OSError("probe exploded")
                return real(cmd, *a, **kw)
            return run
        row = _run_bridges(fake)
        self.assertIn("process probe failed", row["detail"], row)
        self.assertNotIn("configured but not running", row["detail"], row)
        self.assertIn("could not be verified", row.get("restart_veto", ""), row)

    def test_ps_timeout_keeps_the_pin_vetoing(self) -> None:
        def fake(real):
            def run(cmd, *a, **kw):
                argv = cmd if isinstance(cmd, list) else [cmd]
                joined = " ".join(str(x) for x in argv)
                if "pgrep" in joined and "slack-bridge" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{PID}\n", stderr="")
                if "/bin/ps" in joined and "lstart=" in argv:
                    raise subprocess.TimeoutExpired(argv, 5)
                return real(cmd, *a, **kw)
            return run
        row = _run_bridges(fake)
        self.assertIn("could not be verified", row.get("restart_veto", ""), row)

    def test_log_override_under_armed_pin_keeps_finding_drops_remedy(self) -> None:
        def fake(real):
            def run(cmd, *a, **kw):
                argv = cmd if isinstance(cmd, list) else [cmd]
                joined = " ".join(str(x) for x in argv)
                if "pgrep" in joined and "slack-bridge" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{PID}\n", stderr="")
                if "/bin/ps" in joined and "lstart=" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout=LSTART + "\n", stderr="")
                if "lsof" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                return real(cmd, *a, **kw)
            return run
        row = _run_bridges(fake, log_text="60s elapsed with zero events received\n")
        self.assertIn("DO NOT RESTART", row.get("restart_veto", ""), row)
        self.assertIn("[", row["detail"], row)   # override finding bracketed, not replacing

    def test_orphan_note_appends_and_escalates_on_healthy_bridge(self) -> None:
        def fake(real):
            def run(cmd, *a, **kw):
                argv = cmd if isinstance(cmd, list) else [cmd]
                joined = " ".join(str(x) for x in argv)
                if "pgrep" in joined and "slack-bridge" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{PID}\n", stderr="")
                if "/bin/ps" in joined and "lstart=" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout=LSTART + "\n", stderr="")
                if "lsof" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                return real(cmd, *a, **kw)
            return run
        row = _run_bridges(fake, pin_pid="999999")   # pin names a DIFFERENT pid
        self.assertIn("no longer running", row["detail"], row)
        self.assertEqual(row["status"], "warn", row)

    def test_ps_nonzero_rc_keeps_the_pin_vetoing(self) -> None:
        def fake(real):
            def run(cmd, *a, **kw):
                argv = cmd if isinstance(cmd, list) else [cmd]
                joined = " ".join(str(x) for x in argv)
                if "pgrep" in joined and "slack-bridge" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{PID}\n", stderr="")
                if "/bin/ps" in joined and "lstart=" in argv:
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="err")
                return real(cmd, *a, **kw)
            return run
        row = _run_bridges(fake)
        self.assertIn("could not be verified", row.get("restart_veto", ""), row)

    def test_log_override_unpinned_replaces_status_and_detail(self) -> None:
        def fake(real):
            def run(cmd, *a, **kw):
                argv = cmd if isinstance(cmd, list) else [cmd]
                joined = " ".join(str(x) for x in argv)
                if "pgrep" in joined and "slack-bridge" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{PID}\n", stderr="")
                if "/bin/ps" in joined and "lstart=" in argv:
                    return subprocess.CompletedProcess(argv, 0, stdout=LSTART + "\n", stderr="")
                if "lsof" in joined:
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                return real(cmd, *a, **kw)
            return run
        # Pin names a DIFFERENT pid: no armed veto, so the override replaces.
        row = _run_bridges(fake, pin_pid="999999",
                           log_text="60s elapsed with zero events received\n")
        self.assertNotIn("DO NOT RESTART", row.get("restart_veto", "") or "", row)

    def test_check_port_error_branch_survives_a_raising_pin_probe(self) -> None:
        mod = _load()
        with mock.patch.object(mod, "_proc_lstarts", side_effect=OSError("probe dead")), \
             mock.patch.object(mod.socket, "socket", side_effect=RuntimeError("no sockets")):
            row = mod.check_port(1, "voice-agent", probe=True)
        self.assertEqual(row["status"], "error", row)
        self.assertNotIn("restart_veto", row, row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
