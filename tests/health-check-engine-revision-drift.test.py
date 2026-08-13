#!/usr/bin/env python3
"""Regression coverage for the engine source/artifact drift probe.

The bug this guards is invisible by construction: `dist/` is gitignored, so
moving the checkout forward leaves the compiled half on the older build and
nothing reports it. Every case below drives the production probe against a real
git repository rather than a stubbed one, because the discriminator IS the git
comparison.

Run: python3 tests/health-check-engine-revision-drift.test.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


class EngineRevisionDriftTest(unittest.TestCase):
    """`engine/` holds the manifest; `engine/sutando/` is the checkout."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        engine = Path(self._tmp.name) / "engine"
        self.repo = engine / "sutando"
        self.repo.mkdir(parents=True)
        self.manifest = engine / "ENGINE_MANIFEST.json"

        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("one\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "first")
        self.first = _git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _advance(self, n: int = 1) -> str:
        for i in range(n):
            (self.repo / "a.txt").write_text(f"more {i}\n")
            _git(self.repo, "commit", "-qam", f"advance {i}")
        return _git(self.repo, "rev-parse", "HEAD")

    def _write_manifest(self, **fields) -> None:
        self.manifest.write_text(json.dumps(fields))

    def _run(self) -> dict:
        return hc.check_engine_revision_drift(
            repo_dir=self.repo, manifest_path=self.manifest)

    # --- the case that matters ------------------------------------------

    def test_warns_when_source_moved_past_the_built_revision(self) -> None:
        """The live failure: source ahead of the artifacts, silently."""
        self._write_manifest(sha=self.first)
        head = self._advance(3)

        r = self._run()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("3 commits ahead", r["detail"])
        self.assertIn(head[:9], r["detail"])
        self.assertIn(self.first[:9], r["detail"])
        # The reason a checkout cannot fix this must reach the reader.
        self.assertIn("dist/", r["detail"])

    def test_ok_when_source_is_exactly_the_built_revision(self) -> None:
        """Control: the same probe must go quiet, or the warn proves nothing."""
        self._write_manifest(sha=self.first)
        r = self._run()
        self.assertEqual(r["status"], "ok", r)
        self.assertIn(self.first[:9], r["detail"])

    def test_warns_when_built_revision_is_not_in_this_clone(self) -> None:
        """A shallow clone cannot count, but drift is still established."""
        self._write_manifest(sha="0" * 40)
        self._advance()
        r = self._run()
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("diverged", r["detail"])

    # --- degrade cleanly where the question does not apply ---------------

    def test_ok_when_no_manifest(self) -> None:
        r = self._run()
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not a bundled engine", r["detail"])

    def test_ok_when_manifest_unreadable(self) -> None:
        self.manifest.write_text("{not json")
        r = self._run()
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("unreadable", r["detail"])

    def test_ok_when_manifest_has_no_sha(self) -> None:
        self._write_manifest(branch="main", builder="ci")
        r = self._run()
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("no sha", r["detail"])

    def test_ok_when_not_a_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            self._write_manifest(sha=self.first)
            r = hc.check_engine_revision_drift(
                repo_dir=Path(plain), manifest_path=self.manifest)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("skipping", r["detail"])

    # --- wiring ----------------------------------------------------------

    def test_probe_is_registered(self) -> None:
        """An unregistered probe reports nothing and looks identical to green."""
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_engine_revision_drift())", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
