#!/usr/bin/env python3
"""Regression checks for scripts/gen-src-map.py."""

import importlib.util
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_src_map", REPO / "scripts" / "gen-src-map.py")
gen_src_map = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_src_map)

failures = []


def check(name: str, condition: bool) -> None:
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


rows = dict(gen_src_map.collect())
check(
    "Swift modules are indexed",
    {
        "src/Sutando/main.swift",
        "src/Sutando/SutandoConfig.swift",
        "src/scroll-wheel.swift",
    }.issubset(rows),
)
check(
    "TypeScript reference directive is skipped",
    rows["src/web-voice-transport.ts"].startswith("web-voice-transport —"),
)
check(
    "wrapped purpose is joined",
    rows["src/inject-framing.ts"].endswith("MatrixRTC conversation daemon)."),
)
check(
    "block-comment closer is stripped",
    rows["src/observability/claude/_map-util.ts"]
    == "Shared helpers for the Claude Code mappers.",
)
check(
    "Swift MARK header becomes a purpose",
    rows["src/Sutando/main.swift"] == "Sutando Drop Menu Bar App",
)

malformed = [
    (path, purpose)
    for path, purpose in rows.items()
    if "<reference" in purpose
    or purpose.endswith("*/")
    or re.search(r"(?:\b(?:the|and|a|an|to|from|into|of|for|with)|[,(:;—-])$", purpose)
    or purpose.count("(") != purpose.count(")")
    or purpose.count('"') % 2
    or purpose.count("`") % 2
]
check("generated purposes contain no obvious fragments", not malformed)

sync = subprocess.run(
    ["python3", str(REPO / "scripts" / "gen-src-map.py"), "--check"],
    cwd=REPO,
    capture_output=True,
    text=True,
)
check("docs/src-map.md matches generated output", sync.returncode == 0)

if failures:
    for path, purpose in malformed:
        print(f"  malformed {path}: {purpose}")
    if sync.returncode != 0:
        print(sync.stderr)
    print(f"Results: {len(failures)} failed")
    raise SystemExit(1)

print("Results: 7 passed")
