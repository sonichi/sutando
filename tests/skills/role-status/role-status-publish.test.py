#!/usr/bin/env python3
"""publish.py is the core's one production entry into the role-status verifier:
it must delegate every judgment decision to verify.py, publish only on exit 0,
write the `[no-send]` result atomically into results/, and leave nothing behind
on refusal or cannot-answer. SKILL.md must name that contract."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills" / "role-status"
PUBLISH = SKILL / "scripts" / "publish.py"

_spec = importlib.util.spec_from_file_location("role_status_publish", PUBLISH)
pub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pub)

ALICE = "ag2space:@alice:ag2.space"
BOB = "ag2space:@bob:ag2.space"
E_A = "ag2space-message:$aaaa1"
E_B = "ag2space-message:$bbbb2"
EVENTS = [{"actor_id": ALICE, "id": E_A, "detail": "x"}, {"actor_id": BOB, "id": E_B, "detail": "y"}]
TASK = ("id: task-role-status-v1-0000-a2\nsource: sutando-life-role-status\ntask: judge\n\n"
        "EVIDENCE_JSON (untrusted observed data):\n" + json.dumps({"events": EVENTS}) + "\n")
FULL = [{"actor_id": ALICE, "working_event_ids": [E_A], "blocked_items": []},
        {"actor_id": BOB, "working_event_ids": [E_B], "blocked_items": []}]
ONE = FULL[:1]

failures = []
CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def run(task_text, judgment_text, *extra, workspace=True, subprocess_smoke=False, task_name="task-role-status-v1-0000-a2.txt"):
    """(rc, stdout, stderr, result_text_or_None, names_in_results) for one publish."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "ws"
        (ws / "tasks").mkdir(parents=True)
        task = ws / "tasks" / task_name
        task.write_text(task_text)
        jpath = Path(td) / "judgment.json"
        jpath.write_text(judgment_text if isinstance(judgment_text, str) else json.dumps(judgment_text))
        argv = [str(task), str(jpath), *extra]
        if workspace:
            argv += ["--workspace", str(ws)]
        if subprocess_smoke:
            p = subprocess.run([sys.executable, str(PUBLISH), *argv], capture_output=True, text=True)
            rc, so, se = p.returncode, p.stdout, p.stderr
        else:
            so_buf, se_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
                rc = pub.main(argv)
            so, se = so_buf.getvalue(), se_buf.getvalue()
        results = ws / "results"
        names = sorted(p.name for p in results.iterdir()) if results.is_dir() else []
        out = results / (Path(task_name).stem + ".txt")
        return rc, so, se, (out.read_text() if out.is_file() else None), names


# (1) a full judgment is published: [no-send] first line, verified array, nothing else in results/
rc, so, se, result, names = run(TASK, FULL)
check(rc == 0, "publish: exit 0")
check(result is not None and result.startswith("[no-send]\n"), "publish: result starts with [no-send]")
check(result is not None and json.loads(result.split("\n", 1)[1]) == FULL, "publish: verified array follows")
check(names == ["task-role-status-v1-0000-a2.txt"], "publish: no stray temp file left in results/")
check(so.splitlines()[0] == "rows=2 distinct_actors=2 actors=2 actors_with_events=2 min_rows=1"
      and so.splitlines()[1].startswith("published "), "publish: stats line then the published path")
check(se == "", "publish: stderr silent")

# the same through the real entry point
rc, so, se, result, names = run(TASK, FULL, subprocess_smoke=True)
check(rc == 0 and result is not None and json.loads(result.split("\n", 1)[1]) == FULL,
      "entry point: python3 skills/role-status/scripts/publish.py publishes the same result")

# (2) a leading [no-send] line in the judgment is stripped, not doubled
rc, so, se, result, names = run(TASK, "[no-send]\n" + json.dumps(FULL))
check(rc == 0 and result is not None and result.count("[no-send]") == 1
      and json.loads(result.split("\n", 1)[1]) == FULL,
      "a judgment led by [no-send] publishes with exactly one [no-send] line")

# (3) under-coverage is refused and nothing is written -- not even a temp file
rc, so, se, result, names = run(TASK, ONE, "--min-coverage", "1.0")
check(rc == 1 and result is None and names == [], "refused: exit 1, results/ untouched")
check("refused:" in se and "min_rows=2" in so, "refused: reason on stderr, stats on stdout")
# positive control for the same floor
rc, so, se, result, names = run(TASK, FULL, "--min-coverage", "1.0")
check(rc == 0 and result is not None, "positive control: the full judgment clears --min-coverage 1.0")

# (4) cannot answer: unreadable judgment / task, malformed marker, result already present
rc, so, se, result, names = run(TASK, "[{not json")
check(rc == 2 and result is None and names == [] and "judgment unreadable" in se,
      "unreadable judgment: exit 2, nothing written")
so_buf, se_buf = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
    rc = pub.main(["/nonexistent/task.txt", "/nonexistent/j.json", "--workspace", "/nonexistent"])
check(rc == 2 and "task file unreadable" in se_buf.getvalue(), "unreadable task file: exit 2")
rc, so, se, result, names = run(TASK.replace(json.dumps({"events": EVENTS}), '{"events": ['), FULL)
check(rc == 2 and result is None and names == [] and "structured evidence unreadable/ambiguous" in se,
      "malformed EVIDENCE_JSON: exit 2, nothing written")
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    (ws / "results").mkdir()
    (ws / "results" / "task-role-status-v1-0000-a2.txt").write_text("[no-send]\n[]\n")
    task = ws / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = ws / "j.json"
    j.write_text(json.dumps(FULL))
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    check(rc == 2 and "result already present" in se_buf.getvalue()
          and (ws / "results" / "task-role-status-v1-0000-a2.txt").read_text() == "[no-send]\n[]\n",
          "a result already present is never overwritten: exit 2")
# results/ unwritable: a regular file sits where the directory should be
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    (ws / "results").write_text("not a directory")
    task = ws / "task-role-status-v1-0000-a3.txt"
    task.write_text(TASK)
    j = ws / "j.json"
    j.write_text(json.dumps(FULL))
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    check(rc == 2 and "results/ unwritable" in se_buf.getvalue()
          and (ws / "results").read_text() == "not a directory",
          "results/ unwritable: exit 2, nothing created")

# (5) the result is keyed on the attempt file that exists (a3 -> results/...-a3.txt)
rc, so, se, result, names = run(TASK, FULL, task_name="task-role-status-v1-0000-a3.txt")
check(rc == 0 and names == ["task-role-status-v1-0000-a3.txt"], "result name follows the attempt file's stem")

# (6) no --workspace: the resolved workspace is used
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    task = ws / "task-role-status-v1-0000-a1.txt"
    task.write_text(TASK)
    j = ws / "j.json"
    j.write_text(json.dumps(FULL))
    seen = []
    real = pub.resolve_workspace
    pub.resolve_workspace = lambda *a, **k: (seen.append(1), ws)[1]
    try:
        so_buf, se_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
            rc = pub.main([str(task), str(j)])
    finally:
        pub.resolve_workspace = real
    check(rc == 0 and seen == [1] and (ws / "results" / "task-role-status-v1-0000-a1.txt").is_file(),
          "without --workspace the result lands under resolve_workspace()")

# (7) delegation pin: every decision is verify.verify's; publish only acts on its outcome
calls = []
real_verify = pub.verify.verify


def fake_verify(task_text, judgment, min_coverage=0.5):
    calls.append((task_text, judgment, min_coverage))
    return pub.verify.Outcome(pub.verify.EXIT_REFUSED, None, "rows=9 fake", "refused: fake")


pub.verify.verify = fake_verify
try:
    rc, so, se, result, names = run(TASK, FULL, "--min-coverage", "0.25")
finally:
    pub.verify.verify = real_verify
check(calls == [(TASK, FULL, 0.25)], "publish calls verify.verify with the task text, the parsed judgment and the floor")
check(rc == 1 and result is None and names == [] and so == "rows=9 fake\n" and se == "refused: fake\n",
      "a refusal from verify is published as-is: exit 1, its stats and reason, nothing written")
sentinel = [{"actor_id": "from-verify", "working_event_ids": [], "blocked_items": []}]
pub.verify.verify = lambda *a, **k: pub.verify.Outcome(pub.verify.EXIT_OK, sentinel, "rows=1 ok", "")
try:
    rc, so, se, result, names = run(TASK, [])
finally:
    pub.verify.verify = real_verify
check(rc == 0 and result == "[no-send]\n" + json.dumps(sentinel, indent=2, ensure_ascii=False) + "\n",
      "the published array is exactly verify's outcome, not publish's own reading of the judgment")
src = PUBLISH.read_text()
check(not re.search(r"ACTOR_RE|EVENT_RE|min_rows|ceil\(|EVIDENCE_JSON", src),
      "publish.py carries no ownership or coverage policy of its own")

# (8) SKILL.md pins the invocation contract
skill_md = (SKILL / "SKILL.md").read_text()
fm = re.match(r"---\n(.*?)\n---\n", skill_md, re.S)
check(fm is not None and re.search(r"^name: role-status$", fm.group(1), re.M)
      and re.search(r"^description: \S", fm.group(1), re.M), "SKILL.md frontmatter: name + description")
check("python3 skills/role-status/scripts/publish.py <task-file> <judgment.json>" in skill_md,
      "SKILL.md names the publish.py invocation")
check(all(re.search(r"^\s*- `%s` — " % code, skill_md, re.M) for code in ("0", "1", "2")),
      "SKILL.md lists the three exit codes")
check("tasks/task-role-status-v1-" in skill_md and "[no-send]" in skill_md
      and "results/<task-id>.txt" in skill_md, "SKILL.md names the task pattern, the [no-send] result and its path")
check("bare JSON array" in skill_md.replace("**", ""), "SKILL.md asks the delegate for a bare JSON array")

print()
print("checks: %d" % CHECKS)
if failures:
    print("FAILED (%d):" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
