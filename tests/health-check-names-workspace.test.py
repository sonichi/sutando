#!/usr/bin/env python3
"""The report must name the workspace it measured.

Every probe resolves against `WORKSPACE_DIR`, which `resolve_workspace()`
derives from the working directory. On a host with two checkouts the same
command produces two different verdicts and says nothing about which tree it
read — measured: 10 warnings from one, 19 from the other, seconds apart, and
NOT a superset (nine names appear, two disappear). The dangerous cell was
`task-watcher`: `⚠ ... wrote no PID sentinel` against one workspace and
`✓ streaming watcher alive` against the other, for the SAME pid. The proactive
loop reads that probe to decide whether to stop or start a watcher.

Every assertion here reads RENDERED OUTPUT, never the source text. A source
substring stays green when the line is present but unreachable — the reverted-
source control at the bottom is what proves these tests can fail at all.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "src", "health-check.py")
EMITTED = 'print(f"workspace: {WORKSPACE_DIR}")'
# Full line, indentation included: mutating a bare substring can nest the
# statement inside itself, and a SyntaxError is not a demonstration.
ANCHOR = '\n        ' + EMITTED + '\n'

_spec = importlib.util.spec_from_file_location("health_check_ws_mod", SRC)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

OK = {"name": "task-queue", "status": "ok", "detail": "empty"}
OK2 = {"name": "memory-index", "status": "ok", "detail": "24k"}
DOWN = {"name": "voice-agent", "status": "down", "detail": "port 9900"}


def run_main(module, argv, checks):
    """Invoke the real main() with the probes stubbed out — no I/O, no fixes."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(module, "run_all_checks", lambda: [dict(c) for c in checks]), \
            mock.patch.object(sys, "argv", ["health-check.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            module.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


class HumanReportTests(unittest.TestCase):
    def test_human_report_names_the_resolved_workspace(self):
        _, out = run_main(hc, [], [OK, OK2])
        self.assertIn(f"workspace: {hc.WORKSPACE_DIR}", out.splitlines(),
                      f"human report never printed the workspace:\n{out}")

    def test_workspace_line_sits_above_the_probe_rows(self):
        """Printed below the rows, a reader scanning the top still misses it."""
        _, out = run_main(hc, [], [OK, OK2])
        lines = out.splitlines()

        def where(label, pred):
            # index()/next() would RAISE on a missing line, and an error is not
            # a demonstration: the mutation must report as a failure.
            hit = [i for i, ln in enumerate(lines) if pred(ln)]
            self.assertTrue(hit, f"no {label} line in the report:\n{out}")
            return hit[0]

        title = where("title", lambda ln: ln == "Sutando Health Check")
        ws = where("workspace", lambda ln: ln == f"workspace: {hc.WORKSPACE_DIR}")
        rule = where("divider", lambda ln: set(ln) == {"="})
        first_row = where("probe row", lambda ln: "task-queue" in ln)
        self.assertLess(title, ws, f"workspace line precedes the title:\n{out}")
        self.assertLess(ws, rule, f"workspace line falls below the divider:\n{out}")
        self.assertLess(ws, first_row, f"workspace line falls below the rows:\n{out}")

    def test_json_payload_is_untouched(self):
        """--json is a machine interface: prose on stdout breaks json.loads."""
        _, out = run_main(hc, ["--json"], [OK, OK2])
        payload = json.loads(out)
        self.assertEqual(payload["total"], 2)
        self.assertNotIn("workspace:", out)

    def test_quiet_path_is_untouched(self):
        """--quiet feeds cron callers; its issue list must stay bare."""
        code, out = run_main(hc, ["--quiet"], [OK, DOWN])
        self.assertEqual(code, 1)
        self.assertNotIn("workspace:", out)
        self.assertIn("voice-agent", out)


class RevertedSourceControl(unittest.TestCase):
    """Proves the tests above discriminate.

    Deleting the line is the weak mutation — a source-substring test catches
    that one too. This control also runs the `if False:` form, where the text
    survives and only the BEHAVIOR is gone; that is the mutation a source
    assertion cannot see, and it is why this file asserts on output.
    """

    def _import_mutant(self, source):
        # Written beside the original so `Path(__file__).parent.parent` still
        # resolves to the repo and the sibling imports keep working.
        fd, path = tempfile.mkstemp(prefix=".hc-mutant-", suffix=".py",
                                    dir=os.path.join(ROOT, "src"))
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as fh:
            fh.write(source)
        spec = importlib.util.spec_from_file_location("health_check_mutant", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _source(self):
        with open(SRC) as fh:
            source = fh.read()
        self.assertEqual(source.count(ANCHOR), 1,
                         "mutation anchor is not unique — the control would be vacuous")
        return source

    def _assert_silent(self, source, why):
        mod = self._import_mutant(source)
        _, out = run_main(mod, [], [OK, OK2])
        self.assertIn("Sutando Health Check", out,
                      f"harness did not capture the human report ({why})")
        self.assertNotIn("workspace:", out,
                         f"{why}: emission is disabled yet the line appeared")

    def test_removing_the_line_goes_silent(self):
        source = self._source()
        self._assert_silent(source.replace(ANCHOR, "\n"), "line removed")

    def test_unreachable_line_goes_silent(self):
        source = self._source()
        self._assert_silent(source.replace(ANCHOR, f"\n        if False: {EMITTED}\n"),
                            "line present but unreachable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
