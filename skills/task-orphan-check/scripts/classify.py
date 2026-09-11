#!/usr/bin/env python3
"""Step 2 of /task-orphan-check, as a script: classify every live task in
`<workspace>/tasks/` as done / fresh / orphan / import-resume, read-only.

The skill was prose-only ("marker-or-not + age-vs-5min"); its own note said to
promote the classification to a script once the rules grew past that. They
did on 2026-09-11: a consented Claude-Code import task (the desktop's legacy
`channel_id: onboarding-wizard` writer) that the core had not started within
five minutes was, by the prose rule, an ORPHAN — archived into the aggregated
recovery DM on the next boot, although the owner had consented, the import is
resumable, and the watcher's startup sweep would simply re-emit it. This
script encodes the rules so a test can pin them; the agent still performs
step 3 (archive moves, the aggregated DM) from the verdicts printed here.

Verdicts (first match wins):
  done            a completion marker exists — results/<id>.txt (live or
                  archived) or results/proactive-<id>.txt[.sending];
                  for an import task, also status.json phase `done` from a
                  run that started after the task was queued.
  import-resume   an import task whose run has started (status.json newer
                  than the task) but has not finished: left in tasks/ for the
                  watcher's sweep, never archived, whatever its age.
  fresh           younger than the age line — 300 s for any task, 1800 s for
                  an import task that has not started yet.
  orphan          no marker and past the age line: step 3 applies.

Age comes from the immutable `timestamp:` header, then the epoch-ms in the
id, and only then the file mtime (mtime resets on rsync / checkout / sync).

Usage:
  python3 skills/task-orphan-check/scripts/classify.py [--workspace DIR] [--now EPOCH]
Prints one JSON object: {"workspace", "tasks": [...], "counts": {...}}.
Exit 0 always on a readable workspace; 2 when the workspace cannot be resolved.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[2]  # skills/task-orphan-check/scripts -> repo root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402

FRESH_AGE_S = 300
IMPORT_FRESH_AGE_S = 1800
IMPORT_CHANNEL = "onboarding-wizard"
IMPORT_SKILL = "import-claude-context"
IMPORT_STATUS = ("data", "claude-import", "status.json")
_EPOCH_MS_RE = re.compile(r"(\d{13})$")


def parse_iso(value: str) -> float | None:
    """ISO-8601 → epoch seconds; None when unparsable. A bare `Z` is accepted
    (Python < 3.11 rejects it) and a naive stamp is read as UTC."""
    s = (value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def task_queued_at(headers: dict, task_id: str, path: Path) -> tuple[float, str]:
    """(epoch, how) — header timestamp, else id epoch-ms, else mtime."""
    ts = parse_iso(headers.get("timestamp", ""))
    if ts is not None:
        return ts, "timestamp"
    m = _EPOCH_MS_RE.search(task_id)
    if m:
        return int(m.group(1)) / 1000.0, "id"
    try:
        return path.stat().st_mtime, "mtime"
    except OSError:
        return 0.0, "mtime"


def is_import_task(headers: dict, body: str) -> bool:
    """The legacy desktop writer names the wizard channel; a hand-queued owner
    task names the skill. Only owner-tier tasks qualify — the import is an
    owner-only skill, so a non-owner body naming it earns no exemption."""
    tier = ltp.canonical_access_tier(headers.get("access_tier") or "owner")
    if tier != "owner":
        return False
    if (headers.get("channel_id") or "").strip() == IMPORT_CHANNEL:
        return True
    return IMPORT_SKILL in body


def import_status(workspace: Path) -> tuple[str, float | None]:
    """(phase, updated_at epoch) from data/claude-import/status.json; ("", None)
    when absent or unreadable."""
    p = workspace.joinpath(*IMPORT_STATUS)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return "", None
    if not isinstance(data, dict):
        return "", None
    updated = parse_iso(str(data.get("updated_at") or ""))
    if updated is None:
        try:
            updated = p.stat().st_mtime
        except OSError:
            updated = None
    return str(data.get("phase") or ""), updated


def completion_marker(results_dir: Path, task_id: str) -> str:
    """Path of the marker that proves the task completed, or ''."""
    found = ltp.find_result(results_dir, task_id)
    if found is not None:
        return str(found)
    for name in (f"proactive-{task_id}.txt", f"proactive-{task_id}.txt.sending"):
        if (results_dir / name).is_file():
            return str(results_dir / name)
    return ""


def channel_label(headers: dict) -> str:
    name = headers.get("room_name") or headers.get("channel_name")
    cid = headers.get("channel_id") or headers.get("chat_id") or ""
    return f"{name} ({cid})" if name else (cid or "DM")


def classify_task(path: Path, workspace: Path, now: float) -> dict:
    text = path.read_text(errors="replace")
    # Shape-union parser: this pass classifies files from every writer and
    # era (the desktop's import writer is task-mid, the bridges' are task-last).
    parsed = ltp.parse_task_headers_lenient(text)
    headers = parsed.headers
    task_id = (headers.get("id") or path.stem).strip()
    queued_at, age_from = task_queued_at(headers, task_id, path)
    age_s = max(0, int(now - queued_at))
    source = (headers.get("source") or "").strip()
    tier = ltp.canonical_access_tier(headers.get("access_tier") or "owner")
    row = {
        "file": path.name,
        "id": task_id,
        "source": source,
        "access_tier": tier,
        "channel_id": headers.get("channel_id") or headers.get("chat_id") or "",
        "label": channel_label(headers),
        "age_s": age_s,
        "age_from": age_from,
        "import": False,
    }
    marker = completion_marker(workspace / "results", task_id)
    if marker:
        row.update(verdict="done", reason=f"completion marker found at {marker}")
        return row

    if is_import_task(headers, parsed.body):
        row["import"] = True
        phase, updated = import_status(workspace)
        started = updated is not None and updated >= queued_at
        if started and phase == "done":
            row.update(verdict="done",
                       reason="import landed: data/claude-import/status.json phase done, "
                              "written after this task was queued")
            return row
        if started:
            row.update(verdict="import-resume",
                       reason=f"consented import already started (phase {phase or 'unknown'}); "
                              "resumable — left in tasks/ for the watcher's sweep, never archived")
            return row
        if age_s < IMPORT_FRESH_AGE_S:
            row.update(verdict="fresh",
                       reason=f"consented import not started yet, queued {age_s}s ago "
                              f"(< {IMPORT_FRESH_AGE_S}s): watcher will handle")
            return row
        row.update(verdict="orphan",
                   reason=f"consented import never started in {age_s}s "
                          f"(>= {IMPORT_FRESH_AGE_S}s), no completion marker")
        return row

    if age_s < FRESH_AGE_S:
        row.update(verdict="fresh", reason=f"arrived {age_s}s ago, watcher will handle")
        return row
    row.update(verdict="orphan",
               reason=f"no completion marker, queued {age_s}s ago (>= {FRESH_AGE_S}s)")
    return row


def classify_workspace(workspace: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    tasks_dir = workspace / "tasks"
    out = {"workspace": str(workspace), "tasks": [], "counts": {}}
    if not workspace.is_dir():
        out["note"] = f"workspace not found at {workspace}, skipping"
        return out
    if not tasks_dir.is_dir():
        out["note"] = "no tasks/ dir, nothing to recover"
        return out
    # The watcher's glob is `*.txt` at the top level; whatever it would emit is
    # in scope here, and its `.deferred` suffix (skill step 3) falls outside.
    for path in sorted(p for p in tasks_dir.glob("*.txt") if p.is_file()):
        try:
            row = classify_task(path, workspace, now)
        except OSError as exc:  # unreadable file: conservative, surface it
            row = {"file": path.name, "id": path.stem, "verdict": "orphan",
                   "reason": f"unreadable task file ({exc}); treated as orphan"}
        out["tasks"].append(row)
    counts: dict = {}
    for row in out["tasks"]:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    counts["total"] = len(out["tasks"])
    out["counts"] = counts
    return out


def resolve_workspace() -> Path | None:
    try:
        res = subprocess.run(["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    ws = res.stdout.strip()
    return Path(ws) if res.returncode == 0 and ws else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", help="workspace dir (default: scripts/sutando-config.sh workspace)")
    ap.add_argument("--now", type=float, help="epoch seconds to age against (tests)")
    args = ap.parse_args(argv)
    ws = Path(args.workspace) if args.workspace else resolve_workspace()
    if ws is None:
        print("orphan-check: workspace could not be resolved", file=sys.stderr)
        return 2
    print(json.dumps(classify_workspace(ws, args.now), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
