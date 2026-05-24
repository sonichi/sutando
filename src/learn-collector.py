#!/usr/bin/env python3
"""learn-collector — emit JSONL events to state/learning-{instance}.jsonl.

Layer 1 of the loop-redesign learning system (per
notes/loop-redesign-goal-iteration-2026-05-03.md).

Watches:
- tasks/                          new task files                   -> kind="incoming"
- tasks/archive/                  task -> archive transitions      -> kind="archived"
- results/archive/<YYYY-MM>/      new result files (durable home)  -> kind="produced"

Schema (pointer-based — derivable fields read from the file when needed):
  {"ts","instance","source","kind","data":{"task_id","path"}}

`results/` itself is intentionally NOT watched — the discord-bridge moves
files to `results/archive/<YYYY-MM>/` faster than a 5s poll catches, so
the durable archive is the reliable signal.

Other event kinds emitted by the collector itself:
  source="collector", kind in {"started","heartbeat","stopped","loop_error"}

State files (gitignored, sync via memory-sync):
  state/learning-<instance>.jsonl     — append-only event log
  state/learning-<instance>.cursor    — per-directory ctime/mtime cursor for restart
  state/learning-<instance>.heartbeat — single ISO timestamp of last poll (separate
                                        from the jsonl so the event log isn't drowned)

Env vars:
  SUTANDO_ROOT                       override repo root (else __file__/..)
  LEARN_COLLECTOR_INTERVAL_S         poll cadence (default 5)
  LEARN_COLLECTOR_HEARTBEAT_S        heartbeat cadence (default 60)

Run:
  python3 src/learn-collector.py
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_ENV = os.environ.get("SUTANDO_ROOT")
ROOT = Path(ROOT_ENV).resolve() if ROOT_ENV else Path(__file__).resolve().parents[1]

STATE_DIR = ROOT / "state"
TASKS_DIR = ROOT / "tasks"
ARCHIVE_DIR = TASKS_DIR / "archive"
RESULTS_ARCHIVE_DIR = ROOT / "results" / "archive"

POLL_INTERVAL_S = int(os.environ.get("LEARN_COLLECTOR_INTERVAL_S", "5"))
HEARTBEAT_S = int(os.environ.get("LEARN_COLLECTOR_HEARTBEAT_S", "60"))


def resolve_instance() -> str:
    identity_path = ROOT / "stand-identity.json"
    if identity_path.exists():
        try:
            data = json.loads(identity_path.read_text())
            machine = data.get("machine") or data.get("instance")
            if machine:
                return machine
        except Exception:
            pass
    host = socket.gethostname().lower().replace(".local", "")
    if "mini" in host:
        return "mini"
    if "macbook" in host or "mbp" in host:
        return "macbook"
    return host or "unknown"


INSTANCE = resolve_instance()
JSONL_PATH = STATE_DIR / f"learning-{INSTANCE}.jsonl"
CURSOR_PATH = STATE_DIR / f"learning-{INSTANCE}.cursor"
HEARTBEAT_PATH = STATE_DIR / f"learning-{INSTANCE}.heartbeat"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(source: str, kind: str, data: dict) -> None:
    """Atomic append to the JSONL via os.write + O_APPEND.

    Both daemons (learn-collector + learn-log-tail) share this file. A
    single os.write call is atomic up to PIPE_BUF (512 on macOS, 4096 on
    Linux), so concurrent appends don't interleave for events under that
    size. Per cold review on PR #590.
    """
    STATE_DIR.mkdir(exist_ok=True)
    event = {
        "ts": now_iso(),
        "instance": INSTANCE,
        "source": source,
        "kind": kind,
        "data": data,
    }
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(JSONL_PATH), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def load_cursor() -> dict:
    if not CURSOR_PATH.exists():
        return {}
    try:
        return json.loads(CURSOR_PATH.read_text())
    except Exception:
        return {}


def save_cursor(cursor: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = CURSOR_PATH.with_suffix(".cursor.tmp")
    tmp.write_text(json.dumps(cursor))
    tmp.replace(CURSOR_PATH)


def rel_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def scan_dir(directory: Path, cursor_key: str, cursor: dict, source: str, kind: str, recursive: bool) -> int:
    """Emit events for files whose freshness time exceeds the cursor.

    "Freshness" is `max(st_mtime, st_ctime)`. ctime updates on rename, which
    catches files moved into archive/ via `mv` (mtime alone misses these
    because mv preserves mtime).

    Updates `cursor[cursor_key]` to the max freshness seen. Returns count emitted.
    """
    if not directory.exists():
        return 0
    last_seen = cursor.get(cursor_key, 0.0)
    new_max = last_seen
    iterator = directory.rglob("task-*.txt") if recursive else directory.glob("task-*.txt")
    count = 0
    for p in iterator:
        try:
            st = p.stat()
            freshness = max(st.st_mtime, st.st_ctime)
        except OSError:
            continue
        if freshness <= last_seen:
            continue
        emit(source, kind, {"task_id": p.stem, "path": rel_path(p)})
        count += 1
        if freshness > new_max:
            new_max = freshness
    if new_max > last_seen:
        cursor[cursor_key] = new_max
    return count


def write_heartbeat() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    HEARTBEAT_PATH.write_text(now_iso() + "\n")


def main() -> int:
    cursor = load_cursor()
    is_first_run = not cursor
    if is_first_run:
        bootstrap_now = time.time()
        cursor = {
            "tasks": bootstrap_now,
            "tasks_archive": bootstrap_now,
            "results_archive": bootstrap_now,
        }
        save_cursor(cursor)
    emit(
        "collector",
        "started",
        {
            "interval_s": POLL_INTERVAL_S,
            "heartbeat_s": HEARTBEAT_S,
            "jsonl": rel_path(JSONL_PATH),
            "cursor_loaded": not is_first_run,
        },
    )
    last_heartbeat = time.time()
    while True:
        try:
            scan_dir(TASKS_DIR, "tasks", cursor, "tasks", "incoming", recursive=False)
            # recursive=True: the bridge archives to tasks/archive/<YYYY-MM>/
            # (same pattern as results/archive/). Top-level-only would miss
            # every bridge-driven archive move.
            scan_dir(ARCHIVE_DIR, "tasks_archive", cursor, "tasks", "archived", recursive=True)
            scan_dir(RESULTS_ARCHIVE_DIR, "results_archive", cursor, "results", "produced", recursive=True)
            save_cursor(cursor)
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_S:
                # Heartbeat goes to its own single-line file so the event log
                # stays uncluttered. Liveness probes stat the heartbeat file.
                write_heartbeat()
                last_heartbeat = now
        except Exception as e:
            emit("collector", "loop_error", {"error": repr(e)})
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        emit("collector", "stopped", {})
        sys.exit(0)
