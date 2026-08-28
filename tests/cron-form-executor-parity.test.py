#!/usr/bin/env python3
"""A listing surface must never advertise work its own executor will not do.

The disagreement this pins: a codex-task entry carrying both shell_command and
prompt_skill made schedule.list say "shell" while the Codex tick enqueued the
fallback SKILL. Both sides now bind one owner-aware contract, so they agree or
both refuse. Drives the real load_jobs/tick and the real list_schedules.
"""
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sched = _load("codex_scheduler",
              REPO / "skills" / "schedule-crons" / "scripts" / "codex-scheduler.py")
dash = _load("dashboard_schedules", REPO / "src" / "dashboard_schedules.py")

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def write(ws: Path, entries) -> Path:
    p = ws / "hosts" / "h" / "crons.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries))
    return p


CODEX = {"execution": "codex-task", "cron": "0 6 * * *", "timezone": "UTC"}

CASES = [
    ("mixed shell+skill", {**CODEX, "name": "mixed",
                           "shell_command": "echo hi", "prompt_skill": "fallback"}),
    ("blank shell+skill", {**CODEX, "name": "blank",
                           "shell_command": "   ", "prompt_skill": "fallback"}),
    ("shell only", {**CODEX, "name": "shellonly", "shell_command": "echo hi"}),
]

for label, entry in CASES:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        cfg = write(ws, [entry])

        # the real listing
        rows = {r["name"]: r for r in dash.list_schedules(cfg)}
        row = rows[entry["name"]]

        # the real loader the tick drives
        try:
            sched.load_jobs(cfg)
            loader = "accepted"
        except ValueError as e:
            loader = f"rejected: {e}"

        check(row["kind"] == "malformed",
              f"{label}: listing refuses to advertise a form codex cannot run "
              f"(got kind={row['kind']!r})")
        check("fallback" not in row["description"],
              f"{label}: no surface names the fallback skill")
        check(loader.startswith("rejected"),
              f"{label}: the codex loader refuses it ({loader[:60]})")
        check(row["owner"] == "codex", f"{label}: owner is codex")

# The positive control: an ordinary codex skill entry must still run, or the
# checks above would pass by refusing everything.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    cfg = write(ws, [{**CODEX, "name": "ok", "prompt_skill": "digest"}])
    rows = {r["name"]: r for r in dash.list_schedules(cfg)}
    jobs = sched.load_jobs(cfg)
    check(rows["ok"]["kind"] == "skill" and rows["ok"]["prompt_or_skill"] == "digest",
          "control: an ordinary codex skill entry still lists as a skill")
    check(len(jobs) == 1, "control: and the codex loader still accepts it")

# And launchd — the one owner that does shell out — is unaffected.
with tempfile.TemporaryDirectory() as td:
    cfg = write(Path(td), [{"name": "poll", "cron": "0 6 * * *", "launchd": True,
                            "shell_command": "bash scripts/poll.sh",
                            "prompt_skill": "fallback"}])
    row = {r["name"]: r for r in dash.list_schedules(cfg)}["poll"]
    check(row["kind"] == "shell" and row["prompt_or_skill"] == "bash scripts/poll.sh",
          "control: launchd still runs and advertises the shell leg")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
