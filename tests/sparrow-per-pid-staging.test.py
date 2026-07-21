#!/usr/bin/env python3
"""Regression test for the sparrow half of #2222: per-PID atomic-write staging.

remote_gateway_bridge.py staged four state files (last-owner-activity,
gateway-status, task-rooms, inflight) under a SHARED `.json.tmp` name. For
last-owner-activity that is the cross-process #2222 bug directly (the sparrow
bridge is the 4th writer alongside slack/discord/telegram); for the other three
it is latent — safe with one sparrow instance, a torn-write race with two. The
fix stages each under a per-PID name and publishes with os.replace.

The per-PID mechanism itself is proven under real concurrency by
tests/owner-activity-atomic-write.test.py (#2236) — identical pattern. This test
does not re-prove the mechanism; it (1) source-guards all four sparrow sites so
none regresses to the shared name, and (2) behaviorally confirms the live
gateway-status write actually stages a pid-suffixed temp.

Run: python3 tests/sparrow-per-pid-staging.test.py
"""
import importlib
import os
import pathlib
import re
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SPARROW_PY = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"


class TestSparrowPerPidStaging(unittest.TestCase):
    # -- source guard: every state-file write stages per-PID, none shared ---- #

    def test_no_shared_json_tmp_remains(self):
        src = SPARROW_PY.read_text()
        # Drop comment lines so an explanatory comment naming the old form can't trip it.
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        shared = re.findall(r'\.with_suffix\(\s*["\']\.json\.tmp["\']\s*\)', code)
        self.assertEqual(
            shared, [],
            f"{len(shared)} state write(s) still stage under the shared '.json.tmp'",
        )

    def test_all_four_state_files_use_per_pid(self):
        code = SPARROW_PY.read_text()
        for var in ("OWNER_ACTIVITY_FILE", "GATEWAY_STATUS_FILE",
                    "TASK_ROOMS_FILE", "INFLIGHT_FILE"):
            pat = rf'{var}\.with_suffix\(\s*f["\']\.json\.\{{os\.getpid\(\)\}}\.tmp["\']'
            self.assertRegex(
                code, pat,
                f"{var} does not stage under a per-PID temp name",
            )

    # -- behavioral: the live gateway-status write stages a pid-suffixed temp -- #

    def test_gateway_status_write_stages_per_pid(self):
        with tempfile.TemporaryDirectory(prefix="sparrow-2222-") as d:
            os.environ["AGENT_CONNECT_STATE_DIR"] = d
            os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
            os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
            sys.path.insert(0, str(SPARROW_PY.resolve().parents[1]))
            mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
            mod = importlib.reload(mod)

            captured = {}
            real_replace = os.replace

            def _spy(src_path, dst_path):
                captured["tmp"] = str(src_path)
                return real_replace(src_path, dst_path)

            orig = mod.os.replace
            mod.os.replace = _spy
            try:
                mod._emit_gateway_status(True)
            finally:
                mod.os.replace = orig

            self.assertIn("tmp", captured, "gateway-status write never called os.replace")
            self.assertIn(f".{os.getpid()}.tmp", captured["tmp"],
                          f"staging temp was not pid-suffixed: {captured['tmp']!r}")
            # And the published file is valid.
            self.assertTrue((mod.GATEWAY_STATUS_FILE).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
