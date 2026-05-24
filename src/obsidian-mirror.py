"""Obsidian mirror — one-way sync of agent state into the Sutando vault.

Watches workspace dirs and writes/updates files under
  $SUTANDO_WORKSPACE/obsidian-vault/Sutando/Agent/
so the agent's activity is visible in Obsidian's graph + search.

Sources mirrored (decided 2026-05-24 with owner):
  tasks/task-<id>.txt        -> Agent/Tasks/task-<id>.md      (status: pending)
  results/task-<id>.txt      -> Agent/Tasks/task-<id>.md      (update: append Result, status: completed)
  pending-questions.md       -> Agent/Asks.md                 (verbatim)
  notes/*.md                 -> Agent/Notes/<name>.md         (verbatim)

One-way only (workspace -> vault). No reverse sync.

Run standalone:  python3 src/obsidian-mirror.py
Or via startup.sh — health-check.py picks it up by process name.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

import subprocess


def _relink_async(target: Path) -> None:
    """No-op until the LLM-based relink is built.

    The previous regex-on-capitalized-tokens approach was vetoed as dumb
    (Slack 2026-05-24): too noisy, didn't actually verify references. The
    redesign uses LLM-judged inline references + topical similarity in
    tiered footers — see results/task-1779617995654.txt for the pending
    sign-off. Until that ships, mirror writes do NOT modify file content
    beyond the raw mirroring.
    """
    return


# ---- Path resolution ----

def resolve_workspace() -> Path:
    import os
    ws = os.environ.get("SUTANDO_WORKSPACE")
    if ws:
        return Path(ws).expanduser()
    return Path.home() / ".sutando" / "workspace"


WORKSPACE = resolve_workspace()
VAULT = WORKSPACE / "obsidian-vault"
AGENT_DIR = VAULT / "Sutando" / "Agent"
TASKS_DIR = AGENT_DIR / "Tasks"
NOTES_DIR = AGENT_DIR / "Notes"
ASKS_FILE = AGENT_DIR / "Asks.md"

TASK_ID_RE = re.compile(r"^task-(.+)\.txt$")


# ---- Helpers ----

def ensure_vault() -> None:
    """First-call vault skeleton. Cheap to repeat."""
    (VAULT / ".obsidian").mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


def task_id_from_path(path: Path) -> Optional[str]:
    """`tasks/task-1234.txt` -> `task-1234` (kept whole; matches filename in vault)."""
    m = TASK_ID_RE.match(path.name)
    return f"task-{m.group(1)}" if m else None


def parse_task_file(path: Path) -> dict:
    """Tolerant parse — file is either `key: value` lines or free-form body."""
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


def write_task_mirror(task_path: Path) -> None:
    """Create or refresh Agent/Tasks/<id>.md from the source task file."""
    task_id = task_id_from_path(task_path)
    if not task_id:
        return
    ensure_vault()
    info = parse_task_file(task_path)
    mirror = TASKS_DIR / f"{task_id}.md"

    # If a mirror already exists with a Result block, preserve it.
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
    mirror.write_text("\n".join(frontmatter) + "\n".join(body_lines) + "\n", encoding="utf-8")
    print(f"[obsidian-mirror] task -> {mirror}", flush=True)
    _relink_async(mirror)


def write_result_mirror(result_path: Path) -> None:
    """Update Agent/Tasks/<id>.md with the result body + status=completed."""
    task_id = task_id_from_path(result_path)
    if not task_id:
        return
    ensure_vault()
    mirror = TASKS_DIR / f"{task_id}.md"
    try:
        result_body = result_path.read_text(encoding="utf-8", errors="replace").rstrip()
    except FileNotFoundError:
        return

    if not mirror.exists():
        # Result landed before task was mirrored (or task file already archived).
        # Synthesize a minimal mirror so the result is still captured.
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
        print(f"[obsidian-mirror] result (orphan) -> {mirror}", flush=True)
        _relink_async(mirror)
        return

    # Splice: replace frontmatter status, drop any existing ## Result, append fresh.
    existing = mirror.read_text(encoding="utf-8")
    existing = re.sub(r"^status:.*$", "status: completed", existing, count=1, flags=re.MULTILINE)
    if "\n## Result\n" in existing:
        existing = existing.split("\n## Result\n", 1)[0].rstrip() + "\n"
    if not existing.endswith("\n"):
        existing += "\n"
    existing += f"\n## Result\n\n{result_body}\n"
    mirror.write_text(existing, encoding="utf-8")
    print(f"[obsidian-mirror] result -> {mirror}", flush=True)
    _relink_async(mirror)


def mirror_asks() -> None:
    """Mirror pending-questions.md verbatim (debounced caller)."""
    src = WORKSPACE / "pending-questions.md"
    if not src.exists():
        return
    ensure_vault()
    ASKS_FILE.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    print(f"[obsidian-mirror] asks -> {ASKS_FILE}", flush=True)
    _relink_async(ASKS_FILE)


def mirror_note(note_path: Path) -> None:
    if note_path.suffix != ".md":
        return
    ensure_vault()
    dest = NOTES_DIR / note_path.name
    try:
        dest.write_text(note_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        print(f"[obsidian-mirror] note -> {dest}", flush=True)
        _relink_async(dest)
    except FileNotFoundError:
        # source vanished mid-event; ignore
        pass


# ---- Watchdog handlers ----

class TasksHandler(FileSystemEventHandler):
    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        write_task_mirror(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        write_task_mirror(Path(event.src_path))


class ResultsHandler(FileSystemEventHandler):
    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        write_result_mirror(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        write_result_mirror(Path(event.src_path))


class NotesHandler(FileSystemEventHandler):
    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        mirror_note(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        mirror_note(Path(event.src_path))


class WorkspaceRootHandler(FileSystemEventHandler):
    """Filtered to pending-questions.md only."""

    _debounce_at: float = 0.0

    def _maybe(self, src_path: str) -> None:
        if Path(src_path).name != "pending-questions.md":
            return
        # Debounce — Obsidian + editors can fire several events per save.
        now = time.time()
        if now - self._debounce_at < 0.4:
            return
        self._debounce_at = now
        mirror_asks()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)


# ---- Initial sync + main ----

def initial_sync() -> None:
    """Walk source dirs once at startup so the vault catches up before live watching."""
    ensure_vault()
    tasks_dir = WORKSPACE / "tasks"
    results_dir = WORKSPACE / "results"
    notes_dir = WORKSPACE / "notes"

    if tasks_dir.exists():
        for p in sorted(tasks_dir.glob("task-*.txt")):
            write_task_mirror(p)
    if results_dir.exists():
        for p in sorted(results_dir.glob("task-*.txt")):
            write_result_mirror(p)
    if notes_dir.exists():
        for p in sorted(notes_dir.glob("*.md")):
            mirror_note(p)
    mirror_asks()
    # Full relink pass deliberately not run — the previous regex-based
    # relink was vetoed (Slack 2026-05-24). Waiting on the LLM-judged
    # rebuild before reactivating any post-mirror processing.


def main() -> int:
    # Opt-in gate. Mirroring agent state into a vault is invasive — only run
    # when the user explicitly opts in via `SUTANDO_OBSIDIAN_MIRROR=1`. Default
    # is OFF. (Feedback 2026-05-24: "must be opt in before the mirroring is
    # forced in".) The `add_to_vault` voice tool is unaffected — that's
    # always user-initiated.
    import os
    if os.environ.get("SUTANDO_OBSIDIAN_MIRROR", "").lower() not in ("1", "true", "yes", "on"):
        print(
            "[obsidian-mirror] not enabled — set SUTANDO_OBSIDIAN_MIRROR=1 in .env to opt in. Exiting.",
            flush=True,
        )
        return 0
    print(f"[obsidian-mirror] workspace={WORKSPACE} vault={VAULT}", flush=True)
    if not WORKSPACE.exists():
        print(f"[obsidian-mirror] workspace dir missing: {WORKSPACE}", file=sys.stderr)
        return 2

    initial_sync()

    observer = Observer()
    tasks_dir = WORKSPACE / "tasks"
    results_dir = WORKSPACE / "results"
    notes_dir = WORKSPACE / "notes"

    if tasks_dir.exists():
        observer.schedule(TasksHandler(), str(tasks_dir), recursive=False)
    if results_dir.exists():
        observer.schedule(ResultsHandler(), str(results_dir), recursive=False)
    if notes_dir.exists():
        observer.schedule(NotesHandler(), str(notes_dir), recursive=False)
    observer.schedule(WorkspaceRootHandler(), str(WORKSPACE), recursive=False)

    observer.start()
    print("[obsidian-mirror] watching: tasks/ results/ notes/ pending-questions.md", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[obsidian-mirror] shutting down", flush=True)
    finally:
        observer.stop()
        observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
