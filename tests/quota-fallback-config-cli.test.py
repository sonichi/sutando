#!/usr/bin/env python3
"""fallback-config.py — the owner's adjustment path for the model-fallback
ladder. Pins: the key set agrees with the manifest and the proxy's TS reader;
set/unset write the per-host override atomically and print the effective
ladder; validation refuses out-of-range values and an inverted ladder; show
names each value's source.

Run: python3 tests/quota-fallback-config-cli.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "quota-tracker"


def _load():
    spec = importlib.util.spec_from_file_location("fallback_config_cli_test", SKILL / "scripts" / "fallback-config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFallbackConfigCli(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.path = self.m.override_path(self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(list(argv), workspace=self.ws, env=env or {})
        return rc, out.getvalue(), err.getvalue()

    def test_keys_agree_with_manifest_and_proxy_reader(self):
        manifest = json.loads((SKILL / "manifest.json").read_text())["config"]
        self.assertEqual(sorted(manifest), sorted(self.m.KEYS))
        ts = (SKILL / "scripts" / "quota-fallback-config.ts").read_text()
        block = ts[ts.index("export const CONFIG_KEYS"):ts.index("] as const;")]
        ts_keys = sorted("SUTANDO_QUOTA_FALLBACK_" + k for k in re.findall(r"\$\{P\}([A-Z0-9_]+)`", block))
        self.assertEqual(ts_keys, sorted(self.m.KEYS))

    def test_show_without_override_reports_manifest_sources(self):
        rc, out, _ = self.run_cli("show")
        self.assertEqual(rc, 0)
        self.assertIn("7d window: level1 > 85% → level 2, level2 > 95% → level 3", out)
        self.assertIn("5h window: level1 > 90%", out)
        self.assertIn("level2 > 97%", out)
        self.assertIn("projection on, limit 98%", out)
        self.assertIn("low-priority ladder: off", out)
        self.assertNotIn("(override)", out)
        self.assertIn("(manifest)", out)

    def test_override_is_per_host(self):
        self.assertEqual(self.path, self.ws / "hosts" / self.m.host_label() / "quota-fallback-config.json")

    def test_set_writes_override_and_proxy_sees_it_as_the_effective_value(self):
        rc, out, _ = self.run_cli("set", "7d", "level1", "0.90")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.path.read_text()), {"SUTANDO_QUOTA_FALLBACK_7D_LEVEL1": "0.9"})
        self.assertIn("7d window: level1 > 90%", out)
        self.assertIn("(override)", out)
        self.assertFalse(list(self.path.parent.glob(".quota-fallback-config.json.*")), "temp file renamed away")
        rc, out, _ = self.run_cli("set", "5h", "projection", "off")
        self.assertEqual(rc, 0)
        self.assertIn("projection off", out)
        rc, out, _ = self.run_cli("set", "low-priority", "on")
        self.assertIn("low-priority ladder: on", out)
        rc, _, _ = self.run_cli("set", "5h", "projection-clear-samples", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.path.read_text())["SUTANDO_QUOTA_FALLBACK_5H_PROJECTION_CLEAR_SAMPLES"], "5")
        rc, out, _ = self.run_cli("set", "dm-min-interval-sec", "900")
        self.assertEqual(rc, 0)
        self.assertIn("at most one per window per 900s", out)

    def test_target_models_are_validated_against_the_family_levels(self):
        for args in (("set", "level2-model", "claude-opsu-5-5"),      # typo family
                     ("set", "level2-model", "claude-sonnet-5"),       # wrong level
                     ("set", "level3-model", "claude-opus-5-5"),
                     ("set", "level3-model", "gpt-5"),
                     ("set", "level2-model", "claude-opus-5-5[1m]")):  # variants come from the request
            rc, _, err = self.run_cli(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn("fallback-config:", err)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_cli("set", "level2-model", "claude-opus-5")[0], 0)
        self.assertEqual(self.run_cli("set", "level3-model", "claude-haiku-4-5")[0], 0)
        self.assertEqual(json.loads(self.path.read_text())["SUTANDO_QUOTA_FALLBACK_LEVEL3_MODEL"], "claude-haiku-4-5")

    def test_unset_returns_to_manifest_default(self):
        self.run_cli("set", "7d", "level1", "0.90")
        rc, out, _ = self.run_cli("unset", "7d", "level1")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.path.read_text()), {})
        self.assertIn("back to 0.85", out)
        self.assertIn("7d window: level1 > 85%", out)

    def test_validation_refuses_bad_values_and_inverted_ladders(self):
        for args in (("set", "7d", "level1", "1.2"), ("set", "7d", "level1", "0"), ("set", "7d", "level1", "abc"),
                     ("set", "7d", "level1", "0.96"),        # above the 7d level2 (0.95)
                     ("set", "5h", "level2", "0.80"),        # below the 5h level1 (0.90)
                     ("set", "hysteresis", "on"), ("set", "5h", "projection", "sometimes"),
                     ("set", "5h", "projection-clear-samples", "2.5"), ("set", "nope", "0.5"), ("set", "7d", "level1"),
                     ("bogus",)):
            rc, _, err = self.run_cli(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn("fallback-config:", err)
        self.assertFalse(self.path.exists(), "a refused set writes nothing")
        # A ladder stays valid when both ends move together.
        self.assertEqual(self.run_cli("set", "7d", "level2", "0.99")[0], 0)
        self.assertEqual(self.run_cli("set", "7d", "level1", "0.96")[0], 0)

    def test_env_wins_over_override_in_show(self):
        self.run_cli("set", "7d", "level1", "0.90")
        rc, out, _ = self.run_cli("show", env={"SUTANDO_QUOTA_FALLBACK_7D_LEVEL1": "0.80"})
        self.assertEqual(rc, 0)
        self.assertIn("7d window: level1 > 80%", out)
        self.assertIn("(env)", out)


if __name__ == "__main__":
    unittest.main()
