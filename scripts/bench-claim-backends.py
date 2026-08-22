#!/usr/bin/env python3
"""CLI shim for scripts/bench_claim_backends.py (importable module name —
spawn workers and in-process coverage both need one)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_claim_backends import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
