#!/usr/bin/env python3
"""Regression pin: the six python legs, as ci.yml's own step computes them, partition discovery.

Runs the `Run Python standalone tests` step body from ci.yml once per leg (SHARD=1..6)
in a fixture that has every discovered path, the real selector, sharder, cost table and
list, and a stub lane runner that records the file list and worker count it is handed.
Whatever the workflow does to pick a leg's files — selector, sharder, or anything that
replaces them — is what gets measured. It must hold that every discovered suite runs in
exactly one leg, the load-sensitive suites only in leg 6 (heaviest first, two workers),
and legs 1-5 none of them; and a list with a stale or duplicate entry must stop every
leg with the selector's exit 3, so a leg that reads the list without the selector fails.

Run: python3 tests/python-ci-legs-partition.test.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
LIST = "tests/python-load-sensitive-suites.txt"
COSTS = "tests/python-suite-costs.txt"
COPIED = ["scripts/discover-python-tests.sh", "scripts/select-load-sensitive-suites.sh",
          "scripts/shard-by-cost.sh", LIST, COSTS]

LANE_STUB = """#!/usr/bin/env bash
# Records what the step hands the lane runner, and a passing record per file.
cp "$2" "$LEG_OUT/files"; echo "$1" > "$LEG_OUT/workers"; cp "$3"/selector.*.receipt "$LEG_OUT/" 2>/dev/null || true
n=$(wc -l < "$2"); for i in $(seq 1 "$n"); do echo 0 > "$3/$i.rc"; echo 0 > "$3/$i.time"; : > "$3/$i.out"; done
"""
PY_SHIM = """#!/usr/bin/env bash
# Only `python3 -m coverage ...` runs in the step; leave the file it moves.
[ "$1 $2" = "-m coverage" ] && { : > .coverage; exit 0; }
exit 97
"""


def step_body() -> str:
    text = CI.read_text()
    start = text.find("      - name: Run Python standalone tests\n")
    run = text.find("        run: |\n", start)
    if start < 0 or run < 0:
        raise AssertionError("could not find the 'Run Python standalone tests' run: block in ci.yml")
    m = re.match(r"(.*?)(?=\n {6}- name:|\n {2}\w|\Z)", text[run + len("        run: |\n"):], re.S)
    return textwrap.dedent(m.group(1))


def build_fixture(td: Path, discovered) -> Path:
    fx = td / "repo"
    for rel in discovered:
        (fx / rel).parent.mkdir(parents=True, exist_ok=True)
        (fx / rel).touch()
    for rel in COPIED:
        (fx / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, fx / rel)
    (fx / "scripts" / "parallel-suite-lane.sh").write_text(LANE_STUB)
    shim = td / "bin"
    shim.mkdir()
    (shim / "python3").write_text(PY_SHIM)
    (shim / "python3").chmod(0o755)
    return fx


def run_legs(fx: Path, list_text: str, nproc: int = 8, shards=range(1, 7)):
    body = step_body()
    (fx / LIST).write_text(list_text)
    getconf = fx.parent / "bin" / "getconf"
    getconf.write_text(f"#!/usr/bin/env bash\necho {nproc}\n")
    getconf.chmod(0o755)
    legs = {}
    for shard in shards:
        out = Path(tempfile.mkdtemp(dir=fx.parent))
        env = dict(os.environ, SHARD=str(shard), LEG_OUT=str(out), PATH=f"{fx.parent / 'bin'}:{os.environ['PATH']}")
        r = subprocess.run(["bash", "-e", "-c", body], cwd=fx, env=env, capture_output=True, text=True)
        files = (out / "files").read_text().split() if (out / "files").exists() else None
        workers = (out / "workers").read_text().strip() if (out / "workers").exists() else None
        legs[shard] = (r.returncode, files, workers, r.stderr[-400:], sorted(x.name for x in out.glob("selector.*.receipt")))
    return legs


def main() -> int:
    fails = []
    disc = subprocess.run(["bash", str(REPO / "scripts" / "discover-python-tests.sh")], cwd=REPO,
                          capture_output=True, text=True, check=True).stdout.split()
    listed = [ln.strip() for ln in (REPO / LIST).read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    cost = {}
    for ln in (REPO / COSTS).read_text().splitlines():
        if ln and not ln.startswith("#"):
            c, f = ln.split(maxsplit=1)
            cost[f] = max(int(c), 1)

    td_obj = tempfile.TemporaryDirectory()
    fx = build_fixture(Path(td_obj.name), disc)
    real = (REPO / LIST).read_text()
    legs = run_legs(fx, real, 8)
    for shard, (rc, files, workers, err, _r) in legs.items():
        if rc != 0 or files is None:
            fails.append(f"leg {shard}: the step did not reach the lane runner (rc={rc}): {err.strip()}")
    if fails:
        td_obj.cleanup()
        for f in fails:
            print("  FAIL", f)
        return 1

    every = [f for s in legs for f in legs[s][1]]
    dups = sorted({f for f in every if every.count(f) > 1})
    if dups:
        fails.append(f"{len(dups)} suite(s) run in more than one leg, e.g. {dups[:3]}")
    missing = sorted(set(disc) - set(every))
    if missing:
        fails.append(f"{len(missing)} discovered suite(s) run in no leg, e.g. {missing[:3]}")
    shared = sorted(set(listed) & {f for s in range(1, 6) for f in legs[s][1]})
    if shared:
        fails.append(f"load-sensitive suite(s) in legs 1-5: {shared[:3]}")
    leg6 = legs[6][1]
    if sorted(leg6) != sorted(listed):
        fails.append(f"leg 6 runs {len(leg6)} suites, not exactly the {len(listed)} listed")
    if [cost.get(f, 1) for f in leg6] != sorted((cost.get(f, 1) for f in leg6), reverse=True):
        fails.append("leg 6 is not ordered heaviest first")
    # The step's rule: shared legs take the host's core count, leg 6 always two. Checked on
    # a 2-core and an 8-core host so a valid 2-core value is never read as hardcoding.
    for nproc in (2, 8):
        got = legs if nproc == 8 else run_legs(fx, real, nproc)
        shared = {s: got[s][2] for s in range(1, 6)}
        if set(shared.values()) != {str(nproc)}:
            fails.append(f"on a {nproc}-core host legs 1-5 run with {sorted(set(shared.values()))} workers, not {nproc}")
        if got[6][2] != "2":
            fails.append(f"on a {nproc}-core host leg 6 runs with {got[6][2]} workers, not 2")

    # A list that only partitions correctly when it is valid proves nothing about who reads
    # it: a stale or duplicate entry must stop every leg with the selector's exit 3.
    drifted = {
        "stale entry": real.replace(listed[0], listed[0].replace(".test.py", "-renamed.test.py")),
        "duplicate entry": real + listed[0] + "\n",
    }
    for what, text in drifted.items():
        for shard, (rc, files, _w, err, _r) in run_legs(fx, text).items():
            if rc != 3:
                fails.append(f"{what}: leg {shard} exited {rc}, not the selector's 3"
                             f"{' and ran ' + str(len(files)) + ' suites' if files else ''}")

    td_obj.cleanup()
    for f in fails:
        print("  FAIL", f)
    if fails:
        return 1
    print(f"PASS: ci.yml's six legs run all {len(disc)} discovered suites exactly once "
          f"({'/'.join(str(len(legs[s][1])) for s in legs)}); the {len(listed)} listed only in leg 6, "
          "heaviest first, two workers (2- and 8-core hosts); a stale or duplicate list entry stops every leg with exit 3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
