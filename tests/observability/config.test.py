"""Twin of observability-config.test.ts.

The loader reads already-populated ``os.environ`` (it does NOT re-parse .env --
that's the bridges' job, PR #416), so this tests precedence, truthy parsing,
and malformed-override fallback.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability import config as cfgmod  # noqa: E402

KNOBS = (
    "SUTANDO_TENANT_ID",
    "SUTANDO_TENANT_MODE",
    "SUTANDO_METERING_ENABLED",
    "SUTANDO_METERING_ENDPOINT",
    "SUTANDO_METERING_HEADERS",
)


class ObservabilityConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.pop(k, None) for k in KNOBS}
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_defaults_only(self) -> None:
        self.assertEqual(cfgmod.load_observability_config(self.ws), cfgmod.OBSERVABILITY_DEFAULTS)

    def test_env_beats_defaults(self) -> None:
        os.environ["SUTANDO_TENANT_ID"] = "acct_123"
        os.environ["SUTANDO_TENANT_MODE"] = "managed"
        os.environ["SUTANDO_METERING_ENABLED"] = "true"
        os.environ["SUTANDO_METERING_ENDPOINT"] = "https://meter.example"
        cfg = cfgmod.load_observability_config(self.ws)
        self.assertEqual(cfg["tenant"]["id"], "acct_123")
        self.assertEqual(cfg["tenant"]["mode"], "managed")
        self.assertTrue(cfg["metering"]["enabled"])
        self.assertEqual(cfg["metering"]["endpoint"], "https://meter.example")

    def test_metering_enabled_truthy_parse(self) -> None:
        os.environ["SUTANDO_METERING_ENABLED"] = "no"
        self.assertFalse(cfgmod.load_observability_config(self.ws)["metering"]["enabled"])
        os.environ["SUTANDO_METERING_ENABLED"] = "on"
        self.assertTrue(cfgmod.load_observability_config(self.ws)["metering"]["enabled"])

    def test_workspace_override_wins(self) -> None:
        os.environ["SUTANDO_TENANT_MODE"] = "managed"
        (self.ws / "config").mkdir(parents=True, exist_ok=True)
        (self.ws / "config" / "observability.json").write_text(
            json.dumps({"tenant": {"mode": "byok"}, "observability": {"sampling": {"trace": 0.5}}})
        )
        cfg = cfgmod.load_observability_config(self.ws)
        self.assertEqual(cfg["tenant"]["mode"], "byok")  # workspace overrides env
        self.assertEqual(cfg["observability"]["sampling"]["trace"], 0.5)
        self.assertEqual(cfg["metering"]["batchMax"], 100)  # untouched key kept

    def test_malformed_override_falls_back(self) -> None:
        (self.ws / "config").mkdir(parents=True, exist_ok=True)
        (self.ws / "config" / "observability.json").write_text("{ not valid json")
        cfg = cfgmod.load_observability_config(self.ws)
        self.assertEqual(cfg, cfgmod.OBSERVABILITY_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
