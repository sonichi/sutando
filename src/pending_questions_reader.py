"""The one way core reads owner pending questions: through the store adapter an installed
skill declares in its manifest (`pending_questions_store`), loaded by the path an edge injects —
core names no skill, scans no root and carries no question schema. The edge (a thin entry, a
dashboard, agent-api) resolves that path with `skill_roots.declared(DECLARATION, workspace)`
(both installed roots; a conflict is a refusal, not a pick) or takes a `--store-adapter` flag,
and passes the result as `adapter` to every call here.

`gather(workspace, adapter)` returns {"waiting": [items], "done": n | None, "pending_close": [ids],
"unavailable": bool, "reason": str | None, "link": str | None, "notes": [...], "store": where}.
A store that cannot be read is NEVER a measured zero: `unavailable` is True, `done` is None.
With no adapter there is no store to read, so the result is unavailable with that reason —
what the adapter holds locally, lists or counts is the adapter's to say. `waiting`, `count` and
`resolve` are the thin views every core reader uses; a read never reconciles (`reconcile_pass`
does, on demand; the two are the adapter's separate entry points). Never read any file for this.

`resolve` closes through the adapter; with no adapter, or an adapter that raises, it returns
(False, why) and records nothing — the caller keeps the answer it holds (agent-api files the
answer task before it asks for the close).

CLI, for shell readers: `python3 src/pending_questions_reader.py list [--json] | count`; and the
one write a core caller makes through the same contract, `resolve <ask-id> [--answered]`
(Resolved, or Answered), which is `resolve` above and nothing else. `--store-adapter <path>`
injects an adapter file in place of the declared one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from skill_roots import declared

# The manifest field an installed skill declares its adapter script with.
DECLARATION = "pending_questions_store"


_LOADED: dict = {}


def load_adapter(adapter):
    """The adapter module from its file, executed once per process; None when there is none."""
    if not adapter:
        return None
    path = Path(adapter).resolve()
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)  # the same file once; a rewritten file is new
    if key not in _LOADED:
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("pq_store_adapter", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LOADED[key] = mod
    return _LOADED[key]


NO_ADAPTER = "no pending-questions store adapter installed (no skill declares one)"


def _adapter(adapter=None):
    """(module, path) or (None, why). `adapter` is what the edge injected: a script path, or the
    skill_roots.Declaration it resolved; None, or a declaration of none, is NO_ADAPTER exactly."""
    if hasattr(adapter, "gather"):  # an already-loaded module: no second execution
        return adapter, getattr(adapter, "__file__", "<module>")
    path, why = adapter if isinstance(adapter, tuple) else (adapter, None)  # a Declaration is a tuple
    if not path:
        return None, why or NO_ADAPTER
    try:
        return load_adapter(path), str(path)
    except Exception as e:  # noqa: BLE001
        return None, f"adapter {path} failed to load ({type(e).__name__}: {e})"


def _unavailable(reason: str) -> dict:
    return {"waiting": [], "done": None, "pending_close": [], "unavailable": True, "reason": reason,
            "link": None, "store": None, "notes": [f"PENDING QUESTIONS UNAVAILABLE ({reason}); the count is unknown"]}


def gather(workspace, adapter=None) -> dict:
    mod, why = _adapter(adapter)
    if mod is None:
        return _unavailable(why)
    try:
        g = mod.gather(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return _unavailable(f"adapter failed: {type(e).__name__}: {e}")
    g.setdefault("unavailable", False)
    g.setdefault("reason", None)
    g.setdefault("link", None)
    g.setdefault("pending_close", [])
    return g


def waiting(workspace, adapter=None) -> list:
    return gather(workspace, adapter)["waiting"]


def count(workspace, adapter=None) -> dict:
    """{"open": n | None, "done": n | None, "pending_close": n, "unavailable", "reason"}; None is
    unknown, never 0."""
    g = gather(workspace, adapter)
    return {"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
            "pending_close": len(g["pending_close"]), "unavailable": g["unavailable"], "reason": g["reason"]}


def reconcile_pass(workspace, adapter=None) -> dict:
    """The explicit pass: outbox replay, local closes, the legacy ingest; {"errors": [why]} without a store."""
    mod, why = _adapter(adapter)
    if mod is None or not hasattr(mod, "reconcile_pass"):
        return {"flushed": [], "moved": [], "closed": [], "errors": [f"no store to reconcile with: {why}"]}
    try:
        return mod.reconcile_pass(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return {"flushed": [], "moved": [], "closed": [], "errors": [f"adapter failed: {type(e).__name__}: {e}"]}


def reconcile_then_gather(workspace, adapter=None) -> dict:
    """The reminder's pass: `reconcile_pass`, its errors as notes, then `gather` — one load, one
    failure policy (a raised step is a note or UNKNOWN, never a traceback)."""
    rec = reconcile_pass(workspace, adapter)
    g = gather(workspace, adapter)
    g["notes"] = [f"reconcile: FAILED — {e}" for e in rec.get("errors", [])] + list(g.get("notes", []))
    return g


def resolve(workspace, ask_id: str, status: str, adapter=None) -> tuple:
    """(closed, message): the adapter's `resolve`; (False, why) with nothing recorded when there
    is no adapter or it raises (see the module doc)."""
    mod, why = _adapter(adapter)
    if mod is None:
        return False, f"not closed: {why}; nothing records the close"
    try:
        return mod.resolve(Path(workspace), ask_id, status)
    except Exception as e:  # noqa: BLE001
        return False, f"not closed: adapter failed ({type(e).__name__}: {e}); nothing records the close"


def unknown_line(g: dict) -> str:
    return f"pending questions: UNKNOWN — room unreachable ({g['reason']}); {len(g['waiting'])} held locally"


def main(argv=None) -> int:
    import argparse  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="List or count the owner's pending questions (read-only), "
                                 "or resolve one through the declared store.")
    ap.add_argument("command", choices=("list", "count", "resolve"))
    ap.add_argument("ask_id", nargs="?", default=None, help="resolve: the question's ask id")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--answered", action="store_true", help="resolve: mark it Answered, not Resolved")
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--store-adapter", default=None, help="an adapter file in place of the declared one")
    # intermixed: an optional positional after `--workspace` is otherwise swallowed on 3.12
    args = ap.parse_intermixed_args(argv)
    if args.workspace is None:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        args.workspace = resolve_workspace(migrate=False)
    store = declared(DECLARATION, args.workspace, override=args.store_adapter)  # the CLI is an edge
    if args.command == "resolve":
        if not args.ask_id:
            ap.error("resolve needs an ask id")
        ok, msg = resolve(args.workspace, args.ask_id, "Answered" if args.answered else "Resolved", store)
        print(msg)
        return 0 if ok else 1
    g = gather(args.workspace, store)
    for note in g["notes"]:
        print(note, file=sys.stderr)
    if args.command == "count":
        print(json.dumps({"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
                          "pending_close": len(g["pending_close"]), "unavailable": g["unavailable"],
                          "reason": g["reason"]}))
        return 0
    if args.json:
        print(json.dumps({"unavailable": g["unavailable"], "reason": g["reason"], "waiting": g["waiting"]}
                         if g["unavailable"] else g["waiting"], ensure_ascii=False, indent=1))
        return 0
    if g["unavailable"]:
        print(unknown_line(g))
    elif not g["waiting"] and not g["pending_close"]:
        print("0 pending questions" + (f" in {g['store']}" if g.get("store") else ""))
        return 0
    for it in g["waiting"]:
        print(f"- [{it['ask_id']}] {it['title']}" + ("" if it.get("in_room", True) else " (not yet in the room)"))
    for ask_id in g["pending_close"]:
        print(f"- [{ask_id}] closed locally; its row is not in view yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
