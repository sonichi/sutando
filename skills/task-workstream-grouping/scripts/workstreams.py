#!/usr/bin/env python3
"""Snapshot and apply Sutando's model-inferred task workstreams."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from sutando_config import resolve_workspace  # noqa: E402
from task_workstreams import apply_inference, build_classifier_snapshot  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="print the current inert classifier snapshot")
    apply_parser = sub.add_parser("apply", help="validate and atomically apply inferred groups")
    apply_parser.add_argument("file", help="proposal JSON path, or - for stdin")
    args = parser.parse_args(argv)

    workspace = resolve_workspace(REPO)
    if args.command == "snapshot":
        print(json.dumps(build_classifier_snapshot(workspace), ensure_ascii=False, indent=2))
        return 0

    try:
        raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        proposal = json.loads(raw)
        result = apply_inference(workspace, proposal)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"task-workstream-grouping: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
