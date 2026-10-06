#!/usr/bin/env python3
"""Step 2 of /task-orphan-check, as a script: classify every live task in
`<workspace>/tasks/` as done / worker-held / fresh / orphan / import-resume, read-only.

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
  worker-held     a worker seat holds a router sentinel for it
                  (deliveries/<recipient>/<id>{.txt,.accepted,.claimed},
                  skills/worker-pool/scripts/worker_delivery.py): the task is that worker's to
                  answer, not a core orphan — left in tasks/ (the worker
                  reads the payload from there), never archived, never DM'd.
  unknown         deliveries/ could not be read, so whether a worker holds
                  the task is unknowable: never archived, listed in the DM.
                  Reading an unreadable deliveries/ as "nobody holds it" is
                  what hands a worker's task to the core.
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
import os
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
import result_markers  # noqa: E402

# The worker-pool skill is optional: without it there is no router, so nobody can
# hold a task. Any stat failure but ENOENT propagates — unreadable is not absent.
_POOL_SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _holder_of(workspace: Path, task_id: str) -> str | None:
    try:
        os.stat(_POOL_SCRIPTS / "worker_delivery.py")
    except FileNotFoundError:
        return None
    if str(_POOL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_POOL_SCRIPTS))
    import worker_delivery  # noqa: E402
    return worker_delivery.holder_of(workspace, task_id)

# "By tier" line buckets; anything else is counted under "other", never dropped.
TIER_BUCKETS = ("owner", "team", "guest", "ambient")


def tier_bucket(tier: str) -> str:
    return tier if tier in TIER_BUCKETS else "other"


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


def neutralize(text: str) -> str:
    """Make untrusted text inert before it enters a trusted result body.
    A square bracket is the structural precondition of every result marker, so
    removing it cannot leave an action behind without duplicating the grammar."""
    return text.replace("[", "(").replace("]", ")")


def channel_label(headers: dict) -> str:
    name = headers.get("room_name") or headers.get("channel_name")
    cid = headers.get("channel_id") or headers.get("chat_id") or ""
    return neutralize(f"{name} ({cid})" if name else (cid or "DM"))


_KEYWORD_COLON_RE = re.compile(r"(?i)\b(file|send|attach|deduped|channel|reply)(:)")


def _defang(text: str) -> str:
    """One more round on an already bracket-neutralized field: the recognized
    keyword's colon, not just the template's brackets, is what lets a forged value
    hijack the template's OWN surrounding `[...]` -- escaping only `[`/`]` leaves
    `file:`/`send:`/`attach:` (and the other leading markers) still readable as
    themselves."""
    return _KEYWORD_COLON_RE.sub(r"\1ː", text)  # ':' -> MODIFIER LETTER TRIANGULAR COLON


def _safe_line(build, *raw_parts: str) -> str:
    """`build(*parts)` with every `raw_parts` entry neutralized, checked against the
    PRODUCTION marker parser, and escalated (bracket, then keyword-colon) until the
    result is proven to carry zero actions -- the one property that actually matters,
    never a guessed-sufficient escaping scheme. Any per-row line interpolating
    untrusted text (a legacy/missing-header task's own body can forge `id` or
    `access_tier`) goes through this, not a hand-built f-string.

    Does NOT require the parser's returned body to equal the input: parse_markers()
    strips surrounding whitespace even on a fully safe line (an empty preview makes
    the bullet end in ": ", which it trims), and that is not a security property --
    only `actions == []` is."""
    parts = [neutralize(p) for p in raw_parts]
    for _ in range(4):
        line = build(*parts)
        if not result_markers.parse_markers(line).actions:
            return line
        parts = [_defang(p) for p in parts]
    raise ValueError(f"could not make a safe line from {raw_parts!r}")


def recovery_line(task_id: str, tier: str, label: str, age_s: int, text_preview: str) -> str:
    """The exact preview bullet step 3 prints verbatim -- never reconstructed from raw
    headers. `task_id` and `tier` come straight from a legacy/missing-header task's own
    body (parse_task_headers_lenient's body-line fallback, canonical_access_tier's
    pass-through of an unknown value); `label` and `preview` are already neutralized by
    their own builders. `task_id` already carries its own `task-` prefix when the task
    file has one (the `id:` header / filename stem) -- never add a second one."""
    age_m = max(0, age_s // 60)
    return _safe_line(
        lambda tid, t: f"- {tid} [{t}, {label}, {age_m}m ago]: {text_preview}",
        task_id, tier)


def stalled_line(task_id: str, phase: str, idle_human: str) -> str:
    """The exact line step 3 prints verbatim for an `import-stalled` row."""
    return _safe_line(
        lambda tid, ph: f"Import stalled at phase {ph or 'unknown'} since {idle_human} "
                        f"({tid}, still in tasks/ — it resumes on the next sweep; say "
                        '"import my Claude history" to resume it now, or '
                        "`/import-claude-context --discard` to drop the run).",
        task_id, phase)


def unbound_line(task_id: str, phase: str, idle_human: str) -> str:
    """The exact line step 3 prints verbatim for an `import-unbound` row."""
    return _safe_line(
        lambda tid, ph: f"An import run started (phase {ph or 'unknown'}, last moved "
                        f"{idle_human} ago) but cannot be matched to this request ({tid}, "
                        'still in tasks/ — its status carries no task id); say "import my '
                        'Claude history" to re-run it, or `/import-claude-context --discard` '
                        "to drop the run.",
        task_id, phase)


def unknown_line(task_id: str, error_text: str) -> str:
    """The exact line step 3 prints verbatim for an `unknown` (deliveries/
    unreadable) row."""
    return _safe_line(
        lambda tid, err: f"Could not read deliveries/ for {tid} ({err}), so whether a "
                         "worker holds it is unknown — left in tasks/, not archived; if "
                         "it is yours it will be answered by the next sweep, otherwise check "
                         "the host's file permissions (TCC on ~/Documents is the known cause).",
        task_id, error_text)


def idle_human(idle_s: int) -> str:
    """`idle_s` as "<N>d <N>h" / "<N>h <N>m" / "<N>m" -- the step-3 prose's own
    "3d 2h" shape, now computed once in code rather than left for an agent to
    eyeball `import_idle_s` and write by hand."""
    idle_s = max(0, int(idle_s))
    days, rem = divmod(idle_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


PREVIEW_CHARS = 100
# The bridge appends its sandbox block AFTER the ask; the parsed body carries both.
_SYSTEM_BLOCK_RE = re.compile(r"^===\s*SUTANDO SYSTEM INSTRUCTIONS\b", re.M)


def preview(body: str) -> str:
    """The `task:` value as the recovery DM shows it: the ask up to the bridge's
    system-instructions block, neutralized, whitespace collapsed, first
    PREVIEW_CHARS chars. Truncation runs LAST so it cannot re-open a marker."""
    m = _SYSTEM_BLOCK_RE.search(body)
    ask = body[:m.start()] if m else body
    return " ".join(neutralize(ask).split())[:PREVIEW_CHARS]


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
    label = channel_label(headers)
    task_preview = preview(parsed.body)
    row = {
        "file": path.name,
        "id": task_id,
        "source": source,
        "access_tier": tier,
        "channel_id": headers.get("channel_id") or headers.get("chat_id") or "",
        "label": label,
        "preview": task_preview,
        "age_s": age_s,
        "recovery_line": recovery_line(task_id, tier, label, age_s, task_preview),
        "age_from": age_from,
        "import": False,
    }
    marker = completion_marker(workspace / "results", task_id)
    if marker:
        row.update(verdict="done", reason=f"completion marker found at {marker}")
        return row

    # holder_of raises on any stat failure but ENOENT; an unreadable deliveries/
    # must not read as "nobody holds it".
    try:
        holder = _holder_of(workspace, task_id)
    except OSError as exc:
        row["unknown_line"] = unknown_line(task_id, str(exc))
        row.update(verdict="unknown",
                   reason=f"deliveries/ unreadable ({exc}); cannot tell whether a worker holds "
                          "this task — left in tasks/, never archived, list it in the recovery DM")
        return row
    if holder is not None:
        row["holder"] = holder
        row.update(verdict="worker-held",
                   reason=f"router sentinel held by worker {holder} (deliveries/{holder}/); the "
                          "task is that worker's to answer — left in tasks/, never archived, "
                          "never listed in the recovery DM")
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
            row["stalled_line"] = stalled_line(task_id, phase, idle_human(idle_s))
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
            row["unbound_line"] = unbound_line(task_id, phase, idle_human(idle_s))
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
            # Unreadable headers mean no tier/preview; path.name is the one safe fact.
            row = {"file": path.name, "id": path.stem, "access_tier": "unknown",
                   "source": "", "label": "DM", "age_s": 0,
                   "recovery_line": recovery_line(path.stem, "unknown", "DM", 0,
                                                  neutralize(f"(unreadable: {exc})")),
                   "verdict": "orphan",
                   "reason": f"unreadable task file ({exc}); treated as orphan"}
        out["tasks"].append(row)
    counts: dict = {}
    for row in out["tasks"]:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    counts["total"] = len(out["tasks"])
    out["counts"] = counts
    return out


# Step 3c bomb-guard: the preview list is truncated past this many deferred
# orphans; the per-tier/per-channel counts stay accurate over the full set.
PREVIEW_CAP = 30
PREVIEW_SHOWN = 20


def recovery_plan(workspace: Path, now: float | None = None) -> dict:
    """Step 3/3b/3c, owned in code: which files this pass archives -- always by
    the row's own `file` (a real `Path.name` off a real glob, so it cannot
    name anything outside `tasks/`) -- and the complete proactive-recovery
    body text, already verified inert.

    `id` is display/marker data a legacy or missing-header task's own BODY
    can forge to anything, including a path-traversal shape (`../x`) or one
    naming an unrelated real file; it is never a filesystem path here or
    anywhere this plan is acted on. The skill performs exactly the moves this
    returns and prints exactly the body this returns -- it does not derive
    either from `id`, and it does not reconstruct the body from individual
    rows by hand.

    Returns `{"archive": [...], "silent_archive": [...], "body": str|None}`;
    `archive`/`silent_archive` are bare filenames under `tasks/`.
    """
    now = time.time() if now is None else now
    result = classify_workspace(workspace, now)
    tasks = result.get("tasks") or []

    archive: list[str] = []
    silent_archive: list[str] = []
    deferred: list[dict] = []
    stalled: list[dict] = []
    unbound: list[dict] = []
    unknown: list[dict] = []

    for row in tasks:
        verdict = row.get("verdict")
        if verdict == "done":
            archive.append(row["file"])
        elif verdict == "orphan":
            if (row.get("source") or "").strip() in ("voice", "phone"):
                silent_archive.append(row["file"])
            else:
                deferred.append(row)
                archive.append(row["file"])
        elif verdict == "import-stalled":
            stalled.append(row)
        elif verdict == "import-unbound":
            unbound.append(row)
        elif verdict == "unknown":
            unknown.append(row)

    body = None
    if deferred or stalled or unbound or unknown:
        body = _recovery_body(deferred, stalled, unbound, unknown)

    return {"archive": archive, "silent_archive": silent_archive, "body": body}


def _recovery_body(deferred: list[dict], stalled: list[dict], unbound: list[dict],
                   unknown: list[dict]) -> str:
    deferred = sorted(deferred, key=lambda r: r.get("age_s", 0))  # most-recent (youngest) first
    tier_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    for row in deferred:
        tb = tier_bucket(row.get("access_tier") or "")
        tier_counts[tb] = tier_counts.get(tb, 0) + 1
        label = row.get("label") or "DM"
        channel_counts[label] = channel_counts.get(label, 0) + 1

    ages = [row.get("age_s", 0) for row in deferred]
    oldest_m = max(ages) // 60 if ages else 0
    newest_m = min(ages) // 60 if ages else 0

    lines = [
        f"Orphan recovery — {len(deferred)} stale tasks from a prior session "
        f"(oldest {oldest_m}m, newest {newest_m}m, no completion markers).",
        "",
        "By tier: " + ", ".join(
            f"{tb} ({tier_counts.get(tb, 0)})" for tb in (*TIER_BUCKETS, "other")
        ) + ".",
        "By channel: " + "; ".join(
            f"{label} — {count}" for label, count in channel_counts.items()
        ) + ("." if channel_counts else "(none)."),
        "",
        "Previews (most-recent first, first ~100 chars of task body; in-band system "
        "instructions stripped):",
    ]
    shown = deferred[:PREVIEW_SHOWN] if len(deferred) > PREVIEW_CAP else deferred
    lines += [row["recovery_line"] for row in shown]
    if len(deferred) > PREVIEW_CAP:
        lines.append(f"+{len(deferred) - PREVIEW_SHOWN} more — see tasks/archive/ for the "
                     "full list.")

    for row in stalled:
        lines += ["", row["stalled_line"]]
    for row in unbound:
        lines += ["", row["unbound_line"]]
    for row in unknown:
        lines += ["", row["unknown_line"]]

    lines += [
        "",
        "To re-queue a task: move its file from tasks/archive/ back to tasks/ -- use the "
        "filename shown in its preview line above (the task's `file`), never a name built "
        "from its `id`, which can differ.",
        "The archived file retains its original body (incl. system-instructions block for "
        "non-owner tasks), so re-queueing preserves sandboxing.",
        "If none still matter: no action needed — they're already archived.",
    ]
    body = "\n".join(lines) + "\n"
    parsed = result_markers.parse_markers(body)
    if parsed.actions or parsed.body.strip() != body.strip():
        raise ValueError("recovery body is not inert against the production marker parser "
                         "despite every row's own line being verified -- a composition bug, "
                         "not a per-row escaping gap")
    return body


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
    ap.add_argument("--plan", action="store_true",
                    help="print step 3/3b/3c's plan (archive filenames + the complete "
                         "recovery body) instead of the raw per-task classification")
    args = ap.parse_args(argv)
    ws = Path(args.workspace) if args.workspace else resolve_workspace()
    if ws is None:
        print("orphan-check: workspace could not be resolved", file=sys.stderr)
        return 2
    out = recovery_plan(ws, args.now) if args.plan else classify_workspace(ws, args.now)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
