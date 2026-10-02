"""The one way core reads owner pending questions: through the adapter an installed skill
declares in its manifest (`pending_questions_store`), loaded by path — core names no skill.

`gather(workspace)` returns {"waiting": [items], "done": n, "notes": [...], "store": where}:
the adapter's open rows of this host plus the local outbox, or, with no adapter installed
or none that loads, the outbox alone with the reason in `notes`. `waiting`, `count` and
`resolve` are the thin views every core reader uses; never read any file for this.

CLI, for shell readers: `python3 src/pending_questions_reader.py list [--json] | count`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pending_questions_store import declared_adapter, load_adapter, outbox_items  # noqa: E402

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"  # lint-workspace-resolution: allow-repo-root


def _adapter(adapter=None, skills_dir=None):
    path = adapter or declared_adapter(skills_dir or SKILLS_DIR)
    if not path:
        return None, "no pending-questions store adapter installed (no skill declares one)"
    try:
        return load_adapter(path), str(path)
    except Exception as e:  # noqa: BLE001
        return None, f"adapter {path} failed to load ({type(e).__name__}: {e})"


def gather(workspace, adapter=None, skills_dir=None) -> dict:
    mod, why = _adapter(adapter, skills_dir)
    if mod is None:
        return {"waiting": outbox_items(workspace), "done": 0, "store": None,
                "notes": [f"{why}; listing the local outbox only"]}
    try:
        return mod.gather(Path(workspace))
    except Exception as e:  # noqa: BLE001
        return {"waiting": outbox_items(workspace), "done": 0, "store": None,
                "notes": [f"adapter failed ({type(e).__name__}: {e}); listing the local outbox only"]}


def waiting(workspace, adapter=None, skills_dir=None) -> list:
    return gather(workspace, adapter, skills_dir)["waiting"]


def count(workspace, adapter=None, skills_dir=None) -> dict:
    g = gather(workspace, adapter, skills_dir)
    return {"open": len(g["waiting"]), "done": g["done"]}


def resolve(workspace, ask_id: str, status: str, adapter=None, skills_dir=None) -> tuple:
    """(closed, message) from the adapter's `resolve`; (False, why) without one."""
    mod, why = _adapter(adapter, skills_dir)
    if mod is None:
        return False, why
    try:
        return mod.resolve(Path(workspace), ask_id, status)
    except Exception as e:  # noqa: BLE001
        return False, f"adapter failed ({type(e).__name__}: {e})"


def main(argv=None) -> int:
    import argparse  # noqa: PLC0415
    ap = argparse.ArgumentParser(description="List or count the owner's pending questions.")
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
        print(json.dumps({"open": len(g["waiting"]), "done": g["done"]}))
        return 0
    if args.json:
        print(json.dumps(g["waiting"], ensure_ascii=False, indent=1))
        return 0
    if not g["waiting"]:
        print("0 pending questions" + (f" in {g['store']}" if g.get("store") else ""))
        return 0
    for it in g["waiting"]:
        print(f"- [{it['ask_id']}] {it['title']}" + ("" if it.get("in_room", True) else " (not yet in the room)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
