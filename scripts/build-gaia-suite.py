#!/usr/bin/env python3
"""Rebuild the gaia-100 suite from a local GAIA copy.

The repo stores case IDENTIFIERS only. Questions and answers stay in the
dataset you downloaded yourself, so publishing this repo never republishes
the GAIA answer key -- which would contaminate the benchmark for everyone.

Usage:
  python3 scripts/build-gaia-suite.py --gaia-root <dir> [--out benchmarks/gaia-100.json]

<dir> is a GAIA snapshot containing 2023/validation/metadata.parquet, e.g. the
HuggingFace cache path for gaia-benchmark/GAIA. Requires pyarrow.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# Repo-relative data, not workspace state: the manifest ships beside this script.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "benchmarks" / "gaia-100.manifest.json"


def case_id(row: dict) -> str:
    return f"gaia-l{row['Level']}-{row['task_id'][:8]}"


def load_validation(gaia_root: pathlib.Path) -> list[dict]:
    meta = gaia_root / "2023" / "validation" / "metadata.parquet"
    if not meta.exists():
        sys.exit(f"no metadata.parquet under {gaia_root} -- expected {meta}")
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("pyarrow is required to read the GAIA metadata: pip install pyarrow")
    return pq.read_table(meta).to_pylist()


def build(gaia_root: pathlib.Path) -> dict:
    manifest = json.loads(MANIFEST.read_text())
    validation = load_validation(gaia_root)
    rows: dict[str, dict] = {}
    collisions: dict[str, list[str]] = {}
    for r in validation:
        cid = case_id(r)
        if cid in rows:
            # Derived ids truncate task_id to 8 hex chars; a collision would let
            # one row silently displace another's question and answer.
            collisions.setdefault(cid, [rows[cid]["task_id"]]).append(r["task_id"])
        rows[cid] = r
    if collisions:
        detail = "; ".join(f"{c} <- {', '.join(ids)}" for c, ids in sorted(collisions.items()))
        sys.exit(f"case-id collision: derived ids are not injective over this GAIA copy: {detail}")

    missing = [cid for cid in manifest["case_ids"] if cid not in rows]
    if missing:
        sys.exit(f"{len(missing)} manifest case(s) absent from this GAIA copy: {missing[:5]}")

    cases = []
    for cid in manifest["case_ids"]:
        row = rows[cid]
        cases.append({
            "id": cid,
            "category": f"gaia-l{row['Level']}",
            "prompt": manifest["prompt_preamble"] + row["Question"],
            "expect": {"equals": row["Final answer"]},
        })
    return {
        "schema": manifest["schema"],
        "name": manifest["name"],
        "description": ("100 file-less GAIA validation cases, deterministically selected, "
                        "excluding any whose answer entered the operator's context."),
        "cases": cases,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaia-root", required=True, type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path,
                    default=REPO_ROOT / "benchmarks" / "gaia-100.json")
    args = ap.parse_args()

    suite = build(args.gaia_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(suite, indent=2) + "\n")
    print(f"wrote {args.out} -- {len(suite['cases'])} cases")


if __name__ == "__main__":
    main()
