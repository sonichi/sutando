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
from task_archive import task_id_from_filename  # noqa: E402
from task_workstreams import (  # noqa: E402
    apply_inference,
    build_classifier_snapshot,
    build_workstream_context,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="print the current inert classifier snapshot")
    context_parser = sub.add_parser(
        "context", help="print bounded prior context for an assigned task"
    )
    context_parser.add_argument("task", help="task id or task filename")
    context_parser.add_argument("--limit", type=int, default=5)
    apply_parser = sub.add_parser("apply", help="validate and atomically apply inferred groups")
    apply_parser.add_argument("file", help="proposal JSON path, or - for stdin")
    args = parser.parse_args(argv)

    workspace = resolve_workspace(REPO)
    if args.command == "snapshot":
        print(json.dumps(build_classifier_snapshot(workspace), ensure_ascii=False, indent=2))
        return 0
    if args.command == "context":
        # A bare ID passed by the CLI has no suffix to strip, so the owner
        # returns None and the stem fallback preserves that contract.
        task_id = task_id_from_filename(Path(args.task).name) or Path(args.task).stem
        context = build_workstream_context(workspace, task_id, limit=args.limit)
        if context is not None:
            sys.stdout.write(json.dumps(context, ensure_ascii=False, separators=(",", ":")))
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
