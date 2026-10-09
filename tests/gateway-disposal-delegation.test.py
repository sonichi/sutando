#!/usr/bin/env python3
"""The result-file disposal lifecycle has one owner, src/delivery/disposal.py.

Direct contract tests on the core helper (claim, verify, put back, recover,
suppress), plus wiring assertions that the gateway bridge delegates every
transition and defines no private copy of the state machine.

Run: python3 tests/gateway-disposal-delegation.test.py
"""
from __future__ import annotations

import ast
import hashlib
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

    def claim(self, stem="task-a", pid=None, start=1, acquired=None, nonce="deadbeef", restore=False,
              body="answer"):
        gen = hashlib.sha256(body.encode()).hexdigest()[:16]
        name = f".{stem}.disposing-{pid if pid is not None else self.dead_pid()}-{start}-" \
               f"{int(acquired if acquired is not None else time.time())}-{gen}-{nonce}" + (".restore" if restore else "")
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
        aged = self.claim(pid=os.getppid(), start=int(outbox.process_identity(os.getppid()).start_usec or 0),
                          acquired=time.time() - disposal.CLAIM_MAX_S - 1, nonce="5fc00001")
        c.rename(aged)
        self.assertTrue(disposal.owner_holds(aged), "a live owner is never aged out")
        self.assertIs(outbox.process_identity(1).state, outbox.OwnerState.UNKNOWN)
        opaque = self.claim(pid=1, start=0, acquired=time.time() - disposal.CLAIM_MAX_S - 1, nonce="5fc00002")
        aged.rename(opaque)
        self.assertFalse(disposal.owner_holds(opaque), "only an unreadable owner ages out")
        future = self.claim(pid=1, start=0, acquired=time.time() + disposal.FUTURE_SLACK_S + 1, nonce="5fc00003")
        opaque.rename(future)
        self.assertFalse(disposal.owner_holds(future), "a stamp from the future is stale")

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
        self.result("A", stem="task-a").rename(self.claim(stem="task-a", body="A"))
        self.result("B", stem="task-b").rename(self.claim(stem="task-b", nonce="b0000001", body="B"))
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

    def test_a_claim_naming_another_digest_is_restored_not_quarantined(self):
        # The owner died between the claim rename and its check: the body is
        # a reply it never verified, so it goes back live, never into quarantine.
        self.result("NEWER").rename(self.claim(body="OLD", nonce="ab000003"))
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual((self.results / "task-a.txt").read_text(), "NEWER")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("never verified", self.lines[0])

    def test_a_reused_inode_with_a_new_write_time_is_a_distinct_publication(self):
        # Simulated inode reuse: same dev/ino and bytes, a different write time.
        r = self.result()
        _, gen = identity_of(r)
        os.utime(r, ns=(gen.mtime_ns + 1_000, gen.mtime_ns + 1_000))
        self.assertFalse(disposal._is_generation(r, gen))
        undelivered_quarantine.quarantine(r, self.results)
        self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_a_failure_to_read_the_claim_is_reported_not_called_a_replacement(self):
        r = self.result()
        _, gen = identity_of(r)
        real = disposal.identity_of

        def emfile(path):
            if "disposing" in str(path):
                raise OSError(24, "Too many open files")
            return real(path)
        with unittest.mock.patch.object(disposal, "identity_of", emfile):
            with self.assertRaises(OSError) as cm:
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(cm.exception.errno, 24)
        self.assertEqual(r.read_text(), "answer", "the body is back at its name")
        self.assertEqual(disposal.find_claims(self.results), [])

    def test_the_restore_intent_is_registered_before_it_is_published(self):
        r = self.result("OLD")
        _, gen = identity_of(r)
        r.unlink(); self.result("NEWER")
        real = os.rename
        seen = []

        def watch(src, dst, *a, **kw):
            if str(dst).endswith(".restore"):
                seen.append(str(dst) in disposal.ACTIVE_CLAIMS)
            return real(src, dst, *a, **kw)
        with unittest.mock.patch.object(os, "rename", watch):
            with self.assertRaises(disposal.GenerationReplaced):
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(seen, [True], "the .restore path must be held before the rename")

    def test_a_put_back_whose_name_is_retaken_keeps_the_last_link(self):
        # A producer replaces the canonical file between the put-back link and
        # the claim drop: the claim is now that reply's only link and is kept.
        r = self.result("NEWER")
        c = self.claim(restore=True, body="NEWER", nonce="ab000004")
        r.rename(c)
        real = os.stat

        def replace_then_stat(path, *a, **kw):
            if Path(path) == c and not kw.get("_again"):
                tmp = self.results / "task-a.newest"
                tmp.write_text("NEWEST")
                os.replace(tmp, self.results / "task-a.txt")
            return real(path, *a, **kw)
        with unittest.mock.patch.object(os, "stat", replace_then_stat):
            self.assertTrue(disposal.put_back(c, self.results / "task-a.txt", self.lines.append))
        self.assertEqual((self.results / "task-a.txt").read_text(), "NEWEST")
        bodies = [p.read_text() for p in self.quarantined()]
        self.assertEqual(bodies, ["NEWER"], "the superseded reply must survive somewhere visible")
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("superseded", self.lines[0])

    def test_recovery_of_a_duplicate_link_whose_name_is_retaken_keeps_the_body(self):
        r = self.result("NEWER")
        c = self.claim(restore=True, body="NEWER", nonce="ab000005")
        os.link(r, c)                                    # an interrupted put-back
        real = os.stat

        def replace_then_stat(path, *a, **kw):
            if Path(path) == c:
                tmp = self.results / "task-a.newest"
                tmp.write_text("NEWEST")
                os.replace(tmp, self.results / "task-a.txt")
            return real(path, *a, **kw)
        with unittest.mock.patch.object(os, "stat", replace_then_stat):
            disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual((self.results / "task-a.txt").read_text(), "NEWEST")
        self.assertEqual([p.read_text() for p in self.quarantined()], ["NEWER"])
        self.assertEqual(disposal.find_claims(self.results), [])

    def test_a_malformed_claim_name_is_named_once_and_never_touched(self):
        odd = self.results / ".task-a.disposing-123-1-abcd"
        self.result().rename(odd)
        for _ in range(3):
            disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertTrue(odd.exists())
        self.assertEqual(len(self.lines), 1)
        self.assertIn("unknown shape", self.lines[0])

    def test_the_lock_serializes_observers_and_is_reentrant(self):
        lock = self.results / disposal.LOCK_NAME
        with disposal.locked(self.results):
            with disposal.locked(self.results):           # same thread: re-entered, not deadlocked
                self.assertTrue(lock.exists())
            probe = subprocess.run([sys.executable, "-c", (
                "import fcntl,os,sys\n"
                f"fd=os.open({str(lock)!r}, os.O_RDWR)\n"
                "try:\n fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print('free')\n"
                "except BlockingIOError: print('held')")], capture_output=True, text=True)
            self.assertEqual(probe.stdout.strip(), "held", "another process must wait on the lock")
        probe = subprocess.run([sys.executable, "-c", (
            "import fcntl,os\n"
            f"fd=os.open({str(lock)!r}, os.O_RDWR)\nfcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print('free')")],
            capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "free")

    def test_a_pass_that_cannot_take_the_lock_skips_and_says_so_once(self):
        lock = self.results / disposal.LOCK_NAME
        self.result().rename(self.claim(nonce="ab000006"))
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl,os,sys,time\n"
            f"fd=os.open({str(lock)!r}, os.O_RDWR|os.O_CREAT)\nfcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\ntime.sleep(30)")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with unittest.mock.patch.object(disposal, "LOCK_WAIT_S", 0.2):
            for _ in range(3):
                disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual(len(disposal.find_claims(self.results)), 1, "nothing moved without the lock")
        self.assertEqual(len(self.lines), 1)
        self.assertIn("recovery skipped", self.lines[0])

    def test_an_unreadable_copy_or_claim_proves_nothing_to_the_loser(self):
        # EMFILE while matching a candidate is skipped, never read as a match.
        r = self.result()
        _, gen = identity_of(r)
        undelivered_quarantine.quarantine(r, self.results)
        self.result().rename(self.claim(nonce="ab000007"))
        real = disposal.identity_of

        def emfile(path):
            raise OSError(24, "Too many open files")
        with unittest.mock.patch.object(disposal, "identity_of", emfile):
            self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))
        with unittest.mock.patch.object(disposal, "identity_of", real):
            self.assertTrue(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_dropping_a_link_that_already_vanished_is_quiet(self):
        c = self.claim(nonce="ab000008")
        disposal._drop_link(c, self.lines.append, "task-a", self.results)
        self.assertEqual(self.lines, [])

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

    # Every function of the bridge that still moves or removes a file, as of
    # this head; a new one is a private disposal path until proven otherwise.
    MOVERS = {"_atomic_private_json", "_backup_tier_map_to_disk", "_emit_gateway_status",
              "_move_no_clobber", "_publish_staged", "_save_dedup_aliases", "_save_task_rooms",
              "_write_owner_activity", "refresh_routing"}

    def test_no_filesystem_transition_outside_the_known_movers(self):
        movers = {}
        for f in (n for n in ast.walk(self.tree) if isinstance(n, ast.FunctionDef)):
            for c in (n for n in ast.walk(f) if isinstance(n, ast.Call)):
                fn = c.func
                if not isinstance(fn, ast.Attribute):
                    continue
                base = fn.value.id if isinstance(fn.value, ast.Name) else None
                if (base in ("os", "shutil") and fn.attr in ("rename", "replace", "link", "unlink", "remove", "move")) \
                        or (base is None and fn.attr in ("rename", "replace", "unlink", "link_to", "hardlink_to")):
                    movers.setdefault(f.name, set()).add(f"{base or '?'}.{fn.attr}@{c.lineno}")
        self.assertEqual(set(movers) - self.MOVERS, set(),
                         f"a bridge function moves files on its own: {movers}")
        for literal in (".disposing-", "disposing-*", ".restore"):
            self.assertNotIn(literal, self.src, "the bridge must not know the claim namespace")

    def test_the_vendored_copy_is_the_canonical_module(self):
        pkg = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_disposal.py"
        self.assertEqual(pkg.read_text(), (REPO / "src" / "delivery" / "disposal.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
