#!/usr/bin/env python3
"""
Tests for the COMPILED-ARTIFACT branch of `mark_stale_if_outdated` in
`src/health-check.py` (the `binary_path is not None` block).

Background: `health-check.py` has three mtime comparisons. Two of them
cross-check content with git before flagging — the process-start path
(PR #253) and the bridges path (PR #255) — because `git checkout`,
`pull`, and `rebase` bump mtime on files whose content is byte-identical.
The compiled-artifact branch arrived later (PR #529) and was the only one
without that cross-check, so any mtime bump older than the threshold read
as "rebuild needed" regardless of whether the source actually changed.

Observed 2026-07-26: `upstream-sync` fast-forwarded `main` and restored
the deployment branch with a checkout. That restore restamped 116
byte-identical files, `src/Sutando/main.swift` among them, and
health-check reported `sutando-app: stale — binary is 2679 min older than
source — rebuild needed`. The binary was current; `main.swift`'s last
content change predated the build by six days. The alarm recurred on
every sync and trained the reader to ignore the channel.

Cases:
  a) binary NEWER than source            → no flag (below threshold).
  b) source mtime bumped, content
     IDENTICAL to the tree the binary
     was built from                      → no flag (the regression).
  c) source mtime bumped, content
     ACTUALLY CHANGED since the build    → flags "rebuild needed".
  d) git cross-check unavailable
     (no reflog entry at build time)     → flags (fail safe, never hide
                                           a real stale binary).
  e) binary absent                       → compiled branch skipped
                                           entirely.

(b) is the regression case: it FAILS on the parent commit and passes at
HEAD. (c) and (d) are the guard rails that keep the fix from being a
blanket suppression.

Run: python3 tests/health-check-compiled-artifact-stale.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import io
from contextlib import redirect_stdout
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()

# Captured before any patching: the tests stub `hc.subprocess.run`, which is
# the same module object this file imported, so the stub needs an unpatched
# handle to fall through to.
_REAL_RUN = subprocess.run

THRESHOLD = 1800  # seconds; matches mark_stale_if_outdated's default


def _init_git_repo(tmpdir: Path) -> None:
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=tmpdir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmpdir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmpdir, check=True)


def _commit_file(tmpdir: Path, path: str, content: bytes, msg: str,
                 when: float | None = None) -> str:
    """Commit `content` at `path`, optionally backdated to epoch `when`.

    Backdating matters: `_file_unchanged_since` resolves the tree via
    `git reflog HEAD --before=@<ts>`, and reflog entry times come from
    GIT_COMMITTER_DATE. A fixture that commits "now" but dates the binary
    hours earlier has no reflog entry at-or-before the build, so the
    cross-check correctly refuses to resolve and every case fails safe —
    which would make this suite structurally unable to exercise the fix.
    Real timelines commit first and build after; the fixture must too.
    """
    f = tmpdir / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    env = dict(os.environ)
    if when is not None:
        stamp = f"@{int(when)} +0000"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(["git", "add", path], cwd=tmpdir, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=tmpdir, check=True, env=env)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmpdir,
                          capture_output=True, text=True).stdout.strip()


def _set_mtime(p: Path, ts: float) -> None:
    os.utime(p, (ts, ts))


class CompiledArtifactStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        _init_git_repo(self.repo)
        self._patch = patch.object(hc, "REPO_DIR", self.repo)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.now = time.time()
        # Real timeline: source committed 30h ago, binary built 20h ago,
        # so the reflog has an entry at-or-before the build for the
        # cross-check to resolve against.
        self.commit_ts = self.now - 30 * 3600
        self.bin_mtime = self.now - 20 * 3600
        self.src = self.repo / "src" / "App" / "main.swift"
        self.binary = self.repo / "src" / "App" / "App"

    def _build_binary(self):
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_bytes(b"compiled")
        _set_mtime(self.binary, self.bin_mtime)

    def _run(self) -> dict:
        """Invoke the compiled-artifact branch in isolation.

        pgrep is stubbed to return nothing so the process-start path below
        short-circuits — this test is only about the binary comparison.
        """
        check = {"name": "app", "status": "ok", "detail": "running"}
        with patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            hc.mark_stale_if_outdated(check, self.src, "app-pattern",
                                      threshold_sec=THRESHOLD,
                                      binary_path=self.binary)
        return check

    def _fake_run(self, cmd, *args, **kwargs):
        """Pass git through to the real repo; make pgrep return no PIDs.

        Uses the module-level `_REAL_RUN` alias captured at import: patching
        `hc.subprocess.run` rebinds the attribute on the shared `subprocess`
        module object, so calling `subprocess.run` here would re-enter this
        stub and recurse.
        """
        if cmd and "pgrep" in str(cmd[0]):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return _REAL_RUN(cmd, *args, **kwargs)

    # (a) binary newer than source → nothing to flag
    def test_binary_newer_than_source_not_stale(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build_binary()
        _set_mtime(self.src, self.bin_mtime - 3600)  # source older than binary
        check = self._run()
        self.assertEqual(check["status"], "ok")

    # (b) THE REGRESSION: mtime bumped by a checkout, content identical
    def test_idempotent_mtime_bump_is_not_stale(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build_binary()
        # A checkout restamps the file long after the build; content unchanged.
        _set_mtime(self.src, self.now)
        check = self._run()
        self.assertEqual(
            check["status"], "ok",
            "content is byte-identical to the tree the binary was built from; "
            "an mtime-only bump must not report 'rebuild needed'")

    # (c) content really changed after the build → must still flag
    def test_real_content_change_is_stale(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build_binary()
        # Source genuinely edited after the binary was built.
        self.src.write_bytes(b"print(2)  // new behavior\n")
        _set_mtime(self.src, self.now)
        check = self._run()
        self.assertEqual(check["status"], "stale")
        self.assertIn("rebuild needed", check["detail"])

    # (d) no reflog entry at build time → fail safe, still flag
    def test_no_reflog_at_build_time_fails_safe(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build_binary()
        _set_mtime(self.src, self.now)
        # Binary predates the repo's entire reflog → cross-check cannot
        # establish what the content was, so it must NOT suppress.
        self.bin_mtime = self.now - 365 * 24 * 3600
        _set_mtime(self.binary, self.bin_mtime)
        check = self._run()
        self.assertEqual(
            check["status"], "stale",
            "an unresolvable cross-check must fail safe and flag, never hide "
            "a genuinely stale binary")

    # (e) no binary on disk → compiled branch does not apply
    def test_missing_binary_skips_compiled_branch(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        _set_mtime(self.src, self.now)
        check = {"name": "app", "status": "ok", "detail": "running"}
        with patch.object(hc.subprocess, "run", side_effect=self._fake_run):
            hc.mark_stale_if_outdated(check, self.src, "app-pattern",
                                      threshold_sec=THRESHOLD,
                                      binary_path=self.binary)
        self.assertEqual(check["status"], "ok")


class BinaryIsCurrentTests(unittest.TestCase):
    """`_binary_is_current` — the shared predicate behind both the stale
    check and the `--fix` auto-launch gate.

    The auto-launch gate previously compared mtimes directly
    (`binary.st_mtime >= source.st_mtime`), so a checkout that restamped
    `main.swift` made `--fix` refuse to launch a current binary and print
    "needs manual rebuild + relaunch". The app then stayed down until
    someone rebuilt something that did not need rebuilding.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        _init_git_repo(self.repo)
        self._patch = patch.object(hc, "REPO_DIR", self.repo)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.now = time.time()
        self.commit_ts = self.now - 30 * 3600
        self.bin_mtime = self.now - 20 * 3600
        self.src = self.repo / "src" / "App" / "main.swift"
        self.binary = self.repo / "src" / "App" / "App"

    def _build(self):
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_bytes(b"compiled")
        _set_mtime(self.binary, self.bin_mtime)

    def test_binary_newer_by_mtime_is_current(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build()
        _set_mtime(self.src, self.bin_mtime - 60)
        self.assertTrue(hc._binary_is_current(self.binary, self.src))

    def test_idempotent_restamp_is_current(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build()
        _set_mtime(self.src, self.now)  # checkout restamp, content unchanged
        # Pin the behavior delta: the expression the --fix gate used before
        # this change rejects the restamp, the predicate replacing it accepts
        # it. Spelled out here because the new cases cannot fail against the
        # parent commit — the symbol they exercise does not exist there, so
        # they error rather than assert, and an error is a weaker gate.
        old_gate = self.binary.stat().st_mtime >= self.src.stat().st_mtime
        self.assertFalse(old_gate, "fixture must reproduce the restamp")
        self.assertTrue(
            hc._binary_is_current(self.binary, self.src),
            "a restamp with identical content must not block --fix auto-launch")

    def test_real_content_change_is_not_current(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        self._build()
        self.src.write_bytes(b"print(2)\n")
        _set_mtime(self.src, self.now)
        self.assertFalse(
            hc._binary_is_current(self.binary, self.src),
            "a genuine post-build edit must still block auto-launch")

    def test_missing_binary_is_not_current(self):
        _commit_file(self.repo, "src/App/main.swift", b"print(1)\n", "init",
                     when=self.commit_ts)
        _set_mtime(self.src, self.now)
        self.assertFalse(hc._binary_is_current(self.binary, self.src))


class NewestAppSourceTests(unittest.TestCase):
    """`sutando_app_newest_source` picks the newest .swift, not a hardcoded name.

    The app is built from several sources; a check naming only main.swift stops
    noticing edits to its siblings.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "Sutando"
        self.d.mkdir(parents=True)
        self.now = time.time()

    def _write(self, name: str, age_h: float) -> Path:
        p = self.d / name
        p.write_text("// swift\n", encoding="utf-8")
        _set_mtime(p, self.now - age_h * 3600)
        return p

    def test_picks_the_newest_sibling_not_main(self):
        self._write("main.swift", 10)
        newest = self._write("RestartCoordinator.swift", 1)
        self.assertEqual(newest, hc.sutando_app_newest_source(self.d))

    def test_picks_main_when_main_is_newest(self):
        main = self._write("main.swift", 1)
        self._write("RestartCoordinator.swift", 10)
        self.assertEqual(main, hc.sutando_app_newest_source(self.d))

    def test_falls_back_to_main_when_no_sources_exist(self):
        self.assertEqual(self.d / "main.swift", hc.sutando_app_newest_source(self.d))

    def test_ignores_non_swift_and_directories(self):
        main = self._write("main.swift", 5)
        (self.d / "notes.md").write_text("x", encoding="utf-8")
        _set_mtime(self.d / "notes.md", self.now)
        sub = self.d / "nested.swift"
        sub.mkdir()
        _set_mtime(sub, self.now)
        self.assertEqual(main, hc.sutando_app_newest_source(self.d))

    def test_missing_directory_falls_back_to_main(self):
        gone = Path(self.tmp.name) / "absent"
        self.assertEqual(gone / "main.swift", hc.sutando_app_newest_source(gone))

    def test_defaults_to_the_repo_app_dir(self):
        with patch.object(hc, "REPO_DIR", Path(self.tmp.name).parent):
            got = hc.sutando_app_newest_source()
        self.assertEqual("Sutando", got.parent.name)


class NewestSourceSurvivesAnUnreadableDirectory(unittest.TestCase):
    """Both OSError paths fall back to main.swift. A raise here would abort the
    whole health check over an unreadable app directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "Sutando"
        self.d.mkdir(parents=True)

    def test_a_glob_that_raises_falls_back_to_main(self):
        class _GlobRaises(type(Path())):
            def glob(self, _pattern):
                raise OSError("directory unreadable")

        d = _GlobRaises(self.d)
        self.assertEqual(Path(d) / "main.swift", hc.sutando_app_newest_source(d))

    def test_a_stat_that_raises_falls_back_to_main(self):
        class _StatRaises(type(Path())):
            def is_file(self):
                return True

            def stat(self, *a, **k):
                raise OSError("stat refused")

        class _DirWithUnstattableSource(type(Path())):
            def glob(self, _pattern):
                return [_StatRaises(self / "RestartCoordinator.swift")]

        d = _DirWithUnstattableSource(self.d)
        self.assertEqual(Path(d) / "main.swift", hc.sutando_app_newest_source(d))


class FixDispatchNeverAutoLaunchesAStaleBinary(unittest.TestCase):
    """The sutando-app --fix branch launches ONLY a binary proven current.
    An earlier auto-fix leaked duplicate instances (3 concurrent, 2026-04-19),
    so a stale binary must be deferred to a manual rebuild, not opened."""

    def _run_fix(self, checks):
        opened = []
        with (
            patch.object(sys, "argv", ["health-check.py", "--fix"]),
            patch.object(hc, "run_all_checks", return_value=checks),
            patch.object(hc.subprocess, "run",
                         side_effect=lambda cmd, **k: opened.append(cmd)),
        ):
            try:
                with redirect_stdout(io.StringIO()):
                    hc.main()
            except SystemExit:
                pass
        return opened

    def test_a_stale_app_is_not_opened(self):
        checks = [{"name": "sutando-app", "status": "stale",
                   "detail": "binary is 232 min older than source — rebuild needed"}]
        opened = self._run_fix(checks)
        self.assertEqual(
            [c for c in opened if "/usr/bin/open" in c], [],
            "a stale binary must never be auto-launched — that leaks duplicate instances")

    def test_POSITIVE_CONTROL_the_branch_is_actually_reached(self):
        """Without this the negative above passes vacuously. Note the status:
        `issues` filters out "ok" and "warn", so ONLY a non-benign status such
        as "stale" reaches the fix dispatch at all."""
        called = []

        def _spy(*a, **k):
            called.append(1)
            return Path("nonexistent.swift")

        checks = [{"name": "sutando-app", "status": "stale",
                   "detail": "binary is 232 min older than source — rebuild needed"}]
        with patch.object(hc, "sutando_app_newest_source", side_effect=_spy):
            self._run_fix(checks)
        self.assertTrue(called, "the sutando-app fix branch never ran — "
                                "the negative case proves nothing without this")

    def test_a_warn_status_never_reaches_the_dispatch_at_all(self):
        """`issues` excludes "warn", so the branch's own `status == "warn"`
        auto-launch condition cannot fire from main(). Documented here because
        a reader of that branch would reasonably assume it can."""
        called = []
        checks = [{"name": "sutando-app", "status": "warn",
                   "detail": "configured but not running"}]
        with patch.object(hc, "sutando_app_newest_source",
                          side_effect=lambda *a, **k: called.append(1)):
            self._run_fix(checks)
        self.assertEqual(called, [], "a warn check must not enter the fix dispatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
