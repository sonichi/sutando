#!/usr/bin/env python3
"""CLI shim: the benchmark lives in the package (importable, so worker
processes and in-process coverage both work). See ag2_sparrow.bench_claim_backends."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"))  # lint-workspace-resolution: allow-repo-root (package import path, no per-user state)

from ag2_sparrow.bench_claim_backends import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
