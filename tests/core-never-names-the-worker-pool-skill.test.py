#!/usr/bin/env python3
"""Core never names the worker-pool skill (docs/architecture-boundaries.md,
"Optional adapter capabilities"): a core helper may run an injected script
path, but it must not locate a concrete optional skill. A fallback such as
`${SUTANDO_X:-$REPO/skills/worker-pool/...}` is exactly such a location.

Run: python3 tests/core-never-names-the-worker-pool-skill.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = (REPO / "src", REPO / "packages" / "ag2-sparrow" / "ag2_sparrow")
NEEDLE = "skills/worker-pool"


def _is_test_file(p: Path) -> bool:
    return "tests" in p.parts or p.name.startswith("test_") or p.name.endswith(".test.py")


def offenders() -> list[str]:
    hits = []
    for root in ROOTS:
        for p in root.rglob("*"):
            if not p.is_file() or _is_test_file(p) or p.suffix in {".pyc", ".png", ".db"}:
                continue
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if NEEDLE in line:
                    hits.append(f"{p.relative_to(REPO)}:{n}: {line.strip()}")
    return hits


def main() -> int:
    # Positive control: the probe sees the literal it guards against.
    assert NEEDLE in 'x="${SUTANDO_POOL_DELIVERY_SCRIPT:-$REPO/skills/worker-pool/scripts/pool_delivery.py}"'
    hits = offenders()
    for h in hits:
        print("FAIL  " + h)
    print("  ok  core names no worker-pool skill path" if not hits else f"{len(hits)} core file(s) locate the worker-pool skill")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
