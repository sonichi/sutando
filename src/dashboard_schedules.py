#!/usr/bin/env python3
"""Cron parsing, schedule validation and atomic crons.json persistence.

Extracted from `dashboard.py` so the dashboard module is left owning HTTP
adaptation and HTML rendering only. Everything here is pure domain/storage
policy: it receives an already-resolved path and never touches workspace or
host resolution, request/response handling, HTML escaping, cron execution or
launchd.

Behavior is a byte-for-byte move from `dashboard.py` — same cron token set,
same bounds, same response objects and note strings. Nothing was broadened.

Concurrency note (load-bearing — read before refactoring):
`read_crons` is called through the module global inside the mutation
transactions on purpose. `tests/dashboard-editable-schedules.test.py` monkey-
patches it with a deliberately slow read to widen the read→write window so an
UNLOCKED implementation reliably loses a write. Bypassing the global (e.g.
binding it to a local, or inlining the read) leaves that test passing while it
silently stops exercising the race it exists to catch — the repo's only real
concurrency guard. Verified by mutation: dropping `_CRONS_LOCK` makes the
24-thread test fail with lost updates.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from cron_execution_form import (  # noqa: E402
    EXECUTOR_FORMS, LAUNCHD_FORMS, MALFORMED, PROMPT, SHELL, SKILL,
    select_execution_form, select_for_executor)


def cron_field_match(spec: str, value: int) -> bool:
    """Match one cron field value against a spec supporting *, */N, A-B, A,B, N."""
    for token in spec.split(","):
        if token == "*":
            return True
        if token.startswith("*/"):
            try:
                step = int(token[2:])
            except ValueError:
                continue
            if step and value % step == 0:
                return True
        elif "-" in token:
            try:
                a, b = (int(x) for x in token.split("-", 1))
            except ValueError:
                continue
            if a <= value <= b:
                return True
        elif token.isdigit() and int(token) == value:
            return True
    return False


# Per-field value bounds (minute, hour, day-of-month, month, day-of-week).
# dow allows 0-7 (0 and 7 both = Sunday, per cron convention).
CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def cron_field_valid(spec: str, lo: int, hi: int) -> bool:
    """True iff every comma-token of a cron field is syntactically valid and in
    range: ``*``, ``*/N`` (N>0), ``A-B`` (lo<=A<=B<=hi), or a plain integer in
    [lo, hi]. Used to reject a malformed field (e.g. ``foo``) or an out-of-range
    one (e.g. minute ``99``) up front — ``next_run`` can't distinguish
    those from a valid-but-rare cron with no run in the scan horizon (both →
    None), so it must not be the validator (CR #2164, qingyun-wu)."""
    spec = spec.strip()
    if not spec:
        return False
    for token in spec.split(","):
        token = token.strip()
        if token == "*":
            continue
        if token.startswith("*/"):
            step = token[2:]
            if step.isdigit() and int(step) > 0:
                continue
            return False
        if "-" in token:
            a, _, b = token.partition("-")
            if a.isdigit() and b.isdigit() and lo <= int(a) <= int(b) <= hi:
                continue
            return False
        if token.isdigit() and lo <= int(token) <= hi:
            continue
        return False
    return True


def next_run(expr: str, now: datetime, horizon_days: int = 8):
    """Next datetime matching a 5-field cron expr (minute hour dom month dow),
    scanning minute-by-minute up to horizon_days. Returns datetime or None.

    dom/dow are AND-combined (sufficient for our crons, which restrict only one
    of them); the rare cron OR-semantics edge case is not modeled.
    """
    parts = expr.split()
    if len(parts) != 5:
        return None
    mnt, hr, dom, mon, dow = parts
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now + timedelta(days=horizon_days)
    while t <= end:
        cron_dow = (t.weekday() + 1) % 7  # python Mon=0..Sun=6 -> cron Sun=0..Sat=6
        if (cron_field_match(mnt, t.minute) and cron_field_match(hr, t.hour)
                and cron_field_match(dom, t.day) and cron_field_match(mon, t.month)
                and cron_field_match(dow, cron_dow)):
            return t
        t += timedelta(minutes=1)
    return None


def read_crons(path: Path) -> list:
    """Load the cron job list; [] on missing/invalid (never raises)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        jobs = json.loads(p.read_text())
        return jobs if isinstance(jobs, list) else []
    except (OSError, ValueError):
        return []


_RUN_PREFIX_RE = re.compile(r"^Run:?\s*")


def schedule_owner(job: dict) -> str:
    """Which scheduler fires this entry: the OS-backed codex runner, the
    launchd cron-runner, a self-pacing /loop, or the live session's cron."""
    # `launchd` first: cron-runner gates on it alone and never reads
    # `execution`, so a record carrying both is fired by launchd.
    if job.get("launchd"):
        return "launchd"
    if job.get("execution") == "codex-task":
        return "codex"
    if job.get("loop") == "dynamic":
        return "dynamic-loop"
    return "session"


def list_schedules(path: Path, now: datetime | None = None) -> list[dict]:
    """Every crons.json entry — no owner filtering — with its computed next
    run. The read policy behind the dashboard Schedules card and SCP
    schedule.list; [] on missing/invalid file (never raises).

    Per entry: name, cron ("" for a dynamic loop), kind (shell|skill|prompt|
    malformed),
    prompt_or_skill (shell command, skill name or prompt text; "" when
    malformed), owner (session|launchd|
    codex|dynamic-loop — who fires it), description (UNescaped — HTML escaping
    is presentation), next_run (display string: "Mon 21:00 (in 2m)" | ">7d" |
    "invalid"), next_run_ts (epoch seconds, None when uncomputable)."""
    now = now or datetime.now()
    out = []
    for job in read_crons(Path(path)):
        expr = job.get("cron", "")
        skill = job.get("prompt_skill")
        nxt = next_run(expr, now) if expr else None
        if nxt:
            mins = int((nxt - now).total_seconds() // 60)
            if mins < 60:
                rel = f"in {mins}m"
            elif mins < 1440:
                rel = f"in {mins // 60}h{mins % 60:02d}m"
            else:
                rel = f"in {mins // 1440}d{(mins % 1440) // 60}h"
            next_str = f'{nxt.strftime("%a %H:%M")} ({rel})'
        else:
            next_str = ">7d" if expr else "invalid"
        # Owner-aware, and shared with every executor: describing a form this
        # entry's own scheduler cannot run is the silent disagreement.
        owner = schedule_owner(job)
        kind, raw_target = select_for_executor(
            job, EXECUTOR_FORMS.get(owner, LAUNCHD_FORMS))
        # The selector returns the payload verbatim so execution is exact;
        # these are display fields, so they are the ones that may tidy it.
        target = raw_target.strip()
        if job.get("description") and kind != MALFORMED:
            desc = job["description"]
        elif kind == SHELL:
            desc = f"Runs shell command: {target}"
        elif kind == SKILL:
            desc = f"Runs the /{target} skill"
        elif kind == MALFORMED:
            # Never fall through to the skill leg: the runner skips this entry
            # entirely, so any skill named here would describe work that stops.
            desc = f"WILL NOT RUN — {target}"
        else:
            _p = _RUN_PREFIX_RE.sub("", target)
            desc = (_p[:100] + "…") if len(_p) > 100 else _p
        out.append({"name": job.get("name", "?"), "cron": expr,
                    "kind": kind,
                    "prompt_or_skill": "" if kind == MALFORMED else target,
                    "owner": owner,
                    "description": desc,
                    "next_run": next_str,
                    "next_run_ts": int(nxt.timestamp()) if nxt else None})
    return out


# Serializes the full read-merge-write transaction for schedule mutations.
# dashboard runs under ThreadingHTTPServer, so two overlapping POST/DELETE
# requests would otherwise both read the old list, and the later os.replace
# could clobber the earlier acknowledged write (or raise FileNotFoundError off a
# shared temp path). Every upsert/delete holds this lock across read→merge→write
# so mutations are linearizable (CR #2164, qingyun-wu). A module-level Lock is
# process-wide; the dashboard is single-process, so it fully covers the server.
_CRONS_LOCK = threading.Lock()


def write_crons(path: Path, jobs: list) -> None:
    """Persist the cron list atomically (tmp + os.replace) so a crash mid-write
    can't leave a truncated crons.json. Callers MUST hold _CRONS_LOCK for the
    surrounding read-modify-write; the per-writer temp name (pid+uuid) is only
    defense in depth so two writers can never collide on one .tmp path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(jobs, indent=2) + "\n")
        os.replace(tmp, p)
    except OSError:
        # Never leave an orphan temp behind on a failed write.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def validate_job(job: dict) -> str | None:
    """Return an error string if the job is invalid, else None. A job needs a
    non-empty name, a valid 5-field cron expr, and exactly one execution body:
    shell_command, prompt, or prompt_skill."""
    if not isinstance(job, dict):
        return "job must be an object"
    name = (job.get("name") or "").strip()
    if not name:
        return "name is required"
    expr = (job.get("cron") or "").strip()
    fields = expr.split()
    if len(fields) != 5:
        return "cron must be a 5-field expression (min hour dom month dow)"
    # Validate each field's SYNTAX + range directly. next_run returns None
    # for a malformed cron AND for a valid-but-no-run-in-horizon one, so it can't
    # be the gate — a garbage expr like "foo bar baz qux quux" would slip through
    # and be persisted as an uncomputable schedule (CR #2164, qingyun-wu).
    if not all(cron_field_valid(f, lo, hi) for f, (lo, hi) in zip(fields, CRON_BOUNDS)):
        return f"invalid cron expression: {expr!r}"
    has_prompt = bool((job.get("prompt") or "").strip())
    has_skill = bool((job.get("prompt_skill") or "").strip())
    has_shell = bool((job.get("shell_command") or "").strip())
    if sum((has_shell, has_prompt, has_skill)) != 1:
        if "shell_command" not in job:
            # Keep the established API error for the legacy two-form schema.
            return "provide exactly one of prompt or prompt_skill"
        return "provide exactly one of shell_command, prompt or prompt_skill"
    return None


def upsert_schedule(path: Path, body: dict) -> tuple[int, dict]:
    """Pure add/edit: merge `body` onto an existing job by name (so an inline
    cron-only edit inherits its prompt/prompt_skill), validate the merged
    result, persist. Returns (http_status, response_obj). Unit-tested; the
    do_POST handler is a thin wrapper around this."""
    if not isinstance(body, dict):
        return 400, {"error": "malformed JSON body"}
    # Reject a non-string scalar in any text field before calling a string method
    # on it. `{"name": 123}` (or a non-string cron/prompt/…) would otherwise raise
    # AttributeError on `.strip()` and close the request with no JSON 400
    # (CR #2164, qingyun-wu). `null` is allowed here — it's handled downstream as
    # "field absent".
    for _k in ("name", "cron", "prompt", "prompt_skill", "shell_command", "description"):
        _v = body.get(_k)
        if _v is not None and not isinstance(_v, str):
            return 400, {"error": f"{_k} must be a string"}
    name = (body.get("name") or "").strip()
    if not name:
        return 400, {"error": "name is required"}
    # Serialize the whole read→merge→validate→write transaction. Under
    # ThreadingHTTPServer two overlapping upserts (or an upsert racing a delete)
    # would both read the pre-mutation list and the second write would silently
    # clobber the first acknowledged update (CR #2164). The lock makes the
    # transaction linearizable; delete_schedule takes the same lock.
    with _CRONS_LOCK:
        # Module-global call is deliberate — see the concurrency note in the
        # module docstring. Do not bind this to a local.
        jobs = read_crons(path)
        existing = next((j for j in jobs if j.get("name") == name), None)
        merged = dict(existing) if existing else {}
        merged["name"] = name
        for k in ("cron", "prompt", "prompt_skill", "shell_command", "description"):
            if k in body and str(body.get(k)).strip():
                merged[k] = str(body[k]).strip()
        if (body.get("shell_command") or "").strip():
            merged.pop("prompt", None)
            merged.pop("prompt_skill", None)
        elif (body.get("prompt_skill") or "").strip():
            merged.pop("prompt", None)
            merged.pop("shell_command", None)
        elif (body.get("prompt") or "").strip():
            merged.pop("prompt_skill", None)
            merged.pop("shell_command", None)
        if (merged.get("shell_command") or "").strip():
            # Only the launchd runner executes shell jobs and the session
            # scheduler skips them, so an unflagged one would never run at all.
            merged["launchd"] = True
            # `merged` starts from the on-disk entry, so a codex-owned job
            # switched to a shell body would keep both owner markers.
            merged.pop("execution", None)
        err = validate_job(merged)
        if err:
            return 400, {"error": err}
        # Persist the MERGED job — it starts from the existing on-disk entry, so
        # scheduler-specific fields (execution, delivery, retry_minutes, timezone,
        # launchd, room, room_id, …) are preserved. A prior version rebuilt a
        # name/cron/prompt/description whitelist here, silently dropping those on any
        # edit — saving a cron change could disable a Codex job or detach its room
        # (CR #2164, qingyun-wu). The prompt/prompt_skill exclusivity was already
        # applied to `merged` above, so it's write-ready.
        jobs = [j for j in jobs if j.get("name") != name]
        jobs.append(merged)
        write_crons(path, jobs)
        return 200, {"ok": True, "name": name, "count": len(jobs),
                     "note": "Saved. Takes effect on the next /schedule-crons run (restart)."}


def delete_schedule(path: Path, name: str) -> tuple[int, dict]:
    """Pure delete-by-name. Returns (http_status, response_obj)."""
    # Same transaction lock as upsert_schedule — a delete racing an upsert must
    # not read a stale list and re-persist a job the upsert just removed, or vice
    # versa (CR #2164).
    with _CRONS_LOCK:
        # Module-global call is deliberate — see the concurrency note in the
        # module docstring. Do not bind this to a local.
        jobs = read_crons(path)
        remaining = [j for j in jobs if j.get("name") != name]
        if len(remaining) == len(jobs):
            return 404, {"error": "not found", "name": name}
        write_crons(path, remaining)
        return 200, {"deleted": name, "count": len(remaining),
                     "note": "Removed. Takes effect on the next /schedule-crons run (restart)."}
