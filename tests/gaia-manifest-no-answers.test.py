#!/usr/bin/env python3
"""The gaia-100 manifest must carry identifiers only -- never answers.

Publishing GAIA answers contaminates the benchmark irreversibly for every
downstream user, so this is enforced structurally rather than by scanning for
answer strings: a substring scan needs the dataset (CI has none) and cannot
distinguish a leaked answer from a common English word in the fixed preamble.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "gaia-100.manifest.json"

ALLOWED_KEYS = {"schema", "name", "description", "source", "prompt_preamble",
                "excluded", "case_ids"}
CASE_ID = re.compile(r"gaia-l[123]-[0-9a-f]{8}\Z")

# Changing the preamble is a deliberate act: update this digest in the same commit.
METADATA_SHA256 = "c13773d5b4bcbd3c8268e6dd3620d68ef36530698179a0cdbdf7d0c261d0290e"
PREAMBLE_SHA256 = "609ad2dca3d13a6c652903c463f6f85d56540fbfbfe4a43f3441b146b85fc625"

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

    # EVERY free-text field is pinned, not just the preamble. name/description/
    # source are fixed for this suite too, so a substring predicate over them
    # would miss a smuggled answer exactly as the preamble shape check did.
    # excluded.reason is free text too -- the only part of `excluded` that is not
    # an identifier list, so it is pinned alongside the rest of the metadata.
    meta = {k: v for k, v in m.items() if k not in ("case_ids", "excluded")}
    meta["excluded.reason"] = m["excluded"]["reason"]
    meta_digest = hashlib.sha256(
        json.dumps(meta, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    check(meta_digest == METADATA_SHA256,
          "all manifest metadata is byte-identical to the pinned text")

    # Pinned by digest, not by shape. Shape predicates constrained only the TAIL,
    # so any text BEFORE "Question: " passed -- and a GAIA answer is a short
    # declarative string, exactly what a punctuation heuristic misses.
    pre = m["prompt_preamble"]
    check(hashlib.sha256(pre.encode()).hexdigest() == PREAMBLE_SHA256,
          "preamble is byte-identical to the pinned text (no smuggled content)")
    check(pre.endswith("Question: "),
          "preamble ends at the question boundary")

    print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + '; '.join(failures)}"
          f" ({ran - len(failures)}/{ran})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
