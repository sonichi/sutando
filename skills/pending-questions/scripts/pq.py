#!/usr/bin/env python3
"""Owner pending questions, one CLI:

  pq.py ask "<question>" [--context ..] [--urgency live|durable] [--task-file ..]
        [--default-action ..] [--reason ..] [--option 'Label=what it does'] [--priority ..]
  pq.py list [--json]              # what is waiting on the owner (the reminder's set)
  pq.py resolve <ask-id> [--answered]
  pq.py remind [args]              # src/check-pending-questions.py, args passed through

Every verb delegates: `ask` to scripts/ask-owner.py, `list` to the reminder's
gather(), `resolve` to FileStore.set_status and RoomDbStore.close, `remind` to
the reminder itself. This file injects its sibling room-database adapter; the
file ledger works without it.
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


def _room_store(ws: Path):
    from pending_questions_room_db import room_store  # noqa: PLC0415 — optional capability
    return room_store(ws)


def _with_adapter(args: list) -> list:
    return args if "--store-adapter" in args else [*args, "--store-adapter", str(ADAPTER)]


def cmd_ask(args: list) -> int:
    return _load("pq_ask_owner", ASK_OWNER).main(_with_adapter(args))


def cmd_list(args: list) -> int:
    ap = argparse.ArgumentParser(prog="pq.py list")
    ap.add_argument("--json", action="store_true")
    opts = ap.parse_args(args)
    from pending_questions_store import entry_ask_id
    cpq = _load("pq_reminder", REMINDER)
    store, where = _room_store(cpq.WORKSPACE)
    if store is None:
        print(f"room database: not used ({where}); listing from the file", file=sys.stderr)
    questions, notes = cpq.gather(store)
    for note in notes:
        print(note, file=sys.stderr)
    items = [{"ask_id": q.get("ask_id") or entry_ask_id(q.get("body", "")), "title": q["title"],
              "snippet": q.get("snippet", ""), "body": q.get("body", "")} for q in questions]
    if opts.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0
    if not items:
        print(cpq.zero_reason())
        return 0
    print(f"{len(items)} waiting on the owner:")
    for it in items:
        print(f"- [{it['ask_id'] or 'no ask id'}] {it['title']}")
        if it["snippet"] and it["snippet"] != it["title"]:
            print(f"    {it['snippet']}")
    return 0


def cmd_resolve(args: list) -> int:
    ap = argparse.ArgumentParser(prog="pq.py resolve")
    ap.add_argument("ask_id")
    ap.add_argument("--answered", action="store_true", help="mark it Answered, not Resolved")
    ap.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    opts = ap.parse_args(args)
    from pending_questions_ask import ledger_path
    from pending_questions_store import FileStore
    from util_paths import host_label
    if opts.workspace:
        ws = Path(opts.workspace)
    else:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        ws = resolve_workspace(migrate=False)
    status = "Answered" if opts.answered else "Resolved"
    closed = 0
    pq = ledger_path(ws, host_label())
    try:
        FileStore(pq).set_status(opts.ask_id, status)
        print(f"file: {opts.ask_id} -> {status} in {pq}")
        closed += 1
    except Exception as e:  # noqa: BLE001 — the database may still hold it
        print(f"file: not changed — {type(e).__name__}: {e}")
    store, where = _room_store(ws)
    if store is None:
        print(f"room database: not used ({where})")
    else:
        try:
            store.close(opts.ask_id, status)
            print(f"room database: {opts.ask_id} -> {status} in {store.where(opts.ask_id)}")
            closed += 1
        except Exception as e:  # noqa: BLE001
            print(f"room database: not changed — {type(e).__name__}: {e}")
    return 0 if closed else 1


def cmd_remind(args: list) -> int:
    return subprocess.run([sys.executable, str(REMINDER), *_with_adapter(args)]).returncode


VERBS = {"ask": cmd_ask, "list": cmd_list, "resolve": cmd_resolve, "remind": cmd_remind}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in VERBS:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return VERBS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
