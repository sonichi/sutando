#!/usr/bin/env python3
"""The pin-snapshot merge is one locked transaction, and unusable `ps` is unknown.

Three verified defects, each with its own control here:

  live_lstart_by_pid  returned {} when `ps` exited 0 with no parseable row,
                      and {} is "no process is live" — every older pin dropped.
                      Unknown must be None, which keeps them (documented policy).
  merge CLI           loaded both snapshots, probed, then save_pins() OUTSIDE
                      the record lock arm_pin()/release_pin() serialize on, so
                      an arm landing mid-merge was overwritten by the merge.
  provenance          the merged destination carried the migration's write
                      time, so in a C -> A -> B walk the first union out-dated
                      every remaining legacy snapshot; and an exact mtime tie
                      with different bytes was broken by scan order.

Run: python3 tests/process-pins-merge-lock.test.py
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import process_pins as pp  # noqa: E402

FUTURE = "2099-01-01T00:00:00Z"


def _pin(service, pid, lstart, reason, exp=FUTURE):
    return {"service": service, "pid": str(pid), "lstart": lstart, "reason": reason, "expires_at": exp}


def _ps_shim(dir_: Path, body: str) -> dict:
    """A PATH-bound `ps` that answers with `body` (a shell snippet)."""
    dir_.mkdir(parents=True, exist_ok=True)
    sh = dir_ / "ps"
    sh.write_text("#!/bin/sh\n" + body + "\n")
    sh.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{dir_}:{env.get('PATH', '')}"
    return env


class LivenessProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pins-live-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe(self, body):
        env = _ps_shim(self.tmp / "bin", body)
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, %r); import process_pins as p; "
                            "print(repr(p.live_lstart_by_pid()))" % str(REPO / "src")],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_valid_row_is_a_table(self):
        self.assertEqual(self._probe("printf '222 Sun Aug 24 09:11:02 2026\\n'"),
                         "{'222': 'Sun Aug 24 09:11:02 2026'}")

    def test_fatal_exit_is_unknown(self):
        self.assertEqual(self._probe("exit 3"), "None")

    def test_rc0_garbage_is_unknown_not_empty(self):
        self.assertEqual(self._probe("printf 'ps: cannot enumerate\\n'; exit 0"), "None")

    def test_rc0_empty_is_unknown(self):
        self.assertEqual(self._probe("exit 0"), "None")

    def test_partially_unusable_is_unknown(self):
        self.assertEqual(self._probe("printf '222 Sun Aug 24 09:11:02 2026\\ngarbage\\n'"), "None")


def _arm_during(path, service, pid, lstart, delay):
    time.sleep(delay)
    pp.arm_pin(path, service, pid, lstart, "armed-during-merge", FUTURE)


class MergeTransaction(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pins-merge-"))
        self.dst = self.tmp / "dest" / "process-pins.json"
        self.src = self.tmp / "src" / "process-pins.json"
        me = os.getpid()
        self.my_lstart = subprocess.run(["ps", "-o", "lstart=", "-p", str(me)],
                                        capture_output=True, text=True).stdout.strip()
        self.me = me

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, pins, mtime_ns):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pins": pins}, indent=1))
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def _merge(self, env=None, expect_sha=None):
        argv = [sys.executable, str(REPO / "src" / "process_pins.py"), "merge",
                "--into", str(self.dst), "--newer", str(self.src), "--older", str(self.dst)]
        if expect_sha:
            argv += ["--expect-dst-sha256", expect_sha]
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)

    def test_arm_during_merge_survives(self):
        """The reviewer's race: liveness probe paused after the loads, an arm
        lands, the merge commits. Under the lock the arm waits and persists."""
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-before")], base)
        self._write(self.src, [_pin("discord-bridge", 1, "Thu Jan  1 00:00:00 2026", "older-src-dead")], base + 10 ** 9)
        env = _ps_shim(self.tmp / "bin", "sleep 2; printf '%s %s\\n'" % (self.me, self.my_lstart.replace("'", "'\\''")))
        proc = multiprocessing.Process(target=_arm_during,
                                       args=(str(self.dst), "telegram-bridge", str(self.me), self.my_lstart, 0.7))
        proc.start()
        r = self._merge(env=env)
        proc.join(30)
        self.assertEqual(r.returncode, 0, r.stderr)
        reasons = sorted(p["reason"] for p in pp.load_pins(self.dst))
        self.assertIn("armed-during-merge", reasons, f"a concurrent arm was overwritten: {reasons}")
        self.assertIn("dest-before", reasons)

    def test_provenance_is_the_inputs_newest_mtime(self):
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "old")], base)
        self._write(self.src, [_pin("telegram-bridge", self.me, self.my_lstart, "newer")], base + 5 * 10 ** 9)
        r = self._merge()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kept=1", r.stdout)
        self.assertEqual(self.dst.stat().st_mtime_ns, base + 5 * 10 ** 9,
                         "the merged destination must carry the inputs' newest mtime, not now")

    def test_exact_tie_with_different_bytes_is_refused(self):
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-copy", "2027-01-01T00:00:00Z")], base)
        self._write(self.src, [_pin("discord-bridge", self.me, self.my_lstart, "src-copy", FUTURE)], base)
        before = self.dst.read_bytes()
        r = self._merge()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.dst.read_bytes(), before, "a refused merge must not touch the destination")

    def test_moved_destination_outranks_the_callers_ordering(self):
        """The caller measured dst before a concurrent write; the token no longer
        matches, so dst is taken whole and the src becomes the older side."""
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-old")], base)
        token = pp._sha256(self.dst)
        self._write(self.src, [_pin("discord-bridge", self.me, self.my_lstart, "src-version")], base + 10 ** 9)
        # dst moves after the caller's measurement: same identity, new content
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-moved")], base + 2 * 10 ** 9)
        r = self._merge(expect_sha=token)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([p["reason"] for p in pp.load_pins(self.dst)], ["dest-moved"])

    def test_malformed_older_refuses_without_writing(self):
        base = time.time_ns() - 10 ** 12
        self.dst.parent.mkdir(parents=True)
        self.dst.write_text('{"pins": "not-a-list"}'); os.utime(self.dst, ns=(base, base))
        self._write(self.src, [_pin("telegram-bridge", self.me, self.my_lstart, "newer")], base + 10 ** 9)
        r = self._merge()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.dst.read_text(), '{"pins": "not-a-list"}')


class InProcess(unittest.TestCase):
    """The same contracts driven in-process, so the coverage run can see them:
    a subprocess-only test proves behaviour but measures nothing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pins-inproc-"))
        self.dst = self.tmp / "dest" / "process-pins.json"
        self.src = self.tmp / "src" / "process-pins.json"
        self.me = os.getpid()
        self.my_lstart = subprocess.run(["ps", "-o", "lstart=", "-p", str(self.me)],
                                        capture_output=True, text=True).stdout.strip()
        self._path = os.environ.get("PATH", "")

    def tearDown(self):
        os.environ["PATH"] = self._path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _shim(self, body):
        _ps_shim(self.tmp / "bin", body)
        os.environ["PATH"] = f"{self.tmp / 'bin'}:{self._path}"

    def _write(self, path, pins, mtime_ns):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pins": pins}, indent=1))
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def test_liveness_probe_rows(self):
        self._shim("printf '222 Sun Aug 24 09:11:02 2026\\n\\n'")   # trailing blank line tolerated
        self.assertEqual(pp.live_lstart_by_pid(), {"222": "Sun Aug 24 09:11:02 2026"})
        self._shim("exit 3"); self.assertIsNone(pp.live_lstart_by_pid())
        self._shim("printf 'ps: cannot enumerate\\n'; exit 0"); self.assertIsNone(pp.live_lstart_by_pid())
        self._shim("exit 0"); self.assertIsNone(pp.live_lstart_by_pid())
        self._shim("printf '222 Sun Aug 24 09:11:02 2026\\ngarbage\\n'"); self.assertIsNone(pp.live_lstart_by_pid())
        os.environ["PATH"] = str(self.tmp / "nowhere")   # no ps at all
        self.assertIsNone(pp.live_lstart_by_pid())

    def test_merge_snapshots_branches(self):
        live = {"1": "l1", "2": "l2"}
        newer = [_pin("a", 1, "l1", "n")]
        older = [_pin("a", 1, "l1", "same-identity"), _pin("b", 2, "l2", "kept-live"),
                 _pin("c", 3, "l3", "dead"), _pin("d", 2, "wrong", "lstart-mismatch"),
                 _pin("e", 9, "l9", "expired", "2000-01-01T00:00:00Z")]
        merged, kept, dropped = pp.merge_snapshots(newer, older, live, time.time())
        self.assertEqual([p["reason"] for p in kept], ["kept-live"])
        self.assertEqual(sorted(p["reason"] for p in dropped), ["dead", "expired", "lstart-mismatch"])
        self.assertEqual(len(merged), 2)
        # unknown liveness keeps every unexpired older-only pin
        m2, k2, d2 = pp.merge_snapshots(newer, older, None, time.time())
        self.assertEqual(sorted(p["reason"] for p in k2), ["dead", "kept-live", "lstart-mismatch"])

    def test_merge_into_union_and_provenance(self):
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "old")], base)
        self._write(self.src, [_pin("telegram-bridge", self.me, self.my_lstart, "newer")], base + 5 * 10 ** 9)
        kept, dropped, total, newer_is_dst = pp.merge_into(self.dst, self.src, self.dst)
        self.assertEqual((kept, dropped, total, newer_is_dst), (1, 0, 2, False))
        self.assertEqual(self.dst.stat().st_mtime_ns, base + 5 * 10 ** 9)

    def test_merge_into_copies_newer_source_verbatim_when_nothing_kept(self):
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", 1, "Thu Jan  1 00:00:00 2026", "dead-old")], base)
        self._write(self.src, [_pin("telegram-bridge", self.me, self.my_lstart, "newer")], base + 10 ** 9)
        kept, dropped, total, newer_is_dst = pp.merge_into(self.dst, self.src, self.dst)
        self.assertEqual(kept, 0); self.assertFalse(newer_is_dst)
        self.assertEqual(self.dst.read_bytes(), self.src.read_bytes())
        # and the reciprocal: destination newer, the older source pin dead
        # (pid 1 with a wrong lstart) -> nothing kept, destination untouched
        self._write(self.src, [_pin("telegram-bridge", 1, "Thu Jan  1 00:00:00 2026", "dead")], base + 10 ** 9)
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-new")], base + 2 * 10 ** 9)
        before = self.dst.read_bytes()
        kept, _, _, newer_is_dst = pp.merge_into(self.dst, self.dst, self.src)
        self.assertTrue(newer_is_dst); self.assertEqual(self.dst.read_bytes(), before)

    def test_merge_into_refusals(self):
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "a", "2027-01-01T00:00:00Z")], base)
        self._write(self.src, [_pin("discord-bridge", self.me, self.my_lstart, "b")], base)
        with self.assertRaises(ValueError):          # exact tie, different bytes
            pp.merge_into(self.dst, self.src, self.dst)
        self.dst.write_text('{"pins": "no"}')
        with self.assertRaises(ValueError):          # malformed destination
            pp.merge_into(self.dst, self.src, self.dst)
        many = [_pin("s", 100 + i, f"l{i}", f"p{i}") for i in range(pp.MAX_PINS + 1)]
        self._write(self.dst, [], base); self._write(self.src, many, base + 10 ** 9)
        with self.assertRaises(ValueError):          # over the bound
            pp.merge_into(self.dst, self.src, self.dst)

    def test_merge_into_token_mismatch_makes_destination_newest(self):
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-old")], base)
        token = pp._sha256(self.dst)
        self._write(self.src, [_pin("discord-bridge", self.me, self.my_lstart, "src")], base + 10 ** 9)
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "dest-moved")], base + 2 * 10 ** 9)
        _, _, _, newer_is_dst = pp.merge_into(self.dst, self.src, self.dst, expect_dst_sha256=token)
        self.assertTrue(newer_is_dst)
        self.assertEqual([p["reason"] for p in pp.load_pins(self.dst)], ["dest-moved"])

    def test_cli_in_process(self):
        import contextlib
        import io
        base = time.time_ns() - 10 ** 12
        self._write(self.dst, [_pin("discord-bridge", self.me, self.my_lstart, "old")], base)
        self._write(self.src, [_pin("telegram-bridge", self.me, self.my_lstart, "newer")], base + 10 ** 9)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pp._cli(["merge", "--into", str(self.dst), "--newer", str(self.src), "--older", str(self.dst)])
        self.assertEqual(rc, 0); self.assertIn("kept=1", out.getvalue()); self.assertIn("newer=src", out.getvalue())
        self._write(self.src, [_pin("discord-bridge", self.me, self.my_lstart, "tie")], self.dst.stat().st_mtime_ns)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = pp._cli(["merge", "--into", str(self.dst), "--newer", str(self.src), "--older", str(self.dst)])
        self.assertEqual(rc, 2); self.assertIn("merge refused", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
