#!/usr/bin/env python3
"""Owner pending questions, one CLI:

  pq.py ask "<question>" [--context ..] [--urgency live|durable] [--task-file ..]
        [--default-action ..] [--reason ..] [--option 'Label=what it does'] [--priority ..]
  pq.py list [--json]              # what is waiting on the owner — read-only, no reconcile
  pq.py reconcile                  # the explicit pass: outbox replay, local closes, legacy ingest
  pq.py resolve <ask-id> [--answered]
  pq.py remind [--force]           # the reminder (--notify) through src/check-pending-questions.py

Every verb delegates: `ask` to scripts/ask-owner.py with this skill's adapter injected,
`list`, `reconcile` and `resolve` to the sibling room-database adapter (the one reader
and writer), `remind` to the reminder entry with that adapter injected.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))
ADAPTER = HERE / "pending_questions_room_db.py"
REMINDER = REPO / "src" / "check-pending-questions.py"
ASK_OWNER = REPO / "scripts" / "ask-owner.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workspace(opt):
    if opt:
        return Path(opt)
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    return resolve_workspace(migrate=False)


def _with_adapter(args: list) -> list:
    return args if "--store-adapter" in args else [*args, "--store-adapter", str(ADAPTER)]


def cmd_ask(args: list) -> int:
    return _load("pq_ask_owner", ASK_OWNER).main(_with_adapter(args))


def _unknown(g: dict) -> str:
    return f"pending questions: UNKNOWN — room unreachable ({g['reason']}); {len(g['waiting'])} held locally"


def cmd_list(args: list) -> int:
    ap = argparse.ArgumentParser(prog="pq.py list")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    opts = ap.parse_args(args)
    import pending_questions_room_db as adapter  # noqa: PLC0415
    g = adapter.gather(_workspace(opts.workspace))
    for note in g["notes"]:
        print(note, file=sys.stderr)
    items = g["waiting"]
    if opts.json:
        print(json.dumps({"unavailable": True, "reason": g["reason"], "waiting": items} if g["unavailable"]
                         else items, ensure_ascii=False, indent=1))
        return 0
    if g["unavailable"]:
        print(_unknown(g))
    elif not items:
        print("0 pending questions" + (f" in {g['store']}" if g.get("store") else " (no room database; the outbox is empty)"))
        return 0
    else:
        print(f"{len(items)} waiting on the owner:")
    for it in items:
        print(f"- [{it['ask_id']}] {it['title']}" + ("" if it.get("in_room", True) else " (not yet in the room)"))
        if it["snippet"] and it["snippet"] != it["title"]:
            print(f"    {it['snippet']}")
    return 0


def cmd_reconcile(args: list) -> int:
    ap = argparse.ArgumentParser(prog="pq.py reconcile")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    opts = ap.parse_args(args)
    import pending_questions_room_db as adapter  # noqa: PLC0415
    rec = adapter.reconcile_pass(_workspace(opts.workspace))
    for err in rec["errors"]:
        print(f"reconcile: FAILED — {err}")
    print(f"reconcile: filed {len(rec['flushed'])} held, applied {len(rec['closed'])} local close(s), "
          f"moved {len(rec['moved'])} legacy entr(ies)")
    return 1 if rec["errors"] else 0


def cmd_resolve(args: list) -> int:
    ap = argparse.ArgumentParser(prog="pq.py resolve")
    ap.add_argument("ask_id")
    ap.add_argument("--answered", action="store_true", help="mark it Answered, not Resolved")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    opts = ap.parse_args(args)
    import pending_questions_room_db as adapter  # noqa: PLC0415
    ok, msg = adapter.resolve(_workspace(opts.workspace), opts.ask_id, "Answered" if opts.answered else "Resolved")
    print(msg)
    return 0 if ok else 1


def cmd_remind(args: list) -> int:
    args = args if "--notify" in args else ["--notify", *args]
    return subprocess.run([sys.executable, str(REMINDER), *_with_adapter(args)]).returncode


VERBS = {"ask": cmd_ask, "list": cmd_list, "reconcile": cmd_reconcile, "resolve": cmd_resolve, "remind": cmd_remind}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in VERBS:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return VERBS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
