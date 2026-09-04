#!/usr/bin/env python3
"""`check_vendored_resolver_env` must discriminate, not just fire on any copy.

The defect it exists for: a vendored `workspace_default` predating v0.8 (#1440)
still honours $SUTANDO_WORKSPACE, so it resolves elsewhere than every v0.8
consumer whenever that env is set — silently. Measured 2026-09-04 on a live host.

It is a BEHAVIOUR probe rather than a diff against src on purpose: a census that
day found 17 same-named vendored files, 3 content-drifted, one defective, so a
hash gate would have flagged 3 to catch 1.
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hc_vre", _REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


def _vendored(ws: Path, source: Path) -> Path:
    d = ws / "skill-repos" / "someskill" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, d / "workspace_default.py")
    for extra in ("util_paths.py", "sutando_config.py"):
        s = _REPO / "src" / extra
        if s.exists():
            shutil.copy(s, d / extra)
    return d


class VendoredResolverEnvProbe(unittest.TestCase):
    def _ws(self) -> Path:
        return Path(tempfile.mkdtemp())

    def test_zero_copies_reports_coverage_rather_than_going_silent(self):
        """Sutando-Mini on #3892: a probe that finds nothing must SAY so.

        Returning None makes "scanned, found none" indistinguishable from "the scan
        looked in the wrong place", which is the vacuous-green shape this probe was
        written to catch — one layer down, inside the probe itself.
        """
        r = hc.check_vendored_resolver_env(workspace_dir=self._ws())
        self.assertIsNotNone(r, "a zero result went out as silence")
        self.assertEqual(r["status"], "ok")
        self.assertIn("zero copies scanned", r["detail"],
                      "the detail must state coverage, not imply a clean bill")

    def test_a_compliant_copy_reads_ok(self):
        """The discriminating control: without it the probe could warn on any copy."""
        ws = self._ws()
        _vendored(ws, _REPO / "src" / "workspace_default.py")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_a_pre_v08_copy_is_flagged_and_named(self):
        ws = self._ws()
        d = _vendored(ws, _REPO / "src" / "workspace_default.py")
        # A copy that honours the removed env, which is exactly the pre-v0.8 shape.
        (d / "workspace_default.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "def resolve_workspace():\n"
            "    return Path(os.environ.get('SUTANDO_WORKSPACE', '/fallback'))\n"
        )
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("workspace_default.py", r["detail"],
                      "the warn must NAME the offending file, not just count it")

    def test_a_copy_that_cannot_even_import_is_not_flagged(self):
        """A broken copy is not evidence of this defect; refuse to guess."""
        ws = self._ws()
        d = _vendored(ws, _REPO / "src" / "workspace_default.py")
        (d / "workspace_default.py").write_text("this is not python(\n")
        r = hc.check_vendored_resolver_env(workspace_dir=ws)
        self.assertEqual(r["status"], "ok", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
