#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]
SERVE_PATH = SKILL / "scripts" / "serve.py"
SPEC = importlib.util.spec_from_file_location("sutando_life_provider_serve", SERVE_PATH)
assert SPEC and SPEC.loader
SERVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVE)


class ProviderStartupTests(unittest.TestCase):
    def test_launcher_forwards_its_provider_factory_without_serving(self):
        calls = []
        original = SERVE.runtime_main
        SERVE.runtime_main = lambda factories: calls.append(factories)
        try:
            SERVE.main()
        finally:
            SERVE.runtime_main = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], [SERVE.registry_inputs])

    def test_manifest_declares_owner_startup_entrypoint(self):
        manifest = json.loads((SKILL / "manifest.json").read_text())
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["access_tier"], "owner")
        self.assertEqual(manifest["startup"], "./scripts/serve.py")


if __name__ == "__main__":
    unittest.main()
