#!/usr/bin/env python3
"""Regression pin: cost-balanced leg assignment is a partition, deterministic, and balanced.

`scripts/shard-by-cost.sh <shards> <shard> <table>` reads the discovery output on
stdin and prints one leg. Across all legs every file must appear exactly once
(a file assigned twice runs twice; one assigned nowhere never runs), the same
input must give the same legs on every call (a leg must not depend on timing),
a file missing from the table must still be assigned (it costs 1), and the
heaviest leg must not exceed the even share by more than one suite's cost — the
bound longest-first-onto-lightest guarantees.

Run: python3 tests/python-shard-by-cost.test.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "shard-by-cost.sh"


def legs(shards, files, table):
    out = []
    for s in range(1, shards + 1):
        r = subprocess.run(["bash", str(SCRIPT), str(shards), str(s), str(table)],
                           input="\n".join(files) + "\n", capture_output=True, text=True, check=True)
        out.append([ln for ln in r.stdout.splitlines() if ln])
    return out


def main() -> int:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        table = Path(td) / "costs.txt"
        files = [f"tests/s{i:03d}.test.py" for i in range(40)]
        costs = {f: (i % 7) * 10 + 1 for i, f in enumerate(files)}   # 1..61s, repeating
        known = files[:-3]                                             # three files unknown → cost 1
        table.write_text("# comment line\n" + "".join(f"{costs[f]} {f}\n" for f in known))
        cost = lambda f: costs[f] if f in known else 1

        a = legs(3, files, table)
        flat = [f for leg in a for f in leg]
        if sorted(flat) != sorted(files):
            fails.append(f"partition: {len(flat)} assignments for {len(files)} files, "
                         f"missing={sorted(set(files) - set(flat))[:3]} dup={sorted(f for f in set(flat) if flat.count(f) > 1)[:3]}")
        if a != legs(3, files, table):
            fails.append("determinism: a second call assigned differently")
        loads = [sum(cost(f) for f in leg) for leg in a]
        even = sum(cost(f) for f in files) / 3
        if max(loads) > even + max(cost(f) for f in files):
            fails.append(f"balance: heaviest leg {max(loads)} exceeds even share {even:.0f} by more than one suite")
        if not all(any(f in leg for leg in a) for f in files[-3:]):
            fails.append("a file absent from the table was left unassigned")
        r = subprocess.run(["bash", str(SCRIPT), "3", "4", str(table)], input="x\n", capture_output=True, text=True)
        if r.returncode == 0:
            fails.append("shard 4 of 3 was accepted")

    for f in fails:
        print("  FAIL", f)
    if fails:
        return 1
    print(f"PASS: cost-balanced legs partition the files, deterministically, within one suite of even ({loads})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
