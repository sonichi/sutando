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
METADATA_SHA256 = "5453699cf57649e6091e3e36fde126b67e0c696a69ea3642bfbf7a3d547d2292"
PREAMBLE_SHA256 = "609ad2dca3d13a6c652903c463f6f85d56540fbfbfe4a43f3441b146b85fc625"

failures: list[str] = []
ran = 0


def check(cond: bool, label: str) -> None:
    global ran
    ran += 1
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def check_collision_guard() -> None:
    """Derived ids truncate task_id to 8 hex chars; without a guard a collision
    lets one row silently displace another's question and answer."""
    import importlib.util as iu
    import json
    import tempfile
    spec = iu.spec_from_file_location("bg", ROOT / "scripts" / "build-gaia-suite.py")
    bg = iu.module_from_spec(spec)
    spec.loader.exec_module(bg)
    coll = [{"task_id": "deadbeef-1111-4000-8000-000000000001", "Level": 1,
             "Question": "Q-FIRST", "Final answer": "A-FIRST", "file_name": ""},
            {"task_id": "deadbeef-2222-4000-8000-000000000002", "Level": 1,
             "Question": "Q-SECOND", "Final answer": "A-SECOND", "file_name": ""}]
    bg.load_validation = lambda root: coll
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"schema": 1, "name": "x", "prompt_preamble": "P: ",
                   "case_ids": ["gaia-l1-deadbeef"],
                   "excluded": {"reason": "r", "case_ids": []}}, fh)
        bg.MANIFEST = pathlib.Path(fh.name)
    refused = False
    try:
        bg.build(pathlib.Path("/nonexistent"))
    except SystemExit:
        refused = True
    check(refused, "colliding 8-char derived ids are refused, not silently displaced")


def check_excluded_subtree_pinned() -> None:
    """A nested key under `excluded` must move the digest. Naming fields to add
    back is an enumeration; it missed exactly this (qingyun-wu, #3455)."""
    import copy
    m = json.loads(MANIFEST.read_text())

    def digest(man):
        meta = {k: v for k, v in man.items() if k != "case_ids"}
        meta["excluded"] = {k: v for k, v in man["excluded"].items() if k != "case_ids"}
        return hashlib.sha256(
            json.dumps(meta, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    base = digest(m)
    injected = copy.deepcopy(m)
    injected["excluded"]["answers"] = ["SECRET-GAIA-ANSWER"]
    check(digest(injected) != base,
          "a nested key under excluded changes the digest (no silent answer path)")

    # Control: the identifier list is deliberately out of the digest, so
    # reordering it must NOT trip the guard.
    reordered = copy.deepcopy(m)
    reordered["excluded"]["case_ids"] = list(reversed(reordered["excluded"]["case_ids"]))
    check(digest(reordered) == base,
          "reordering excluded.case_ids does not trip the digest (control)")


def check_builder_paths() -> None:
    """Exercise the builder without a GAIA copy: CI has none, so every path
    here is stubbed at load_validation."""
    import importlib.util as iu
    import json as _json
    import tempfile
    spec = iu.spec_from_file_location("bg2", ROOT / "scripts" / "build-gaia-suite.py")
    bg = iu.module_from_spec(spec)
    spec.loader.exec_module(bg)

    check(bg.case_id({"Level": 2, "task_id": "abcdef12-3456"}) == "gaia-l2-abcdef12",
          "case_id derives level and the 8-char prefix")

    rows = [{"task_id": "aaaaaaaa-1111", "Level": 1, "Question": "Q1",
             "Final answer": "A1", "file_name": ""},
            {"task_id": "bbbbbbbb-2222", "Level": 3, "Question": "Q2",
             "Final answer": "A2", "file_name": ""}]
    bg.load_validation = lambda root: rows
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump({"schema": 1, "name": "n", "prompt_preamble": "P: ",
                    "case_ids": ["gaia-l1-aaaaaaaa", "gaia-l3-bbbbbbbb"],
                    "excluded": {"reason": "r", "case_ids": []}}, fh)
        bg.MANIFEST = pathlib.Path(fh.name)
    suite = bg.build(pathlib.Path("/nonexistent"))
    check([c["id"] for c in suite["cases"]] == ["gaia-l1-aaaaaaaa", "gaia-l3-bbbbbbbb"],
          "build preserves manifest order")
    check(suite["cases"][0]["prompt"] == "P: Q1"
          and suite["cases"][0]["expect"] == {"equals": "A1"},
          "build applies the preamble and wraps expect")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump({"schema": 1, "name": "n", "prompt_preamble": "P: ",
                    "case_ids": ["gaia-l1-cccccccc"],
                    "excluded": {"reason": "r", "case_ids": []}}, fh)
        bg.MANIFEST = pathlib.Path(fh.name)
    missing = False
    try:
        bg.build(pathlib.Path("/nonexistent"))
    except SystemExit:
        missing = True
    check(missing, "a manifest case absent from the GAIA copy is refused")

    # Fresh module: load_validation is stubbed above, so the stub would answer.
    fresh = iu.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    absent = False
    try:
        fresh.load_validation(pathlib.Path(tempfile.mkdtemp()))
    except SystemExit:
        absent = True
    check(absent, "a GAIA root without metadata.parquet is refused")


def main() -> int:
    m = json.loads(MANIFEST.read_text())

    check(set(m) <= ALLOWED_KEYS,
          f"no unexpected top-level keys (extra: {sorted(set(m) - ALLOWED_KEYS)})")

    ids = m["case_ids"]
    check(all(CASE_ID.match(c) for c in ids), "every case_id is a bare identifier")
    check(len(ids) == len(set(ids)), "case_ids are unique")
    check(all(CASE_ID.match(c) for c in m["excluded"]["case_ids"]),
          "every excluded id is a bare identifier")

    # Pin every key except the two identifier lists: naming fields to add back
    # is an enumeration, and it missed excluded's other nested keys.
    meta = {k: v for k, v in m.items() if k != "case_ids"}
    meta["excluded"] = {k: v for k, v in m["excluded"].items() if k != "case_ids"}
    meta_digest = hashlib.sha256(
        json.dumps(meta, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    check(meta_digest == METADATA_SHA256,
          "all manifest metadata is byte-identical to the pinned text")

    # Pinned by digest, not shape: shape predicates constrained only the tail,
    # so any text before "Question: " passed.
    pre = m["prompt_preamble"]
    check(hashlib.sha256(pre.encode()).hexdigest() == PREAMBLE_SHA256,
          "preamble is byte-identical to the pinned text (no smuggled content)")
    check(pre.endswith("Question: "),
          "preamble ends at the question boundary")

    check_collision_guard()
    check_excluded_subtree_pinned()
    check_builder_paths()

    print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + '; '.join(failures)}"
          f" ({ran - len(failures)}/{ran})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
