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
                  for an import task, also status.json BOUND TO THIS TASK
                  (`task_id` equal to the task's `id:`) at a TERMINAL phase
                  (`done`, `staged`, `discarded`, `forgot`) — the run this
                  task started reached its end; a staged digest waits on an
                  owner reply, which arrives as a new task.
  import-stalled  an import task whose bound run is at a resumable phase but
                  whose status.json has not moved for IMPORT_STALL_S
                  (3600 s): left in tasks/ (the watcher's sweep still resumes
                  it) but reported, so step 3 lists it in the recovery DM.
  import-resume   an import task whose bound run is at a resumable phase and
                  moved within the stall bound: left in tasks/ for the
                  watcher's sweep, never archived, whatever its age.
  import-unbound  an import task, and a status.json newer than it that
                  carries NO task_id (a legacy writer, or index.py run
                  without --task-id): it may be this task's run or another's,
                  so it is never archived — left in tasks/ and listed in the
                  recovery DM ("an import run started but cannot be matched
                  to this request; say 'import my Claude history' to re-run").
                  Fail toward recovery: a spurious DM line is cheap, a wrong
                  archive loses the owner's import.
  fresh           younger than the age line — 300 s for any task, 1800 s for
                  an import task whose run has not started.
  orphan          no marker and past the age line: step 3 applies.

Identity rule (review of #4177, qingyun-wu + john-the-dev): status.json is one
global file, so its timestamp alone cannot say WHOSE run it reports — an older
run A ending (done/staged/discarded/forgot) after a genuine new request B was
queued read as B's completion, and B was archived without ever executing. A
status counts for a task only when `status.task_id == task.id` (index.py
`--task-id` mints the run and `_common.write_status` stamps every phase with
it). A status bound to a DIFFERENT task is, for this task, the same as no
status: B gets the not-started rule (fresh under 30 min, else orphan with the
recovery line — it was never executed, which is what the owner must hear).

An *import task* is an owner-tier task that carries a run INTENT: the legacy
desktop header `channel_id: onboarding-wizard`, the slash command
`/import-claude-context` as a standalone token, or one of the documented
trigger sentences ("import my Claude history", "import my Claude Code
history", "read my Claude Code sessions", "bring my Claude context along",
case-insensitive). A path or a bare skill-name mention ("… touches
skills/import-claude-context/SKILL.md") is NOT one — review 4177: the bare
substring parked unrelated owner tasks forever.

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
# A started import whose status.json has not moved for this long is reported
# as stalled (precedent: schedule-crons `active_stale_minutes`, default 60).
IMPORT_STALL_S = 3600
IMPORT_CHANNEL = "onboarding-wizard"
IMPORT_STATUS = ("data", "claude-import", "status.json")
# Every phase skills/import-claude-context/scripts write_status() with; the test pins the
# union against those scripts, so a new writer phase fails until it is placed on a side.
IMPORT_TERMINAL_PHASES = frozenset({"done", "staged", "discarded", "forgot"})
IMPORT_RESUMABLE_PHASES = frozenset({"indexed", "extracted", "summarizing", "rolling-up"})
# skills/import-claude-context/SKILL.md's trigger sentences (case-insensitive, any whitespace
# between words) and the slash command as a standalone token (never inside a path).
IMPORT_TRIGGER_SENTENCES = (
    "import my Claude history",
    "import my Claude Code history",
    "read my Claude Code sessions",
    "bring my Claude context along",
)
_IMPORT_INTENT_RE = re.compile(
    "|".join([r"(?<![\w/.\-])/import-claude-context(?![\w/\-])"]
             + [r"\s+".join(re.escape(w) for w in s.split()) for s in IMPORT_TRIGGER_SENTENCES]),
    re.IGNORECASE,
)
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


def import_intent(headers: dict, body: str) -> str:
    """The run intent this task carries, or '' when it is not an import task:
    'channel' for the legacy desktop writer's `channel_id: onboarding-wizard`,
    else the matched slash command / trigger sentence. Only owner-tier tasks
    qualify — the import is an owner-only skill, so a non-owner body carrying
    the sentence earns no exemption. A path or bare skill-name mention is not
    an intent: an owner review task quoting `skills/import-claude-context/…`
    must classify like any other task (orphan at 5 min), not be parked."""
    tier = ltp.canonical_access_tier(headers.get("access_tier") or "owner")
    if tier != "owner":
        return ""
    if (headers.get("channel_id") or "").strip() == IMPORT_CHANNEL:
        return "channel"
    m = _IMPORT_INTENT_RE.search(body)
    return " ".join(m.group(0).split()) if m else ""


def import_status(workspace: Path) -> tuple[str, float | None, str | None]:
    """(phase, updated_at epoch, task_id) from data/claude-import/status.json;
    ("", None, None) when absent or unreadable. task_id is None when the
    status carries none (legacy writer, or a run started without --task-id)."""
    p = workspace.joinpath(*IMPORT_STATUS)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return "", None, None
    if not isinstance(data, dict):
        return "", None, None
    updated = parse_iso(str(data.get("updated_at") or ""))
    if updated is None:
        try:
            updated = p.stat().st_mtime
        except OSError:
            updated = None
    tid = data.get("task_id")
    tid = tid.strip() if isinstance(tid, str) and tid.strip() else None
    return str(data.get("phase") or ""), updated, tid


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

    intent = import_intent(headers, parsed.body)
    if intent:
        row["import"] = True
        row["import_intent"] = intent
        phase, updated, status_task = import_status(workspace)
        present = updated is not None
        if present:
            row["import_task_id"] = status_task
        # The status is THIS task's run only by identity, never by timestamp.
        bound = present and status_task is not None and status_task == task_id
        # A status with no task_id that post-dates the task may be its run or
        # another's: not enough to archive, enough to report.
        unbound = present and status_task is None and updated >= queued_at
        if bound or unbound:
            idle_s = max(0, int(now - updated))
            row["import_phase"] = phase or "unknown"
            row["import_idle_s"] = idle_s
        if bound and phase in IMPORT_TERMINAL_PHASES:
            row.update(verdict="done",
                       reason=f"import run ended: data/claude-import/status.json phase {phase} "
                              f"(terminal), bound to this task (task_id {task_id})")
            return row
        if bound and idle_s >= IMPORT_STALL_S:
            row.update(verdict="import-stalled",
                       reason=f"consented import started but stalled at phase {phase or 'unknown'}: "
                              f"status.json (bound to this task) last moved {idle_s}s ago "
                              f"(>= {IMPORT_STALL_S}s); left in tasks/ so the watcher's sweep can "
                              "resume it, never archived — list it in the recovery DM")
            return row
        if bound:
            row.update(verdict="import-resume",
                       reason=f"consented import already started (phase {phase or 'unknown'}, "
                              f"moved {idle_s}s ago, status.json bound to this task); resumable — "
                              "left in tasks/ for the watcher's sweep, never archived")
            return row
        if unbound:
            row.update(verdict="import-unbound",
                       reason=f"an import run started after this task was queued (phase "
                              f"{phase or 'unknown'}, moved {idle_s}s ago) but status.json carries "
                              "no task_id, so it cannot be matched to this request; left in tasks/, "
                              "never archived — list it in the recovery DM with the re-run line")
            return row
        if not present:
            why = "no status.json"
        elif status_task is not None:
            why = f"status.json belongs to another run (task_id {status_task})"
        else:
            why = "status.json predates this task and carries no task_id"
        if age_s < IMPORT_FRESH_AGE_S:
            row.update(verdict="fresh",
                       reason=f"consented import not started yet ({why}), queued {age_s}s ago "
                              f"(< {IMPORT_FRESH_AGE_S}s): watcher will handle")
            return row
        row.update(verdict="orphan",
                   reason=f"consented import never started ({why}) in {age_s}s "
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
