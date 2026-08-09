#!/usr/bin/env python3
"""Precedence regression for report-usage.py's cloud-origin resolution
(sonichi#2180 review, john-the-dev blocker).

The blocker: the script read an invented `AG2_CLOUD_ORIGIN` straight out of
os.environ with a hardcoded default, so the setting was undeclared and
undiscoverable. `skills/MANIFEST.md` requires a config-only manifest to declare
it, with `CLI > env > manifest > config-file > state` precedence. This asserts
the two rungs this script actually implements — **env > manifest > fallback** —
and that the shipped manifest really declares the key.

Runs on the stock macOS interpreter (3.9) as well as 3.12: no `X | Y` runtime
annotations, no walrus-in-comprehension, no tomllib.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "skill-usage-report"
sys.path.insert(0, str(SKILL / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "report_usage", SKILL / "scripts" / "report-usage.py"
)
report_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report_usage)


def _write_manifest(tmpdir, config):
    """Write a manifest.json carrying `config` and return its path."""
    p = Path(tmpdir) / "manifest.json"
    body = {"name": "skill-usage-report", "version": "1.0.0"}
    if config is not None:
        body["config"] = config
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


class CloudOriginPrecedence(unittest.TestCase):
    def test_env_overrides_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            mp = _write_manifest(td, {"AG2_CLOUD_ORIGIN": "https://from-manifest"})
            got = report_usage.resolve_cloud_origin(
                {"AG2_CLOUD_ORIGIN": "https://from-env"}, mp
            )
        self.assertEqual(got, "https://from-env")

    def test_manifest_used_when_env_absent(self):
        """The rung the blocker was about: with no env var, the DECLARED
        manifest default must win — not a constant buried in the script."""
        with tempfile.TemporaryDirectory() as td:
            mp = _write_manifest(td, {"AG2_CLOUD_ORIGIN": "https://from-manifest"})
            got = report_usage.resolve_cloud_origin({}, mp)
        self.assertEqual(got, "https://from-manifest")

    def test_empty_env_is_not_an_override(self):
        """`AG2_CLOUD_ORIGIN= cmd` means "leave it alone", not "use an empty
        origin" — an empty value would build a garbage URL."""
        with tempfile.TemporaryDirectory() as td:
            mp = _write_manifest(td, {"AG2_CLOUD_ORIGIN": "https://from-manifest"})
            got = report_usage.resolve_cloud_origin({"AG2_CLOUD_ORIGIN": ""}, mp)
        self.assertEqual(got, "https://from-manifest")

    def test_falls_back_when_manifest_missing(self):
        """Cron-invoked: an unreadable manifest must degrade, not raise."""
        missing = Path(tempfile.gettempdir()) / "no-such-manifest-2180.json"
        if missing.exists():
            missing.unlink()
        got = report_usage.resolve_cloud_origin({}, missing)
        self.assertEqual(got, report_usage.CLOUD_FALLBACK)

    def test_falls_back_when_manifest_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "manifest.json"
            mp.write_text("{not json", encoding="utf-8")
            got = report_usage.resolve_cloud_origin({}, mp)
        self.assertEqual(got, report_usage.CLOUD_FALLBACK)

    def test_falls_back_when_config_is_not_an_object(self):
        with tempfile.TemporaryDirectory() as td:
            mp = _write_manifest(td, "not-an-object")
            got = report_usage.resolve_cloud_origin({}, mp)
        self.assertEqual(got, report_usage.CLOUD_FALLBACK)

    def test_shipped_manifest_declares_the_key(self):
        """Guards the actual regression: the shipped skill must DECLARE the
        setting, so this fails if manifest.json is deleted or the key is
        renamed back into env-only territory."""
        shipped = SKILL / "manifest.json"
        self.assertTrue(shipped.is_file(), "skill-usage-report ships no manifest.json")
        m = json.loads(shipped.read_text(encoding="utf-8"))
        self.assertIn("config", m, "manifest declares no config block")
        self.assertIn(
            "AG2_CLOUD_ORIGIN", m["config"], "manifest does not declare AG2_CLOUD_ORIGIN"
        )
        self.assertTrue(str(m["config"]["AG2_CLOUD_ORIGIN"]).startswith("http"))

    def test_script_has_no_ad_hoc_environ_read(self):
        """The specific shape that was flagged: a bare
        `os.environ.get("AG2_CLOUD_ORIGIN", ...)` at import time. Resolution
        must go through resolve_cloud_origin so the manifest is consulted."""
        src = (SKILL / "scripts" / "report-usage.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'os.environ.get("AG2_CLOUD_ORIGIN"',
            src,
            "report-usage.py still reads AG2_CLOUD_ORIGIN ad-hoc from os.environ",
        )
        self.assertIn("def resolve_cloud_origin(", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
