#!/usr/bin/env python3
"""The result-file disposal lifecycle has one owner, src/delivery/disposal.py.

Direct contract tests on the core helper (claim, verify, put back, recover,
suppress), plus wiring assertions that the gateway bridge delegates every
transition and defines no private copy of the state machine.

Run: python3 tests/gateway-disposal-delegation.test.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import outbox
import undelivered_quarantine
from delivery import disposal
from delivery.readiness import identity_of

BRIDGE = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"
PRIVATE = {"_claim_owner_holds", "_recover_disposing_claim", "_quarantine_generation",
           "_put_back", "_disposed_copy_exists", "_self_token", "_claim_stem"}


class CoreContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name) / "results"
        self.results.mkdir()
        self.lines: list[str] = []

    def result(self, body="answer", stem="task-a"):
        p = self.results / f"{stem}.txt"
        p.write_text(body)
        return p

    def dead_pid(self):
        p = subprocess.Popen(["true"]); p.wait()
        return p.pid

    def claim(self, stem="task-a", pid=None, start=1, acquired=None, nonce="deadbeef", restore=False):
        name = f".{stem}.disposing-{pid if pid is not None else self.dead_pid()}-{start}-" \
               f"{int(acquired if acquired is not None else time.time())}-{nonce}" + (".restore" if restore else "")
        return self.results / name

    def quarantined(self, stem="task-a"):
        return undelivered_quarantine.find_quarantined(self.results, stem)

    def test_the_generation_read_is_quarantined_once(self):
        r = self.result()
        _, gen = identity_of(r)
        target = disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertTrue(target.exists() and not r.exists())
        self.assertEqual(self.lines, [])
        with self.assertRaises(FileNotFoundError):
            disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertTrue(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_a_newer_reply_under_the_name_is_put_back_with_a_recorded_intent(self):
        r = self.result("OLD")
        _, gen = identity_of(r)
        r.unlink(); self.result("NEWER")
        with self.assertRaises(disposal.GenerationReplaced):
            disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(r.read_text(), "NEWER")
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_equal_bytes_under_another_inode_are_a_distinct_publication(self):
        r = self.result()
        _, gen = identity_of(r)
        other = self.results / "task-a.later"               # another inode, equal bytes
        other.write_text("answer")
        undelivered_quarantine.quarantine(other, self.results)
        self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_the_acquisition_time_lives_in_the_name_not_the_mtime(self):
        r = self.result()
        old = time.time() - 700
        os.utime(r, (old, old))
        pid, start = disposal.self_token()
        c = self.claim(pid=os.getppid(), start=int(outbox.process_identity(os.getppid()).start_usec or 0))
        r.rename(c)
        self.assertTrue(disposal.owner_holds(c), "a live owner keeps a fresh claim on an old reply")
        stale = self.claim(pid=os.getppid(), start=int(outbox.process_identity(os.getppid()).start_usec or 0),
                           acquired=time.time() - disposal.CLAIM_MAX_S - 1, nonce="5fc00001")
        c.rename(stale)
        self.assertFalse(disposal.owner_holds(stale), "past the bound the claim is stuck")

    def test_liveness_is_checked_before_age(self):
        r = self.result()
        c = self.claim(acquired=time.time() - 10 ** 6)   # ancient, but the owner is dead either way
        r.rename(c)
        self.assertFalse(disposal.owner_holds(c))
        live = self.claim(pid=os.getppid(), start=int(outbox.process_identity(os.getppid()).start_usec or 0), nonce="11e00001")
        self.result().rename(live)
        self.assertTrue(disposal.owner_holds(live))

    def test_a_dead_owners_claim_is_quarantined_with_one_line(self):
        self.result().rename(self.claim())
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("recovered", self.lines[0])

    def test_an_abandoned_restore_claim_goes_back_to_its_name(self):
        self.result("NEWER").rename(self.claim(restore=True))
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual((self.results / "task-a.txt").read_text(), "NEWER")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("restored", self.lines[0])

    def test_an_interrupted_put_back_leaves_no_duplicate(self):
        r = self.result()
        c = self.claim(restore=True)
        os.link(r, c)                                    # died between link and unlink
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertTrue(r.exists())
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.lines, [])

    def test_one_broken_claim_does_not_stop_the_others(self):
        self.result("A", stem="task-a").rename(self.claim(stem="task-a"))
        self.result("B", stem="task-b").rename(self.claim(stem="task-b", nonce="b0000001"))
        q = undelivered_quarantine.quarantine_dir(self.results)
        q.write_text("not a directory")
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual(len(self.lines), 2, "\n".join(self.lines))
        self.assertTrue(all("could not recover" in l for l in self.lines))
        self.assertEqual(len(disposal.find_claims(self.results)), 2, "the bodies stay visible")
        q.unlink()
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.quarantined("task-a")) + len(self.quarantined("task-b")), 2)

    def test_edges_a_recovery_pass_can_meet(self):
        results = self.results
        self.assertIsNone(disposal.recover_claim(results, results / ".odd-name", self.lines.append))
        self.assertTrue(disposal.owner_holds(results / ".odd-name"), "an unknown shape is never touched")
        self.assertFalse(disposal._same_file(results / "missing-a", results / "missing-b"))
        # a .restore claim whose canonical name was retaken: kept where the operator looks
        self.result("NEWEST")
        self.result("NEWER", stem="task-a.old").rename(self.claim(restore=True, nonce="ab000001"))
        disposal.recover_abandoned_claims(results, self.lines.append)
        self.assertEqual((results / "task-a.txt").read_text(), "NEWEST")
        self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("superseded", self.lines[0])
        # a .restore claim that vanished under the recoverer: nothing to do, no line
        gone = self.claim(restore=True, nonce="ab000002")
        self.result("X", stem="task-a.gone").rename(gone)
        real = os.link

        def steal(src, dst, *a, **kw):
            if Path(src) == gone:
                gone.unlink()
                raise FileNotFoundError(2, "gone", str(src))
            return real(src, dst, *a, **kw)
        (results / "task-a.txt").unlink()
        with unittest.mock.patch.object(os, "link", steal):
            self.assertIsNone(disposal.recover_claim(results, gone, self.lines.append))
        self.assertEqual(len(self.lines), 1)

    def test_an_active_claim_of_this_process_is_held(self):
        pid, start = disposal.self_token()
        c = self.claim(pid=pid, start=start)
        self.result().rename(c)
        self.assertFalse(disposal.owner_holds(c), "a claim we are not moving was abandoned")
        disposal.ACTIVE_CLAIMS.add(str(c))
        try:
            self.assertTrue(disposal.owner_holds(c))
        finally:
            disposal.ACTIVE_CLAIMS.discard(str(c))


class BridgeDelegates(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(BRIDGE.read_text())
        self.src = BRIDGE.read_text()

    def names(self):
        return {n.name for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)}

    def test_no_private_copy_of_the_state_machine(self):
        self.assertEqual(self.names() & PRIVATE, set())
        for banned in ("_DISPOSING =", "_ACTIVE_CLAIMS", "DISPOSING_CLAIM_MAX_S", ".disposing-"):
            self.assertNotIn(banned, self.src, banned)

    def test_the_bridge_imports_the_owner(self):
        self.assertIn("from . import result_disposal as disposal", self.src)

    def calls_in(self, fname):
        f = next(n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef) and n.name == fname)
        out = set()
        for n in ast.walk(f):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "disposal":
                out.add(n.func.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                out.add(n.func.id)
        return out

    def test_every_transition_is_delegated(self):
        self.assertIn("quarantine_generation", self.calls_in("_quarantine_undelivered"))
        self.assertIn("disposed_copy_exists", self.calls_in("_quarantine_undelivered"))
        self.assertIn("recover_abandoned_claims", self.calls_in("_recover_disposing_claims"))
        self.assertIn("_recover_disposing_claims", self.calls_in("_post_ready_results"))
        self.assertIn("_recover_disposing_claims", self.calls_in("_reconcile_orphan_results"))

    def test_the_vendored_copy_is_the_canonical_module(self):
        pkg = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_disposal.py"
        self.assertEqual(pkg.read_text(), (REPO / "src" / "delivery" / "disposal.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
