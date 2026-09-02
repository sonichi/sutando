#!/usr/bin/env python3
"""Contract for src/witness_owed.py and its wiring into self-upgrade."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("witness_owed", ROOT / "src" / "witness_owed.py")
wo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wo)
HOST_A, HOST_B = "host-a", "host-b"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, msg, body=""):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", msg, *(["-m", body] if body else []))
    return _git(repo, "rev-parse", "HEAD")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        self.base = _commit(self.repo, "base")
        # A topic branch whose head is the recorded PR head.
        _git(self.repo, "checkout", "-q", "-b", "topic")
        self.owed = _commit(self.repo, "owed pr work")
        _git(self.repo, "checkout", "-q", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def merge_topology(self):
        # The merge commit needs an identity too; the runner has none configured.
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "merge", "-q", "--no-ff", "-m", "merge topic (#12)", "topic")
        return _commit(self.repo, "later")

    def squash_topology(self):
        # GitHub squash: ONE new commit, parent = base, subject ends in (#N);
        # the PR head is NOT an ancestor.
        sq = _commit(self.repo, "feat: owed pr work (#12)")
        later = _commit(self.repo, "later")
        assert subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor",
                               self.owed, later]).returncode == 1
        return sq, later

    def open12(self, host=HOST_A):
        return wo.open_record(self.ws, "o/r", 12, self.owed, host, "no supervised lane", "001")


class Records(Fixture):
    def test_open_requires_every_field(self):
        for bad in ({"head": "notasha"}, {"host": ""}, {"reason": " "}, {"opened_by": ""}):
            kw = dict(head=self.owed, host="h", reason="r", opened_by="me")
            kw.update(bad)
            with self.assertRaises(ValueError, msg=bad):
                wo.open_record(self.ws, "o/r", 1, **kw)
        with self.assertRaises(ValueError):
            wo.open_record(self.ws, "o r", 1, head=self.owed, host="h", reason="r", opened_by="me")

    def test_record_lives_in_the_carried_per_host_subtree(self):
        p = self.open12()
        self.assertEqual(p.relative_to(self.ws).parts[:3], ("hosts", HOST_A, "witness-owed"))
        self.assertFalse((self.ws / "state").exists(), "nothing under state/, which the vault never carries")
        include = json.loads((ROOT / "sutando.config.json").read_text())["vault"]["sync"]["include"]
        self.assertIn("hosts/*/", include, "the shipped carrier must cover the record path")

    def test_a_record_opened_on_one_host_is_seen_by_every_host(self):
        # Host A opens; host B's reader (same carried tree) sees it and is refused.
        self.open12(host=HOST_A)
        later = self.merge_topology()
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base, host=HOST_B)), 1)
        self.assertEqual([r["host"] for r in wo.list_open(self.ws)], [HOST_A])

    def test_open_list_close_round_trip(self):
        self.open12()
        recs = wo.list_open(self.ws)
        self.assertEqual([(r["repo"], r["pr"], r["head"]) for r in recs], [("o/r", 12, self.owed)])
        with self.assertRaises(ValueError):
            wo.close_record(self.ws, "o/r", 12, "", HOST_A)
        closed = wo.close_record(self.ws, "o/r", 12, "https://example/pr/12#c1", HOST_A)
        self.assertEqual(wo.list_open(self.ws), [])
        self.assertEqual(closed.parent.name, "closed")
        self.assertIn("closed_at", json.loads(closed.read_text()))
        with self.assertRaises(FileNotFoundError):
            wo.close_record(self.ws, "o/r", 12, "again", HOST_A)

    def test_a_malformed_record_blocks_rather_than_vanishes(self):
        d = wo.records_dir(self.ws, HOST_A)
        d.mkdir(parents=True)
        (d / "o-r#3.json").write_text("{not json")
        (d / "o-r#4.json").write_text(json.dumps({"repo": "o/r"}))
        hits = wo.blocking(self.ws, self.repo, self.merge_topology())
        self.assertEqual(sorted(h["reason"][:10] for h in hits), ["unreadable", "unreadable"])


class Gate(Fixture):
    def test_merge_topology_blocks_only_a_target_that_newly_contains_the_head(self):
        self.open12()
        later = self.merge_topology()
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base)), 1)
        self.assertEqual(wo.blocking(self.ws, self.repo, self.base), [])
        self.assertEqual(wo.blocking(self.ws, self.repo, later, later), [])
        wo.close_record(self.ws, "o/r", 12, "posted", HOST_A)
        self.assertEqual(wo.blocking(self.ws, self.repo, later, self.base), [])

    def test_squash_topology_is_recognised_by_the_merge_subject(self):
        # The PR head is not an ancestor of main after a squash merge; the
        # gate must still see the owed PR in the range it is about to activate.
        self.open12()
        sq, later = self.squash_topology()
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base)), 1)
        self.assertEqual(wo.blocking(self.ws, self.repo, later, later), [], "already active: nothing new")
        self.assertEqual(wo.blocking(self.ws, self.repo, self.base), [])
        # A different PR whose head this clone never fetched: absent is not an
        # error, and its number is not in the range, so it does not block.
        other = wo.open_record(self.ws, "o/r", 13, "deadbeef" * 5, HOST_A, "r", "001")
        hits = wo.blocking(self.ws, self.repo, later, self.base)
        self.assertEqual(sorted(h["pr"] for h in hits), [12])
        other.unlink()

    def test_rebase_topology_is_recognised_by_a_body_naming_the_head(self):
        self.open12()
        _commit(self.repo, "rebased: owed pr work", body=f"Squashed from {self.owed}")
        later = _commit(self.repo, "later")
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base)), 1)

    def test_an_unfetched_head_is_not_an_error_but_a_broken_repo_is(self):
        # Squash merges leave the PR head unfetched on every deploying clone;
        # that must resolve through the subject scan, not fail closed forever.
        self.open12()
        sq, later = self.squash_topology()
        wo.open_record(self.ws, "o/r", 1, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", HOST_A, "r", "me")
        hits = wo.blocking(self.ws, self.repo, later, self.base)
        self.assertEqual(sorted(h["pr"] for h in hits), [12], "only the PR named in the range blocks")
        self.assertIs(wo._is_ancestor(self.repo, "deadbeef" * 5, later), False)
        self.assertIs(wo._is_ancestor(self.repo, self.base, later), True)
        self.assertIs(wo._is_ancestor(self.repo, later, self.base), False)
        # A ref git cannot resolve, or no repository at all, is a real error
        # and blocks every record — with or without a current ref.
        self.assertIsNone(wo._is_ancestor(self.repo, self.base, "no-such-ref"))
        hits = wo.blocking(self.ws, Path(self.tmp.name) / "not-a-repo", later, later)
        self.assertEqual(len(hits), 2)
        self.assertTrue(all("git could not answer" in h["reason"] for h in hits))

    def test_canary_releases_only_the_owing_host(self):
        self.open12(host=HOST_A)
        later = self.merge_topology()
        with self.assertRaises(ValueError):
            wo.mark_canary(self.ws, "o/r", 12, HOST_B)
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, host=HOST_B)), 1)
        wo.mark_canary(self.ws, "o/r", 12, HOST_A)
        self.assertEqual(wo.blocking(self.ws, self.repo, later, host=HOST_A), [])
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, host=HOST_B)), 1)
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later)), 1)
        with self.assertRaises(FileNotFoundError):
            wo.mark_canary(self.ws, "o/r", 99, HOST_A)


class Round4Blockers(Fixture):
    """keweichen's round-4 blockers, each with the control that was missing."""

    def test_all_keys_present_but_invalid_head_is_malformed_and_blocks(self):
        p = self.open12(); later = self.merge_topology()
        d = json.loads(p.read_text()); d["head"] = ""; p.write_text(json.dumps(d))
        recs = wo.list_open(self.ws)
        self.assertEqual([r.get("malformed") for r in recs], [True])
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base)), 1)
        for bad in ({"pr": True}, {"pr": 0}, {"repo": "nope"}, {"canary": "other-host"},
                    {"opened_at": "yesterday"}, {"host": ""}):
            d2 = dict(json.loads(p.read_text()) if p.exists() else {}); d2 = {**d2, **bad}
            p.write_text(json.dumps({**d, "head": self.owed, **bad}))
            self.assertTrue(wo.list_open(self.ws)[0].get("malformed"), bad)

    def test_a_record_whose_path_disagrees_with_its_payload_is_malformed(self):
        p = self.open12()
        wrong = p.with_name("o-r#13.json"); p.rename(wrong)
        self.assertTrue(wo.list_open(self.ws)[0].get("malformed"))
        wrong.rename(p)
        elsewhere = self.ws / "hosts" / HOST_B / "witness-owed" / p.name
        elsewhere.parent.mkdir(parents=True); p.rename(elsewhere)
        self.assertTrue(wo.list_open(self.ws)[0].get("malformed"))

    def test_an_unrelated_repositorys_same_pr_number_does_not_block(self):
        wo.open_record(self.ws, "unrelated/project", 12, "f" * 40, HOST_A, "elsewhere", "001")
        sq, later = self.squash_topology()   # subject ends in (#12)
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base, target_repo="o/r")), 0)
        self.assertEqual(len(wo.blocking(self.ws, self.repo, later, self.base)), 1,
                         "without a target repo the (#N) match is unscoped — the CLI always passes --repo")
        wo.open_record(self.ws, "o/r", 12, self.owed, HOST_A, "ours", "001")
        hits = wo.blocking(self.ws, self.repo, later, self.base, target_repo="o/r")
        self.assertEqual([h["repo"] for h in hits], ["o/r"])

    def test_only_the_owing_host_may_close_and_a_peer_tombstones(self):
        p = self.open12(host=HOST_A); later = self.merge_topology()
        with self.assertRaises(ValueError):
            wo.close_record(self.ws, "o/r", 12, "https://x/12", HOST_B)
        self.assertTrue(p.exists(), "a foreign close must not touch the owner's file")
        stone = wo.tombstone_record(self.ws, "o/r", 12, "https://x/12", HOST_B)
        self.assertEqual(stone.parent.parent.parent.name, HOST_B, "the tombstone lives in the closer's subtree")
        self.assertTrue(p.exists(), "the owner's record is untouched")
        self.assertEqual(wo.list_open(self.ws), [], "a tombstone for the exact head closes the record")
        self.assertEqual(wo.blocking(self.ws, self.repo, later, self.base, target_repo="o/r"), [])
        # A re-opened record for a NEW head is not covered by the old tombstone.
        p.unlink(); wo.open_record(self.ws, "o/r", 12, later, HOST_A, "reopened", "001")
        self.assertEqual(len(wo.list_open(self.ws)), 1)

    def test_freshness_two_workspaces_stale_copy_blocks_fresh_copy_passes(self):
        # Host B's workspace holds a COPY of host A's subtree (what the vault carries).
        ws_b = Path(self.tmp.name) / "ws-b"
        self.open12(host=HOST_A); wo.publish(self.ws, HOST_A); later = self.merge_topology()
        import shutil; shutil.copytree(self.ws / "hosts" / HOST_A, ws_b / "hosts" / HOST_A)
        # Fresh stamp: the record itself blocks (it is open), not staleness.
        hits = wo.blocking(ws_b, self.repo, later, self.base, host=HOST_B, target_repo="o/r", max_age_s=3600)
        self.assertEqual([h.get("stale", False) for h in hits], [False])
        # Host A closes at home; B's copy has not been refreshed: B must not activate on
        # a stale view, and it must not read a stale-but-present stamp as fresh forever.
        wo.close_record(self.ws, "o/r", 12, "https://x/12", HOST_A)
        self.assertEqual(wo.blocking(self.ws, self.repo, later, self.base, host=HOST_A, target_repo="o/r"), [])
        old = json.loads((ws_b / "hosts" / HOST_A / "witness-owed" / wo.STAMP_NAME).read_text())
        old["published_at"] = "2000-01-01T00:00:00Z"; (ws_b / "hosts" / HOST_A / "witness-owed" / wo.STAMP_NAME).write_text(json.dumps(old))
        hits = wo.blocking(ws_b, self.repo, later, self.base, host=HOST_B, target_repo="o/r", max_age_s=3600)
        self.assertTrue(any(h.get("stale") for h in hits), "a stale foreign stamp blocks by itself")
        # A missing stamp is the same as a stale one.
        (ws_b / "hosts" / HOST_A / "witness-owed" / wo.STAMP_NAME).unlink()
        self.assertEqual(wo.stale_hosts(ws_b, HOST_B, 3600), [HOST_A])
        # After a refresh (the vault's copy step), the closed record is gone and the stamp is fresh.
        shutil.rmtree(ws_b / "hosts" / HOST_A); wo.publish(self.ws, HOST_A)
        shutil.copytree(self.ws / "hosts" / HOST_A, ws_b / "hosts" / HOST_A)
        self.assertEqual(wo.blocking(ws_b, self.repo, later, self.base, host=HOST_B, target_repo="o/r", max_age_s=3600), [])


class Cli(Fixture):
    def _run(self, *args):
        import contextlib
        import io
        import types
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = wo.main(["--workspace", str(self.ws), *args])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 2
        return types.SimpleNamespace(returncode=rc, stdout=out.getvalue(), stderr=err.getvalue())

    def test_entry_point_runs_as_a_process(self):
        r = subprocess.run([sys.executable, str(ROOT / "src" / "witness_owed.py"),
                            "--workspace", str(self.ws), "list"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_check_exit_codes_and_messages(self):
        later = self.merge_topology()
        rr = str(self.repo)
        self.assertEqual(self._run("check", "--ref", later, "--repo-root", rr).returncode, 0)
        r = self._run("open", "o/r#7", "--head", self.owed, "--host", HOST_A,
                      "--reason", "no supervised lane on any host", "--by", "001")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run("check", "--ref", later, "--repo-root", rr)
        self.assertEqual((r.returncode, "witness owed: o/r#7" in r.stderr), (3, True))
        r = self._run("list"); self.assertIn("o/r#7 head=" + self.owed[:8], r.stdout)
        self.assertEqual(self._run("check", "--ref", later, "--repo-root", rr, "--host", HOST_A).returncode, 3)
        self.assertEqual(self._run("canary", "o/r#7", "--host", HOST_B).returncode, 5, "wrong host refused")
        self.assertEqual(self._run("canary", "o/r#7", "--host", HOST_A).returncode, 0)
        self.assertEqual(self._run("check", "--ref", later, "--repo-root", rr, "--host", HOST_A).returncode, 0)
        self.assertEqual(self._run("check", "--ref", later, "--repo-root", rr, "--host", HOST_B).returncode, 3)
        self.assertEqual(self._run("close", "o/r#7", "--witness", "thread", "--host", HOST_A).returncode, 0)
        self.assertEqual(self._run("close", "o/r#7", "--witness", "thread", "--host", HOST_A).returncode, 5)
        self.assertEqual(self._run("check", "--ref", later, "--repo-root", rr).returncode, 0)
        self.assertNotEqual(self._run("open", "bad key", "--head", self.owed, "--host", "h",
                                      "--reason", "r", "--by", "me").returncode, 0)


class UpgradeWiring(unittest.TestCase):
    SRC = (ROOT / "skills/self-upgrade/scripts/upgrade.sh").read_text()

    def test_self_upgrade_checks_the_record_before_it_pulls(self):
        gate = self.SRC.index("witness_owed.py")
        pull = self.SRC.index("git pull --ff-only")
        self.assertLess(gate, pull, "the gate must run before the head changes")
        self.assertIn('check --ref "$REMOTE/$BRANCH"', self.SRC)
        self.assertIn("--current HEAD", self.SRC)
        self.assertIn("--canary", self.SRC)

    def test_gate_fails_closed_and_uses_the_canonical_python(self):
        for token in ('[ -n "$GATE_WS" ] ||', '[ -n "$GATE_PY" ] ||',
                      '[ -f "$GATE_HELPER" ] ||', 'sutando-config.sh" python-bin',
                      '[ -n "$GATE_REPO" ] ||', '--repo "$GATE_REPO"', '--max-age "$GATE_MAX_AGE"',
                      'sync-workspace.sh" --pull-only ||', 'publish --host "$GATE_HOST"'):
            self.assertIn(token, self.SRC, token)
        self.assertNotIn('python3 "$REPO/src/witness_owed.py"', self.SRC, "bare python3 may hit the CLT stub")
        # A missing host label cannot release a record, so it must not stop
        # `check`; it must stop a canary declaration.
        canary = self.SRC.index('if [ -n "$CANARY" ]')
        self.assertLess(canary, self.SRC.index('[ -n "$GATE_HOST" ] ||'))
        self.assertIn('${GATE_HOST:+--host "$GATE_HOST"}', self.SRC)


if __name__ == "__main__":
    unittest.main()
