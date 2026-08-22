#!/usr/bin/env python3
"""The benchmark IS gates 1-2 of the C-canonical ruling, so its correctness
asserts must run — and be COVERED — in CI. main() is driven IN-PROCESS
(the repo's coverage gate does not instrument subprocesses; see the
review-checks-py precedent), which works because the benchmark lives in the
package: worker processes re-import ag2_sparrow.bench_claim_backends by name.
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from bench_claim_backends import main  # noqa: E402

# Shim import covers its lines; only the __main__ body is CLI-only.
import bench_claim_backends as bench  # noqa: E402


def unit_controls() -> None:
    """In-process reachability for branches the quick matrix cannot hit:
    child bodies, worker-death recovery, CLI modes, verdicts, git fallback."""
    import multiprocessing as mp
    import subprocess
    import tempfile as tf
    from pathlib import Path as P
    from unittest import mock

    from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend

    # child bodies, called directly
    with tf.TemporaryDirectory() as td:
        r = P(td) / "r"
        DesignAClaimBackend(r).publish("item-0", b"x")
        q = mp.Queue()
        bench._proc_worker("a", str(r), 1, 0, q)
        assert q.get(timeout=5) == 1, "_proc_worker claims and completes"
    with tf.TemporaryDirectory() as td:
        r = P(td) / "r"
        DesignAClaimBackend(r).publish("hang-item", b"x")
        ev = mp.Event()
        with mock.patch("time.sleep", side_effect=RuntimeError("no hang in tests")):
            try:
                bench._claim_and_hang("a", str(r), "hang-item", ev)
            except RuntimeError:
                pass
        assert ev.is_set(), "_claim_and_hang signals after claiming"

    # worker death: a Process that never runs -> Empty branch, terminate scan, raise
    class DeadProcess:
        exitcode = 1
        def __init__(self, *a, **k): pass
        def start(self): pass
        def join(self, timeout=None): pass
        def is_alive(self): return False
    with tf.TemporaryDirectory() as td,          mock.patch.object(bench.mp, "Process", DeadProcess):
        try:
            bench.bench_procs("a", 5, 1, b"x")
            raise AssertionError("worker death must raise, not hang")
        except RuntimeError as e:
            assert "workers reported" in str(e)

    # alive-straggler branch: terminate() must be reached
    class HungProcess(DeadProcess):
        terminated = []
        def is_alive(self): return True
        def terminate(self): HungProcess.terminated.append(True)
    with tf.TemporaryDirectory() as td,          mock.patch.object(bench.mp, "Process", HungProcess):
        try:
            bench.bench_procs("a", 5, 1, b"x")
        except RuntimeError:
            pass
        assert HungProcess.terminated, "a hung worker gets terminated"

    # a failing verdict exits 1, and --stamp lands in the JSON record
    with tf.TemporaryDirectory() as td,          mock.patch.object(bench, "_verdict", return_value=False):
        out = P(td) / "b.json"
        rc = bench.main(["--quick", "--stamp", "T0", "--json", str(out)])
        assert rc == 1, "violated invariants exit nonzero"
        # the fail branch returns before the json write — absence is correct
        assert not out.exists(), "no result file is written on a failed verdict"

    # CLI matrix selection, all three modes + deep
    assert bench._select_matrix(True, None, False) == ([40], (1, 2), 30)
    assert bench._select_matrix(False, 7, False) == ([7], (1, 4, 16), 20)
    assert bench._select_matrix(False, None, False)[0] == [100, 10_000]
    assert bench._select_matrix(False, None, True)[0] == [100, 10_000, 100_000]

    # verdicts: pass and fail
    good = {"procs_1": {"exactly_once": True}, "crash": {"ok": True},
            "conflict": {"ok": True}}
    bad = {"procs_1": {"exactly_once": False}, "crash": {"ok": True},
           "conflict": {"ok": True}}
    assert bench._verdict(good, good, (1,)) is True
    assert bench._verdict(good, bad, (1,)) is False

    # head_sha degrades to "unknown" when git is unusable
    with mock.patch.object(bench.subprocess, "run",
                           side_effect=OSError("no git")):
        assert bench.head_sha() == "unknown"
    print("unit controls OK")
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("bench_shim", REPO / "scripts" / "bench-claim-backends.py")
_shim = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_shim)
assert _shim.main is main, "shim must re-export the module's main"


def run() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "bench.json"
        rc = main(["--quick", "--json", str(out)])
        if rc != 0:
            print("FAIL: benchmark exited nonzero (correctness invariant violated)")
            sys.exit(1)
        data = json.loads(out.read_text())
        for kind in ("a", "c"):
            sc = data["scenarios"][kind]
            assert sc["crash"]["ok"], f"{kind}: crash invariants"
            assert sc["conflict"]["ok"], f"{kind}: conflict invariants"
            assert any(k.startswith("procs_") and sc[k]["exactly_once"]
                       for k in sc), f"{kind}: exactly-once"
        print("OK — quick matrix in-process, all correctness invariants held")


# The __main__ guard is LOAD-BEARING: spawn children re-import this module,
# and an unguarded module-level main() trips _check_not_importing_main.
if __name__ == "__main__":
    unit_controls()
    run()
