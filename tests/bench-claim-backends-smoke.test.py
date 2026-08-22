#!/usr/bin/env python3
"""The benchmark IS gates 1-2 of the C-canonical ruling, so its correctness
asserts must run in CI. --quick exercises every scenario (both backends,
contention, kill injection, conflict) in seconds; exit 0 == all invariants held.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "bench.json"
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "bench-claim-backends.py"),
         "--quick", "--json", str(out)],
        capture_output=True, text=True, timeout=300)
    print(r.stdout[-600:])
    if r.returncode != 0:
        print(r.stderr[-400:], file=sys.stderr)
        print("FAIL: benchmark exited nonzero (correctness invariant violated)")
        sys.exit(1)
    data = json.loads(out.read_text())
    for kind in ("a", "c"):
        s = data["scenarios"][kind]
        assert s["crash"]["ok"], f"{kind}: crash invariants"
        assert s["conflict"]["ok"], f"{kind}: conflict invariants"
        assert any(k.startswith("procs_") and s[k]["exactly_once"]
                   for k in s), f"{kind}: exactly-once"
    print("OK — quick matrix ran, all correctness invariants held, JSON well-formed")
