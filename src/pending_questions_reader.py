"""The one way core reads owner pending questions: through the adapter an installed skill
declares in its manifest (`pending_questions_store`), picked by that field alone and loaded
by path — core names no skill, carries no question schema, and refuses when more than one
skill declares it.

`gather(workspace)` returns {"waiting": [items], "done": n | None, "pending_close": [ids],
"unavailable": bool, "reason": str | None, "link": str | None, "notes": [...], "store": where}.
A store that cannot be read is NEVER a measured zero: `unavailable` is True, `done` is None.
With no adapter installed there is no store to read, so the result is unavailable with that
reason — what the adapter holds locally, lists or counts is the adapter's to say. `waiting`,
`count` and `resolve` are the thin views every core reader uses; a read never reconciles
(`reconcile_pass` does, on demand). Never read any file for this.

`resolve` closes through the adapter; with no adapter, or an adapter that raises, it returns
(False, why) and records nothing — the caller keeps the answer it holds (agent-api files the
answer task before it asks for the close).

CLI, for shell readers: `python3 src/pending_questions_reader.py list [--json] | count`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"  # lint-workspace-resolution: allow-repo-root
# The manifest field an installed skill declares its adapter script with.
DECLARATION = "pending_questions_store"


class AdapterConflict(RuntimeError):
    """More than one installed skill declares the store; none is picked."""


def declared_adapters(skills_dirs) -> list:
    """(skill name, script) per installed skill whose manifest declares the field with a
    script resolving inside its own directory — a manifest may come from a third party."""
    dirs = [Path(skills_dirs)] if isinstance(skills_dirs, (str, Path)) else [Path(d) for d in skills_dirs]
    out = []
    for manifest in sorted(m for d in dirs for m in d.glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = data.get(DECLARATION) if isinstance(data, dict) else None
        if not isinstance(rel, str) or not rel or data.get("enabled") is False:
            continue
        skill = manifest.parent.resolve()
        script = (skill / rel).resolve()
        if script.is_relative_to(skill) and script.is_file():
            out.append((manifest.parent.name, script))
    return out


def declared_adapter(skills_dirs) -> Optional[Path]:
    """The one declared adapter; None when none; AdapterConflict when several."""
    found = declared_adapters(skills_dirs)
    if len(found) > 1:
        raise AdapterConflict("more than one skill declares pending_questions_store: "
                              + ", ".join(sorted(n for n, _ in found)) + "; refusing to pick one")
    return found[0][1] if found else None


def load_adapter(adapter):
    """The adapter module from its file; None when there is none."""
    if not adapter:
        return None
    import importlib.util  # noqa: PLC0415
    spec = importlib.util.spec_from_file_location("pq_store_adapter", str(adapter))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NO_ADAPTER = "no pending-questions store adapter installed (no skill declares one)"


def _adapter(adapter=None, skills_dir=None):
    """(module, path) or (None, why); `why` is NO_ADAPTER exactly when none is declared."""
    try:
        path = adapter or declared_adapter(skills_dir or SKILLS_DIR)
    except AdapterConflict as e:
        return None, str(e)
    if not path:
        return None, NO_ADAPTER
    try:
        return load_adapter(path), str(path)
    except Exception as e:  # noqa: BLE001
        return None, f"adapter {path} failed to load ({type(e).__name__}: {e})"


def _unavailable(reason: str) -> dict:
    return {"waiting": [], "done": None, "pending_close": [], "unavailable": True, "reason": reason,
            "link": None, "store": None, "notes": [f"PENDING QUESTIONS UNAVAILABLE ({reason}); the count is unknown"]}


def gather(workspace, adapter=None, skills_dir=None, reconcile: bool = False) -> dict:
    mod, why = _adapter(adapter, skills_dir)
    if mod is None:
        return _unavailable(why)
    try:
        g = mod.gather(Path(workspace), reconcile=reconcile) if reconcile else mod.gather(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return _unavailable(f"adapter failed: {type(e).__name__}: {e}")
    g.setdefault("unavailable", False)
    g.setdefault("reason", None)
    g.setdefault("link", None)
    g.setdefault("pending_close", [])
    return g


def waiting(workspace, adapter=None, skills_dir=None) -> list:
    return gather(workspace, adapter, skills_dir)["waiting"]


def count(workspace, adapter=None, skills_dir=None) -> dict:
    """{"open": n | None, "done": n | None, "pending_close": n, "unavailable", "reason"}; None is
    unknown, never 0."""
    g = gather(workspace, adapter, skills_dir)
    return {"open": None if g["unavailable"] else len(g["waiting"]), "done": g["done"],
            "pending_close": len(g["pending_close"]), "unavailable": g["unavailable"], "reason": g["reason"]}


def reconcile_pass(workspace, adapter=None, skills_dir=None) -> dict:
    """The explicit pass: outbox replay, local closes, the legacy ingest; {"errors": [why]} without a store."""
    mod, why = _adapter(adapter, skills_dir)
    if mod is None or not hasattr(mod, "reconcile_pass"):
        return {"flushed": [], "moved": [], "closed": [], "errors": [f"no store to reconcile with: {why}"]}
    try:
        return mod.reconcile_pass(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return {"flushed": [], "moved": [], "closed": [], "errors": [f"adapter failed: {type(e).__name__}: {e}"]}


def resolve(workspace, ask_id: str, status: str, adapter=None, skills_dir=None) -> tuple:
    """(closed, message): the adapter's `resolve`; (False, why) with nothing recorded when there
    is no adapter or it raises (see the module doc)."""
    mod, why = _adapter(adapter, skills_dir)
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
    ap = argparse.ArgumentParser(description="List or count the owner's pending questions (read-only).")
    ap.add_argument("command", choices=("list", "count"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workspace", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.workspace is None:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        args.workspace = resolve_workspace(migrate=False)
    g = gather(args.workspace)
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
