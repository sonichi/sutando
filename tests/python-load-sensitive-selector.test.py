#!/usr/bin/env python3
"""Regression pin: the load-sensitive selector partitions and fails loudly on drift.

`scripts/select-load-sensitive-suites.sh only|without <list>` reads the discovery
output on stdin. `only` must print each listed suite exactly once and `without`
everything else, so the two together are the discovery list; an entry that is
not discovered (renamed, deleted, mistyped) or that is listed twice must exit
non-zero and name it — a dropped entry would quietly return that suite to the
shared four-worker legs. The committed list must select cleanly against the real
discovery, so a rename fails here before it fails in CI.

Run: python3 tests/python-load-sensitive-selector.test.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "select-load-sensitive-suites.sh"
DISCOVER = REPO / "scripts" / "discover-python-tests.sh"
LIST = REPO / "tests" / "python-load-sensitive-suites.txt"


def select(mode, listing, discovered):
    return subprocess.run(["bash", str(SCRIPT), mode, str(listing)],
                          input="".join(f"{f}\n" for f in discovered), capture_output=True, text=True)


def main() -> int:
    fails = []
    disc = [f"tests/s{i}.test.py" for i in "abcde"]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        good = td / "good.txt"
        good.write_text("# comment\n\ntests/sb.test.py\ntests/sd.test.py\n")
        only = select("only", good, disc)
        without = select("without", good, disc)
        if only.returncode != 0 or only.stdout.split() != ["tests/sb.test.py", "tests/sd.test.py"]:
            fails.append(f"only: rc={only.returncode} out={only.stdout.split()} (comments/blank lines must be ignored)")
        if without.returncode != 0 or without.stdout.split() != ["tests/sa.test.py", "tests/sc.test.py", "tests/se.test.py"]:
            fails.append(f"without: rc={without.returncode} out={without.stdout.split()}")
        if sorted(only.stdout.split() + without.stdout.split()) != sorted(disc):
            fails.append("only + without is not the discovery list")

        stale = td / "stale.txt"
        stale.write_text("tests/sb.test.py\ntests/renamed.test.py\n")
        for mode in ("only", "without"):
            r = select(mode, stale, disc)
            if r.returncode == 0:
                fails.append(f"{mode}: a listed-but-undiscovered entry was silently dropped (rc 0)")
            if "tests/renamed.test.py" not in r.stderr:
                fails.append(f"{mode}: the stale entry was not named on stderr: {r.stderr.strip()!r}")
            if r.stdout.strip():
                fails.append(f"{mode}: printed a list despite the stale entry")

        dup = td / "dup.txt"
        dup.write_text("tests/sb.test.py\ntests/sd.test.py\ntests/sb.test.py\n")
        r = select("only", dup, disc)
        if r.returncode == 0 or "tests/sb.test.py" not in r.stderr:
            fails.append(f"a duplicate entry was accepted: rc={r.returncode} err={r.stderr.strip()!r}")

        r = select("both", good, disc)
        if r.returncode == 0:
            fails.append("an unknown mode was accepted")

    # The committed list against the real discovery: every entry present, none twice.
    real = subprocess.run(["bash", str(DISCOVER)], cwd=str(REPO), capture_output=True, text=True)
    discovered = [p for p in real.stdout.splitlines() if p]
    listed = [ln.strip() for ln in LIST.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    r = select("only", LIST, discovered)
    if r.returncode != 0:
        fails.append(f"the committed list does not select cleanly: {r.stderr.strip()}")
    elif sorted(r.stdout.split()) != sorted(listed):
        fails.append(f"leg 6 would run {len(r.stdout.split())} suites for {len(listed)} listed")
    rest = select("without", LIST, discovered)
    if set(rest.stdout.split()) & set(listed):
        fails.append("a listed suite is still in the shared legs")
    if sorted(rest.stdout.split() + r.stdout.split()) != sorted(discovered):
        fails.append("leg 6 + legs 1-5 is not the real discovery list")

    for f in fails:
        print("  FAIL", f)
    if fails:
        return 1
    print(f"PASS: selector partitions ({len(listed)} listed of {len(discovered)} discovered), "
          "fails loudly on a stale or duplicate entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
