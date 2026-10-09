#!/usr/bin/env python3
"""The result-file disposal lifecycle has one owner, src/delivery/disposal.py.

Direct contract tests on the core helper (claim, verify, put back, recover,
suppress), plus wiring assertions that the gateway bridge delegates every
transition and defines no private copy of the state machine.

Run: python3 tests/gateway-disposal-delegation.test.py
"""
from __future__ import annotations

import ast
import ctypes
import errno
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


def _park_in_quarantine(rfile, results, when=None):
    """Fixture: put a result where the quarantine reader lists it."""
    return undelivered_quarantine.place(Path(rfile), results, Path(rfile).stem, when=when)
from delivery import disposal
from delivery.readiness import identity_of

BRIDGE = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"
PRIVATE = {"_claim_owner_holds", "_recover_disposing_claim", "_quarantine_generation",
           "_put_back", "_self_token", "_claim_stem", "_drop_link", "_is_generation"}


class CoreContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name) / "results"
        self.results.mkdir()
        self.lines: list[str] = []
        disposal._REPORTED.discard("rename:fallback")     # once-per-process lines start fresh per test

    def result(self, body="answer", stem="task-a"):
        p = self.results / f"{stem}.txt"
        p.write_text(body)
        return p

    def dead_pid(self):
        p = subprocess.Popen(["true"]); p.wait()
        return p.pid

    def claim(self, stem="task-a", pid=None, start=1, acquired=None, nonce="deadbeef", restore=False,
              body="answer", ino=None, mtime=None):
        # When the live result at `stem` holds `body` its inode and write time are
        # the identity; any other combination names a generation that is not the file.
        digest = hashlib.sha256(body.encode()).hexdigest()
        live = self.results / f"{stem}.txt"
        if ino is None or mtime is None:
            # The generation is the file holding `body`: the live result, or a
            # claim/quarantined copy it was already moved to.
            same = False
            for cand in [live] + sorted(live.parent.glob(f'.{live.stem}.disposing-*')) \
                    + sorted((live.parent / 'undelivered').glob(f'{live.stem}-*.txt')):
                try:
                    if cand.read_bytes() == body.encode():
                        st = os.stat(cand)
                        same = True
                        break
                except OSError:
                    continue
            ino = ino if ino is not None else (st.st_ino if same else 0)
            mtime = mtime if mtime is not None else (st.st_mtime_ns if same else 0)
        name = f".{stem}.disposing-{pid if pid is not None else self.dead_pid()}-{start}-" \
               f"{int(acquired if acquired is not None else time.time())}-{ino}-{mtime}-{digest}-{nonce}" \
               + (".restore" if restore else "")
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
        _park_in_quarantine(other, self.results)
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
        # pid 1 reads UNKNOWN (EPERM) on macOS but ALIVE on the Linux runners: stub the probe.
        unknown = outbox.ProcessIdentity(1, outbox.OwnerState.UNKNOWN)
        probe = unittest.mock.patch.object(disposal, "process_identity", lambda pid: unknown)
        probe.start(); self.addCleanup(probe.stop)
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

    def test_an_interrupted_put_back_leaves_no_claim_and_unlinks_nothing(self):
        # A second name of the live reply (an older link-based put-back left it)
        # is moved where the operator looks, never unlinked after a check.
        r = self.result()
        c = self.claim(restore=True)
        os.link(r, c)                                    # died between link and unlink
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertTrue(r.exists())
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual([os.stat(p).st_ino for p in self.quarantined()], [os.stat(r).st_ino])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("second name", self.lines[0])

    def test_a_duplicate_is_never_unlinked_even_when_its_other_name_is_retaken(self):
        # The producer retakes the canonical name at the instant an unlink would
        # run: nothing is unlinked, so the reply keeps a name either way.
        r = self.result("NEWER")
        c = self.claim(restore=True, body="NEWER", nonce="ab000011")
        os.link(r, c)
        real = os.unlink

        def retake_then_unlink(path, *a, **kw):
            if Path(path) == c:
                tmp = self.results / "task-a.newest"
                tmp.write_text("NEWEST")
                os.replace(tmp, self.results / "task-a.txt")
            return real(path, *a, **kw)
        with unittest.mock.patch.object(os, "unlink", retake_then_unlink):
            disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual([p.read_text() for p in self.quarantined()], ["NEWER"], "the reply survives")
        self.assertEqual(disposal.find_claims(self.results), [])

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
        real = disposal.rename_noreplace

        def steal(src, dst, *a, **kw):
            if Path(src) == gone:
                gone.unlink()
                raise FileNotFoundError(2, "gone", str(src))
            return real(src, dst, *a, **kw)
        (results / "task-a.txt").unlink()
        with unittest.mock.patch.object(disposal, "rename_noreplace", steal):
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
        _park_in_quarantine(r, self.results)
        self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_a_failure_to_read_the_claim_is_reported_not_called_a_replacement(self):
        r = self.result()
        _, gen = identity_of(r)
        def emfile(fd, generation):
            raise OSError(24, "Too many open files")
        with unittest.mock.patch.object(disposal, "_verify_fd", emfile):
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
        # The name is retaken right before the put-back: the no-replace rename
        # refuses, nothing is unlinked, recovery keeps the claim as a superseded copy.
        r = self.result("NEWER")
        c = self.claim(restore=True, body="NEWER", nonce="ab000004")
        r.rename(c)
        real = disposal.rename_noreplace

        def producer_first(src, dst, *a, **kw):
            if Path(src) == c:
                tmp = self.results / "task-a.newest"
                tmp.write_text("NEWEST")
                os.replace(tmp, self.results / "task-a.txt")
            return real(src, dst, *a, **kw)
        with unittest.mock.patch.object(disposal, "rename_noreplace", producer_first):
            self.assertFalse(disposal.put_back(c, self.results / "task-a.txt", self.lines.append))
        self.assertEqual((self.results / "task-a.txt").read_text(), "NEWEST")
        self.assertEqual(c.read_text(), "NEWER", "the reply keeps its only link")
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        bodies = [p.read_text() for p in self.quarantined()]
        self.assertEqual(bodies, ["NEWER"], "the superseded reply must survive somewhere visible")
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("superseded", self.lines[0])

    def test_a_rewrite_of_the_claimed_inode_during_hashing_is_not_verified(self):
        # The inode is rewritten while it is hashed: equal bytes are not the
        # generation read, so the reply goes back live instead of into quarantine.
        r = self.result("answer")
        _, gen = identity_of(r)
        real_read = os.read
        rewritten = []

        def rewrite_mid_hash(fd, n):
            data = real_read(fd, n)
            if data and not rewritten:
                rewritten.append(True)
                with open(r if r.exists() else disposal.find_claims(self.results)[0], "r+b") as f:
                    f.seek(0); f.write(b"answer"); f.flush(); os.fsync(f.fileno())
                os.utime(disposal.find_claims(self.results)[0], ns=(gen.mtime_ns + 5_000, gen.mtime_ns + 5_000))
            return data
        with unittest.mock.patch.object(os, "read", rewrite_mid_hash):
            with self.assertRaises(disposal.GenerationReplaced):
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertTrue(rewritten)
        self.assertEqual(r.read_text(), "answer", "the rewritten reply stays live")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(self.lines, [], "caught before the move, so no undo was needed")

    def test_without_a_primitive_nothing_is_put_back_and_the_body_stays_visible(self):
        # No kernel no-replace rename: the put-back is refused outright, the
        # claim keeps the body, and recovery keeps it where the operator looks.
        r = self.result("NEWER")
        c = self.claim(restore=True, body="NEWER", nonce="ab00000a")
        r.rename(c)
        real, real_link = os.unlink, os.link

        def retake_then_unlink(path, *a, **kw):
            tmp = self.results / "task-a.newest"
            tmp.write_text("NEWEST")
            os.replace(tmp, self.results / "task-a.txt")
            return real(path, *a, **kw)

        def link_only_into_quarantine(src, dst, *a, **kw):
            # A link is only ever a no-clobber placement into undelivered/, never a put-back.
            if "undelivered" not in str(dst):
                raise AssertionError(f"links to {dst}")
            return real_link(src, dst, *a, **kw)
        with unittest.mock.patch.object(disposal.undelivered_quarantine, "_RENAME", None), \
                unittest.mock.patch.object(disposal.undelivered_quarantine, "RENAME_PRIMITIVE", "none"), \
                unittest.mock.patch.object(os, "unlink", retake_then_unlink), \
                unittest.mock.patch.object(os, "link", link_only_into_quarantine):
            self.assertFalse(disposal.put_back(c, self.results / "task-a.txt", self.lines.append))
            self.assertEqual(c.read_text(), "NEWER", "the claim keeps the body")
            self.assertFalse((self.results / "task-a.txt").exists())
            disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual([p.read_text() for p in self.quarantined()], ["NEWER"])
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.lines), 2, "\n".join(self.lines))
        self.assertIn("never put back", self.lines[0])
        self.assertIn("cannot put back", self.lines[1])
        self.assertNotIn("superseded", self.lines[1], "nothing retook the name; it was not superseded")

    def test_the_kernel_rename_refuses_a_taken_name_without_moving(self):
        if disposal.undelivered_quarantine.RENAME_PRIMITIVE == "link":
            self.skipTest("no kernel no-replace rename on this platform")
        a = self.result("A", stem="task-a")
        b = self.result("B", stem="task-b")
        with self.assertRaises(FileExistsError):
            disposal.rename_noreplace(a, b, self.lines.append)
        self.assertEqual((a.read_text(), b.read_text()), ("A", "B"))
        disposal.rename_noreplace(a, self.results / "task-c.txt", self.lines.append)
        self.assertFalse(a.exists())
        self.assertEqual((self.results / "task-c.txt").read_text(), "A")
        self.assertEqual(self.lines, [])

    def test_recovery_skips_a_missing_results_directory(self):
        missing = Path(self.tmp.name) / "never-created"
        for _ in range(3):
            disposal.recover_abandoned_claims(missing, self.lines.append)
        self.assertFalse(missing.exists())
        self.assertEqual(self.lines, [])

    def test_a_lock_the_filesystem_refuses_skips_recovery_and_says_so_once(self):
        # ENOLCK (NFS without lockd): recovery must not raise into the drain.
        self.result().rename(self.claim(nonce="ab00000b"))

        def no_lockd(fd, *, blocking=True):
            raise OSError(77, "No locks available")
        with unittest.mock.patch.object(disposal, "lock_fd", no_lockd):
            for _ in range(3):
                disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual(len(disposal.find_claims(self.results)), 1, "nothing moved without the lock")
        self.assertEqual(len(self.lines), 1)
        self.assertIn("recovery skipped", self.lines[0])

    def test_two_threads_serialize_on_the_lock(self):
        import threading
        order = []
        inside = threading.Event()
        release = threading.Event()

        def holder():
            with disposal.locked(self.results):
                order.append("A in"); inside.set(); release.wait(5); order.append("A out")

        def waiter():
            inside.wait(5)
            with disposal.locked(self.results):
                order.append("B in")
        ta, tb = threading.Thread(target=holder), threading.Thread(target=waiter)
        ta.start(); tb.start()
        inside.wait(5); time.sleep(0.1)
        self.assertEqual(order, ["A in"], "B must wait while A holds the lock")
        release.set(); ta.join(5); tb.join(5)
        self.assertEqual(order, ["A in", "A out", "B in"])

    def test_a_replaced_lock_file_is_not_trusted(self):
        # The lock is on the fd; if the file at the lock's name is not that fd,
        # a second locker could get in. locked() relocks the file now there.
        lock = self.results / disposal.LOCK_NAME
        real = disposal._try_lock
        swapped = []

        def swap_once(fd):
            ok = real(fd)
            if ok and not swapped:
                swapped.append(True)
                lock.unlink(); lock.write_text("")
            return ok
        with unittest.mock.patch.object(disposal, "_try_lock", swap_once):
            with disposal.locked(self.results):
                st = os.stat(lock)
                probe = subprocess.run([sys.executable, "-c", (
                    "import fcntl,os\n"
                    f"fd=os.open({str(lock)!r}, os.O_RDWR)\n"
                    "try:\n fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print('free')\n"
                    "except BlockingIOError: print('held')")], capture_output=True, text=True)
                self.assertEqual(probe.stdout.strip(), "held", "the file now at the lock's name must be the one held")
        self.assertEqual(len(swapped), 1)

    def test_the_lock_refuses_a_symlink_or_a_directory(self):
        lock = self.results / disposal.LOCK_NAME
        lock.mkdir()
        with self.assertRaises(OSError):
            with disposal.locked(self.results):
                pass
        lock.rmdir()
        target = self.results / "elsewhere"; target.write_text("")
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            with disposal.locked(self.results):
                pass

    def test_the_module_loads_without_fcntl(self):
        # The published package is OS-independent: the lock goes through
        # file_lock's abstraction, never a bare fcntl import.
        code = "\n".join([
            "import sys, types, subprocess, ctypes.util   # stdlib first: it must not be fooled",
            "sys.modules['fcntl'] = None",
            "m = types.ModuleType('msvcrt'); m.LK_NBLCK = 2; m.LK_UNLCK = 0",
            "m.locking = lambda fd, mode, n: None",
            "sys.modules['msvcrt'] = m",
            f"sys.path.insert(0, {str(REPO / 'src')!r})",
            "from delivery import disposal",
            "import file_lock",
            "assert file_lock.fcntl is None",
            "print('loaded', disposal.LOCK_NAME)"])
        probe = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr[-800:])
        self.assertIn("loaded", probe.stdout)


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
        _park_in_quarantine(r, self.results)
        self.result().rename(self.claim(nonce="ab000007"))
        real = disposal.identity_of

        def emfile(path):
            raise OSError(24, "Too many open files")
        with unittest.mock.patch.object(disposal, "identity_of", emfile):
            self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))
        with unittest.mock.patch.object(disposal, "identity_of", real):
            self.assertTrue(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_keeping_a_duplicate_that_already_vanished_is_quiet(self):
        c = self.claim(nonce="ab000008")
        disposal._keep_duplicate(c, self.lines.append, "task-a", self.results)
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

    # ── round 8: every branch the gate flagged, executed on the main thread ──

    def fake_rename(self, err):
        # A kernel primitive that fails with `err`, the way ctypes reports it.
        def prim(_a, _b):
            ctypes.set_errno(err)
            return -1
        return prim

    def test_a_filesystem_without_the_primitive_refuses_every_put_back_from_then_on(self):
        a = self.result("A")
        for err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            with unittest.mock.patch.object(disposal.undelivered_quarantine, "_RENAME", self.fake_rename(err)), \
                    unittest.mock.patch.object(disposal.undelivered_quarantine, "RENAME_PRIMITIVE", "probe"):
                dst = self.results / f"task-{err}.txt"
                with self.assertRaises(FileExistsError):
                    disposal.rename_noreplace(a, dst, self.lines.append)
                self.assertEqual((disposal.undelivered_quarantine.RENAME_PRIMITIVE, disposal.undelivered_quarantine._RENAME), ("none", None))
            self.assertFalse(dst.exists())
            self.assertEqual(a.read_text(), "A", "nothing moved")
        self.assertEqual(len(self.lines), 1, "the refusal is reported once per process")
        self.assertIn("no no-replace rename", self.lines[0])

    def test_any_other_kernel_error_raises_and_moves_nothing(self):
        a = self.result("A")
        with unittest.mock.patch.object(disposal.undelivered_quarantine, "_RENAME", self.fake_rename(errno.EACCES)), \
                unittest.mock.patch.object(disposal.undelivered_quarantine, "RENAME_PRIMITIVE", "probe"):
            with self.assertRaises(OSError) as cm:
                disposal.rename_noreplace(a, self.results / "task-b.txt", self.lines.append)
            self.assertEqual(cm.exception.errno, errno.EACCES)
            self.assertEqual(disposal.undelivered_quarantine.RENAME_PRIMITIVE, "probe", "an unexplained error is not a missing primitive")
        self.assertEqual(a.read_text(), "A")
        self.assertFalse((self.results / "task-b.txt").exists())
        self.assertEqual(self.lines, [])

    def test_the_probe_names_no_primitive_where_none_is_known(self):
        with unittest.mock.patch.object(sys, "platform", "freebsd14"):
            self.assertEqual(disposal.undelivered_quarantine._probe_rename(), ("none", None))

    def test_without_a_primitive_a_rewritten_reply_stays_in_its_claim_for_recovery(self):
        # The undo of a post-verification rewrite needs a put-back; without the
        # primitive the body goes back into the private claim, never quarantine.
        r = self.result("answer")
        _, gen = identity_of(r)
        real_move = disposal._move_into_quarantine

        def rewrite_before_the_move(src, dst, log):
            if ".disposing-" in str(src):
                with open(src, "r+b") as fh:
                    fh.seek(0); fh.write(b"ANSWER")
                os.utime(src, ns=(gen.mtime_ns, gen.mtime_ns))
            return real_move(src, dst, log)
        with unittest.mock.patch.object(disposal.undelivered_quarantine, "_RENAME", None), \
                unittest.mock.patch.object(disposal.undelivered_quarantine, "RENAME_PRIMITIVE", "none"), \
                unittest.mock.patch.object(disposal, "_move_into_quarantine", rewrite_before_the_move):
            with self.assertRaises(disposal.GenerationReplaced):
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(self.quarantined(), [])
        self.assertEqual([c.read_text() for c in disposal.find_claims(self.results)], ["ANSWER"])
        self.assertTrue(any("kept in its claim" in l for l in self.lines), "\n".join(self.lines))

    def test_the_lock_refuses_a_fifo(self):
        # A FIFO opens read-write without blocking, so only the regular-file check stops it.
        lock = self.results / disposal.LOCK_NAME
        os.mkfifo(lock)
        with self.assertRaises(OSError) as cm:
            with disposal.locked(self.results):
                pass
        self.assertEqual(cm.exception.errno, errno.EINVAL)

    def test_a_busy_errno_from_the_lock_call_reads_as_held(self):
        fd = os.open(self.results / disposal.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        for err in sorted(disposal._BUSY_ERRNOS):
            with unittest.mock.patch.object(disposal, "lock_fd",
                                            lambda _fd, *, blocking=True, e=err: (_ for _ in ()).throw(OSError(e, "busy"))):
                self.assertFalse(disposal._try_lock(fd))
        with unittest.mock.patch.object(disposal, "lock_fd",
                                        lambda _fd, *, blocking=True: (_ for _ in ()).throw(OSError(errno.EIO, "io"))):
            with self.assertRaises(OSError):
                disposal._try_lock(fd)

    def test_a_lock_file_removed_on_every_attempt_is_given_up_after_eight(self):
        # The re-check after flock sees no file at the lock's name each time;
        # locked() retries a bounded number of times, then reports ESTALE.
        lock = self.results / disposal.LOCK_NAME
        real = disposal._try_lock
        seen = []

        def unlink_after_lock(fd):
            ok = real(fd)
            if ok:
                seen.append(1)
                lock.unlink()
            return ok
        with unittest.mock.patch.object(disposal, "_try_lock", unlink_after_lock):
            with self.assertRaises(OSError) as cm:
                with disposal.locked(self.results):
                    self.fail("never locked")
        self.assertEqual(cm.exception.errno, errno.ESTALE)
        self.assertEqual(len(seen), 8)

    def pin_quarantine_clock(self, when=7):
        # Every move into undelivered/ chooses its name from the clock; pinning
        # it makes the first choice collide with an earlier copy on purpose.
        return unittest.mock.patch.object(undelivered_quarantine.time, "time_ns", lambda: when)

    def prior_evidence(self, stem="task-a", when=7):
        d = self.results / undelivered_quarantine.DIRNAME
        d.mkdir(exist_ok=True)
        prior = d / undelivered_quarantine.quarantine_name(stem, when)
        prior.write_text("PRIOR-EVIDENCE")
        return prior

    def test_terminal_quarantine_never_overwrites_an_earlier_copy(self):
        prior = self.prior_evidence()
        r = self.result("answer")
        _, gen = identity_of(r)
        with self.pin_quarantine_clock():
            target = disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(prior.read_text(), "PRIOR-EVIDENCE", "earlier evidence was overwritten")
        self.assertNotEqual(target, prior)
        self.assertEqual(sorted(p.read_text() for p in self.quarantined()), ["PRIOR-EVIDENCE", "answer"])

    def test_recovery_never_overwrites_an_earlier_copy(self):
        prior = self.prior_evidence()
        r = self.result("answer")
        _, gen = identity_of(r)
        c = self.claim(nonce="ab000020")                  # names the very publication it holds
        r.rename(c)
        with self.pin_quarantine_clock():
            disposal.recover_claim(self.results, c, self.lines.append)
        self.assertEqual(prior.read_text(), "PRIOR-EVIDENCE")
        self.assertEqual(sorted(p.read_text() for p in self.quarantined()), ["PRIOR-EVIDENCE", "answer"])
        self.assertEqual(disposal.find_claims(self.results), [])

    def test_a_kept_duplicate_never_overwrites_an_earlier_copy(self):
        prior = self.prior_evidence()
        c = self.claim(nonce="ab000021")
        self.result("DUPLICATE-BODY").rename(c)
        with self.pin_quarantine_clock():
            disposal._keep_duplicate(c, self.lines.append, "task-a", self.results)
        self.assertEqual(prior.read_text(), "PRIOR-EVIDENCE")
        self.assertEqual(sorted(p.read_text() for p in self.quarantined()), ["DUPLICATE-BODY", "PRIOR-EVIDENCE"])

    def test_without_a_primitive_quarantine_still_never_overwrites(self):
        prior = self.prior_evidence()
        r = self.result("answer")
        _, gen = identity_of(r)
        with self.pin_quarantine_clock(), \
                unittest.mock.patch.object(undelivered_quarantine, "_RENAME", None), \
                unittest.mock.patch.object(undelivered_quarantine, "RENAME_PRIMITIVE", "none"):
            disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(prior.read_text(), "PRIOR-EVIDENCE")
        self.assertEqual(sorted(p.read_text() for p in self.quarantined()), ["PRIOR-EVIDENCE", "answer"])
        self.assertEqual(disposal.find_claims(self.results), [])

    def test_place_gives_up_rather_than_overwrite_when_no_name_is_free(self):
        d = self.results / undelivered_quarantine.DIRNAME
        d.mkdir(exist_ok=True)
        for n in range(7, 7 + undelivered_quarantine._PLACE_TRIES):
            (d / undelivered_quarantine.quarantine_name("task-a", n)).write_text(f"P{n}")
        src = self.result("answer")
        with self.assertRaises(FileExistsError):
            undelivered_quarantine.place(src, self.results, "task-a", when=7)
        self.assertEqual(src.read_text(), "answer", "nothing moved")

    def test_place_passes_through_a_refusal_that_is_not_a_taken_name(self):
        def cannot(_s, _d):
            raise FileExistsError(errno.ENOTSUP, "no primitive")
        with self.assertRaises(FileExistsError) as cm:
            undelivered_quarantine.place(self.result("answer"), self.results, "task-a", move=cannot)
        self.assertEqual(cm.exception.errno, errno.ENOTSUP)

    def test_a_duplicate_name_is_kept_where_the_operator_looks(self):
        c = self.claim(nonce="ab00000c")
        self.result("only copy").rename(c)
        disposal._keep_duplicate(c, self.lines.append, "task-a", self.results)
        self.assertFalse(c.exists())
        self.assertEqual([p.read_text() for p in self.results.glob("undelivered/task-a-*.txt")], ["only copy"])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("kept as", self.lines[0])

    def rewrite_at_the_final_move(self, gen, new=b"ANSWER"):
        # The producer rewrites the claimed inode in place between the
        # verification and the final rename, same size, original mtime restored.
        real_move = disposal._move_into_quarantine

        def rewrite_then_move(src, dst, log):
            if ".disposing-" in str(src):
                with open(src, "r+b") as fh:
                    fh.seek(0); fh.write(new)
                os.utime(src, ns=(gen.mtime_ns, gen.mtime_ns))
            return real_move(src, dst, log)
        return unittest.mock.patch.object(disposal, "_move_into_quarantine", rewrite_then_move)

    def test_a_rewrite_after_verification_goes_back_live_not_into_quarantine(self):
        r = self.result("answer")
        _, gen = identity_of(r)
        with self.rewrite_at_the_final_move(gen):
            with self.assertRaises(disposal.GenerationReplaced):
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(r.read_text(), "ANSWER", "the rewritten reply is live, not quarantined")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("rewritten after verification", self.lines[0])

    def test_a_rewrite_after_verification_with_the_name_retaken_waits_in_its_claim(self):
        r = self.result("answer")
        _, gen = identity_of(r)
        real = disposal.rename_noreplace

        def producer_first(src, dst, *a, **kw):
            if "undelivered" in str(src):                 # the undo's put-back finds the name taken
                tmp = self.results / "task-a.newest"
                tmp.write_text("NEWEST")
                os.replace(tmp, self.results / "task-a.txt")
            return real(src, dst, *a, **kw)
        with self.rewrite_at_the_final_move(gen), \
                unittest.mock.patch.object(disposal, "rename_noreplace", producer_first):
            with self.assertRaises(disposal.GenerationReplaced):
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(r.read_text(), "NEWEST")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual([c.read_text() for c in disposal.find_claims(self.results)], ["ANSWER"])
        self.assertIn("kept in its claim", self.lines[-1])
        # The next pass judges it again: its name is taken, so it is kept visible.
        disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual([p.read_text() for p in self.quarantined()], ["ANSWER"])
        self.assertEqual(disposal.find_claims(self.results), [])

    def test_recovery_re_verifies_the_body_after_its_own_move(self):
        # A dead owner's claim is verified, then rewritten in place at the
        # final rename: recovery puts the changed reply back live, not quarantine.
        r = self.result("answer")
        _, gen = identity_of(r)
        c = self.claim(nonce="ab000012")                  # names the very publication it holds
        r.rename(c)
        with self.rewrite_at_the_final_move(gen):
            self.assertEqual(disposal.recover_claim(self.results, c, self.lines.append), r)
        self.assertEqual(r.read_text(), "ANSWER")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("rewritten after verification", self.lines[0])

    def test_a_move_that_cannot_be_re_verified_stays_where_it_went_and_says_so(self):
        r = self.result("answer")
        _, gen = identity_of(r)
        calls = []
        real = disposal._verify_fd

        def emfile_after_the_move(fd, generation):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(errno.EMFILE, "Too many open files")
            return real(fd, generation)
        with unittest.mock.patch.object(disposal, "_verify_fd", emfile_after_the_move):
            target = disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(target.read_text(), "answer")
        self.assertEqual(len(self.lines), 1)
        self.assertIn("could not re-verify", self.lines[0])

    def test_a_precheck_error_skips_the_pass_and_is_said_once(self):
        real = Path.is_dir

        def eio(self_):
            if self_ == results:
                raise OSError(errno.EIO, "Input/output error")
            return real(self_)
        results = self.results
        with unittest.mock.patch.object(Path, "is_dir", eio):
            for _ in range(3):
                disposal.recover_abandoned_claims(self.results, self.lines.append)
        self.assertEqual(len(self.lines), 1, "\n".join(self.lines))
        self.assertIn("recovery skipped this pass", self.lines[0])

    def test_an_undo_whose_moved_file_was_taken_elsewhere_is_quiet(self):
        # The name is retaken after the move and the quarantined copy is taken
        # by someone else before the undo: nothing of ours is left, nothing said.
        r = self.result("answer")
        _, gen = identity_of(r)
        real = os.rename
        real_move = disposal._move_into_quarantine

        def rewrite_retake_then_move(src, dst, log):
            with open(src, "r+b") as fh:
                fh.seek(0); fh.write(b"ANSWER")
            os.utime(src, ns=(gen.mtime_ns, gen.mtime_ns))
            real_move(src, dst, log)
            tmp = self.results / "task-a.newest"
            tmp.write_text("NEWEST")
            os.replace(tmp, self.results / "task-a.txt")

        def lose(src, dst, *a, **kw):
            if "undelivered" in str(src) and ".disposing-" in str(dst):
                raise FileNotFoundError(errno.ENOENT, "taken elsewhere", str(src))
            return real(src, dst, *a, **kw)
        with unittest.mock.patch.object(disposal, "_move_into_quarantine", rewrite_retake_then_move), \
                unittest.mock.patch.object(os, "rename", lose):
            with self.assertRaises(disposal.GenerationReplaced):
                disposal.quarantine_generation(self.results, r, gen, self.lines.append)
        self.assertEqual(r.read_text(), "NEWEST")
        self.assertEqual(disposal.find_claims(self.results), [])
        self.assertEqual(self.lines, [])

    def test_a_claim_recovery_cannot_read_is_left_alone(self):
        c = self.claim(nonce="ab000014")
        self.result("answer").rename(c)

        def emfile(fd, generation):
            raise OSError(errno.EMFILE, "Too many open files")
        with unittest.mock.patch.object(disposal, "_verify_fd", emfile):
            self.assertIsNone(disposal.recover_claim(self.results, c, self.lines.append))
        self.assertEqual(c.read_text(), "answer", "untouched until it can be read")
        self.assertEqual(self.lines, [])

    def test_a_claim_that_is_not_a_regular_file_is_never_moved(self):
        c = self.claim(nonce="ab000013")
        c.mkdir()
        self.assertIsNone(disposal.recover_claim(self.results, c, self.lines.append))
        self.assertTrue(c.is_dir())
        self.assertFalse((self.results / "task-a.txt").exists())

    def test_a_claim_that_cannot_be_matched_proves_nothing_to_the_loser(self):
        # An EMFILE while matching a claim is skipped (the copies before it did not match).
        r = self.result("answer")
        _, gen = identity_of(r)
        self.result("other", stem="task-a").rename(self.claim(nonce="ab00000d", body="other"))
        _park_in_quarantine(self.result("third"), self.results)
        real = disposal._is_generation

        def emfile_on_claims(path, generation):
            if ".disposing-" in str(path):
                raise OSError(errno.EMFILE, "Too many open files")
            return real(path, generation)
        with unittest.mock.patch.object(disposal, "_is_generation", emfile_on_claims):
            self.assertFalse(disposal.disposed_copy_exists(self.results, "task-a", gen, self.lines.append))

    def test_equal_bytes_under_the_claims_named_identity_are_restored_not_quarantined(self):
        # The claim names publication A (inode, write time, digest); it holds B,
        # a distinct publication with A's bytes. Identity, not bytes, decides.
        a = self.result("same bytes")
        _, gen_a = identity_of(a)
        a.unlink()
        b = self.result("same bytes")
        os.utime(b, ns=(gen_a.mtime_ns + 5_000_000, gen_a.mtime_ns + 5_000_000))
        c = self.claim(nonce="ab00000e", body="same bytes", ino=gen_a.ino, mtime=gen_a.mtime_ns)
        b.rename(c)
        self.assertEqual(disposal.recover_claim(self.results, c, self.lines.append), self.results / "task-a.txt")
        self.assertEqual((self.results / "task-a.txt").read_text(), "same bytes")
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("never verified", self.lines[0])


class RetirementOutcome(unittest.TestCase):
    """`retire_generation` names how it ended; only PLACED and SOURCE_GONE are
    the requested disposition."""

    setUp = CoreContract.setUp
    result = CoreContract.result
    quarantined = CoreContract.quarantined

    def retire(self, r, gen, directory=None):
        directory = directory or self.results / "archive"
        return disposal.retire_generation(self.results, r, gen, self.lines.append,
                                          directory, ["task-a-x.txt", "task-a-y.txt"])

    def publish(self, r, body):
        tmp = r.with_name(".producer.tmp")
        tmp.write_text(body)
        os.replace(tmp, r)

    def test_placed(self):
        r = self.result()
        _, gen = identity_of(r)
        done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.PLACED)
        self.assertEqual(done.path, self.results / "archive" / "task-a-x.txt")
        self.assertTrue(done.retired)

    def test_source_gone(self):
        r = self.result()
        _, gen = identity_of(r)
        r.unlink()
        done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.SOURCE_GONE)
        self.assertTrue(done.retired)

    def test_a_missing_destination_is_failed_not_source_gone(self):
        r = self.result()
        _, gen = identity_of(r)
        with unittest.mock.patch.object(disposal, "_move_into_quarantine",
                                        side_effect=FileNotFoundError(errno.ENOENT, "no such dir")):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.FAILED)
        self.assertFalse(done.retired)
        self.assertEqual(r.read_text(), "answer", "the body goes back live")

    def test_a_reply_published_after_the_move_is_replacement_live(self):
        r = self.result()
        _, gen = identity_of(r)
        real = disposal._move_into_quarantine

        def move_then_publish(src, dst, log):
            real(src, dst, log)
            self.publish(r, "BODY-B")
        with unittest.mock.patch.object(disposal, "_move_into_quarantine", move_then_publish):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.REPLACEMENT_LIVE)
        self.assertFalse(done.retired)
        self.assertEqual(r.read_text(), "BODY-B")
        self.assertEqual(done.path.read_text(), "answer")

    def test_a_replacement_before_the_claim_is_replacement_live(self):
        r = self.result()
        _, gen = identity_of(r)
        self.publish(r, "BODY-B")
        done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.REPLACEMENT_LIVE)
        self.assertEqual(r.read_text(), "BODY-B")

    def test_a_busy_lock_is_failed_and_does_not_raise(self):
        import contextlib

        @contextlib.contextmanager
        def busy(_results_dir):
            raise disposal.DisposalBusy(errno.EAGAIN, "held")
            yield
        r = self.result()
        _, gen = identity_of(r)
        with unittest.mock.patch.object(disposal, "locked", busy):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.FAILED)
        self.assertIn("busy", done.cause)
        self.assertEqual(r.read_text(), "answer")

    def test_a_lock_error_that_is_not_busy_is_failed_and_does_not_raise(self):
        for name, err in (("lock_fd", OSError(errno.ENOLCK, "No locks available")),
                          ("_open_lock", PermissionError(errno.EACCES, "Permission denied"))):
            with self.subTest(at=name):
                r = self.result()
                _, gen = identity_of(r)
                with unittest.mock.patch.object(disposal, name, side_effect=err):
                    done = self.retire(r, gen)
                self.assertIs(done.outcome, disposal.Retirement.FAILED)
                self.assertIn(err.strerror, done.cause)
                self.assertEqual(r.read_text(), "answer")

    def test_an_unlock_error_after_the_move_keeps_the_real_outcome(self):
        r = self.result()
        _, gen = identity_of(r)
        with unittest.mock.patch.object(disposal, "unlock_fd",
                                        side_effect=OSError(errno.EIO, "unlock failed")):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.PLACED)
        self.assertEqual(done.path, self.results / "archive" / "task-a-x.txt")
        self.assertEqual(done.path.read_text(), "answer")
        self.assertTrue(done.retired)
        self.assertIn("unlock failed", done.warning)
        self.assertTrue(any("unlock failed" in l for l in self.lines), self.lines)

    def test_a_placement_that_falls_back_is_fallback_with_its_real_destination(self):
        r = self.result()
        _, gen = identity_of(r)
        real = disposal._move_into_quarantine

        def refuse_archive(src, dst, log):
            if Path(dst).parent.name == "archive":
                self.publish(r, "BODY-B")
                raise OSError(errno.EIO, "EIO")
            return real(src, dst, log)
        with unittest.mock.patch.object(disposal, "_move_into_quarantine", refuse_archive):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.FALLBACK)
        self.assertFalse(done.retired)
        self.assertEqual(done.path.parent.name, "undelivered")
        self.assertEqual(done.path.read_text(), "answer")
        self.assertIn(done.path.name, done.cause)
        self.assertEqual(r.read_text(), "BODY-B")

    def test_a_newer_reply_kept_quarantined_without_a_primitive_is_failed(self):
        r = self.result()
        _, gen = identity_of(r)
        self.publish(r, "BODY-B")
        with unittest.mock.patch.object(disposal.undelivered_quarantine, "_RENAME", None), \
                unittest.mock.patch.object(disposal.undelivered_quarantine, "RENAME_PRIMITIVE", "none"):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.FAILED)
        self.assertIn("newer reply", done.cause)
        self.assertEqual([p.read_text() for p in self.quarantined()], ["BODY-B"])

    def test_a_placement_with_nothing_kept_anywhere_is_failed(self):
        r = self.result()
        _, gen = identity_of(r)

        def refuse(src, dst, log):
            self.publish(r, "BODY-B")
            raise OSError(errno.EIO, "EIO")
        with unittest.mock.patch.object(disposal, "_move_into_quarantine", refuse), \
                unittest.mock.patch.object(disposal, "_recover_claim", return_value=None):
            done = self.retire(r, gen)
        self.assertIs(done.outcome, disposal.Retirement.FAILED)
        self.assertIn("no copy was kept", done.cause)


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
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in ("disposal", "team_result_guard"):
                out.add(n.func.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                out.add(n.func.id)
        return out

    def test_every_transition_is_delegated(self):
        self.assertIn("quarantine_generation", self.calls_in("_quarantine_undelivered"))
        self.assertIn("_disposed_copy_exists", self.calls_in("_quarantine_undelivered"))
        self.assertIn("disposed_copy_exists", self.calls_in("_disposed_copy_exists"))
        self.assertIn("recover_abandoned_claims", self.calls_in("_recover_disposing_claims"))
        self.assertIn("_recover_disposing_claims", self.calls_in("_post_ready_results"))
        self.assertIn("_recover_disposing_claims", self.calls_in("_reconcile_orphan_results"))
        self.assertIn("retire_generation", self.calls_in("_retire_orphan"))
        self.assertIn("_retire_orphan", self.calls_in("_quarantine_orphan"))
        self.assertIn("_retire_orphan", self.calls_in("_reconcile_orphan_results"))

    def test_an_unsent_reply_at_a_delivered_id_is_ruled_by_the_outbox_owner(self):
        for fn in ("_deliver_result_payload", "_reconcile_orphan_results"):
            self.assertIn("delivered_body_differs", self.calls_in(fn), fn)
            self.assertIn("_quarantine_unsent", self.calls_in(fn), fn)
        f = next(n for n in ast.walk(self.tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_deliver_result_payload")
        rulings = [n for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "delivered_body_differs"]
        self.assertEqual(len(rulings), 2, "the confirmed arm and the terminal-delivered arm")
        self.assertNotIn("_record_sent_this_body", self.names())

    def test_guard_records_have_one_writer_contract(self):
        """Every write and archive of an owner-review record goes through the guard's
        ledger-locked owner; the bridge never writes or moves one itself."""
        self.assertIn("archive_record", self.calls_in("_archive_resolved_review"))
        for fn in ("_route_withheld_review", "_publish_review", "_resolve_review_card",
                   "_handle_review_decision"):
            calls = self.calls_in(fn)
            self.assertIn("update_record", calls, fn)
            self.assertNotIn("_atomic_private_json", calls, fn)
        f = next(n for n in ast.walk(self.tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_archive_resolved_review")
        moves = [n.func.attr for n in ast.walk(f) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr in ("replace", "rename")]
        self.assertEqual(moves, [], "the bridge moves a review record itself")

    def test_a_reply_ruled_unsent_goes_through_the_delivery_guards(self):
        calls = self.calls_in("_quarantine_unsent")
        for guard in ("_owner_mention_disposition", "_guarded_result_body", "parse_markers"):
            self.assertIn(guard, calls, guard)

    # Every function of the bridge that still moves or removes a file, as of
    # this head; a new one is a private disposal path until proven otherwise.
    MOVERS = {"_atomic_private_json", "_backup_tier_map_to_disk", "_emit_gateway_status",
              "_publish_staged", "_save_dedup_aliases", "_save_task_rooms",
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
        links = [f"{c.lineno}" for c in ast.walk(self.tree) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute) and c.func.attr in ("link", "link_to", "hardlink_to")]
        self.assertEqual(links, [], "a hard link in the bridge is a private move into undelivered/")
        for literal in (".disposing-", "disposing-*", ".restore"):
            self.assertNotIn(literal, self.src, "the bridge must not know the claim namespace")

    def test_the_vendored_copy_is_the_canonical_module(self):
        pkg = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_disposal.py"
        self.assertEqual(pkg.read_text(), (REPO / "src" / "delivery" / "disposal.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
