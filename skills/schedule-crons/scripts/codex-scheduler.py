#!/usr/bin/env python3
"""Durable OS-backed scheduler for explicitly opted-in Codex task jobs.

The scheduler is intentionally a skill implementation, not core infrastructure.
It reads the canonical per-host crons.json and only handles entries whose
``execution`` is ``codex-task``. A launchd timer calls ``tick`` once per minute.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from local_task_protocol import serialize_task_last  # noqa: E402
from task_body_guard import confine_user_content  # noqa: E402
from sutando_config import resolve_core_runtime  # noqa: E402
from task_archive import find_task_file  # noqa: E402


LABEL = "com.sutando.codex-schedules"
STATE_VERSION = 1
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_RETRY_MINUTES = 15
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_ACTIVE_STALE_MINUTES = 60
MAX_CATCHUP_MINUTES = 7 * 24 * 60


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_workspace(repo: Path) -> Path:
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "sutando-config.sh"), "workspace"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def resolve_host_label(repo: Path) -> str:
    result = subprocess.run(
        ["bash", str(repo / "scripts" / "sutando-config.sh"), "host-label"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower() or "job"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value)
    os.replace(tmp, path)


@contextmanager
def scheduler_lock(workspace: Path) -> Iterator[None]:
    lock_path = workspace / "state" / "schedules" / "codex-scheduler.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another scheduler tick is already running")
        yield


def _field_values(spec: str, minimum: int, maximum: int, *, sunday_7: bool = False) -> set[int]:
    values: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            raise ValueError("empty cron field token")
        base, slash, step_text = token.partition("/")
        step = int(step_text) if slash else 1
        if step <= 0:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron value {base!r} outside {minimum}-{maximum}")
        values.update(range(start, end + 1, step))
    if sunday_7 and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def cron_matches(expression: str, local_dt: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have five fields")
    minute = _field_values(fields[0], 0, 59)
    hour = _field_values(fields[1], 0, 23)
    day = _field_values(fields[2], 1, 31)
    month = _field_values(fields[3], 1, 12)
    weekday = _field_values(fields[4], 0, 7, sunday_7=True)
    cron_weekday = (local_dt.weekday() + 1) % 7
    day_match = local_dt.day in day
    weekday_match = cron_weekday in weekday
    # Vixie cron treats restricted day-of-month and day-of-week fields as OR.
    if fields[2] != "*" and fields[4] != "*":
        calendar_match = day_match or weekday_match
    else:
        calendar_match = day_match and weekday_match
    return (
        local_dt.minute in minute
        and local_dt.hour in hour
        and local_dt.month in month
        and calendar_match
    )


def load_jobs(config_path: Path, *, include_main_loop: bool = False) -> list[dict[str, Any]]:
    raw = json.loads(config_path.read_text())
    if not isinstance(raw, list):
        raise ValueError("crons.json must contain a JSON array")
    jobs: list[dict[str, Any]] = []
    names: set[str] = set()
    slugs: dict[str, str] = {}
    for raw_entry in raw:
        if not isinstance(raw_entry, dict):
            continue
        canonical_main_loop = (
            raw_entry.get("name") == "main-loop"
            and raw_entry.get("prompt_skill") == "proactive-loop"
            and not raw_entry.get("launchd")
        )
        implicit_main_loop = (
            include_main_loop and canonical_main_loop and raw_entry.get("execution") is None
        )
        if raw_entry.get("execution") != "codex-task" and not implicit_main_loop:
            continue
        entry = dict(raw_entry)
        if canonical_main_loop:
            # Codex has no session CronCreate surface. Turn the canonical loop
            # into one low-priority task per fire while keeping crons.json
            # unchanged so switching back to Claude restores session ownership.
            entry.pop("prompt_skill", None)
            entry["prompt"] = (
                "Run exactly one proactive-loop pass using skills/proactive-loop/SKILL.md. "
                "Do not arm another recurring loop; the durable Codex scheduler owns the cadence."
            )
            entry["_silent_result"] = True
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("codex-task entries require a non-empty name")
        if name in names:
            raise ValueError(f"duplicate codex-task job name: {name}")
        names.add(name)
        slug = safe_name(name)
        if slug in slugs:
            raise ValueError(
                f"codex-task job names collide after normalization: {slugs[slug]!r} and {name!r}"
            )
        slugs[slug] = name
        if not isinstance(entry.get("cron"), str):
            raise ValueError(f"{name}: cron is required")
        # Validate cron and timezone up front rather than silently missing runs.
        cron_matches(entry["cron"], datetime(2026, 1, 1, tzinfo=timezone.utc))
        try:
            ZoneInfo(entry.get("timezone", DEFAULT_TIMEZONE))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"{name}: unknown timezone") from exc
        if not isinstance(entry.get("prompt") or entry.get("prompt_skill"), str):
            raise ValueError(f"{name}: prompt or prompt_skill is required")
        jobs.append(entry)
    return jobs


def codex_runtime_selected(repo: Path | None = None) -> bool:
    return resolve_core_runtime(repo or repo_root()) == "codex"


def _task_paths(
    workspace: Path, job: dict[str, Any], slot: datetime, attempt: int = 1
) -> tuple[str, Path, Path, Path]:
    stamp = int(slot.timestamp())
    slug = safe_name(job["name"])
    attempt_suffix = "" if attempt == 1 else f"-a{attempt}"
    task_id = f"task-cron-{slug}-{stamp}{attempt_suffix}"
    task_path = workspace / "tasks" / f"{task_id}.txt"
    result_path = workspace / "results" / f"{task_id}.txt"
    proactive_path = workspace / "results" / f"proactive-{slug}-{stamp}.txt"
    return task_id, task_path, result_path, proactive_path


def _task_body(
    workspace: Path, job: dict[str, Any], slot: datetime, now: datetime, attempt: int = 1
) -> tuple[str, str]:
    task_id, _, result_path, proactive_path = _task_paths(workspace, job, slot, attempt)
    prompt = confine_user_content(job.get("prompt") or f"/{job['prompt_skill']}")
    if job.get("delivery") == "proactive":
        prompt += (
            f" Write the concise owner-facing result to {proactive_path}, then write "
            f"[no-send] to {result_path} so this scheduled task is archived without a duplicate reply."
        )
    elif job.get("_silent_result"):
        prompt += (
            f" When the pass is complete, write [no-send] to {result_path} "
            "so the scheduler records completion without messaging the owner."
        )
    body = serialize_task_last(
        [("id", task_id),
         ("timestamp", iso(now)),
         ("source", "cron"),
         ("interaction_type", "system_event"),
         ("access_tier", "owner"),
         ("priority", "low"),
         ("schedule_name", job["name"]),
         ("schedule_slot", iso(slot))],
        prompt)
    return task_id, body


def _enqueue(
    workspace: Path, job: dict[str, Any], slot: datetime, now: datetime, attempt: int = 1
) -> str:
    task_id, body = _task_body(workspace, job, slot, now, attempt)
    task_path = workspace / "tasks" / f"{task_id}.txt"
    atomic_text(task_path, body)
    return task_id


def _current_task_ids(current: dict[str, Any]) -> list[str]:
    values = current.get("task_ids")
    if isinstance(values, list):
        task_ids = [value for value in values if isinstance(value, str) and value]
        if task_ids:
            return task_ids
    task_id = current.get("task_id")
    return [task_id] if isinstance(task_id, str) and task_id else []


def _find_result(workspace: Path, task_ids: list[str]) -> Path | None:
    archive = workspace / "results" / "archive"
    for task_id in task_ids:
        live = workspace / "results" / f"{task_id}.txt"
        if live.exists():
            return live
        legacy = archive / f"{task_id}.txt"
        if legacy.exists():
            return legacy
        archived = sorted(archive.glob(f"*/{task_id}.txt"))
        if archived:
            return archived[-1]
    return None


def _task_is_active(workspace: Path, task_ids: list[str]) -> bool:
    tasks = workspace / "tasks"
    for task_id in task_ids:
        # find_task_file covers bare AND every state suffix; a private glob here
        # recognised .claimed- but not .assigned-, so an assigned job retried.
        if find_task_file(tasks, task_id) is not None:
            return True
        if (tasks / "processed" / f"{task_id}.txt").exists():
            return True
    return False


def _alert(workspace: Path, job_name: str, message: str, now: datetime) -> Path:
    stamp = int(now.timestamp())
    path = workspace / "results" / f"proactive-schedule-alert-{safe_name(job_name)}-{stamp}.txt"
    atomic_text(path, f"Schedule alert — {job_name}: {message}\n")
    return path


def _minute_slots(previous: datetime | None, now: datetime) -> list[datetime]:
    current = now.replace(second=0, microsecond=0)
    if previous is None:
        return [current]
    start = previous.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if start > current:
        return [current]
    count = int((current - start).total_seconds() // 60) + 1
    if count > MAX_CATCHUP_MINUTES:
        start = current - timedelta(minutes=MAX_CATCHUP_MINUTES - 1)
        count = MAX_CATCHUP_MINUTES
    return [start + timedelta(minutes=i) for i in range(count)]


def tick(
    workspace: Path,
    host_label: str,
    now: datetime | None = None,
    *,
    include_main_loop: bool | None = None,
) -> dict[str, Any]:
    now = (now or utc_now()).astimezone(timezone.utc)
    config_path = workspace / "hosts" / host_label / "crons.json"
    state_path = workspace / "state" / "schedules" / "codex-scheduler.json"
    with scheduler_lock(workspace):
        jobs = load_jobs(
            config_path,
            include_main_loop=(
                codex_runtime_selected() if include_main_loop is None else include_main_loop
            ),
        )
        try:
            state = json.loads(state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = {"version": STATE_VERSION, "jobs": {}}
        state.setdefault("jobs", {})
        events: list[dict[str, Any]] = []
        slots = _minute_slots(parse_iso(state.get("last_tick_at")), now)

        for job in jobs:
            name = job["name"]
            job_state = state["jobs"].setdefault(name, {})
            current = job_state.get("current")
            retry_minutes = max(1, int(job.get("retry_minutes", DEFAULT_RETRY_MINUTES)))
            max_attempts = max(1, int(job.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))
            active_stale_minutes = max(
                retry_minutes,
                int(job.get("active_stale_minutes", DEFAULT_ACTIVE_STALE_MINUTES)),
            )

            if current:
                task_ids = _current_task_ids(current)
                result_path = _find_result(workspace, task_ids)
                if result_path is not None:
                    job_state["last_success_at"] = iso(now)
                    job_state["last_success_slot"] = current["slot"]
                    job_state["last_result"] = str(result_path)
                    job_state["current"] = None
                    current = None
                    events.append({"job": name, "event": "completed"})
                else:
                    last_enqueue = parse_iso(current.get("last_enqueue_at")) or now
                    age = (now - last_enqueue).total_seconds()
                    active = _task_is_active(workspace, task_ids)
                    active_stale = active and age >= active_stale_minutes * 60
                    # Never create a second watcher event while any prior attempt
                    # remains queued, claimed, or processed. Retrying a live task
                    # can duplicate irreversible side effects. An attempt cannot
                    # block its schedule forever, though: after the active ceiling,
                    # fail and alert instead of launching an ambiguous duplicate.
                    if active_stale:
                        message = (
                            f"task still active after {active_stale_minutes} minutes; "
                            f"refusing duplicate retry for {current['task_id']}"
                        )
                        alert_path = _alert(workspace, name, message, now)
                        job_state["last_failure_at"] = iso(now)
                        job_state["last_failure"] = message
                        job_state["last_alert"] = str(alert_path)
                        job_state["current"] = None
                        current = None
                        events.append({"job": name, "event": "failed"})
                    elif age >= retry_minutes * 60 and not active:
                        if int(current.get("attempts", 1)) < max_attempts:
                            slot = parse_iso(current["slot"])
                            assert slot is not None
                            attempt = int(current.get("attempts", 1)) + 1
                            task_id = _enqueue(workspace, job, slot, now, attempt)
                            current["attempts"] = attempt
                            current["task_id"] = task_id
                            current["task_ids"] = [*task_ids, task_id]
                            current["last_enqueue_at"] = iso(now)
                            events.append({"job": name, "event": "retried", "attempt": current["attempts"]})
                        else:
                            message = f"no result after {max_attempts} attempts; last task {current['task_id']}"
                            alert_path = _alert(workspace, name, message, now)
                            job_state["last_failure_at"] = iso(now)
                            job_state["last_failure"] = message
                            job_state["last_alert"] = str(alert_path)
                            job_state["current"] = None
                            current = None
                            events.append({"job": name, "event": "failed"})

            if current is None:
                tz = ZoneInfo(job.get("timezone", DEFAULT_TIMEZONE))
                last_slot = parse_iso(job_state.get("last_scheduled_slot"))
                due = [slot for slot in slots if (last_slot is None or slot > last_slot) and cron_matches(job["cron"], slot.astimezone(tz))]
                if due:
                    # Coalesce multiple missed intervals into the newest due slot.
                    slot = due[-1]
                    task_id = _enqueue(workspace, job, slot, now)
                    job_state["last_scheduled_slot"] = iso(slot)
                    job_state["current"] = {
                        "slot": iso(slot),
                        "task_id": task_id,
                        "task_ids": [task_id],
                        "attempts": 1,
                        "first_enqueue_at": iso(now),
                        "last_enqueue_at": iso(now),
                    }
                    events.append({"job": name, "event": "enqueued", "task_id": task_id})

        configured_names = {job["name"] for job in jobs}
        for name in state["jobs"]:
            state["jobs"][name]["configured"] = name in configured_names
        state.update({
            "version": STATE_VERSION,
            "host_label": host_label,
            "config_path": str(config_path),
            "last_tick_at": iso(now),
            "updated_at": iso(now),
        })
        atomic_json(state_path, state)
        return {"events": events, "state_path": str(state_path), "job_count": len(jobs)}


def health(workspace: Path, host_label: str, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    now = (now or utc_now()).astimezone(timezone.utc)
    state_path = workspace / "state" / "schedules" / "codex-scheduler.json"
    problems: list[str] = []
    try:
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return 1, {"ok": False, "problems": ["scheduler has no readable state"], "state_path": str(state_path)}
    last_tick = parse_iso(state.get("last_tick_at"))
    if last_tick is None or (now - last_tick).total_seconds() > 180:
        problems.append("scheduler heartbeat is older than 3 minutes")
    for name, job_state in state.get("jobs", {}).items():
        if job_state.get("configured") is False:
            continue
        if job_state.get("last_failure_at") and (
            not job_state.get("last_success_at")
            or job_state["last_failure_at"] > job_state["last_success_at"]
        ):
            problems.append(f"{name}: latest run failed")
    return (1 if problems else 0), {
        "ok": not problems,
        "host_label": host_label,
        "last_tick_at": state.get("last_tick_at"),
        "problems": problems,
        "jobs": state.get("jobs", {}),
        "state_path": str(state_path),
    }


def install(
    workspace: Path,
    host_label: str,
    repo: Path,
    *,
    write_only: bool = False,
    include_main_loop: bool | None = None,
) -> Path:
    config_path = workspace / "hosts" / host_label / "crons.json"
    jobs = load_jobs(
        config_path,
        include_main_loop=(
            codex_runtime_selected(repo) if include_main_loop is None else include_main_loop
        ),
    )
    if not jobs:
        raise ValueError("no crons.json entries opt in with execution=codex-task")
    logs = workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            str(Path(__file__).resolve()),
            "tick",
            "--workspace", str(workspace),
            "--host-label", host_label,
        ],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        # Missing calendar fields are wildcards in launchd, so this fires once
        # per minute without StartInterval's short-lived-job deferral.
        "StartCalendarInterval": {},
        "StandardOutPath": str(logs / "codex-scheduler.log"),
        "StandardErrorPath": str(logs / "codex-scheduler.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    # Imported HERE rather than at module scope, and only `install` ever needs
    # it. `plistlib` pulls in `xml.parsers.expat` -> the `pyexpat` C extension,
    # which dlopens libexpat: a Python whose pyexpat was built against a
    # different libexpat than it finds at runtime raises ImportError at import
    # time. At module scope that killed EVERY subcommand — including `tick`,
    # which launchd invokes once a minute, and `health`, whose entire job is to
    # report that the scheduler is broken. Neither writes a plist.
    #
    # Measured on a live host 2026-08-03, same file, same commit:
    #
    #   /opt/homebrew/bin/python3 3.14.5 -> `tick` and `health` both die with
    #                                       ImportError: dlopen … pyexpat …
    #                                       _XML_SetAllocTrackerActivationThreshold
    #   /usr/bin/python3          3.9.6  -> both reach real logic
    #
    # A durable scheduler that cannot run is worse than one that is absent: the
    # launchd job stays loaded and `--status` still reports it. Sibling fix to
    # #2588 (same defect shape in src/health-check.py).
    try:
        import plistlib
    except ImportError as exc:  # pragma: no cover - platform-dependent
        raise SystemExit(
            f"codex-scheduler: cannot write the launchd plist — this Python "
            f"cannot import plistlib ({exc.__class__.__name__}: {exc}). "
            f"Re-run `install` with an interpreter whose pyexpat works — the "
            f"system python usually does. `tick` and `health` are unaffected."
        )
    tmp = plist_path.with_name(f".{plist_path.name}.{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    os.replace(tmp, plist_path)
    if not write_only:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True)
        subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], check=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
    return plist_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("tick", "health", "install"))
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--host-label")
    parser.add_argument("--write-only", action="store_true", help="write plist without loading it")
    args = parser.parse_args()
    repo = repo_root()
    workspace = (args.workspace or resolve_workspace(repo)).resolve()
    host_label = args.host_label or resolve_host_label(repo)
    try:
        if args.command == "tick":
            print(json.dumps(tick(workspace, host_label)))
        elif args.command == "health":
            code, report = health(workspace, host_label)
            print(json.dumps(report, indent=2, sort_keys=True))
            return code
        else:
            print(install(workspace, host_label, repo, write_only=args.write_only))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"codex-scheduler: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
