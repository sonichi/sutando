#!/usr/bin/env python3
"""
obsidian-mirror.py — one-shot sweep of workspace state into the Obsidian vault.

Safe to run repeatedly (idempotent). Mirrors:
  tasks/task-*.txt          → <vault>/Sutando/Agent/Tasks/<id>.md
  results/task-*.txt        → appended as Result block in matching task note
  notes/*.md                → <vault>/Sutando/Notes/<slug>.md
  pending-questions.md      → <vault>/Sutando/Agent/Asks.md

Opt-in gate: requires SUTANDO_OBSIDIAN_MIRROR=1 or --force.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Workspace / vault resolution
# ---------------------------------------------------------------------------

def resolve_workspace() -> Path:
    ws = os.environ.get("SUTANDO_WORKSPACE", str(Path.home() / ".sutando" / "workspace"))
    return Path(ws.replace("~", str(Path.home())))


def _resolve_vault(workspace: Path) -> Path:
    return workspace / "obsidian-vault"


def _ensure_vault(vault: Path) -> None:
    for sub in [
        vault / ".obsidian",
        vault / "Sutando" / "Notes",
        vault / "Sutando" / "Thoughts",
        vault / "Sutando" / "Agent" / "Tasks",
    ]:
        sub.mkdir(parents=True, exist_ok=True)

    asks = vault / "Sutando" / "Agent" / "Asks.md"
    if not asks.exists():
        asks.write_text("# Pending Questions\n\n")

    tasks_summary = vault / "Sutando" / "Tasks.md"
    if not tasks_summary.exists():
        tasks_summary.write_text("# Sutando Tasks\n\n")


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------

def _opt_in_gate(force: bool) -> bool:
    if force:
        return True
    if os.environ.get("SUTANDO_OBSIDIAN_MIRROR", "").strip() == "1":
        return True
    print(
        "[mirror] SUTANDO_OBSIDIAN_MIRROR not set — skipping. "
        "Set to 1 in .env or pass --force to run.",
        file=sys.stderr,
    )
    return False


# ---------------------------------------------------------------------------
# Since filter
# ---------------------------------------------------------------------------

def _parse_since(since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    now = datetime.now(tz=timezone.utc)
    m = re.fullmatch(r"(\d+)([smhd])", since.strip())
    if not m:
        raise ValueError(f"Invalid --since format: {since!r}  (use e.g. 1h, 30m, 2d)")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
             "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    return now - delta


def _newer_than(path: Path, cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return True
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime >= cutoff


# ---------------------------------------------------------------------------
# Task file parsing
# ---------------------------------------------------------------------------

_TASK_ID_RE = re.compile(r"task-(\d+(?:\.\d+)?)")


def _parse_task_file(path: Path) -> dict:
    text = path.read_text(errors="replace")
    m = _TASK_ID_RE.search(path.stem)
    task_id = m.group(1) if m else path.stem
    return {"id": task_id, "path": path, "text": text}


def _write_task_mirror(vault: Path, task: dict) -> Path:
    dest = vault / "Sutando" / "Agent" / "Tasks" / f"task-{task['id']}.md"
    # Preserve existing Result block if re-running
    result_block = ""
    if dest.exists():
        existing = dest.read_text(errors="replace")
        result_match = re.search(r"## Result\n(.*)", existing, re.DOTALL)
        if result_match:
            result_block = "\n\n## Result\n" + result_match.group(1).rstrip()

    content = f"# Task {task['id']}\n\n{task['text'].strip()}{result_block}\n"
    dest.write_text(content)
    return dest


def _write_result_mirror(vault: Path, result_path: Path, text: str) -> bool:
    m = _TASK_ID_RE.search(result_path.stem)
    if not m:
        return False
    task_id = m.group(1)
    dest = vault / "Sutando" / "Agent" / "Tasks" / f"task-{task_id}.md"
    if not dest.exists():
        return False

    existing = dest.read_text(errors="replace")
    if "## Result" in existing:
        # Replace existing result block
        new_content = re.sub(
            r"\n\n## Result\n.*",
            f"\n\n## Result\n{text.strip()}\n",
            existing,
            flags=re.DOTALL,
        )
    else:
        new_content = existing.rstrip() + f"\n\n## Result\n{text.strip()}\n"
    dest.write_text(new_content)
    return True


# ---------------------------------------------------------------------------
# Mirror passes
# ---------------------------------------------------------------------------

def _mirror_tasks(workspace: Path, vault: Path, cutoff: Optional[datetime], quiet: bool) -> int:
    tasks_dir = workspace / "tasks"
    if not tasks_dir.exists():
        return 0
    count = 0
    for p in sorted(tasks_dir.glob("task-*.txt")):
        if not _newer_than(p, cutoff):
            continue
        task = _parse_task_file(p)
        dest = _write_task_mirror(vault, task)
        if not quiet:
            print(f"  [task] {p.name} → {dest.relative_to(vault)}", flush=True)
        count += 1
    return count


def _mirror_results(workspace: Path, vault: Path, cutoff: Optional[datetime], quiet: bool) -> int:
    results_dir = workspace / "results"
    if not results_dir.exists():
        return 0
    count = 0
    for p in sorted(results_dir.glob("task-*.txt")):
        if not _newer_than(p, cutoff):
            continue
        try:
            text = p.read_text(errors="replace")
            ok = _write_result_mirror(vault, p, text)
            if ok:
                if not quiet:
                    print(f"  [result] {p.name} → appended", flush=True)
                count += 1
        except OSError:
            pass
    return count


def _mirror_notes(workspace: Path, vault: Path, cutoff: Optional[datetime], quiet: bool) -> int:
    notes_dir = workspace / "notes"
    if not notes_dir.exists():
        # Try repo-relative notes/
        repo = Path(__file__).resolve().parent.parent
        notes_dir = repo / "notes"
    if not notes_dir.exists():
        return 0
    count = 0
    dest_dir = vault / "Sutando" / "Notes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(notes_dir.glob("*.md")):
        if not _newer_than(p, cutoff):
            continue
        dest = dest_dir / p.name
        dest.write_text(p.read_text(errors="replace"))
        if not quiet:
            print(f"  [note] {p.name} → {dest.relative_to(vault)}", flush=True)
        count += 1
    return count


def _mirror_pending_questions(workspace: Path, vault: Path, quiet: bool) -> None:
    # Try workspace first, then repo root
    candidates = [
        workspace / "pending-questions.md",
        Path(__file__).resolve().parent.parent / "pending-questions.md",
    ]
    src = next((p for p in candidates if p.exists()), None)
    if not src:
        return
    dest = vault / "Sutando" / "Agent" / "Asks.md"
    dest.write_text(src.read_text(errors="replace"))
    if not quiet:
        print(f"  [asks] pending-questions.md → {dest.relative_to(vault)}", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror workspace state into Obsidian vault")
    parser.add_argument("--force", action="store_true", help="bypass SUTANDO_OBSIDIAN_MIRROR gate")
    parser.add_argument("--since", metavar="DURATION", help="only mirror items newer than this (e.g. 1h, 30m)")
    parser.add_argument("--quiet", action="store_true", help="suppress per-file output")
    args = parser.parse_args()

    if not _opt_in_gate(args.force):
        sys.exit(0)

    try:
        cutoff = _parse_since(args.since)
    except ValueError as e:
        print(f"[mirror] {e}", file=sys.stderr)
        sys.exit(1)

    workspace = resolve_workspace()
    vault = _resolve_vault(workspace)
    _ensure_vault(vault)

    if not args.quiet:
        print(f"[mirror] workspace: {workspace}", flush=True)
        print(f"[mirror] vault:     {vault}", flush=True)
        if cutoff:
            print(f"[mirror] since:     {cutoff.isoformat()}", flush=True)

    tasks = _mirror_tasks(workspace, vault, cutoff, args.quiet)
    results = _mirror_results(workspace, vault, cutoff, args.quiet)
    notes = _mirror_notes(workspace, vault, cutoff, args.quiet)
    _mirror_pending_questions(workspace, vault, args.quiet)

    print(
        f"[mirror] done — {tasks} tasks, {results} results, {notes} notes mirrored",
        flush=True,
    )


if __name__ == "__main__":
    main()
