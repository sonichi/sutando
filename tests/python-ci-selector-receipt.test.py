#!/usr/bin/env python3
"""Runtime pin: every python leg runs only a list the shared selector produced, checked in CI.

The selector writes a receipt (mode, list hash, output hash) when it selects, and the
step's unconditional `verify` exits 4 unless the files about to run match that receipt.
This runs ci.yml's real step for legs 1 (`without`), 6 (`only`) and 7 (`serial`): with the real
selector each leg verifies and reaches the lane with its receipt; with a selector that
drops its receipt, or whose output is altered after it is hashed, each leg must stop with
exit 4 before the lane runs; and legs 6-7 must stop when one file is added to or dropped
from their run list after sharding, the selection left intact. A workflow that skips
verification passes those spoiled runs and fails here; one that skips the selector has no receipt and fails in CI itself.

Run: python3 tests/python-ci-selector-receipt.test.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("legs", REPO / "tests" / "python-ci-legs-partition.test.py")
legs_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legs_mod)
SEL = "scripts/select-load-sensitive-suites.sh"

# Wraps the real selector; `$SPOIL` decides what happens to its receipt or output.
WRAPPER = """#!/usr/bin/env bash
here="$(dirname "$0")"
if [ "$1" = verify ]; then exec bash "$here/select-real.sh" "$@"; fi
out="$(bash "$here/select-real.sh" "$@")" || exit $?
case "$SPOIL" in
  drop) rm -f "$3/selector.$1.receipt" ;;
  extra) out="$out"$'\\n'"$EXTRA" ;;
esac
printf '%s\\n' "$out"
"""

# Wraps the real sharder: the selection stays intact, the leg's run list changes after it.
SHARD_WRAPPER = """#!/usr/bin/env bash
out="$(bash "$(dirname "$0")/shard-real.sh" "$@")" || exit $?
case "$SPOIL" in
  files-add) out="$out"$'\\n'"$EXTRA" ;;
  files-drop) out="$(printf '%s\\n' "$out" | sed '$d')" ;;
esac
[ -z "$out" ] || printf '%s\\n' "$out"
"""


def main() -> int:
    fails = []
    disc = subprocess.run(["bash", str(REPO / "scripts" / "discover-python-tests.sh")], cwd=REPO,
                          capture_output=True, text=True, check=True).stdout.split()
    real = (REPO / legs_mod.LIST).read_text()
    listed = [ln.split()[0] for ln in real.splitlines() if ln.strip() and not ln.startswith("#")]
    with tempfile.TemporaryDirectory() as td:
        fx = legs_mod.build_fixture(Path(td), disc)
        legs = legs_mod.run_legs(fx, real, 8, (1, 6, 7))
        for shard, receipt in ((1, "selector.without.receipt"), (6, "selector.only.receipt"),
                               (7, "selector.serial.receipt")):
            rc, files, _w, err, receipts = legs[shard]
            if rc != 0 or files is None:
                fails.append(f"real selector: leg {shard} did not reach the lane (rc={rc}): {err.strip()}")
            elif receipts != [receipt]:
                fails.append(f"real selector: leg {shard} ran with receipts {receipts}, not [{receipt}]")

        (fx / SEL).rename(fx / "scripts" / "select-real.sh")
        (fx / SEL).write_text(WRAPPER)
        import os
        unlisted = next(f for f in disc if f not in listed)
        for spoil, extra_by_leg in (("drop", {1: "", 6: "", 7: ""}),
                                    ("extra", {1: listed[0], 6: unlisted, 7: unlisted})):
            for shard in (1, 6, 7):
                os.environ["SPOIL"], os.environ["EXTRA"] = spoil, extra_by_leg[shard]
                rc, files, _w, err, _r = legs_mod.run_legs(fx, real, 8, (shard,))[shard]
                if rc != 4 or files is not None:
                    fails.append(f"selector output {'without a receipt' if spoil == 'drop' else 'altered after hashing'}: "
                                 f"leg {shard} exited {rc}{' and reached the lane' if files is not None else ''}, "
                                 "not 4 before the lane")
                elif "selector receipt check" not in err:
                    fails.append(f"leg {shard} exited 4 without the receipt-check message: {err.strip()!r}")

        # Legs 6-7 must run exactly the selection: one file added to or dropped from the run
        # list after sharding, with the selection and its receipt intact, stops the leg.
        (fx / "scripts" / "shard-by-cost.sh").rename(fx / "scripts" / "shard-real.sh")
        (fx / "scripts" / "shard-by-cost.sh").write_text(SHARD_WRAPPER)
        for spoil in ("files-add", "files-drop"):
            for shard in (6, 7):
                os.environ["SPOIL"], os.environ["EXTRA"] = spoil, unlisted
                rc, files, _w, err, _r = legs_mod.run_legs(fx, real, 8, (shard,))[shard]
                if rc != 4 or files is not None or "is not exactly the selected suites" not in err:
                    fails.append(f"run list {'added to' if spoil == 'files-add' else 'dropped from'} after sharding: leg {shard} exited {rc}"
                                 f"{' and reached the lane' if files is not None else ''}, not 4 with "
                                 f"'is not exactly the selected suites': {err.strip()[-160:]!r}")
        os.environ.pop("SPOIL", None)
        os.environ.pop("EXTRA", None)

    for f in fails:
        print("  FAIL", f)
    if fails:
        return 1
    print("PASS: legs 1, 6 and 7 run only selector-receipted lists; a missing receipt or an output "
          "altered after selection stops each leg with exit 4 before the lane; legs 6-7 also stop when "
          "their run list gains or loses a file after sharding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
