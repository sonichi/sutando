#!/usr/bin/env python3
"""Alias: `current-track-write.py append <file>`. Kept so the shorter name keeps working."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("current_track_write", Path(__file__).with_name("current-track-write.py"))
_mod = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_mod)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return _mod.main(["append", *argv]) if len(argv) == 1 else _mod.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
