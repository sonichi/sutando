#!/usr/bin/env python3
"""pool-bind CLI: list prints the bindings table, unpin reports the drop —
the read/confirm paths a pin operator actually sees.

Run: python3 tests/pool-bind-cli.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "pool_bind", REPO / "scripts" / "pool-bind.py")
pool_bind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_bind)


class CliTests(unittest.TestCase):
    def _ws(self, td):
        ws = Path(td)
        (ws / "tasks").mkdir()
        (ws / "state").mkdir()
        return ws

    def test_list_prints_bindings_json(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            with redirect_stdout(io.StringIO()):
                pool_bind.main(["pin", "!r:x", "worker-2"], workspace=ws)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = pool_bind.main(["list"], workspace=ws)
            self.assertEqual(rc, 0)
            table = json.loads(out.getvalue())
            self.assertEqual(table["!r:x"]["instance"], "worker-2")

    def test_unpin_reports_the_dropped_row(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            with redirect_stdout(io.StringIO()):
                pool_bind.main(["pin", "!r:y", "worker-3", "--dedicated"],
                               workspace=ws)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = pool_bind.main(["unpin", "!r:y"], workspace=ws)
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(), "unpinned")
            out2 = io.StringIO()
            with redirect_stdout(out2):
                pool_bind.main(["list"], workspace=ws)
            self.assertNotIn('"pinned": true', out2.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
