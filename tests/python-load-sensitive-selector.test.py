#!/usr/bin/env python3
"""Regression pin: the load-sensitive selector partitions and fails loudly on drift.

`scripts/select-load-sensitive-suites.sh only|without <list>` reads the discovery
output on stdin. `only` must print each listed suite exactly once and `without`
everything else, so the two together are the discovery list; an entry that is
not discovered (renamed, deleted, mistyped) or that is listed twice must exit
non-zero and name it — a dropped entry would quietly return that suite to the
shared four-worker legs. The committed list must select cleanly against the real
discovery, so a rename fails here before it fails in CI. That ci.yml actually routes
each leg through it is pinned by running the step: tests/python-ci-legs-partition.test.py.

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


RECEIPTS = Path(tempfile.mkdtemp())


def select(mode, listing, discovered):
    return subprocess.run(["bash", str(SCRIPT), mode, str(listing), str(RECEIPTS)],
                          input="".join(f"{f}\n" for f in discovered), capture_output=True, text=True)


def verify(kind, listing, selected, run):
    return subprocess.run(["bash", str(SCRIPT), "verify", kind, str(listing), str(RECEIPTS), str(selected), str(run)],
                          capture_output=True, text=True)


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
            if r.returncode != 3:
                fails.append(f"{mode}: a listed-but-undiscovered entry exited {r.returncode}, not 3")
            if "tests/renamed.test.py" not in r.stderr:
                fails.append(f"{mode}: the stale entry was not named on stderr: {r.stderr.strip()!r}")
            if r.stdout.strip():
                fails.append(f"{mode}: printed a list despite the stale entry")

        dup = td / "dup.txt"
        dup.write_text("tests/sb.test.py\ntests/sd.test.py\ntests/sb.test.py\n")
        r = select("only", dup, disc)
        if r.returncode != 3 or "tests/sb.test.py" not in r.stderr:
            fails.append(f"a duplicate entry was accepted: rc={r.returncode} err={r.stderr.strip()!r}")

        tagged = td / "tagged.txt"
        tagged.write_text("tests/sb.test.py\ntests/sd.test.py serial\n")
        o, sr, w = (select(m, tagged, disc) for m in ("only", "serial", "without"))
        if (o.stdout.split(), sr.stdout.split()) != (["tests/sb.test.py"], ["tests/sd.test.py"]):
            fails.append(f"serial tag: only={o.stdout.split()} serial={sr.stdout.split()}")
        if sorted(o.stdout.split() + sr.stdout.split() + w.stdout.split()) != sorted(disc):
            fails.append("only + serial + without is not the discovery list")
        untagged = td / "untagged.txt"
        untagged.write_text("tests/sb.test.py\n")
        r = select("serial", untagged, disc)
        if r.returncode != 3 or "leg 7: the selector emitted no serial suites" not in r.stderr:
            fails.append(f"an empty serial leg did not fail with its reason: rc={r.returncode} err={r.stderr.strip()!r}")
        badtag = td / "badtag.txt"
        badtag.write_text("tests/sb.test.py later\n")
        r = select("only", badtag, disc)
        if r.returncode != 3 or "later" not in r.stderr:
            fails.append(f"an unknown tag was accepted: rc={r.returncode}")

        r = select("both", good, disc)
        if r.returncode == 0:
            fails.append("an unknown mode was accepted")

        # verify: the receipt binds the run list to what the selector emitted for this list.
        sel = td / "sel.txt"
        sel.write_text(select("only", good, disc).stdout)
        rev = td / "rev.txt"
        rev.write_text("".join(reversed(sel.read_text().splitlines(keepends=True))))
        r = verify("only", good, sel, rev)
        if r.returncode != 0:
            fails.append(f"verify rejected the selector's own output in another order: {r.stderr.strip()}")
        cases = {
            "receipt missing": lambda: (RECEIPTS / "selector.only.receipt").unlink(),
            "selected list edited": lambda: sel.write_text(sel.read_text() + "tests/sa.test.py\n"),
            "list edited after selection": lambda: good.write_text(good.read_text() + "# edited\n"),
        }
        for what, spoil in cases.items():
            select("only", good, disc)
            sel.write_text(select("only", good, disc).stdout)
            spoil()
            r = verify("only", good, sel, sel)
            if r.returncode != 4 or "selector receipt check" not in r.stderr:
                fails.append(f"verify, {what}: exited {r.returncode}, not 4: {r.stderr.strip()!r}")
            good.write_text("# comment\n\ntests/sb.test.py\ntests/sd.test.py\n")
        rest = td / "rest.txt"
        rest.write_text(select("without", good, disc).stdout)
        extra = td / "extra.txt"
        extra.write_text(rest.read_text() + "tests/sb.test.py\n")
        r = verify("without", good, rest, extra)
        if r.returncode != 4:
            fails.append(f"verify accepted a shared leg running a listed suite: exited {r.returncode}")

    # The committed list against the real discovery: every entry present, none twice.
    real = subprocess.run(["bash", str(DISCOVER)], cwd=str(REPO), capture_output=True, text=True)
    discovered = [p for p in real.stdout.splitlines() if p]
    listed = [ln.split()[0] for ln in LIST.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    r = select("only", LIST, discovered)
    ser = select("serial", LIST, discovered)
    if r.returncode != 0 or ser.returncode != 0:
        fails.append(f"the committed list does not select cleanly: {(r.stderr + ser.stderr).strip()}")
    elif sorted(r.stdout.split() + ser.stdout.split()) != sorted(listed):
        fails.append(f"legs 6-7 would run {len(r.stdout.split() + ser.stdout.split())} suites for {len(listed)} listed")
    rest = select("without", LIST, discovered)
    if set(rest.stdout.split()) & set(listed):
        fails.append("a listed suite is still in the shared legs")
    if sorted(rest.stdout.split() + r.stdout.split() + ser.stdout.split()) != sorted(discovered):
        fails.append("legs 6-7 + legs 1-5 is not the real discovery list")

    for f in fails:
        print("  FAIL", f)
    if fails:
        return 1
    print(f"PASS: selector partitions ({len(listed)} listed of {len(discovered)} discovered), "
          "fails loudly (exit 3) on a stale or duplicate entry; verify (exit 4) binds a run list to its receipt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
