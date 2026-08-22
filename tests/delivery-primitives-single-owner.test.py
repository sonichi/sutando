#!/usr/bin/env python3
"""Convergence gate ② prerequisite (issue #3279, construction layer):
delivery primitives have exactly one owner definition each.

An adapter that re-defines DeliveryOutcome, a ClaimBackend, or the marker
parser has forked delivery semantics — the private-copy drift class that
shipped real bugs (marker parsers that leaked control text; sparrow copies
whose drift surfaced as coverage failures). This test enumerates DEFINITION
sites by AST across src/ and the sparrow package: the owner set is pinned,
any new site fails by path, and the vendored twins must stay byte-identical
so "the copy" can never quietly become "a second implementation".
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def rel(p: Path) -> str:
    return str(p.relative_to(REPO))


# ── enumerate definition sites over every production module ────────────────
scan_files = [p for p in list((REPO / "src").glob("*.py")) + list(PKG.rglob("*.py"))
              if ".test" not in p.name]

sites: dict[str, set[str]] = {"DeliveryOutcome": set(), "ClaimBackend": set(),
                              "parse_markers": set()}
for f in scan_files:
    try:
        tree = ast.parse(f.read_text(), filename=str(f))
    except SyntaxError:
        failures.append(f"unparseable production module: {rel(f)}")
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if node.name == "DeliveryOutcome":
                sites["DeliveryOutcome"].add(rel(f))
            if node.name.endswith("ClaimBackend"):
                sites["ClaimBackend"].add(rel(f))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "parse_markers":
                sites["parse_markers"].add(rel(f))

OWNERS = {
    "DeliveryOutcome": {
        "src/outbox.py",
        "packages/ag2-sparrow/ag2_sparrow/outbox.py",
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/contract.py",
    },
    "ClaimBackend": {
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/contract.py",
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/backend_a.py",
        "packages/ag2-sparrow/ag2_sparrow/delivery_core/backend_c.py",
    },
    "parse_markers": {
        "src/result_markers.py",
        "packages/ag2-sparrow/ag2_sparrow/result_markers.py",
    },
}

for prim, owners in OWNERS.items():
    found = sites[prim]
    extra = found - owners
    missing = owners - found
    check(not extra,
          f"{prim}: no definition outside its owners (new: {sorted(extra)})")
    check(not missing,
          f"{prim}: every pinned owner still defines it (gone: {sorted(missing)})")

# ── vendored twins are byte-identical (drift = a second implementation) ────
for name in ("outbox.py", "outbox_adapter.py", "result_markers.py"):
    a, b = REPO / "src" / name, PKG / name
    check(a.exists() and b.exists() and a.read_bytes() == b.read_bytes(),
          f"vendored twin byte-identical: src/{name} == ag2_sparrow/{name}")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
