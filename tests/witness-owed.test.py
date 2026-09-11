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


class Round4Edges(Fixture):
    """The error and skip branches the round-4 code added."""

    def test_validation_and_publish_reject_the_shapes_they_name(self):
        with self.assertRaises(ValueError):
            wo.validate_record(["not", "an", "object"], self.ws / "x.json")
        with self.assertRaises(ValueError):
            wo.publish(self.ws, "")

    def test_invalid_or_unreadable_tombstones_are_ignored(self):
        p = self.open12(host=HOST_A)
        tdir = wo.records_dir(self.ws, HOST_B) / "tombstones"; tdir.mkdir(parents=True)
        (tdir / "o-r#12.json").write_text(json.dumps({"repo": "o/r", "pr": 12}))   # no head, no witness
        (tdir / "broken.json").write_text("{not json")
        (tdir / "dir.json").mkdir()
        self.assertEqual(wo.tombstones(self.ws), {})
        self.assertEqual([r["pr"] for r in wo.list_open(self.ws)], [12], "an invalid tombstone closes nothing")

    def test_staleness_never_counts_this_host_and_an_unresolvable_current_ref_blocks(self):
        self.open12(host=HOST_A)
        self.assertEqual(wo.stale_hosts(self.ws, HOST_A, 1), [], "a host is never stale to itself")
        later = self.merge_topology()
        hits = wo.blocking(self.ws, self.repo, later, "no-such-ref", target_repo="o/r")
        self.assertEqual(len(hits), 1)
        self.assertIn("git could not answer", hits[0]["reason"])

    def test_tombstone_helper_error_branches_through_the_cli(self):
        r = wo.main(["--workspace", str(self.ws), "tombstone", "o/r#12", "--witness", "w", "--host", HOST_B])
        self.assertEqual(r, 5, "no open record")
        self.open12(host=HOST_A)
        with self.assertRaises(ValueError):
            wo.tombstone_record(self.ws, "o/r", 12, "", HOST_B)
        with self.assertRaises(ValueError):
            wo.tombstone_record(self.ws, "o/r", 12, "w", "")
        self.assertEqual(wo.main(["--workspace", str(self.ws), "publish", "--host", HOST_B]), 0)
        self.assertTrue((wo.records_dir(self.ws, HOST_B) / wo.STAMP_NAME).exists())
        self.assertEqual(wo.main(["--workspace", str(self.ws), "tombstone", "o/r#12", "--witness", "w", "--host", HOST_B]), 0)
        self.assertEqual(wo.list_open(self.ws), [])

    def test_cli_resolves_the_workspace_when_none_is_given(self):
        # Read-only: `list` on the resolved (real) workspace prints open records, if any.
        self.assertEqual(wo.main(["list"]), 0)


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
        pull = self.SRC.index("git merge --ff-only")
        self.assertLess(gate, pull, "the gate must run before the head changes")
        self.assertIn('check --ref "$TARGET_SHA"', self.SRC)
        self.assertIn("--current HEAD", self.SRC)
        self.assertIn("--canary", self.SRC)

    def test_gate_fails_closed_and_uses_the_canonical_python(self):
        for token in ('[ -n "$GATE_WS" ] ||', '[ -n "$GATE_PY" ] ||',
                      '[ -f "$GATE_HELPER" ] ||', 'sutando-config.sh" python-bin',
                      '[ -n "$GATE_REPO" ] ||', '--repo "$GATE_REPO"', '--max-age "$GATE_MAX_AGE"',
                      'sync-workspace.sh" --pull-strict', 'publish --host "$GATE_HOST"'):
            self.assertIn(token, self.SRC, token)
        self.assertNotIn('python3 "$REPO/src/witness_owed.py"', self.SRC, "bare python3 may hit the CLT stub")
        # A missing host label cannot release a record, so it must not stop
        # `check`; it must stop a canary declaration.
        canary = self.SRC.index('if [ -n "$CANARY" ]')
        self.assertLess(canary, self.SRC.index('[ -n "$GATE_HOST" ] ||'))
        self.assertIn('${GATE_HOST:+--host "$GATE_HOST"}', self.SRC)

    def test_the_sync_the_gate_trusts_can_report_a_failed_pull(self):
        """The default tick returns the PUSH's rc, so its zero cannot mean
        "peer records arrived"; the gate must ask for the strict reading."""
        sync = (ROOT / "scripts/sync-workspace.sh").read_text()
        self.assertIn("--pull-strict|pull-strict)", sync, "sync-workspace owns the strict mode")
        self.assertNotIn("_pull_only_impl || true", sync, "a call site discards the pull's rc")
        self.assertIn('sync-workspace.sh" --pull-strict', self.SRC)
        self.assertNotIn('sync-workspace.sh" ||', self.SRC, "the bare default tick is back")


class Round5Blockers(Fixture):
    """keweichen's round-5 P1-2 controls, each reproduced against the
    production writers/readers. Every one of these passed BEFORE the fix."""

    def _open(self, repo="o/r", pr=12, host=HOST_A):
        return wo.open_record(self.ws, repo, pr, self.owed, host, "why", "me")

    def test_a_malformed_tombstone_cannot_clear_an_open_record(self):
        # Wrong filename, wrong owners, no timestamp — yet three fields were
        # enough to close a peer's record.
        self._open()
        self.assertEqual(len(wo.list_open(self.ws)), 1)
        bad = wo.records_dir(self.ws, HOST_B) / "tombstones" / "not-the-record-name.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(json.dumps({"repo": "o/r", "pr": 12, "head": self.owed,
                                   "witness": "https://example/x"}))
        self.assertEqual(len(wo.list_open(self.ws)), 1,
                         "a tombstone missing owners/timestamp and misnamed still closed the record")

    def test_a_tombstone_cannot_be_written_by_the_owing_host(self):
        self._open()
        stone = wo.records_dir(self.ws, HOST_A) / "tombstones" / wo.record_key("o/r", 12)
        stone.parent.mkdir(parents=True, exist_ok=True)
        stone.write_text(json.dumps({"repo": "o/r", "pr": 12, "head": self.owed,
                                     "owed_by": HOST_A, "closed_by": HOST_A,
                                     "witness": "https://example/x", "closed_at": "2026-09-04T00:00:00Z"}))
        self.assertEqual(len(wo.list_open(self.ws)), 1,
                         "a host tombstoned its OWN record, bypassing close_record's checks")

    def test_a_stamp_naming_another_host_is_not_freshness(self):
        wo.open_record(self.ws, "o/r", 12, self.owed, HOST_B, "why", "me")
        d = wo.records_dir(self.ws, HOST_B)
        wo._atomic_write(d / wo.STAMP_NAME, {"host": HOST_A, "published_at": wo._now()})
        self.assertEqual(wo.stale_hosts(self.ws, HOST_A, 3600.0), [HOST_B],
                         "host-a's stamp vouched for host-b's directory")

    def test_valid_repo_names_cannot_collide_on_one_record_file(self):
        # `a-b/c` and `a/b-c` both became `a-b-c` under replace('/', '-').
        first = self._open(repo="a-b/c", pr=7)
        second = self._open(repo="a/b-c", pr=7)
        self.assertNotEqual(first, second, "two distinct repos share one record file")
        keys = {r["repo"] for r in wo.list_open(self.ws)}
        self.assertEqual(keys, {"a-b/c", "a/b-c"},
                         f"one repo's record overwrote the other's: {keys}")
        self.assertIsNotNone(wo.find_record(self.ws, "a-b/c", 7))
        self.assertIsNotNone(wo.find_record(self.ws, "a/b-c", 7))

    def test_a_host_cannot_escape_the_workspace(self):
        for bad in ("../../escaped", "a/b", ".", "..", "", "  "):
            with self.assertRaises(ValueError, msg=f"host {bad!r} was accepted"):
                wo.records_dir(self.ws, bad)

    def test_concurrent_writers_do_not_destroy_each_other(self):
        import threading
        path = wo.records_dir(self.ws, HOST_A) / "concurrent.json"
        start = threading.Barrier(2)
        errors = []

        def w(tag):
            try:
                start.wait(timeout=5)
                wo._atomic_write(path, {"tag": tag})
            except Exception as exc:            # noqa: BLE001 - the control IS the type
                errors.append(type(exc).__name__)

        ts = [threading.Thread(target=w, args=(t,)) for t in ("a", "b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(errors, [], f"a shared .tmp name made concurrent writers race: {errors}")
        self.assertTrue(path.is_file())
        self.assertIn(json.loads(path.read_text())["tag"], ("a", "b"))
        leftovers = list(path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


class Round5Publication(Fixture):
    """keweichen's round-5 P1-1 controls: a stamp is a claim about RECORDS."""

    def test_a_record_opened_after_the_stamp_unpublishes_the_host(self):
        wo.publish(self.ws, HOST_A)
        self.assertFalse(wo.unpublished(self.ws, HOST_A), "a just-published empty host reads as published")
        self.open12(host=HOST_A)
        self.assertTrue(wo.unpublished(self.ws, HOST_A),
                        "a record opened after the stamp still read as published")
        wo.publish(self.ws, HOST_A)
        self.assertFalse(wo.unpublished(self.ws, HOST_A))

    def test_the_gate_names_this_hosts_own_unpublished_records(self):
        later = self.merge_topology()
        wo.publish(self.ws, HOST_A)
        self.open12(host=HOST_A)
        hits = wo.blocking(self.ws, self.repo, later, self.base, host=HOST_A,
                           target_repo="o/r", max_age_s=3600)
        own = [h for h in hits if h.get("stale") and h.get("host") == HOST_A]
        self.assertTrue(own, f"the host's own unpublished records did not block it: {hits}")
        self.assertIn("publish and push", own[0]["reason"])

    def test_a_peer_stamp_that_does_not_describe_its_records_is_stale(self):
        import shutil
        self.open12(host=HOST_A)
        wo.publish(self.ws, HOST_A)
        ws_b = Path(self.tmp.name) / "ws-b2"
        shutil.copytree(self.ws / "hosts" / HOST_A, ws_b / "hosts" / HOST_A)
        self.assertEqual(wo.stale_hosts(ws_b, HOST_B, 3600), [],
                         "a faithfully carried subtree should be fresh")
        # The carried subtree gains a record its stamp never covered — the shape
        # a mid-write copy or a hand edit produces.
        extra = ws_b / "hosts" / HOST_A / "witness-owed" / wo.record_key("o/r", 99)
        extra.write_text(json.dumps({"repo": "o/r", "pr": 99, "head": "c" * 40, "host": HOST_A,
                                     "reason": "r", "opened_by": "x",
                                     "opened_at": "2026-09-04T00:00:00Z", "canary": None}))
        self.assertEqual(wo.stale_hosts(ws_b, HOST_B, 3600), [HOST_A],
                         "a fresh timestamp beside records it never covered read as published")

    def test_a_host_with_nothing_to_publish_never_blocks_itself(self):
        # Every host without records once blocked itself, which is a fleet-wide
        # deadlock rather than a safety property.
        later = self.merge_topology()
        self.assertFalse(wo.unpublished(self.ws, HOST_B))
        hits = wo.blocking(self.ws, self.repo, later, self.base, host=HOST_B,
                           target_repo="o/r", max_age_s=3600)
        self.assertEqual([h for h in hits if h.get("host") == HOST_B], [])


class UpgradePublicationWiring(unittest.TestCase):
    """The stamp must be written where a push can carry it, and before the
    fleet is read. Ordering, not spelling: the indices are the assertion."""

    def setUp(self):
        self.sh = (ROOT / "skills" / "self-upgrade" / "scripts" / "upgrade.sh").read_text()

    def test_this_host_publishes_before_it_reads_the_fleet(self):
        publish = self.sh.index('publish --host "$GATE_HOST"')
        sync = self.sh.index('bash "$REPO/scripts/sync-workspace.sh"')
        check = self.sh.index('check --ref "$TARGET_SHA"')
        self.assertLess(publish, sync, "the stamp must travel WITH the records the sync pushes")
        self.assertLess(sync, check, "the fleet is read before it is refreshed")

    def test_the_pre_gate_sync_is_a_full_tick_not_pull_only(self):
        # --pull-only never publishes this host, so a fleet of pull-only
        # updaters ages every stamp out and then refuses forever.
        check = self.sh.index('check --ref "$TARGET_SHA"')
        # Only INVOCATIONS count: the first draft of this assertion matched its
        # own explanatory comment and failed on prose.
        calls = [ln for ln in self.sh[:check].splitlines()
                 if "sync-workspace.sh" in ln and not ln.lstrip().startswith("#")]
        self.assertTrue(calls, "nothing refreshes the fleet before the gate reads it")
        self.assertEqual([c for c in calls if "--pull-only" in c], [])

    def test_a_canary_declaration_restamps_before_the_gate_reads_it(self):
        # Declaring a canary MUTATES this host's record; without a re-stamp the
        # gate refuses the very host the canary was declared for.
        declare = self.sh.index('canary "$CANARY" --host "$GATE_HOST"')
        publish = self.sh.index('publish --host "$GATE_HOST"', declare)
        check = self.sh.index('check --ref "$TARGET_SHA"')
        self.assertLess(publish, check)

    def test_nothing_heavy_sits_between_the_pull_and_the_restart_handoff(self):
        # A synchronous vault push here delayed the handoff past its budget.
        pull = self.sh.index('git merge --ff-only')
        handoff = self.sh.index('new-session -d -s "$SERVICE_SESSION"')
        self.assertNotIn("sync-workspace.sh", self.sh[pull:handoff])


class Round7Serialization(Fixture):
    """keweichen round-5 P1-3: close and open on one record key must not
    interleave. The control is the production writers, raced."""

    HEAD_A, HEAD_B = "a" * 40, "b" * 40

    def _race(self, mod=wo):
        """Run keweichen's ordering: pause close() the instant it archives,
        let a concurrent open() fire, then resume. Returns (errors, close_exc)."""
        import threading
        import time
        mod.open_record(self.ws, "o/r", 12, self.HEAD_A, HOST_A, "why", "me")
        paused, errors, real_write = threading.Event(), [], mod._atomic_write

        def instrumented(path, data):
            real_write(path, data)
            if path.parent.name == "closed":
                paused.set()
                time.sleep(1.5)

        def opener():
            if not paused.wait(10):
                errors.append("close never archived")
                return
            try:
                mod.open_record(self.ws, "o/r", 12, self.HEAD_B, HOST_A, "reopened", "me")
            except Exception as exc:             # noqa: BLE001 - the race IS the subject
                errors.append(repr(exc))

        t = threading.Thread(target=opener)
        t.start()
        mod._atomic_write = instrumented
        close_exc = None
        try:
            mod.close_record(self.ws, "o/r", 12, "https://x/12", HOST_A)
        except Exception as exc:                 # noqa: BLE001 - same
            close_exc = exc
        finally:
            mod._atomic_write = real_write
        t.join(20)
        return errors, close_exc

    def _closed_head(self):
        return json.loads((wo.records_dir(self.ws, HOST_A) / "closed"
                           / wo.record_key("o/r", 12)).read_text())["head"]

    def test_a_close_cannot_delete_a_concurrently_reopened_hold(self):
        errors, close_exc = self._race()
        self.assertEqual(errors, [], f"the concurrent open failed: {errors}")
        self.assertIsNone(close_exc, f"the serialized close must succeed: {close_exc!r}")
        # THE POINT: the lock serializes them, so the reopened B survives the
        # close of A — the old ordering archived A and unlinked B.
        heads = [r["head"] for r in wo.list_open(self.ws)]
        self.assertEqual(heads, [self.HEAD_B],
                         f"the reopened hold vanished; closed={self._closed_head()}")
        self.assertEqual(self._closed_head(), self.HEAD_A)

    def test_without_the_lock_the_close_refuses_rather_than_deleting(self):
        """Control: disable ONLY the lock and the production compare-and-swap
        still refuses to unlink bytes it did not archive."""
        import contextlib
        real_lock = wo.record_lock
        wo.record_lock = lambda *a, **k: contextlib.nullcontext()
        try:
            errors, close_exc = self._race()
        finally:
            wo.record_lock = real_lock
        self.assertEqual(errors, [])
        self.assertIsInstance(close_exc, ValueError)
        self.assertIn("rewritten while closing", str(close_exc))
        self.assertEqual([r["head"] for r in wo.list_open(self.ws)], [self.HEAD_B],
                         "an unlocked close deleted the reopened hold")

    def test_the_lock_file_is_not_part_of_what_is_published(self):
        wo.open_record(self.ws, "o/r", 12, self.HEAD_A, HOST_A, "why", "me")
        wo.publish(self.ws, HOST_A)
        self.assertTrue(list((wo.records_dir(self.ws, HOST_A) / ".locks").glob("*.lock")))
        self.assertFalse(wo.unpublished(self.ws, HOST_A), "a lock file moved the digest")


class Round7SerializationControl(Fixture):
    """Before/after on ONE fixture: the PRE-FIX writer, built from the shipped
    module by disabling exactly the two added mechanisms, loses the hold."""

    HEAD_A, HEAD_B = Round7Serialization.HEAD_A, Round7Serialization.HEAD_B
    _race = Round7Serialization._race
    _closed_head = Round7Serialization._closed_head

    def _prefix_module(self):
        src = (ROOT / "src" / "witness_owed.py").read_text()
        out = src.replace("fcntl.flock(fh.fileno(), fcntl.LOCK_EX)", "pass", 1)
        out = out.replace("fcntl.flock(fh.fileno(), fcntl.LOCK_UN)", "pass", 1)
        out = out.replace("if path.read_text() != raw:", "if False:", 1)
        self.assertNotIn("LOCK_EX", out, "the lock substitution was a no-op")
        self.assertNotIn("path.read_text() != raw", out, "the CAS substitution was a no-op")
        mod = importlib.util.module_from_spec(spec)
        exec(compile(out, "witness_owed_prefix", "exec"), mod.__dict__)
        return mod

    def test_the_pre_fix_writer_reproduces_the_lost_hold(self):
        control = self._prefix_module()
        errors, close_exc = self._race(control)
        self.assertEqual(errors, [])
        self.assertIsNone(close_exc, "the pre-fix close raised; it used to succeed silently")
        # CONTROL: close archived A and unlinked the newly opened B.
        self.assertEqual(wo.list_open(self.ws), [], "the pre-fix defect did not reproduce")
        self.assertEqual(self._closed_head(), self.HEAD_A)


class Round7HostLabel(Fixture):
    """keweichen round-5 P1-4: the helper must accept every label the host
    contract can produce, and still refuse anything that is not one segment."""

    def test_a_legal_label_with_a_space_is_accepted_end_to_end(self):
        # `_host_label()`/`_host()` trim the ENDS only, so `My Mac` is legal and
        # `hosts/My Mac/` is the directory the vault carries.
        self.assertEqual(wo.validate_host("My Mac"), "My Mac")
        p = wo.open_record(self.ws, "o/r", 12, self.owed, "My Mac", "why", "me")
        self.assertEqual(p.relative_to(self.ws).parts[:3], ("hosts", "My Mac", "witness-owed"))
        later = self.merge_topology()
        hits = wo.blocking(self.ws, self.repo, later, self.base, host="My Mac", target_repo="o/r")
        self.assertEqual([h["host"] for h in hits], ["My Mac"],
                         "a legal host label made the gate raise instead of answering")
        self.assertEqual(wo.records_digest(self.ws, "My Mac") != wo.EMPTY_DIGEST, True)

    def test_the_shell_and_python_host_resolvers_agree_that_ends_are_trimmed(self):
        """Data pin on the contract this validator follows: both resolvers trim
        the ends only, which is exactly why an internal space must be kept."""
        py = (ROOT / "src" / "util_paths.py").read_text()
        sh = (ROOT / "scripts" / "sync-workspace.sh").read_text()
        self.assertIn("label containing a space is preserved", sh)
        self.assertIn("Matches SutandoConfig.hostLabel()", py)

    def test_a_host_that_is_not_one_segment_is_still_refused(self):
        for bad in ("../../escaped", "a/b", ".", "..", "", "  ", " lead", "trail ",
                    "nul\x00", "bell\x07", "back\\slash"):
            with self.assertRaises(ValueError, msg=f"host {bad!r} was accepted"):
                wo.validate_host(bad)


class Round7Publication(Fixture):
    """keweichen round-5 P1-1: what "published" means, and when the gate may
    trust a peer's directory."""

    def _peer_ws(self):
        return Path(self.tmp.name) / "ws-peer"

    def _push_cmd(self):
        """Stand-in for the vault push: copy this host's subtree to the peer."""
        import shlex
        return (f"cp -R {shlex.quote(str(self.ws / 'hosts'))} "
                f"{shlex.quote(str(self._peer_ws()))}/")

    def _cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "src" / "witness_owed.py"),
                               "--workspace", str(self.ws), *args],
                              capture_output=True, text=True)

    def test_a_published_hold_opened_on_one_host_blocks_the_gate_on_another(self):
        peer = self._peer_ws(); peer.mkdir(parents=True)
        r = self._cli("open", "o/r#12", "--head", self.owed, "--host", HOST_A,
                      "--reason", "no supervised lane", "--by", "001",
                      "--publish-with", self._push_cmd())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wo.unpublished(self.ws, HOST_A), "a successful push left the host unpublished")
        later = self.merge_topology()
        hits = wo.blocking(peer, self.repo, later, self.base, host=HOST_B,
                           target_repo="o/r", max_age_s=3600)
        self.assertEqual([(h["repo"], h["pr"]) for h in hits], [("o/r", 12)],
                         "a published hold was invisible to the peer's gate")

    def test_a_push_that_fails_withdraws_the_stamp_and_says_the_hold_is_local(self):
        r = self._cli("open", "o/r#12", "--head", self.owed, "--host", HOST_A,
                      "--reason", "no supervised lane", "--by", "001",
                      "--publish-with", "exit 7")
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn("on host-a only", r.stderr)
        self.assertFalse((wo.records_dir(self.ws, HOST_A) / wo.STAMP_NAME).exists(),
                         "a failed push left a stamp claiming peers can see the hold")
        # And the un-published host cannot activate: it blocks on itself.
        later = self.merge_topology()
        hits = wo.blocking(self.ws, self.repo, later, self.base, host=HOST_A,
                           target_repo="o/r", max_age_s=3600)
        self.assertTrue(any(h.get("stale") and "publish and push" in h["reason"] for h in hits),
                        f"an unpublished host activated anyway: {hits}")

    def test_open_without_publish_with_says_the_hold_is_not_in_force_fleet_wide(self):
        r = self._cli("open", "o/r#12", "--head", self.owed, "--host", HOST_A,
                      "--reason", "no supervised lane", "--by", "001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NOT PUBLISHED", r.stderr)
        self.assertTrue(wo.unpublished(self.ws, HOST_A))

    def test_the_deploying_host_must_have_pulled_before_it_trusts_the_fleet(self):
        """The (a) clause of the contract is the caller's: upgrade.sh asks for
        the strict tick, whose exit 3 is a refused pull."""
        sh = (ROOT / "skills" / "self-upgrade" / "scripts" / "upgrade.sh").read_text()
        self.assertIn('sync-workspace.sh" --pull-strict', sh)
        self.assertIn('if [ "$GATE_SYNC_RC" = "3" ]', sh)


class Round7MaxAge(Fixture):
    """keweichen round-5 P2: a bound that accepts infinity is not a bound."""

    def test_the_python_boundary_rejects_infinity_nan_and_non_positive(self):
        for bad in ("1e400", "inf", "-inf", "nan", "0", "-1", "abc", None):
            with self.assertRaises(ValueError, msg=f"max age {bad!r} was accepted"):
                wo.validate_max_age(bad)
        self.assertEqual(wo.validate_max_age("3600"), 3600.0)

    def test_blocking_refuses_an_infinite_bound_rather_than_disabling_expiry(self):
        self.open12(host=HOST_A)
        with self.assertRaises(ValueError):
            wo.blocking(self.ws, self.repo, self.base, None, HOST_B, "o/r", float("inf"))

    def test_the_cli_exits_2_on_an_unbounded_max_age_instead_of_passing(self):
        r = subprocess.run([sys.executable, str(ROOT / "src" / "witness_owed.py"),
                            "--workspace", str(self.ws), "check", "--ref", self.base,
                            "--repo-root", str(self.repo), "--max-age", "1e400"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("finite", r.stderr)


class Round7Declaration(unittest.TestCase):
    """keweichen round-5 P2: the bound is declared in the skill manifest, per
    CLAUDE.md, and the shell keeps no second copy of its rule."""

    MANIFEST = ROOT / "skills" / "self-upgrade" / "manifest.json"
    SRC = (ROOT / "skills/self-upgrade/scripts/upgrade.sh").read_text()

    def test_the_manifest_declares_the_setting_with_a_default(self):
        m = json.loads(self.MANIFEST.read_text())
        self.assertTrue(m.get("enabled"), "a disabled manifest declares nothing")
        self.assertEqual(m["config"]["SUTANDO_WITNESS_MAX_AGE"], "3600")

    def test_the_updater_reads_env_then_manifest_and_owns_no_numeric_policy(self):
        self.assertIn('GATE_MAX_AGE="${SUTANDO_WITNESS_MAX_AGE:-}"', self.SRC)
        self.assertIn("skills/self-upgrade/manifest.json", self.SRC)
        # The finite/positive rule lives in the helper that consumes the value.
        self.assertIn("from witness_owed import validate_max_age", self.SRC)
        self.assertNotIn("awk -v v=", self.SRC, "the shell kept its own copy of the bound's rule")


if __name__ == "__main__":
    unittest.main()
