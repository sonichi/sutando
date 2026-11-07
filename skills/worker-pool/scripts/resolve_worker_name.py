#!/usr/bin/env python3
"""Resolve a worker name said aloud to the roster recipient it names.

Prints one JSON object: {"worker": id} or {"error": "unknown"|"ambiguous"|"no-roster",
"known": [names]}. Exit 0 either way; the voice tool reads the verdict.

Run: python3 skills/worker-pool/scripts/resolve_worker_name.py "<name>" [--workspace W]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_roster as pr  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name")
    ap.add_argument("--workspace", default=None)
    args = ap.parse_args(argv)
    roster = pr.load_roster(args.workspace)
    verdict = {"error": "no-roster", "known": []} if roster is None else pr.resolve_spoken_name(roster, args.name)
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
