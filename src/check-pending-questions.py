#!/usr/bin/env python3
"""Thin entry for the pending-questions reminder: hands argv to the `remind` of the adapter
an installed skill declares (`--store-adapter <path>` overrides), resolved here across the
installed roots (src/skill_roots.py) and injected into src/pending_questions_reader.py. The
pass itself — reconcile, list, and with `--notify` the reminder — is the skill's. Without an
adapter nothing can be reminded: the held questions are listed and nothing is sent, which is
also what a flagless run does.
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pending_questions_reader as reader
from skill_roots import declared


def _takes_resolved(remind) -> bool:
    """Whether the adapter's `remind` accepts the `resolved` keyword (a two-arg one still works)."""
    try:
        params = inspect.signature(remind).parameters
    except (TypeError, ValueError):
        return False
    return "resolved" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    override = argv[argv.index("--store-adapter") + 1] if "--store-adapter" in argv[:-1] else None
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    workspace = resolve_workspace(migrate=False)
    store = reader.resolve_adapter(declared(reader.DECLARATION, workspace, override=override))
    mod, why = store  # resolved once for this invocation; carried through, never resolved again
    if mod is not None and hasattr(mod, "remind"):
        if _takes_resolved(mod.remind):
            return int(mod.remind(argv, workspace, resolved=store) or 0)
        return int(mod.remind(argv, workspace) or 0)
    g = reader.gather(workspace, store)
    for note in g["notes"]:
        print(note, file=sys.stderr)
    if g["unavailable"]:
        print(reader.unknown_line(g))
    else:
        print(f"{len(g['waiting'])} pending questions; nothing sent (no store adapter with a reminder: {why})")
    for q in g["waiting"]:
        print(f"- [{q['ask_id']}] {q['title']}" + ("" if q.get("in_room", True) else " (not yet in the room)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
