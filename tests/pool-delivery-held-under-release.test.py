#!/usr/bin/env python3
"""held_by_other_instance must not lose a hold while release() is renaming.

find() probes .txt, .accepted and .claimed in turn. release() renames
.accepted -> .txt under the per-recipient arbitration lock. With the reader
outside that lock, the rename can land between two probes and the task is
visible under neither name while it never stopped being owned — the Stop hook
then tells this instance to process a peer's work.

Drives the PRODUCTION writers (accept/release), not a bare rename: a harness
that renames directly bypasses arbitration() and cannot show the difference
either way.
"""
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import pool_delivery as pd  # noqa: E402

WRITER = '''
import sys, pathlib
sys.path.insert(0, {src!r})
import pool_delivery as pd
ws = pathlib.Path(sys.argv[1]); rec = sys.argv[2]; tid = sys.argv[3]
d = pd.deliveries_dir(ws, rec)
for _ in range(6000):
    acc = d / (tid + pd.ACCEPTED_SUFFIX)
    pend = d / (tid + pd.PENDING_SUFFIX)
    try:
        if acc.exists():
            pd.release(acc)
        elif pend.exists():
            pd.accept(pend)
    except Exception:
        pass
'''


class HeldUnderRelease(unittest.TestCase):
    def test_a_hold_is_never_lost_while_the_owner_cycles_it(self):
        ws = pathlib.Path(tempfile.mkdtemp())
        rec = "worker-" + "b" * 25
        d = pd.deliveries_dir(ws, rec)
        d.mkdir(parents=True, exist_ok=True)
        tid = "task-" + "a" * 24
        (d / (tid + pd.ACCEPTED_SUFFIX)).write_text("")

        script = pathlib.Path(tempfile.mkdtemp()) / "writer.py"
        script.write_text(WRITER.format(src=str(REPO / "src")))
        proc = subprocess.Popen([sys.executable, str(script), str(ws), rec, tid],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            misses = reads = 0
            deadline = time.time() + 3.0
            while time.time() < deadline and proc.poll() is None:
                reads += 1
                if pd.held_by_other_instance(ws, tid, "core") is None:
                    misses += 1
        finally:
            proc.terminate()
            proc.wait()

        self.assertGreater(reads, 200, "the probe barely ran; it proves nothing")
        self.assertEqual(
            misses, 0,
            f"{misses} of {reads} reads reported the task UNHELD while "
            f"{rec} owned it throughout — the Stop hook would hand a peer's "
            f"work to this instance")


if __name__ == "__main__":
    unittest.main()
