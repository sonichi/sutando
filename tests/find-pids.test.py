"""Tests for process inspection in src/sutando_platform.py.

find_pids underpins health-check's bridge/service detection. On Windows it
shells to Get-CimInstance (no pgrep); on macOS/Linux it uses pgrep -f. Both
honor a trailing `$` as an end-of-command-line anchor so a real
`python …/foo.py` process matches `foo\\.py$` while a shell that merely
mentions `foo.py` mid-command-line does not.

The snapshot and executable helpers expose the same normalized process facts
to health-check on every supported OS.

These tests spawn controlled child processes with identifiable command lines
(rather than asserting against ambient processes) so they're deterministic on
any machine. Patterns are chosen to be absent from the test runner's own
command line to avoid self-matching the harness.

Run: `python tests/find-pids.test.py`  (use `python`, not `python3`, on Windows)
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "sutando_platform", ROOT / "src" / "sutando_platform.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFindPids(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self._procs = []

    def tearDown(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass

    def _spawn_sleeper(self, marker: str):
        """Spawn a python child that sleeps, with `marker` as a trailing argv
        token so its command line ENDS with the marker. Returns the Popen."""
        p = subprocess.Popen(
            [sys.executable, "-c", "import sys,time; time.sleep(30)", marker],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._procs.append(p)
        time.sleep(0.5)  # let it appear in the process table
        return p

    # A nonsense pattern (absent from any real command line) returns nothing.
    def test_nonexistent_pattern_returns_empty(self):
        # The query tags itself and skips $PID, so this literal cannot self-match
        # and is unlikely to occur in another process.
        self.assertEqual(self.mod.find_pids("nopE_no_such_proc_7Xq"), [])

    # A spawned child with a unique trailing marker is found by that marker.
    def test_finds_spawned_child_by_marker(self):
        marker = "sutando_fp_test_marker_alpha"
        child = self._spawn_sleeper(marker)
        pids = self.mod.find_pids(marker)
        self.assertIn(str(child.pid), pids,
                      f"spawned child {child.pid} not found via find_pids({marker!r}); got {pids}")

    # The `$` end-anchor matches a child whose command line ENDS with the marker.
    def test_end_anchor_matches_trailing_marker(self):
        marker = "sutando_fp_test_marker_beta"
        child = self._spawn_sleeper(marker)  # marker is the last argv token
        pids = self.mod.find_pids(marker + "$")
        self.assertIn(str(child.pid), pids,
                      f"end-anchored find_pids({marker+'$'!r}) should match trailing marker; got {pids}")

    # The `$` anchor does NOT match when the marker is mid-command-line.
    def test_end_anchor_rejects_midline_marker(self):
        marker = "sutando_fp_test_marker_gamma"
        # marker is NOT the last token — a trailing arg follows it.
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", marker, "TRAILER"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs.append(p)
        time.sleep(0.5)
        pids = self.mod.find_pids(marker + "$")
        self.assertNotIn(str(p.pid), pids,
                         f"end-anchored find_pids({marker+'$'!r}) must NOT match a mid-line marker; got {pids}")
        # …but the unanchored form DOES find it.
        self.assertIn(str(p.pid), self.mod.find_pids(marker))

    def test_windows_query_allows_for_cold_cim_startup(self):
        completed = subprocess.CompletedProcess([], 0, stdout="4242\n", stderr="")
        with mock.patch.object(self.mod, "is_macos", return_value=False), \
                mock.patch.object(self.mod, "is_linux", return_value=False), \
                mock.patch.object(self.mod, "is_windows", return_value=True), \
                mock.patch.object(self.mod.subprocess, "run", return_value=completed) as run:
            self.assertEqual(self.mod.find_pids("marker"), ["4242"])
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 15)

    def test_process_executable_finds_current_python(self):
        executable = self.mod.process_executable(os.getpid())
        self.assertIsNotNone(executable)
        self.assertIn("python", Path(executable).name.lower())

    def test_process_snapshot_lists_current_process(self):
        snapshot = self.mod.process_snapshot()
        self.assertIsNotNone(snapshot)
        pids = {line.split(None, 1)[0] for line in snapshot.splitlines()[1:]}
        self.assertIn(str(os.getpid()), pids)

    def test_windows_process_snapshot_normalizes_cim_json(self):
        payload = json.dumps([{
            "ProcessId": 4242,
            "ParentProcessId": 7,
            "CommandLine": "C:\\Program Files\\Python\\python.exe bridge.py",
        }])
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        with mock.patch.object(self.mod, "is_macos", return_value=False), \
                mock.patch.object(self.mod, "is_linux", return_value=False), \
                mock.patch.object(self.mod, "is_windows", return_value=True), \
                mock.patch.object(self.mod.subprocess, "run", return_value=completed) as run:
            snapshot = self.mod.process_snapshot()
        self.assertIn(
            "4242 7 C:\\Program Files\\Python\\python.exe bridge.py", snapshot)
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 15)

    def test_process_snapshot_distinguishes_empty_success_from_failure(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
        platform = (
            mock.patch.object(self.mod, "is_macos", return_value=True),
            mock.patch.object(self.mod, "is_linux", return_value=False),
            mock.patch.object(self.mod, "is_windows", return_value=False),
        )
        with platform[0], platform[1], platform[2], \
                mock.patch.object(self.mod.subprocess, "run", return_value=ok):
            self.assertEqual(self.mod.process_snapshot(), "")
        with platform[0], platform[1], platform[2], \
                mock.patch.object(self.mod.subprocess, "run", return_value=bad):
            self.assertIsNone(self.mod.process_snapshot())


if __name__ == "__main__":
    unittest.main()
