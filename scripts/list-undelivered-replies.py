#!/usr/bin/env python3
"""Name every reply sitting in results/ that no consumer is coming for, and
where it was supposed to go.

READ-ONLY by design: it prints, it never sends. Recovering a reply is a
side-effecting act on someone's room, so the decision stays with the operator.

Why this exists: a bridge can discard a task's tracked id while the task is
still being worked (see remote_gateway_bridge._reconcile_abandoned). The
follower then writes a perfectly good reply that nothing will deliver. The
health probe names the FILE; the destination lives in the task header, which
is by then in tasks/archive/ — so recovery meant hand-joining two places.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_HDR = re.compile(r"^(channel_id|chat_id|source|user_id|access_tier):\s*(\S+)", re.M)


def _resolve_workspace(repo: Path) -> Path:
    sys.path.insert(0, str(repo / "src"))
    from workspace_default import resolve_workspace  # noqa: PLC0415
    return Path(resolve_workspace())


def _task_header(ws: Path, task_id: str) -> dict:
    """The header, wherever the task currently lives — a finished task has
    already been archived, which is exactly when its reply goes orphaned."""
    for cand in [ws / "tasks" / f"{task_id}.txt",
                 ws / "tasks" / "archive" / f"{task_id}.txt",
                 *(ws / "tasks").glob(f"{task_id}.assigned-*.txt"),
                 *(ws / "tasks").glob(f"{task_id}.claimed-*.txt")]:
        try:
            return dict(_HDR.findall(cand.read_text(errors="replace")[:8192]))
        except OSError:
            continue
    return {}


def undelivered(ws: Path) -> list:
    out = []
    tasks = ws / "tasks"
    for res in sorted((ws / "results").glob("task-*.txt")):
        task_id = res.stem
        # Queued, assigned or claimed: a consumer may yet reach this pair. The
        # literal dot stops `{id}*.txt` prefix-matching and hiding a real orphan.
        if (tasks / f"{task_id}.txt").exists() or any(tasks.glob(f"{task_id}.*.txt")):
            continue
        out.append((task_id, res, _task_header(ws, task_id)))
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", help="override the resolved workspace")
    args = ap.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root (sys.path only; resolution goes through the loader)
    ws = Path(args.workspace) if args.workspace else _resolve_workspace(repo)

    rows = undelivered(ws)
    if not rows:
        print("no replies awaiting delivery")
        return 0
    print(f"{len(rows)} repl(y/ies) with no consumer coming:\n")
    for task_id, res, hdr in rows:
        dest = hdr.get("channel_id") or hdr.get("chat_id") or "UNKNOWN"
        print(f"  {task_id}")
        print(f"    bytes  {res.stat().st_size}")
        print(f"    source {hdr.get('source', '?')}  tier {hdr.get('access_tier', '?')}"
              f"  from {hdr.get('user_id', '?')}")
        print(f"    dest   {dest}")
        if dest == "UNKNOWN":
            print("    ^ no channel in the header — do NOT guess a room")
        print()
    print("Review each body before sending; this script never delivers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
