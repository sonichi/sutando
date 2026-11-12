#!/usr/bin/env python3
"""Ask the owner a question he will actually see, and record it.

Usage:
  python3 scripts/ask-owner.py "Merge #123 despite the absent CLA check?" \
      [--context "why it is blocked"] [--urgency live|durable] \
      [--task-file <workspace>/tasks/task-<id>.txt] \
      [--default "merge it" --reason "CI is green" --option "Hold=wait for the CLA"] \
      [--priority High|Medium|Low] [--unless-open] [--store-adapter <path>] \
      [--ref 'label=#123;url=https://…;title=…;state=ready' --effect "Hold=nothing merges" --primary "Hold"] \
      [--open-question]

A question with a known set of answers passes one --option per answer; --context is the why,
never the choices. A question or context that reads as a list of choices ("(1) … (2) …",
"Options:", "A) … B)", "yes/no", "X or Y?") with no --option is refused (exit 2) and nothing is
asked; --open-question says the answer really is free text. The question's first sentence is
the card's headline: keep it short and put the background in --context.

The ask belongs to the store adapter an installed skill declares, resolved here across the
installed roots (src/skill_roots.py; --store-adapter overrides): with --task-file it routes
the question to that task's own conversation, records it as a row of the owner's room
database, holds it in the workspace outbox while the room is unreachable, and reminds.
With --default and/or --option, a question bound for AG2 Space is asked as a decision card
(one button per choice) instead of text; the owner's click comes back as a `hitl_click: true`
task, and `python3 src/pending_questions_reader.py clicks` hands it to the core.
The buttons are the --option labels, in the order given. A --default that names one of them
(case and outer whitespace ignored) marks that option as the proposed one and adds no button;
any other --default is its own first button, Approve.
Optional, card only: --ref (repeatable, at most 20) is `key=value` pairs joined by `;` — label
and url (https) required; title, state (ready|waiting|open|merged|closed), state_label and group
optional; a value holds no `;`. --effect 'Label=what clicking it does' (repeatable) and
--primary 'Label' name a button by its label. The refs are also listed as links, one per line,
wherever the question is asked as text.
Always exits 0 after a non-empty question: every failure is printed, never raised. With
--unless-open, a question whose title matches one the store holds open is reported
(`already open: [<id>]`) and not filed again, so a carried list can be filed without doubling rows.

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
from agent_local_name import local_name
from local_record import RecordDir, iso, new_name, write_text_whole
from owner_question import MAX_REFS, Question, QuestionError, enumerated_choice, parse_ref, refs_from  # noqa: F401
from proactive_routing import proactive_filename
from result_markers import neutralize_markers
from skill_roots import declared

RECORDS = "ask-owner"


def _option(text: str):
    label, sep, consequence = text.partition("=")
    if not sep or not label.strip():
        raise argparse.ArgumentTypeError("an option is 'Label=what it does'")
    return label.strip(), consequence.strip()


def _ref(text: str) -> dict:
    try:
        return parse_ref(text).to_wire()
    except QuestionError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _effect(text: str):
    label, sep, what = text.partition("=")
    if not sep or not label.strip() or not what.strip():
        raise argparse.ArgumentTypeError("an effect is 'Option label=what clicking it does'")
    return label.strip(), what.strip()


def ask_without_store(question: str, context, ws: Path, why: str, refs=()) -> list:
    """Core's part alone: the DM through the proactive path, then one generic local record.
    Returns the report lines; nothing raises past here."""
    from workspace_default import status_path  # noqa: PLC0415 — heavy loader
    name = new_name("ask")
    title = " ".join(neutralize_markers(question).split())[:120]
    asked = Question.from_text(question, context, asker=local_name(workspace=ws), refs=refs_from(refs))
    body = "[dm-only]\n" + neutralize_markers(asked.text())
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
    ap.add_argument("--context", default=None, help="one or two lines of why; the choices go in --option")
    ap.add_argument("--urgency", choices=("live", "durable"), default="live",
                    help="live also fires the macOS notification; durable sends and records only")
    ap.add_argument("--task-file", default=None,
                    help="the task being worked: its source/channel is where the question goes")
    ap.add_argument("--default", "--default-action", dest="default", default=None,
                    help="the action taken on Approve, or the label of the --option that is proposed")
    ap.add_argument("--reason", default=None, help="why that is the proposed default")
    ap.add_argument("--option", action="append", type=_option, default=[],
                    help="'Label=what it does', repeatable; the buttons, in this order")
    ap.add_argument("--ref", action="append", type=_ref, default=[],
                    help="'label=#123;url=https://…[;title=…;state=…;state_label=…;group=…]', repeatable")
    ap.add_argument("--effect", action="append", type=_effect, default=[],
                    help="'Option label=what clicking it does', repeatable")
    ap.add_argument("--primary", default=None, help="the label of the button shown as primary")
    ap.add_argument("--open-question", action="store_true",
                    help="the answer is free text: ask as text even when the wording reads as a list of choices")
    ap.add_argument("--priority", choices=("High", "Medium", "Low"), default="Medium")
    ap.add_argument("--unless-open", action="store_true",
                    help="file nothing when an open question already has this title")
    ap.add_argument("--store-adapter", default=None,
                    help="a store adapter file; default: the one an installed skill declares")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not args.question.strip():
        print("ask-owner: the question is empty", file=sys.stderr)
        return 2
    if len(args.ref) > MAX_REFS:
        ap.error(f"at most {MAX_REFS} --ref, got {len(args.ref)}")
    # The store builds the buttons; the model only has to know whether the caller gave any.
    try:
        Question.from_text(args.question, args.context, refs=refs_from(args.ref)).validate(
            open_question=args.open_question or bool(args.option), proposes=bool((args.default or "").strip()))
    except QuestionError as e:
        ap.error(f"{e}. As flags: pass --option 'Label=what it does' once per answer, --context for "
                 "the why, --open-question for free text")
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
        print("\n".join(ask_without_store(args.question, args.context, ws, why, args.ref)))
        return 0
    dup = reader.open_duplicate(ws, args.question, mod) if args.unless_open else None
    if dup is not None:
        print(f"already open: [{dup.get('ask_id')}] {dup.get('title')} — not filed again")
        return 0
    kw = dict(context=args.context, urgency=args.urgency, task_file=args.task_file, workspace=ws,
              default_action=args.default, reason=args.reason, options=args.option, priority=args.priority)
    extra = {k: v for k, v in (("refs", args.ref), ("effects", args.effect), ("primary", args.primary)) if v}
    if extra and not hasattr(mod, "action_id"):
        print("ask-owner: this store adapter takes no --ref/--effect/--primary; asked without them", file=sys.stderr)
    elif extra:
        for label in [e[0] for e in args.effect] + ([args.primary] if args.primary else []):
            try:
                mod.action_id(args.default, args.option, label)
            except ValueError as e:
                ap.error(f"{e}; --effect and --primary name a button by its label")
        kw["extra"] = extra
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
