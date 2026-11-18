#!/usr/bin/env python3
"""The heaviest sentinel audit: the whole Hatch adapter suite, run under the audit hook.

It reuses the sentinels and leak rules of tests/hermetic-env-sentinels.test.py unchanged and lives
in its own file so neither file's summed child runtime nears the CI per-file cap.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hermetic_env_sentinels", Path(__file__).resolve().parent / "hermetic-env-sentinels.test.py")
_sentinels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sentinels)


class HatchAdapterSentinels(unittest.TestCase):
    check = _sentinels.Sentinels.check

    def test_the_hatch_adapter_suite_reads_no_outer_selector(self):
        self.check(["packages/ag2-sparrow/tests/test_hatch_adapter.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
