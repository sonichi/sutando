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

import builtins
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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

    def test_abbreviated_sha_of_the_same_commit_is_not_drift(self) -> None:
        """A probe that prints "X != X (0 commits ahead)" discredits itself.

        The abbreviation satisfies `cat-file -e` and `merge-base --is-ancestor`,
        so before the fix it reached the ahead-branch with a count of zero
        instead of being recognised as the same commit. Reported by john-the-dev
        on #2864 with this repro; it cannot fire against today's manifest
        producer (it writes a full 40-char sha) but the failure mode is a
        self-contradiction, so it is closed rather than left latent.
        """
        for width in (7, 12, 40):
            with self.subTest(width=width):
                self._write_manifest(sha=self.first[:width])
                r = self._run()
                self.assertEqual(r["status"], "ok", r)
                self.assertIn("matches built revision", r["detail"])
                self.assertNotIn("!=", r["detail"])

    def test_a_tag_naming_head_is_not_drift(self) -> None:
        """Normalising via rev-parse also accepts any other ref-ish value."""
        _git(self.repo, "tag", "built-here")
        self._write_manifest(sha="built-here")
        r = self._run()
        self.assertEqual(r["status"], "ok", r)
        self.assertNotIn("!=", r["detail"])

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

    # --- the defensive branches -----------------------------------------
    #
    # Written deliberately, so tested deliberately. An untested `except` is a
    # guess about what the failure looks like, and the whole contract of this
    # probe is that it degrades instead of taking the health run down with it.

    def test_ok_when_git_resolver_reports_no_runnable_git(self) -> None:
        self._write_manifest(sha=self.first)
        self._advance()
        mod = types.ModuleType("git_binary")
        mod.resolve_git = lambda: None            # resolver: no usable git
        with mock.patch.dict(sys.modules, {"git_binary": mod}):
            r = self._run()
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("no runnable git", r["detail"])

    def test_falls_back_to_plain_git_when_resolver_is_absent(self) -> None:
        """Composes on a tree where `git_binary` does not exist yet."""
        self._write_manifest(sha=self.first)
        head = self._advance(2)
        real_import = builtins.__import__

        def _no_resolver(name, *a, **kw):
            if name == "git_binary":
                raise ImportError("no git_binary here")
            return real_import(name, *a, **kw)

        with mock.patch.object(builtins, "__import__", _no_resolver):
            r = self._run()
        # Still answers, via a bare `git`.
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("2 commits ahead", r["detail"])
        self.assertIn(head[:9], r["detail"])

    def test_ok_when_git_cannot_be_executed(self) -> None:
        self._write_manifest(sha=self.first)
        self._advance()
        real_run = hc.subprocess.run

        def _boom(argv, *a, **kw):
            raise OSError("git is not executable here")

        hc.subprocess.run = _boom
        try:
            r = self._run()
        finally:
            hc.subprocess.run = real_run
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("git not runnable", r["detail"])

    def test_drift_still_reported_when_the_ahead_count_cannot_be_taken(self) -> None:
        """The count is a nicety; the drift is the finding and must survive."""
        self._write_manifest(sha=self.first)
        head = self._advance(2)
        real_run = hc.subprocess.run

        def _boom_after_head(argv, *a, **kw):
            # Let `rev-parse HEAD` through; fail everything used for counting.
            if "rev-parse" not in argv:
                raise OSError("cannot count from here")
            return real_run(argv, *a, **kw)

        hc.subprocess.run = _boom_after_head
        try:
            r = self._run()
        finally:
            hc.subprocess.run = real_run
        self.assertEqual(r["status"], "warn", r)
        self.assertIn(head[:9], r["detail"])
        self.assertIn("dist/", r["detail"])

    # --- wiring ----------------------------------------------------------

    def test_probe_is_registered(self) -> None:
        """An unregistered probe reports nothing and looks identical to green."""
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_engine_revision_drift())", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
