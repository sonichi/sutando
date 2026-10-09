#!/usr/bin/env python3
"""Ask the owner a question he will actually see, and record it.

Usage:
  python3 scripts/ask-owner.py "Merge #123 despite the absent CLA check?" \
      [--context "why it is blocked"] [--urgency live|durable] \
      [--task-file <workspace>/tasks/task-<id>.txt] \
      [--default "merge it" --reason "CI is green" --option "Hold=wait for the CLA"] \
      [--priority High|Medium|Low] [--store-adapter <path>]

The ask belongs to the store adapter an installed skill declares, resolved here across the
installed roots (src/skill_roots.py; --store-adapter overrides): with --task-file it routes
the question to that task's own conversation, records it as a row of the owner's room
database, holds it in the workspace outbox while the room is unreachable, and reminds.
Always exits 0 after a non-empty question: every failure is printed, never raised.

With no adapter this entry does only what core can: it queues the question to the
owner's DM on the bridge he was last active on (the proactive path; --task-file and the
routing it implies need the skill) and keeps one generic local record of the ask under
`<workspace>/state/ask-owner/`, which nothing lists, counts, closes or reminds — the
report says so.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
import pending_questions_reader as reader  # noqa: E402
from local_record import RecordDir, iso, new_name, write_text_whole
from proactive_routing import proactive_filename
from result_markers import neutralize_markers
from skill_roots import declared
from util_paths import host_label

RECORDS = "ask-owner"


def _option(text: str):
    label, sep, consequence = text.partition("=")
    if not sep or not label.strip():
        raise argparse.ArgumentTypeError("an option is 'Label=what it does'")
    return label.strip(), consequence.strip()


def ask_without_store(question: str, context, ws: Path, why: str) -> list:
    """Core's part alone: the DM through the proactive path, then one generic local record.
    Returns the report lines; nothing raises past here."""
    from workspace_default import status_path  # noqa: PLC0415 — heavy loader
    name = new_name("ask")
    title = " ".join(neutralize_markers(question).split())[:120]
    body = f"[dm-only]\nQuestion for you from the {host_label()} core — it needs your word:\n\n" \
           f"{neutralize_markers(question).strip()}\n"
    if context and context.strip():
        body += f"\n{neutralize_markers(context).strip()}\n"
    body += "\nReply here.\n"
    lines = [f"recorded: NO STORE — {why}; nothing lists, counts, closes or reminds this question"]
    queued, send_error = None, None
    try:
        queued = write_text_whole(ws / "results" / proactive_filename(name, None), body)  # a drain claims it on sight
        lines.append(f"sent: queued owner-dm (last-active bridge) via results/{queued.name} "
                     "(a bridge drain delivers it; nothing re-raises it without the skill)")
    except Exception as e:  # noqa: BLE001 — the record below still says it was asked
        send_error = f"{type(e).__name__}: {e}"
        lines.append(f"sent: FAILED — {send_error} (ask by hand)")
    try:
        p = RecordDir(status_path(RECORDS, ws)).write(name, {
            "id": name, "title": title, "body": body, "created": iso(),
            "queued": queued.name if queued else None, "error": send_error})
        lines.append(f"record: {p}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"record: FAILED — {type(e).__name__}: {e} (NOT recorded anywhere; ask by hand)")
    return lines


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
    mod, why = reader._adapter(declared(reader.DECLARATION, ws, override=args.store_adapter))
    if mod is None or not hasattr(mod, "ask_owner"):
        why = why if mod is None else f"adapter {why} has no ask_owner"
        print(f"ask-owner: NO STORE ({why}); the owner is asked, and nothing holds the question — "
              "ask by hand if he does not answer", file=sys.stderr)
        print("\n".join(ask_without_store(args.question, args.context, ws, why)))
        return 0
    kw = dict(context=args.context, urgency=args.urgency, task_file=args.task_file, workspace=ws,
              default_action=args.default, reason=args.reason, options=args.option, priority=args.priority)
    store, where = mod.room_store(ws)
    if store is None:
        print(f"room database: not used ({where}); the question is held in the outbox")
    out = mod.ask_owner(args.question, store=store, **kw)
    lines = mod.report_lines(out)
    if out.get("outbox") and out.get("db_error"):
        print(f"ask-owner: ROOM DATABASE WRITE FAILED ({out['db_error']}); the question is held in "
              f"{out['outbox']} until the next reconcile", file=sys.stderr)
    elif out.get("outbox"):
        print(f"ask-owner: the row landed but its history was not committed ({out.get('history_error')}); "
              f"the question stays held in {out['outbox']} until the next reconcile", file=sys.stderr)
    elif out.get("record") is None:
        print(f"ask-owner: NOT RECORDED ({out.get('db_error')}); the owner is asked but nothing holds the "
              "question — ask by hand if he does not answer", file=sys.stderr)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
