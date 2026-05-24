"""Obsidian sync — one-shot sweep of agent state into the Sutando vault.

Does a single pass and exits. No background process, no polling.
Run on-demand or wire into the user's own crons.json at whatever
cadence they want.

Sources mirrored (decided 2026-05-24 with owner):
  tasks/task-<id>.txt        -> Agent/Tasks/task-<id>.md      (status: pending)
  results/task-<id>.txt      -> Agent/Tasks/task-<id>.md      (update: append Result, status: completed)
  pending-questions.md       -> Agent/Asks.md                 (verbatim)
  notes/*.md                 -> Agent/Notes/<name>.md         (verbatim)

One-way only (workspace -> vault). No reverse sync.

Gated by `SUTANDO_OBSIDIAN_MIRROR` env var — exits cleanly when unset,
unless `--force` is passed (used by the on-demand voice tool).

Usage:
  python3 src/obsidian-mirror.py            # respects opt-in env var
  python3 src/obsidian-mirror.py --force    # explicit user run, bypass gate
  python3 src/obsidian-mirror.py --since 1h # only sync sources modified in last hour
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional


# ---- Path resolution ----

def resolve_workspace() -> Path:
    ws = os.environ.get("SUTANDO_WORKSPACE")
    if ws:
        return Path(ws).expanduser()
    return Path.home() / ".sutando" / "workspace"


TASK_ID_RE = re.compile(r"^task-(.+)\.txt$")


def _ensure_vault(vault: Path) -> None:
    (vault / ".obsidian").mkdir(parents=True, exist_ok=True)
    (vault / "Sutando" / "Agent" / "Tasks").mkdir(parents=True, exist_ok=True)
    (vault / "Sutando" / "Agent" / "Notes").mkdir(parents=True, exist_ok=True)


def _task_id_from_path(path: Path) -> Optional[str]:
    m = TASK_ID_RE.match(path.name)
    return f"task-{m.group(1)}" if m else None


def _parse_task_file(path: Path) -> dict:
    info: dict = {"raw": ""}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return info
    info["raw"] = text
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if k in {"id", "timestamp", "task", "source", "channel_id", "user_id", "access_tier", "priority"}:
                info[k] = v
    return info


def _write_task_mirror(vault: Path, task_path: Path) -> bool:
    task_id = _task_id_from_path(task_path)
    if not task_id:
        return False
    info = _parse_task_file(task_path)
    mirror = vault / "Sutando" / "Agent" / "Tasks" / f"{task_id}.md"

    # Preserve any existing Result block (in case we re-run and only the source was modified).
    existing_result = ""
    if mirror.exists():
        try:
            existing = mirror.read_text(encoding="utf-8")
            if "\n## Result\n" in existing:
                existing_result = "\n## Result\n" + existing.split("\n## Result\n", 1)[1]
        except Exception:
            pass

    status = "completed" if existing_result else "pending"
    frontmatter = [
        "---",
        f"id: {task_id}",
        f"status: {status}",
        f"source: {info.get('source', 'unknown')}",
        f"access_tier: {info.get('access_tier', 'owner')}",
        f"priority: {info.get('priority', 'normal')}",
        f"ts_source: {info.get('timestamp', '')}",
        f"ts_mirror: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "---",
        "",
    ]
    body_lines = [
        f"# {task_id}",
        "",
        "## Request",
        "",
        "```",
        info["raw"].rstrip(),
        "```",
        existing_result.rstrip() if existing_result else "",
        "",
    ]
    new_content = "\n".join(frontmatter) + "\n".join(body_lines) + "\n"
    if mirror.exists() and mirror.read_text(encoding="utf-8") == new_content:
        return False
    mirror.write_text(new_content, encoding="utf-8")
    return True


def _write_result_mirror(vault: Path, result_path: Path) -> bool:
    task_id = _task_id_from_path(result_path)
    if not task_id:
        return False
    mirror = vault / "Sutando" / "Agent" / "Tasks" / f"{task_id}.md"
    try:
        result_body = result_path.read_text(encoding="utf-8", errors="replace").rstrip()
    except FileNotFoundError:
        return False

    if not mirror.exists():
        frontmatter = [
            "---",
            f"id: {task_id}",
            "status: completed",
            "source: unknown",
            f"ts_mirror: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "---",
            "",
            f"# {task_id}",
            "",
            "_(Task source file not seen — result captured below.)_",
            "",
            "## Result",
            "",
            result_body,
            "",
        ]
        mirror.write_text("\n".join(frontmatter) + "\n", encoding="utf-8")
        return True

    existing = mirror.read_text(encoding="utf-8")
    new = re.sub(r"^status:.*$", "status: completed", existing, count=1, flags=re.MULTILINE)
    if "\n## Result\n" in new:
        new = new.split("\n## Result\n", 1)[0].rstrip() + "\n"
    if not new.endswith("\n"):
        new += "\n"
    new += f"\n## Result\n\n{result_body}\n"
    if new == existing:
        return False
    mirror.write_text(new, encoding="utf-8")
    return True


def _mirror_asks(vault: Path, workspace: Path) -> bool:
    src = workspace / "pending-questions.md"
    if not src.exists():
        return False
    dest = vault / "Sutando" / "Agent" / "Asks.md"
    content = src.read_text(encoding="utf-8", errors="replace")
    if dest.exists() and dest.read_text(encoding="utf-8") == content:
        return False
    dest.write_text(content, encoding="utf-8")
    return True


def _mirror_note(vault: Path, note_path: Path) -> bool:
    if note_path.suffix != ".md":
        return False
    dest = vault / "Sutando" / "Agent" / "Notes" / note_path.name
    try:
        content = note_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    if dest.exists() and dest.read_text(encoding="utf-8") == content:
        return False
    dest.write_text(content, encoding="utf-8")
    return True


def _within_window(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime >= cutoff
    except FileNotFoundError:
        return False


def sweep(vault: Path, workspace: Path, since_seconds: Optional[int] = None) -> dict:
    """Single pass over all source dirs. Returns counts by kind."""
    _ensure_vault(vault)
    cutoff = (time.time() - since_seconds) if since_seconds else 0.0

    counts = {"tasks": 0, "results": 0, "notes": 0, "asks": 0, "scanned": 0}

    tasks_dir = workspace / "tasks"
    if tasks_dir.exists():
        for p in sorted(tasks_dir.glob("task-*.txt")):
            counts["scanned"] += 1
            if cutoff and not _within_window(p, cutoff):
                continue
            if _write_task_mirror(vault, p):
                counts["tasks"] += 1

    results_dir = workspace / "results"
    if results_dir.exists():
        for p in sorted(results_dir.glob("task-*.txt")):
            counts["scanned"] += 1
            if cutoff and not _within_window(p, cutoff):
                continue
            if _write_result_mirror(vault, p):
                counts["results"] += 1

    notes_dir = workspace / "notes"
    if notes_dir.exists():
        for p in sorted(notes_dir.glob("*.md")):
            counts["scanned"] += 1
            if cutoff and not _within_window(p, cutoff):
                continue
            if _mirror_note(vault, p):
                counts["notes"] += 1

    asks_src = workspace / "pending-questions.md"
    if asks_src.exists() and (not cutoff or _within_window(asks_src, cutoff)):
        if _mirror_asks(vault, workspace):
            counts["asks"] = 1

    return counts


def _parse_since(value: str) -> int:
    """Parse '30m', '1h', '6h', '1d' etc. → seconds. Plain number = seconds."""
    if not value:
        return 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value[-1].lower() in multipliers:
        return int(value[:-1]) * multipliers[value[-1].lower()]
    return int(value)


def main(argv) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the SUTANDO_OBSIDIAN_MIRROR opt-in gate. Use only for explicit user invocations.",
    )
    p.add_argument(
        "--since",
        default=None,
        help="Only sync sources modified within the given window (e.g. 30m, 1h, 6h, 1d). Default: full sweep.",
    )
    p.add_argument("--vault", help="Override vault path.")
    args = p.parse_args(argv)

    if not args.force and os.environ.get("SUTANDO_OBSIDIAN_MIRROR", "").lower() not in ("1", "true", "yes", "on"):
        print(
            "[obsidian-mirror] not enabled — set SUTANDO_OBSIDIAN_MIRROR=1 in .env to opt in, "
            "or pass --force for an explicit one-shot run. Exiting.",
            flush=True,
        )
        return 0

    workspace = resolve_workspace()
    if not workspace.exists():
        print(f"[obsidian-mirror] workspace dir missing: {workspace}", file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser() if args.vault else workspace / "obsidian-vault"

    since_seconds = _parse_since(args.since) if args.since else None
    counts = sweep(vault, workspace, since_seconds=since_seconds)

    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if k != "scanned")
    print(f"[obsidian-mirror] swept {counts['scanned']} sources — {summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
