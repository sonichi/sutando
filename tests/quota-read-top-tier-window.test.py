#!/usr/bin/env python3
"""read-quota.py surfaces the API's top-tier-model weekly lane (`7d_oi`) when the
proxy captured it, and prints nothing extra when it did not."""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"
_BASE = {"anthropic-ratelimit-unified-status": "allowed",
         "anthropic-ratelimit-unified-5h-utilization": "0.12",
         "anthropic-ratelimit-unified-7d-utilization": "0.04",
         "anthropic-ratelimit-unified-7d-reset": "1788598800"}
_OI = {"anthropic-ratelimit-unified-7d_oi-utilization": "0.83",
       "anthropic-ratelimit-unified-7d_oi-reset": "1788598800"}


class TopTierWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-oi-")); self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._env); shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, headers, argv):
        os.environ["SUTANDO_WORKSPACE"] = str(self.tmp); os.environ["SUTANDO_TEST_MODE"] = "1"
        state = self.tmp / "state"; state.mkdir(parents=True, exist_ok=True)
        (state / "quota-state.json").write_text(json.dumps({"headers": headers}))
        sys.modules.pop("read_quota_oi_under_test", None)
        spec = importlib.util.spec_from_file_location("read_quota_oi_under_test", _SCRIPT)
        mod = importlib.util.module_from_spec(spec); sys.path.insert(0, str(REPO / "src"))
        spec.loader.exec_module(mod)
        mod._update_burn_rate = lambda *a, **k: None
        os.environ["ANTHROPIC_BASE_URL"] = f"{mod._PROXY_SCHEME}://127.0.0.1:{mod._PROXY_PORT}"
        out = io.StringIO(); real = sys.argv; sys.argv = list(argv)
        try:
            with redirect_stdout(out):
                try: mod.main()
                except SystemExit: pass
        finally:
            sys.argv = real
        return out.getvalue()

    def test_human_output_carries_the_lane_when_captured(self):
        out = self._run({**_BASE, **_OI}, ["read-quota.py"])
        self.assertIn("7d-oi window (top-tier models): 83% used, 17% remaining", out)
        # It follows the 7d block, so quota-tier's Resets[0]/[1] still mean 5h/7d.
        self.assertLess(out.index("7d window:"), out.index("7d-oi window"))

    def test_json_output_carries_the_lane_when_captured(self):
        d = json.loads(self._run({**_BASE, **_OI}, ["read-quota.py", "--json"]))
        self.assertEqual(d["utilization_7d_oi"], 0.83)
        self.assertEqual(d["remaining_7d_oi_pct"], 17)
        self.assertIn("reset_7d_oi", d)

    def test_absent_header_prints_and_emits_nothing_extra(self):
        out = self._run(_BASE, ["read-quota.py"]); self.assertNotIn("7d-oi", out)
        d = json.loads(self._run(_BASE, ["read-quota.py", "--json"]))
        self.assertNotIn("utilization_7d_oi", d); self.assertNotIn("remaining_7d_oi_pct", d)


if __name__ == "__main__":
    unittest.main(verbosity=1)
