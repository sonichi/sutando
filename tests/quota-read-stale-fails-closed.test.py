#!/usr/bin/env python3
"""A routed-but-stale quota file must not answer "proceed".

`--gate` is the machine path: a budget governor exits on it and never sees the
human STALE banner. A proxy that is still the configured endpoint but has
stopped writing leaves `routed` true while every number is a fossil, so
staleness has to fail the gate the way not-routed already does.

Run: python3 tests/quota-read-stale-fails-closed.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"
_PROXY = "http://localhost:7846"


def _load_module(workspace: Path, age_seconds: float):
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    sys.modules.pop("read_quota_under_test", None)
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "quota-state.json"
    reset = int(time.time()) + 3600
    # Deliberately generous headroom: if the gate still says no, it is the age
    # talking and not exhaustion.
    path.write_text(json.dumps({"available": True, "headers": {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.10",
        "anthropic-ratelimit-unified-7d-utilization": "0.10",
        "anthropic-ratelimit-unified-5h-reset": str(reset),
        "anthropic-ratelimit-unified-7d-reset": str(reset),
    }}))
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    spec = importlib.util.spec_from_file_location("read_quota_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class StaleFailsClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-stale-"))
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _result(self, age_seconds: float, base_url: str | None) -> dict:
        mod = _load_module(self.tmp, age_seconds)
        if base_url is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = base_url
        argv = sys.argv
        sys.argv = ["read-quota.py", "--json"]
        try:
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mod.main()
            return json.loads(buf.getvalue())
        finally:
            sys.argv = argv

    def test_stale_and_routed_is_unavailable(self) -> None:
        r = self._result(13 * 86400, _PROXY)          # Michael's shape: 13 days
        self.assertTrue(r["stale"], "fixture must be stale")
        self.assertTrue(r["routed"], "fixture must still look routed")
        self.assertFalse(r["available"], "13-day-old data must not read as available")
        self.assertEqual(r["unavailable_reason"], "stale")

    def test_just_past_threshold_is_unavailable(self) -> None:
        r = self._result(31 * 60, _PROXY)             # threshold is 30 min
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"], "stale")

    def test_fresh_and_routed_still_available(self) -> None:
        """The guard must not swallow the healthy case."""
        r = self._result(5, _PROXY)
        self.assertFalse(r["stale"])
        self.assertTrue(r["available"])
        self.assertIsNone(r["unavailable_reason"])

    def test_not_routed_outranks_stale(self) -> None:
        """A foreign file's age says nothing; the routing fault is the one to report."""
        r = self._result(13 * 86400, None)
        self.assertFalse(r["available"])
        self.assertEqual(r["unavailable_reason"], "not-routed")

    # `--json` returns before the gate, so the cases above never reach it. A budget
    # governor sees only the exit code and needs its own invocation.

    def _gate_exit(self, age_seconds: float, base_url: str | None) -> int:
        mod = _load_module(self.tmp, age_seconds)
        if base_url is None:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
        else:
            os.environ["ANTHROPIC_BASE_URL"] = base_url
        old = sys.argv
        sys.argv = ["read-quota.py", "--gate"]
        try:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                mod.main()
        except SystemExit as e:
            return int(e.code or 0)
        finally:
            sys.argv = old
        return 0

    def test_stale_routed_gate_exits_nonzero(self) -> None:
        """The reason this PR exists: a stale-but-routed session must fail the gate."""
        self.assertEqual(self._gate_exit(13 * 86400, _PROXY), 1)

    def test_just_past_threshold_gate_exits_nonzero(self) -> None:
        self.assertEqual(self._gate_exit(31 * 60, _PROXY), 1)

    def test_fresh_routed_gate_exits_zero(self) -> None:
        """Control: the gate must still pass a healthy session, so the two cases
        above cannot go green by refusing everything."""
        self.assertEqual(self._gate_exit(5, _PROXY), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
