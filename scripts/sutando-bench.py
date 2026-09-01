#!/usr/bin/env python3
"""CLI shim for the importable sutando_bench module."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sutando_bench import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
