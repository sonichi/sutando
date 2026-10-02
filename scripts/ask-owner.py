#!/usr/bin/env python3
"""Ask the owner a question he will actually see, and record it.

Usage:
  python3 scripts/ask-owner.py "Merge #123 despite the absent CLA check?" \
      [--context "why it is blocked"] [--urgency live|durable] \
      [--task-file <workspace>/tasks/task-<id>.txt] \
      [--default "merge it" --reason "CI is green" --option "Hold=wait for the CLA"] \
      [--priority High|Medium|Low] [--store-adapter <path>]

With --task-file the question goes to that task's own conversation; without it,
to the owner's DM on the bridge he was last active on. Always exits 0 after a
non-empty question: every failure is printed, never raised. The ask itself belongs
to the store adapter an installed skill declares (--store-adapter overrides the
discovery): it records the question as a row of the owner's room database, holding
it in the workspace outbox while the room is unreachable. With no adapter this
entry does only core's part — one outbox record and the queued proactive message —
and says so.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
import pending_questions_ask as core_ask
import pending_questions_reader as reader


def _option(text: str):
    label, sep, consequence = text.partition("=")
    if not sep or not label.strip():
        raise argparse.ArgumentTypeError("an option is 'Label=what it does'")
    return label.strip(), consequence.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send a pending question to the owner and record it.")
    ap.add_argument("question")
    ap.add_argument("--context", default=None, help="one or two lines of why / options")
    ap.add_argument("--urgency", choices=("live", "durable"), default="live",
                    help="live also fires the macOS notification; durable sends and records only")
    ap.add_argument("--task-file", default=None,
                    help="the task being worked: its source/channel is where the question goes")
    ap.add_argument("--default", "--default-action", dest="default", default=None,
                    help="the action taken on Approve")
    ap.add_argument("--reason", default=None, help="why that is the proposed default")
    ap.add_argument("--option", action="append", type=_option, default=[],
                    help="'Label=what it does', repeatable; Approve is always listed first")
    ap.add_argument("--priority", choices=("High", "Medium", "Low"), default="Medium")
    ap.add_argument("--store-adapter", default=None,
                    help="a store adapter file; default: the one an installed skill declares")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not args.question.strip():
        print("ask-owner: the question is empty", file=sys.stderr)
        return 2
    if args.workspace:
        ws = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        ws = resolve_workspace(migrate=False)
    kw = dict(context=args.context, urgency=args.urgency, task_file=args.task_file, workspace=ws,
              default_action=args.default, reason=args.reason, options=args.option, priority=args.priority)
    mod, why = reader._adapter(args.store_adapter)
    if mod is not None and hasattr(mod, "ask_owner"):
        store, where = mod.room_store(ws)
        if store is None:
            print(f"room database: not used ({where}); the question is held in the outbox")
        out = mod.ask_owner(args.question, store=store, **kw)
        lines = mod.report_lines(out)
    else:
        print(f"room database: not used ({why}); the question is held in the outbox")
        out = core_ask.ask_owner(args.question, **kw)
        lines = core_ask.report_lines(out)
    if out.get("outbox"):
        print(f"ask-owner: ROOM DATABASE WRITE FAILED ({out['db_error']}); the question is held in "
              f"{out['outbox']} until the next reconcile", file=sys.stderr)
    elif out.get("record") is None:
        print(f"ask-owner: NOT RECORDED ({out.get('db_error')}); the owner is asked but nothing holds the "
              "question — ask by hand if he does not answer", file=sys.stderr)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
