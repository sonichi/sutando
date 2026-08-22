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
    run()
