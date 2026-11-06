#!/usr/bin/env python3
"""Regression pin: the required-context job asserts every job it needs.

`tsc + tests (clean install)` is the one context the main ruleset requires;
it runs with `if: always()` and passes unless a `check` call fails. A job that
is in its `needs:` list but not in a `check` call can fail without the gate
noticing — the required context turns green over a red suite. This reads the
job's `needs:` and its `check "<id>"` calls from ci.yml and fails on any gap.

Run: python3 tests/required-tests-checks-every-need.test.py
"""
import re
import sys
from pathlib import Path

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def gap_in(text: str):
    m = re.search(r"^  required-tests:\n(.*?)(?=^  \S|\Z)", text, re.S | re.M)
    if not m:
        return None, "no required-tests job in ci.yml"
    job = m.group(1)
    needs = re.search(r"needs:\s*\[([^\]]*)\]", job)
    if not needs:
        return None, "required-tests has no needs: list"
    needed = {n.strip() for n in needs.group(1).split(",") if n.strip()}
    checked = {b for _a, b in re.findall(r'check\s+"([^"]+)"\s+"\$\{\{\s*needs\.([^.]+)\.result', job)}
    return sorted(needed), sorted(needed - checked)


def main() -> int:
    needed, gap = gap_in(CI.read_text())
    if needed is None:
        print("FAIL:", gap); return 1
    if gap:
        print(f"FAIL: required-tests needs {needed} but never checks {gap} — a failure there would not gate the required context")
        return 1
    # Control: the pin must see a gap when a check line is removed.
    text = CI.read_text()
    line = next(ln for ln in text.splitlines() if 'check "' in ln and "needs." in ln)
    _n, control_gap = gap_in(text.replace(line + "\n", "", 1))
    if not control_gap:
        print("FAIL: control — removing a check line left no gap, so this pin cannot detect one"); return 1
    print(f"PASS: required-tests checks every job it needs ({needed}); control gap {control_gap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
