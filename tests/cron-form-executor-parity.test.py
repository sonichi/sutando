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

# --- two owner markers on one record ---------------------------------------

# A record carrying both listed as codex-owned "WILL NOT RUN" while launchd
# dispatched it.
cr_spec = importlib.util.spec_from_file_location("cron_runner", REPO / "src" / "cron-runner.py")
cr = importlib.util.module_from_spec(cr_spec)
cr_spec.loader.exec_module(cr)

with tempfile.TemporaryDirectory() as td:
    cfg = write(Path(td), [{**CODEX, "name": "convert", "prompt_skill": "digest"}])
    status, _ = dash.upsert_schedule(
        cfg, {"name": "convert", "cron": "0 6 * * *", "shell_command": "echo hi"})
    stored = {j["name"]: j for j in json.loads(cfg.read_text())}["convert"]
    check(status == 200, "writer: the shell conversion is accepted")
    check(stored.get("launchd") is True and "execution" not in stored,
          "writer: converting a codex job to a shell body leaves ONE owner marker "
          f"(got launchd={stored.get('launchd')!r} execution={stored.get('execution')!r})")

# A record that already carries both — hand-edited, or written before the fix.
CONFLICT = {"name": "conflict", "cron": "2 6 * * *", "timezone": "UTC",
            "execution": "codex-task", "launchd": True, "shell_command": "echo hi"}

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = write(root, [CONFLICT, {**CODEX, "name": "sibling", "prompt_skill": "digest"}])

    row = {r["name"]: r for r in dash.list_schedules(cfg)}["conflict"]
    check(row["owner"] == "launchd",
          f"conflict: listed under the owner that fires it (got {row['owner']!r})")
    check(row["kind"] == "shell" and row["prompt_or_skill"] == "echo hi",
          f"conflict: the listing names what launchd will run (got kind={row['kind']!r})")

    # "threw" and "claimed it" are different defects; an uncaught ValueError
    # here IS the tick-wide abort, so catch it and report it as a failed check.
    try:
        loaded = [j["name"] for j in sched.load_jobs(cfg)]
    except ValueError as exc:
        loaded = f"raised: {exc}"
    check(isinstance(loaded, list) and "conflict" not in loaded,
          f"conflict: the codex loader does not claim it (got {loaded})")
    check(isinstance(loaded, list) and "sibling" in loaded,
          "conflict: and one bad record no longer stops every other codex schedule "
          f"from loading (got {loaded})")

    # Why launchd wins: it really does dispatch the record. Drive the real
    # run() rather than assert the premise.
    cr.TASKS_DIR = root / "tasks"
    cr.CRONS_FILE = root / "crons.json"
    cr.STATE_FILE = root / "state" / "cron-runner-state.json"
    cr.CRONS_FILE.write_text(json.dumps([CONFLICT]))
    cr.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # cron-runner evaluates expressions in LOCAL time.
    now = int(datetime(2026, 7, 2, 6, 2).timestamp())
    cr.STATE_FILE.write_text(json.dumps({"conflict": now - 60}))
    ran = []
    cr._run_shell_command = lambda name, target, timeout: ran.append((name, target))
    emitted = cr.run(now_epoch=now)
    check(emitted == ["conflict"] and ran == [("conflict", "echo hi")],
          f"conflict: launchd really is the executor (emitted={emitted} ran={ran})")

# --- raw payload vs display, and session runnability -----------------------

# keweichen r6 P1-1: schedule.list stripped the target while both executors ran
# it verbatim, so the API advertised a different command from the one that fires.
with tempfile.TemporaryDirectory() as td:
    cfg = write(Path(td), [{**CODEX, "name": "pad", "prompt_skill": " morning \n"}])
    row = {r["name"]: r for r in dash.list_schedules(cfg)}["pad"]
    _, codex_target = sched.select_for_executor(sched.load_jobs(cfg)[0], sched.CODEX_FORMS)
    check(row["prompt_or_skill"] == " morning \n",
          f"schedule.list must carry the RAW target the executor runs (got {row['prompt_or_skill']!r})")
    check(row["prompt_or_skill"] == codex_target,
          "the API field and the Codex executor disagree about the payload")
    check(row["description"] == "Runs the /morning skill",
          f"the DESCRIPTION is the field that may tidy (got {row['description']!r})")

with tempfile.TemporaryDirectory() as td:
    cfg = write(Path(td), [{"name": "body", "cron": "0 6 * * *", "launchd": True,
                            "prompt": "\n  preserve leading\ntrailing  \n\n"}])
    row = {r["name"]: r for r in dash.list_schedules(cfg)}["body"]
    check(row["prompt_or_skill"] == "\n  preserve leading\ntrailing  \n\n",
          "a tuned prompt body must reach the API verbatim, whitespace included")

# keweichen r6 P1-2: a shell-carrying record must not suppress step 4's fallback
# while nothing registers a driver — that leaves the session with no loop at all.
MIXED = {"name": "main-loop", "cron": "*/5 * * * *",
         "shell_command": "echo hi", "prompt_skill": "proactive-loop"}
with tempfile.TemporaryDirectory() as td:
    cfg = write(Path(td), [MIXED])
    row = {r["name"]: r for r in dash.list_schedules(cfg)}["main-loop"]
    check(row["owner"] == "session", f"unmarked mixed record is session-owned (got {row['owner']!r})")
    kind, _ = dash.select_for_executor(MIXED, dash.EXECUTOR_FORMS["session"])
    check(kind == dash.MALFORMED,
          "the shared policy must call a shell-carrying record NOT session-runnable")

hc_spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(hc_spec)
sys.modules["health_check"] = hc
try:
    hc_spec.loader.exec_module(hc)
except SystemExit:
    pass
check(hasattr(hc, "check_session_cron_registration"),
      "health-check still exposes the session-cron probe")

skill = (REPO / "skills" / "schedule-crons" / "SKILL.md").read_text()
step4 = skill.split("4. **Fallback")[1].split("\n5. ")[0]
check("step 3 could actually register" in step4,
      "step 4 must exclude entries step 3 skipped, or a mixed record kills the driver")
check("shell_command" in step4 and "NO recurring driver" in step4,
      "step 4 must name the shell-carrying case and its consequence")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
