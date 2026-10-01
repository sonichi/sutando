#!/usr/bin/env python3
"""Ask the owner a question he will actually see, and keep the ledger.

Usage:
  python3 scripts/ask-owner.py "Merge #123 despite the absent CLA check?" \
      [--context "why it is blocked"] [--urgency live|durable] \
      [--task-file <workspace>/tasks/task-<id>.txt]

With --task-file the question goes to that task's own conversation; without it,
to the owner's DM on the bridge he was last active on. Always exits 0 after a
non-empty question: every failure is printed, never raised, and the ledger
entry in hosts/<host>/pending-questions.md stands either way.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # lint-workspace-resolution: allow-repo-root
from pending_questions_ask import ask_owner, report_lines  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Send a pending question to the owner and record it.")
    ap.add_argument("question")
    ap.add_argument("--context", default=None, help="one or two lines of why / options")
    ap.add_argument("--urgency", choices=("live", "durable"), default="live",
                    help="live also fires the macOS notification; durable sends and records only")
    ap.add_argument("--task-file", default=None,
                    help="the task being worked: its source/channel is where the question goes")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if not args.question.strip():
        print("ask-owner: the question is empty", file=sys.stderr)
        return 2
    out = ask_owner(args.question, context=args.context, urgency=args.urgency,
                    task_file=args.task_file,
                    workspace=Path(args.workspace) if args.workspace else None)
    print("\n".join(report_lines(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
