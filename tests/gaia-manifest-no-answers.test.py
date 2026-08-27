#!/usr/bin/env python3
"""The gaia-100 manifest must carry identifiers only -- never answers.

Publishing GAIA answers contaminates the benchmark irreversibly for every
downstream user, so this is enforced structurally rather than by scanning for
answer strings: a substring scan needs the dataset (CI has none) and cannot
distinguish a leaked answer from a common English word in the fixed preamble.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "gaia-100.manifest.json"

ALLOWED_KEYS = {"schema", "name", "description", "source", "prompt_preamble",
                "excluded", "case_ids"}
CASE_ID = re.compile(r"gaia-l[123]-[0-9a-f]{8}\Z")

failures: list[str] = []
ran = 0


def check(cond: bool, label: str) -> None:
    global ran
    ran += 1
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def main() -> int:
    m = json.loads(MANIFEST.read_text())

    check(set(m) <= ALLOWED_KEYS,
          f"no unexpected top-level keys (extra: {sorted(set(m) - ALLOWED_KEYS)})")

    ids = m["case_ids"]
    check(all(CASE_ID.match(c) for c in ids), "every case_id is a bare identifier")
    check(len(ids) == len(set(ids)), "case_ids are unique")
    check(all(CASE_ID.match(c) for c in m["excluded"]["case_ids"]),
          "every excluded id is a bare identifier")

    # An answer would have to ride in some free-text field. The preamble is the
    # only one that varies with the suite, and it must be case-INDEPENDENT --
    # that is what makes a substring hit inside it meaningless.
    blob = json.dumps({k: v for k, v in m.items()
                       if k not in ("case_ids", "excluded", "prompt_preamble")})
    check("equals" not in blob, "no expect/equals structure in manifest metadata")

    pre = m["prompt_preamble"]
    check(pre.endswith("Question: "),
          "preamble ends at the question boundary (carries no case content)")
    check(not any(c in pre for c in ("?", "\t")) and pre.count("\n") == 2,
          "preamble is fixed prose, not an embedded case")

    print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + '; '.join(failures)}"
          f" ({ran - len(failures)}/{ran})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
