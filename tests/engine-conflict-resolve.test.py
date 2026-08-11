#!/usr/bin/env python3
"""Tests for skills/engine-conflict-resolve (prepare/propose/apply).

Hermetic: local file:// upstream, every checkout under a scratch root whose
path contains a space (mirrors ~/Library/Application Support/...). The engine
fixture reproduces exactly what the desktop updater leaves behind after a
conflicted merge: snapshot branch at HEAD, sparse !/workspace/ guard, live
workspace symlink, ENGINE_UPDATE_PENDING.json in the updater's own shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "engine-conflict-resolve" / "scripts"

# Hermetic git: no user/system config, deterministic identity for fixtures.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
FIXTURE_IDENT = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@test",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@test",
}


def git(repo, *args, check=True):
    env = dict(os.environ)
    env.update(FIXTURE_IDENT)
    proc = subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def run_script(name, *args, env=None):
    return subprocess.run([sys.executable, str(SCRIPTS / name)] + list(args),
                          capture_output=True, text=True, env=env)


def out_json(proc):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(f"non-JSON stdout: {proc.stdout!r}\nstderr: {proc.stderr}")


class EngineFixture:
    """Post-conflict engine state, as update-engine-git.sh records it."""

    def __init__(self, conflicting=True):
        self.root = Path(tempfile.mkdtemp(prefix="engine conflict test "))
        upstream = self.root / "upstream"
        upstream.mkdir()
        git(self.root, "init", "-q", "-b", "main", str(upstream))
        (upstream / "src").mkdir()
        (upstream / "src" / "app.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
        (upstream / "README.md").write_text("readme v1\n")
        git(upstream, "add", "-A")
        git(upstream, "commit", "-q", "-m", "V1")
        self.v1 = git(upstream, "rev-parse", "HEAD").stdout.strip()
        (upstream / "src" / "app.txt").write_text("line1\nline2\nRELEASE line3\nline4\nline5\n")
        git(upstream, "commit", "-q", "-am", "V2 release")
        self.new_sha = git(upstream, "rev-parse", "HEAD").stdout.strip()

        engine_parent = self.root / "engine"
        self.engine = engine_parent / "sutando"
        git(self.root, "clone", "-q", f"file://{upstream}", str(self.engine))

        # Local line: branch off V1 with a local change (conflicting or not).
        # The updater's snapshot branch marks the same commit (named-branch case:
        # the local line advances on merge, the snapshot ref stays frozen).
        git(self.engine, "checkout", "-q", "-b", "local-line", self.v1)
        if conflicting:
            (self.engine / "src" / "app.txt").write_text("line1\nline2\nLOCAL line3\nline4\nline5\n")
        else:
            (self.engine / "README.md").write_text("readme v1\nlocal addition\n")
        git(self.engine, "commit", "-q", "-am", "local work")
        self.old_sha = git(self.engine, "rev-parse", "HEAD").stdout.strip()
        git(self.engine, "branch", "local-changes-2026-08-10", self.old_sha)

        # The attach-installed workspace guard + live workspace symlink.
        git(self.engine, "config", "core.sparseCheckout", "true")
        git(self.engine, "config", "core.sparseCheckoutCone", "false")
        info = self.engine / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "sparse-checkout").write_text("/*\n!/workspace/\n")
        self.live_ws = self.root / "live workspace"
        self.live_ws.mkdir()
        (self.live_ws / "user-state.txt").write_text("precious user state\n")
        (self.engine / "workspace").symlink_to(self.live_ws)

        self.pending = engine_parent / "ENGINE_UPDATE_PENDING.json"
        self.pending.write_text(json.dumps({
            "new_sha": self.new_sha,
            "old_sha": self.old_sha,
            "snapshot_branch": "local-changes-2026-08-10",
            "conflicting_files": ["src/app.txt"] if conflicting else [],
            "ts": "2026-08-10T00:00:00Z",
            "deferred": False,
        }, indent=2))
        self.scratch = self.root / "scratch dir" / "wt"

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def prepare(self, env=None):
        return run_script("prepare.py", "--pending", str(self.pending),
                          "--engine", str(self.engine), "--scratch", str(self.scratch),
                          env=env)

    @property
    def proposal(self):
        return self.engine.parent / "ENGINE_CONFLICT_PROPOSAL.json"

    def apply(self, merged_sha):
        return run_script("apply.py", "--pending", str(self.pending),
                          "--engine", str(self.engine), "--merged-sha", merged_sha)

    def assert_invariants(self, t):
        ws = self.engine / "workspace"
        t.assertTrue(ws.is_symlink(), "workspace symlink must survive")
        t.assertEqual(os.path.realpath(ws), os.path.realpath(self.live_ws))
        t.assertEqual((self.live_ws / "user-state.txt").read_text(), "precious user state\n")
        t.assertEqual(git(self.engine, "rev-parse", "refs/heads/local-changes-2026-08-10").stdout.strip(),
                      self.old_sha, "snapshot branch must still point at the user's work")


class TestCleanMergeFastPath(unittest.TestCase):
    def setUp(self):
        self.fx = EngineFixture(conflicting=False)
        self.addCleanup(self.fx.cleanup)

    def test_clean_merge_prepare_then_apply(self):
        fx = self.fx
        p = fx.prepare()
        self.assertEqual(p.returncode, 0, p.stderr)
        data = out_json(p)
        self.assertEqual(data["status"], "clean")
        merged = data["merged_sha"]
        self.assertTrue(merged)
        # prepare never touches the live checkout
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), fx.old_sha)
        self.assertTrue(fx.pending.is_file())

        # No proposal record yet — apply must refuse even a valid merged_sha.
        early = fx.apply(merged)
        self.assertEqual(early.returncode, 5, early.stdout + early.stderr)
        self.assertEqual(out_json(early)["reason"], "proposal-missing")
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), fx.old_sha)

        # Clean path still runs propose: that is what records the proposal.
        pr = run_script("propose.py", "--scratch", str(fx.scratch))
        self.assertEqual(pr.returncode, 0, pr.stdout + pr.stderr)
        self.assertEqual(out_json(pr)["merged_sha"], merged)
        self.assertTrue(fx.proposal.is_file())

        a = fx.apply(merged)
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        self.assertEqual(out_json(a)["status"], "applied")
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), merged)
        self.assertFalse(fx.pending.exists(), "pending must be cleared")
        self.assertFalse(fx.proposal.exists(), "proposal record must be cleared")
        self.assertNotIn(str(fx.scratch),
                         git(fx.engine, "worktree", "list", "--porcelain").stdout)
        self.assertFalse(fx.scratch.exists(), "scratch worktree removed")
        fx.assert_invariants(self)


class TestConflictEndToEnd(unittest.TestCase):
    def setUp(self):
        self.fx = EngineFixture(conflicting=True)
        self.addCleanup(self.fx.cleanup)

    def resolve_in_scratch(self):
        """Simulate the agent's semantic resolution: keep both intents."""
        f = self.fx.scratch / "src" / "app.txt"
        self.assertIn("<<<<<<<", f.read_text(), "conflict markers are the agent's input")
        f.write_text("line1\nline2\nRESOLVED line3 (local+release)\nline4\nline5\n")
        git(self.fx.scratch, "add", "--", "src/app.txt")

    def test_conflict_resolve_propose_apply(self):
        fx = self.fx
        p = fx.prepare()
        self.assertEqual(p.returncode, 0, p.stderr)
        data = out_json(p)
        self.assertEqual(data["status"], "conflicts")
        self.assertEqual(data["conflicting_files"], ["src/app.txt"])
        self.assertEqual(os.path.realpath(data["scratch"]), os.path.realpath(fx.scratch))
        # live checkout untouched, no merge state there
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), fx.old_sha)
        self.assertEqual(git(fx.engine, "status", "--porcelain", "--", ".",
                             ":(exclude)workspace").stdout.strip(), "")

        # prepare re-run while the scratch merge is open: reused, same answer
        p2 = fx.prepare()
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertEqual(out_json(p2)["status"], "conflicts")

        self.resolve_in_scratch()
        pr = run_script("propose.py", "--scratch", str(fx.scratch))
        self.assertEqual(pr.returncode, 0, pr.stdout + pr.stderr)
        prop = out_json(pr)
        self.assertEqual(prop["status"], "proposed")
        merged = prop["merged_sha"]
        self.assertIn("src/app.txt", [f["path"] for f in prop["files"]])
        self.assertTrue(any("src/app.txt" in l and "hand-merged" in l
                            for l in prop["summary_lines"]), prop["summary_lines"])
        self.assertIn("src/app.txt", prop["diffstat"])
        # merge commit: parents are the snapshot tip and the release
        parents = git(fx.scratch, "rev-list", "--parents", "-n", "1", merged).stdout.split()
        self.assertEqual(set(parents[1:]), {fx.old_sha, fx.new_sha})
        self.assertIn("Sutando <noreply@local>",
                      git(fx.scratch, "log", "-1", "--format=%an <%ae>", merged).stdout)

        a = fx.apply(merged)
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), merged)
        self.assertEqual((fx.engine / "src" / "app.txt").read_text(),
                         "line1\nline2\nRESOLVED line3 (local+release)\nline4\nline5\n")
        self.assertFalse(fx.pending.exists())
        self.assertFalse(fx.scratch.exists())
        self.assertFalse((fx.engine.parent / "ENGINE_UPDATE_LOCK.d").exists(),
                         "apply must release the updater lock")
        fx.assert_invariants(self)

    def test_propose_refuses_unmerged(self):
        fx = self.fx
        self.assertEqual(fx.prepare().returncode, 0)
        pr = run_script("propose.py", "--scratch", str(fx.scratch))
        self.assertEqual(pr.returncode, 2)
        data = out_json(pr)
        self.assertEqual(data["status"], "unmerged")
        self.assertEqual(data["unmerged_paths"], ["src/app.txt"])

    def test_apply_refuses_live_lock_then_reclaims_dead(self):
        fx = self.fx
        self.assertEqual(fx.prepare().returncode, 0)
        self.resolve_in_scratch()
        merged = out_json(run_script("propose.py", "--scratch", str(fx.scratch)))["merged_sha"]

        lock = fx.engine.parent / "ENGINE_UPDATE_LOCK.d"
        lock.mkdir()
        (lock / "info").write_text(f"pid={os.getpid()}\nts=2026-08-10T00:00:00Z\n")
        a = fx.apply(merged)
        self.assertEqual(a.returncode, 3, a.stdout + a.stderr)
        self.assertEqual(out_json(a)["reason"], "lock-busy")
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), fx.old_sha,
                         "busy apply must not touch the checkout")
        self.assertTrue(fx.pending.is_file())
        self.assertTrue((lock / "info").is_file(), "holder's lock must be preserved")

        # Same lock, but the recorded pid is dead → reclaimed with a log line.
        dead = subprocess.Popen(["true"])
        dead.wait()
        (lock / "info").write_text(f"pid={dead.pid}\nts=2026-08-10T00:00:00Z\n")
        a2 = fx.apply(merged)
        self.assertEqual(a2.returncode, 0, a2.stdout + a2.stderr)
        self.assertIn("reclaiming stale lock", a2.stderr)
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), merged)
        self.assertFalse(lock.exists(), "reclaimed lock released after apply")
        fx.assert_invariants(self)

    def test_apply_refuses_unrecorded_descendant(self):
        """Reviewer P1 repro: a post-proposal scratch commit must not land."""
        fx = self.fx
        self.assertEqual(fx.prepare().returncode, 0)
        self.resolve_in_scratch()
        merged = out_json(run_script("propose.py", "--scratch", str(fx.scratch)))["merged_sha"]

        # An unreviewed commit on top of the proposal, in the shared object db.
        (fx.scratch / "sneaky.txt").write_text("never shown to the owner\n")
        git(fx.scratch, "add", "-A", "--", ".", ":(exclude)workspace")
        git(fx.scratch, "commit", "-q", "-m", "post-proposal descendant")
        descendant = git(fx.scratch, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(descendant, merged)

        a = fx.apply(descendant)
        self.assertEqual(a.returncode, 5, a.stdout + a.stderr)
        self.assertEqual(out_json(a)["reason"], "proposal-mismatch")
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), fx.old_sha,
                         "refused apply must leave the checkout untouched")
        self.assertTrue(fx.pending.is_file())
        self.assertTrue(fx.proposal.is_file())

        # The recorded proposal itself still lands.
        a2 = fx.apply(merged)
        self.assertEqual(a2.returncode, 0, a2.stdout + a2.stderr)
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), merged)
        self.assertFalse((fx.engine / "sneaky.txt").exists(),
                         "the unreviewed descendant's content must not land")
        self.assertFalse(fx.proposal.exists())
        fx.assert_invariants(self)

    def test_apply_refuses_when_checkout_moved(self):
        fx = self.fx
        self.assertEqual(fx.prepare().returncode, 0)
        self.resolve_in_scratch()
        merged = out_json(run_script("propose.py", "--scratch", str(fx.scratch)))["merged_sha"]

        (fx.engine / "new-note.txt").write_text("moved after prepare\n")
        git(fx.engine, "add", "-A", "--", ".", ":(exclude)workspace")
        git(fx.engine, "commit", "-q", "-m", "user kept working")
        moved_head = git(fx.engine, "rev-parse", "HEAD").stdout.strip()

        a = fx.apply(merged)
        self.assertEqual(a.returncode, 4, a.stdout + a.stderr)
        self.assertEqual(out_json(a)["reason"], "checkout-moved")
        self.assertEqual(git(fx.engine, "rev-parse", "HEAD").stdout.strip(), moved_head,
                         "refusal must leave the checkout exactly as found")
        self.assertTrue(fx.pending.is_file(), "pending stays for the app to re-detect")
        self.assertFalse((fx.engine.parent / "ENGINE_UPDATE_LOCK.d").exists(),
                         "lock released on refusal")

    def test_prepare_reports_stale_pending(self):
        fx = self.fx
        (fx.engine / "new-note.txt").write_text("stale\n")
        git(fx.engine, "add", "-A", "--", ".", ":(exclude)workspace")
        git(fx.engine, "commit", "-q", "-m", "moved before prepare")
        p = fx.prepare()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(out_json(p)["reason"], "pending-stale")
        self.assertFalse(fx.scratch.exists(), "no scratch created for stale state")


class TestGitResolution(unittest.TestCase):
    """Installed-host runtime: sanitized PATH, possibly no system git at all."""

    def setUp(self):
        self.fx = EngineFixture(conflicting=True)
        self.addCleanup(self.fx.cleanup)
        self.real_git = shutil.which("git")
        self.assertTrue(self.real_git)

    def sanitized_env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = ""
        env.pop("SUTANDO_GIT", None)
        env.update(extra)
        return env

    def test_no_git_is_clean_json_not_a_traceback(self):
        p = self.fx.prepare(env=self.sanitized_env())
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        data = out_json(p)
        self.assertEqual(data["status"], "no-git")
        self.assertIn("SUTANDO_GIT", data["error"])
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(self.fx.scratch.exists())

    def test_sutando_git_env_works_without_path(self):
        p = self.fx.prepare(env=self.sanitized_env(SUTANDO_GIT=self.real_git))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(out_json(p)["status"], "conflicts")

    def test_git_flag_overrides(self):
        p = run_script("prepare.py", "--pending", str(self.fx.pending),
                       "--engine", str(self.fx.engine), "--scratch", str(self.fx.scratch),
                       "--git", self.real_git, env=self.sanitized_env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(out_json(p)["status"], "conflicts")


if __name__ == "__main__":
    unittest.main(verbosity=2)
