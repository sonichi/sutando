#!/usr/bin/env python3
"""`sync-conflicts-unmerged` warns on peer content keep-ours preserved.

Every arm builds a REAL vault (git init + real backup directories) and calls the
probe with no injection. An earlier revision passed a fake runner into all eight
arms, so none executed the code the defect lived in — the probe's only reachable
arm on real data was its subprocess timeout, and the suite could not see it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


def _vault(td: str, batches: dict[str, list[str]] | None = None) -> Path:
    """A real git checkout, optionally with real keep-ours backup batches."""
    ws = Path(td) / "vault"
    ws.mkdir(parents=True)
    subprocess.run(["git", "-C", str(ws), "init", "-q", "-b", "main"], check=True)
    for batch, rels in (batches or {}).items():
        for rel in rels:
            f = ws / ".git" / "sutando-sync-conflicts" / batch / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("peer content\n")
    return ws


class SyncConflictsUnmerged(unittest.TestCase):
    def test_preserved_files_warn_and_name_the_counts_and_oldest_batch(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {
                "20260802T000000Z-origin_host_A": ["memory/MEMORY.md", "notes/x.md"],
                "20260903T000000Z-origin_host_B": ["memory/y.md"],
            })
            r = hc.check_sync_conflicts_unmerged(ws)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("3 peer file(s)", r["detail"])
        self.assertIn("2 keep-ours batch(es)", r["detail"])
        self.assertIn("20260802T000000Z-origin_host_A", r["detail"])   # oldest, not newest
        self.assertIn("sync-conflicts-report.py", r["detail"])

    def test_no_backup_root_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(hc.check_sync_conflicts_unmerged(_vault(td))["status"], "ok")

    def test_an_empty_backup_root_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td)
            (ws / ".git" / "sutando-sync-conflicts").mkdir(parents=True)
            r = hc.check_sync_conflicts_unmerged(ws)
        self.assertEqual(r["status"], "ok", r)

    def test_a_non_git_workspace_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "plain"
            plain.mkdir()
            r = hc.check_sync_conflicts_unmerged(plain)
        self.assertEqual(r["status"], "ok", r)

    def test_it_does_not_diff_and_so_stays_fast_on_a_large_tree(self):
        # The predecessor diffed contents: 24s over 892MB here, timing out on a
        # peer, so its `except` arm was the only one real data reached.
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {"20260802T000000Z-origin_host_A": [f"n/{i}.md" for i in range(400)]})
            import time
            t0 = time.monotonic()
            r = hc.check_sync_conflicts_unmerged(ws)
            elapsed = time.monotonic() - t0
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("400 peer file(s)", r["detail"])
        self.assertLess(elapsed, 5.0, f"probe took {elapsed:.1f}s on 400 files")

    def test_an_unreadable_backup_root_is_unobserved_never_zero(self):
        # Real permissions, real OSError: iterdir on a 000 directory.
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {"20260802T000000Z-origin_host_A": ["memory/x.md"]})
            root = ws / ".git" / "sutando-sync-conflicts"
            root.chmod(0o000)
            try:
                r = hc.check_sync_conflicts_unmerged(ws)
            finally:
                root.chmod(0o755)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not asserting a count", r["detail"])

    def test_a_git_call_that_raises_is_unobserved_never_zero(self):
        # Real git ERRORS (non-zero) rather than raising, so the only way to
        # reach this guard is to make the call itself throw.
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {"20260802T000000Z-origin_host_A": ["memory/x.md"]})
            real = hc.subprocess.run

            def boom(*a, **k):
                raise OSError("git exploded")

            hc.subprocess.run = boom
            try:
                r = hc.check_sync_conflicts_unmerged(ws)
            finally:
                hc.subprocess.run = real
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not asserting a count", r["detail"])
        self.assertIn("OSError", r["detail"])

    def test_a_retired_entry_is_not_counted_again(self):
        """The ledger is written by the REPORTER, so the fixture runs it.

        An earlier version fabricated a list of bare paths; the real writer
        stores a dict keyed `<batch>/<rel>@<digest>`, so the test agreed with
        the probe's wrong assumption instead of with the producer.
        """
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {"20260802T000000Z-origin_host_A": ["memory/x.md"]})
            reporter = REPO / "scripts" / "sync-conflicts-report.py"
            before = hc.check_sync_conflicts_unmerged(ws)
            self.assertEqual(before["status"], "warn", before)
            rc = subprocess.run(
                [sys.executable, str(reporter), str(ws), "--retire",
                 "20260802T000000Z-origin_host_A/memory/x.md"],
                capture_output=True, text=True)
            ledger = ws / ".git" / "sutando-sync-conflicts" / ".retired.json"
            self.assertTrue(ledger.is_file(), f"reporter wrote no ledger: {rc.stdout}{rc.stderr}")
            keys = list(json.loads(ledger.read_text()))
            self.assertTrue(any("@" in k for k in keys),
                            f"fixture precondition: the real key carries a digest, got {keys}")
            r = hc.check_sync_conflicts_unmerged(ws)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("retired", r["detail"])

    def test_a_non_git_child_does_not_answer_about_its_ancestor(self):
        # `rev-parse` searches ancestors: a child dir under a real vault would
        # otherwise be reported on using the ANCESTOR's backups.
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {"20260802T000000Z-origin_host_A": ["memory/x.md"]})
            child = ws / "not-a-repo"
            child.mkdir()
            r = hc.check_sync_conflicts_unmerged(child)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not a git top level", r["detail"])

    def test_the_warn_does_not_claim_the_files_are_absent_from_the_live_copy(self):
        # The cheap count cannot know reconciliation; saying so would be a
        # stronger claim than the probe measured.
        with tempfile.TemporaryDirectory() as td:
            ws = _vault(td, {"20260802T000000Z-origin_host_A": ["memory/x.md"]})
            r = hc.check_sync_conflicts_unmerged(ws)
        self.assertEqual(r["status"], "warn", r)
        self.assertNotIn("none merged back", r["detail"])
        self.assertIn("not retired", r["detail"])

    def test_the_probe_is_registered_in_the_run(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertEqual(src.count("checks.append(check_sync_conflicts_unmerged())"), 1)




class RetirementIdentityIsTheCopy(unittest.TestCase):
    """Retirement identifies one preserved COPY, not a path.

    The production backup writer can write a destination that already exists, so
    the same path holds different content over time. Keying retirement on the
    path alone makes the probe report a NEVER-retired copy as retired — and this
    warning is the only notice that new peer content was preserved.
    """

    BATCH = "20260912T000000Z-batch"
    REL = "notes/x.md"
    ORIGINAL = "peer content that nobody merged back\n"
    REPLACED = "DIFFERENT peer content that nobody retired\n"

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.ws = _vault(self.td)
        self.saved = (self.ws / ".git" / "sutando-sync-conflicts"
                      / self.BATCH / self.REL)
        self.saved.parent.mkdir(parents=True, exist_ok=True)
        self.saved.write_text(self.ORIGINAL)

    def reporter(self, *extra):
        """The REAL CLI — a hand-written ledger would not prove the keys agree."""
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "sync-conflicts-report.py"),
             str(self.ws), *extra], capture_output=True, text=True).returncode

    def retire(self):
        rc = self.reporter("--retire", f"{self.BATCH}/{self.REL}")
        self.assertEqual(rc, 0, "the real --retire CLI did not succeed")

    def status(self):
        return hc.check_sync_conflicts_unmerged(workspace=self.ws)["status"]

    def test_the_probe_and_the_reporter_agree_before_any_retirement(self):
        self.assertEqual((self.status(), self.reporter()), ("warn", 1))

    def test_retiring_the_copy_clears_the_warning(self):
        self.retire()
        self.assertEqual((self.status(), self.reporter()), ("ok", 0))

    def test_REACTIVATION_changed_content_at_a_retired_path_warns_again(self):
        """The false clean: path-keyed retirement reports content nobody retired
        as retired, silencing the only warning that it was preserved."""
        self.retire()
        self.saved.write_text(self.REPLACED)
        self.assertEqual(
            self.reporter(), 1, "fixture: the reporter must see the new bytes")
        self.assertEqual(
            self.status(), "warn",
            "changed content at a retired path read as retired — the probe "
            "cleared a warning about bytes nobody has ever retired")

    def test_RESTORED_content_is_retired_again_without_a_second_retire(self):
        """The other direction: identity is the bytes, so putting the retired
        copy back must not need re-retiring. Without this arm the reactivation
        arm could pass by never clearing again."""
        self.retire()
        self.saved.write_text(self.REPLACED)
        self.saved.write_text(self.ORIGINAL)
        self.assertEqual((self.status(), self.reporter()), ("ok", 0))

    def test_an_unreadable_copy_is_UNOBSERVED_not_retired(self):
        """Bounding the hash cost must not assert an unchecked copy is retired."""
        self.retire()
        self.saved.write_bytes(b"\xff\xfe raw bytes")
        self.assertIn(self.status(), ("warn", "ok"))

    def test_the_probe_does_not_respell_the_retirement_key(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertNotIn('rsplit("@"', src,
                         "stripping the digest keys retirement on the path")
        self.assertIn("_sync_conflicts_entry_key", src)
        self.assertNotIn("sha256", src.split("def check_sync_conflicts_unmerged")[1][:2000],
                         "a second digest spelling is how the two readers drift")


if __name__ == "__main__":
    unittest.main(verbosity=1)
