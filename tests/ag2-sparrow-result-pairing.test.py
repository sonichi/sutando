#!/usr/bin/env python3
"""The shipped package must obey the canonical result-pairing contract."""
import importlib.util
import sys
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ
isolate_os_environ()
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow import result_write

spec = importlib.util.spec_from_file_location("paired_contract", REPO / "tests/result-write-pairing.test.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
contract.result_write = result_write

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(contract)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not outcome.wasSuccessful())
