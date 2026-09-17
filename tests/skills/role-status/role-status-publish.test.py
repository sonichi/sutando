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
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills" / "role-status"
PUBLISH = SKILL / "scripts" / "publish.py"
PUBLISH_SH = SKILL / "scripts" / "publish.sh"

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


def claim_of(ws, task_name="task-role-status-v1-0000-a2.txt"):
    return ws / "state" / "role-status" / "claims" / Path(task_name).stem


# (1) a full judgment is published: [no-send] first line, verified array, nothing else in results/
rc, so, se, result, names = run(TASK, FULL)
check(rc == 0, "publish: exit 0")
check(result is not None and result.startswith("[no-send]\n"), "publish: result starts with [no-send]")
check(result is not None and json.loads(result.split("\n", 1)[1]) == FULL, "publish: verified array follows")
check(names == ["task-role-status-v1-0000-a2.txt"], "publish: no stray temp file left in results/")
check(so.splitlines()[0] == "rows=2 distinct_actors=2 actors=2 actors_with_events=2 min_rows=1"
      and so.splitlines()[1].startswith("published "), "publish: stats line then the published path")
check(se == "", "publish: stderr silent")
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = Path(td) / "j.json"
    j.write_text(json.dumps(FULL))
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    check(rc == 0 and claim_of(ws).is_file()
          and claim_of(ws).read_text() == str(ws / "results" / "task-role-status-v1-0000-a2.txt") + "\n",
          "publish: the claim state/role-status/claims/<task-id> is created and names the result path")
    check(sorted(p.name for p in (ws / "state" / "role-status" / "claims").iterdir()) == ["task-role-status-v1-0000-a2"],
          "publish: exactly one claim file, no temp file beside it")

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
    out = ws / "results" / "task-role-status-v1-0000-a2.txt"
    check(rc == 2 and "result already published (claim %s) -- recovered from %s" % (claim_of(ws), out) in se_buf.getvalue()
          and out.read_text() == "[no-send]\n[]\n",
          "a result already published with no claim on file is never overwritten: exit 2")
    check(claim_of(ws).read_text() == str(out) + "\n",
          "a result already published with no claim on file: the claim is committed against it")
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
    check(rc == 0 and seen == [1] and (ws / "results" / "task-role-status-v1-0000-a1.txt").is_file()
          and claim_of(ws, "task-role-status-v1-0000-a1.txt").is_file(),
          "without --workspace the result and the claim land under resolve_workspace()")
check(pub.claim_path(Path("/w"), "/x/tasks/task-role-status-v1-0000-a3.txt")
      == Path("/w") / "state" / "role-status" / "claims" / "task-role-status-v1-0000-a3",
      "claim_path: <workspace>/state/role-status/claims/<task-file-stem>")

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
check("bash skills/role-status/scripts/publish.sh <task-file> <judgment.json>" in skill_md,
      "SKILL.md names the publish.sh invocation")
check(not re.search(r"(^|[\s`(])python3\s", skill_md, re.M), "SKILL.md invokes no bare python3")
check("state/role-status/claims/<task-id>" in skill_md and "never removed" in skill_md
      and "by hand" in skill_md, "SKILL.md names the claim path, its retention and hand removal")
check(all(w in skill_md for w in ("**in progress**", "**committed**", "**rolled back**", "**abandoned**", "flock"))
      and "publication in progress" in skill_md and "recovered from" in skill_md,
      "SKILL.md documents the claim state machine and its two refusal messages")
check(all(re.search(r"^\s*- `%s` — " % code, skill_md, re.M) for code in ("0", "1", "2")),
      "SKILL.md lists the three exit codes")
check("tasks/task-role-status-v1-" in skill_md and "[no-send]" in skill_md
      and "results/<task-id>.txt" in skill_md, "SKILL.md names the task pattern, the [no-send] result and its path")
check("bare JSON array" in skill_md.replace("**", ""), "SKILL.md asks the delegate for a bare JSON array")

# (9) exclusive publication: two publishers past every check race at the claim lock;
# exactly one wins and commits, the loser exits 2 and never touches the result.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j1, j2 = Path(td) / "j1.json", Path(td) / "j2.json"
    j1.write_text(json.dumps(FULL))
    j2.write_text(json.dumps(FULL[::-1]))       # distinct payload per racer: names the winner
    out = ws / "results" / "task-role-status-v1-0000-a2.txt"
    payload_of = {"a": FULL, "b": FULL[::-1]}
    rcs = {}

    def go(name, j):
        rcs[name] = pub.main([str(task), str(j), "--workspace", str(ws)])

    # sequential control: the second publish finds the first one's result
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        go("a", j1)
        go("b", j2)
    check([rcs["a"], rcs["b"]] == [0, 2] and out.read_text().endswith(json.dumps(FULL, indent=2) + "\n"),
          "sequential control: rcs [0, 2], the first payload stays")
    check("cannot answer: result already published (claim %s)" % claim_of(ws) in se_buf.getvalue(),
          "sequential control: the loser is refused on the claim, which names its path")
    out.unlink()
    claim_of(ws).unlink()
    rcs.clear()

    barrier = threading.Barrier(2, timeout=10)
    real_open = pub.claim_open

    def open_after_barrier(path):
        barrier.wait()                      # both past every check, neither holds the claim yet
        return real_open(path)

    so_buf, se_buf = io.StringIO(), io.StringIO()
    pub.claim_open = open_after_barrier
    try:
        with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
            threads = [threading.Thread(target=go, args=("a", j1)), threading.Thread(target=go, args=("b", j2))]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
    finally:
        pub.claim_open = real_open
    winners = [n for n, rc in rcs.items() if rc == 0]
    check(sorted(rcs.values()) == [0, 2], "raced: exactly one rc 0 and one rc 2 (%r)" % rcs)
    check(len(winners) == 1 and claim_of(ws).read_text() == str(out) + "\n",
          "raced: the winner's claim is committed with the result path")
    check(len(winners) == 1 and out.read_text()
          == "[no-send]\n" + json.dumps(payload_of[winners[0]], indent=2, ensure_ascii=False) + "\n",
          "raced: the result is the winner's payload, untouched by the loser")
    check(so_buf.getvalue().count("published ") == 1 and se_buf.getvalue().count("cannot answer:") == 1
          and re.search(r"cannot answer: (publication in progress|result already published) \(claim ", se_buf.getvalue()),
          "raced: one `published` line, one refusal on the claim (in progress or already published)")
    check(sorted(p.name for p in (ws / "results").iterdir()) == ["task-role-status-v1-0000-a2.txt"],
          "raced: no stray temp file left in results/")
# the writers themselves: an existing target is never replaced, the temp file never lingers;
# the claim lock is exclusive across descriptors and its body is one write.
with tempfile.TemporaryDirectory() as td:
    target = Path(td) / "r.txt"
    target.write_text("first")
    check(pub.link_exclusive(str(target), b"second") is False and target.read_text() == "first"
          and sorted(p.name for p in Path(td).iterdir()) == ["r.txt"],
          "link_exclusive on an existing target: False, content kept, no temp file")
    check(pub.link_exclusive(str(Path(td) / "n.txt"), b"new") is True
          and (Path(td) / "n.txt").read_bytes() == b"new"
          and sorted(p.name for p in Path(td).iterdir()) == ["n.txt", "r.txt"],
          "link_exclusive on a fresh target: True, written, no temp file")
    c = str(Path(td) / "c")
    fd = pub.claim_open(c)
    check(fd is not None and os.path.getsize(c) == 0 and pub.claim_body(fd) == "",
          "claim_open creates the claim empty and holds it")
    check(pub.claim_open(c) is None, "claim_open on a held claim: None (the lock is exclusive across descriptors)")
    check(pub.claim_commit(fd, "/r/x.txt") is None and Path(c).read_text() == "/r/x.txt\n" and pub.claim_body(fd) == "/r/x.txt",
          "claim_commit writes the result path; claim_body reads it back")
    os.close(fd)
    fd2 = pub.claim_open(c)
    check(fd2 is not None and pub.claim_body(fd2) == "/r/x.txt" and os.path.getsize(c) == len("/r/x.txt\n"),
          "claim_open on a released committed claim: reopens without truncating")
    os.close(fd2)
check("write_atomic" not in src and "os.replace" not in src,
      "publish.py's production write is the exclusive one, never a replace")

# (10) the claim outlives the result: after the [no-send] consumer's archive rename
# (task-bridge archiveFile) a second publisher is still refused, the archive untouched.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j1, j2 = Path(td) / "j1.json", Path(td) / "j2.json"
    j1.write_text(json.dumps(FULL))
    j2.write_text(json.dumps(FULL[::-1]))
    live = ws / "results" / "task-role-status-v1-0000-a2.txt"
    archived = ws / "results" / "archive" / "2026-09" / "task-role-status-v1-0000-a2.txt"
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc1 = pub.main([str(task), str(j1), "--workspace", str(ws)])
        archived.parent.mkdir(parents=True)
        os.rename(str(live), str(archived))          # the consumer's archive step
        rc2 = pub.main([str(task), str(j2), "--workspace", str(ws)])
    first = "[no-send]\n" + json.dumps(FULL, indent=2, ensure_ascii=False) + "\n"
    check([rc1, rc2] == [0, 2], "archive interleave: rcs [0, 2] (%r)" % [rc1, rc2])
    check("cannot answer: result already published (claim %s)" % claim_of(ws) in se_buf.getvalue(),
          "archive interleave: the second publisher is refused on the claim")
    check(archived.read_text() == first and not live.exists()
          and sorted(p.name for p in (ws / "results").iterdir()) == ["archive"],
          "archive interleave: archived payload unchanged, no new live result, no temp file")
    check(so_buf.getvalue().count("published ") == 1, "archive interleave: exactly one `published` line")
    # (c) a fresh task id with no claim publishes beside it
    task3 = ws / "tasks" / "task-role-status-v1-0000-a3.txt"
    task3.write_text(TASK)
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc3 = pub.main([str(task3), str(j2), "--workspace", str(ws)])
    check(rc3 == 0 and (ws / "results" / "task-role-status-v1-0000-a3.txt").is_file()
          and claim_of(ws, "task-role-status-v1-0000-a3.txt").is_file(),
          "a fresh task id with no claim publishes; the a2 claim does not bar a3")
# (d) a COMMITTED stale claim with no result anywhere still refuses: the claim is the
# record of truth (the result was archived or removed); recovery is removing it by hand.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = Path(td) / "j.json"
    j.write_text(json.dumps(FULL))
    claim_of(ws).parent.mkdir(parents=True)
    claim_of(ws).write_text("stale\n")
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    check(rc == 2 and "result already published (claim " in se_buf.getvalue()
          and sorted(p.name for p in (ws / "results").iterdir()) == [],
          "a committed stale claim with no result anywhere: exit 2, nothing written (results/ empty, no temp file)")
    check(claim_of(ws).read_text() == "stale\n", "a committed stale claim is left exactly as found")
    claim_of(ws).unlink()                            # the documented hand recovery
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    check(rc == 0 and (ws / "results" / "task-role-status-v1-0000-a2.txt").is_file(),
          "positive control: with the claim removed by hand the same publish lands")

# (10b) crash consistency of the two-phase claim, against the production writer.
def fresh_ws(td, judgment=FULL):
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = Path(td) / "j.json"
    j.write_text(json.dumps(judgment))
    return ws, task, j


def publish_in(ws, task, j):
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    return rc, so_buf.getvalue(), se_buf.getvalue()


# (a) the link fails for a reason other than an existing target: rolled back, retryable
with tempfile.TemporaryDirectory() as td:
    ws, task, j = fresh_ws(td)
    live = ws / "results" / "task-role-status-v1-0000-a2.txt"
    real_link = os.link

    def link_denied(src, dst, *a, **k):
        os.link = real_link                          # once
        raise PermissionError(13, "Permission denied", dst)

    os.link = link_denied
    try:
        rc, so, se = publish_in(ws, task, j)
    finally:
        os.link = real_link
    check(rc == 2 and "cannot answer: results/ unwritable: [Errno 13] Permission denied" in se and not live.exists(),
          "link raises PermissionError: exit 2 with the actual error, no result")
    check(not claim_of(ws).exists() and sorted(p.name for p in (ws / "results").iterdir()) == [],
          "link raises PermissionError: the claim is rolled back, no temp file")
    rc, so, se = publish_in(ws, task, j)
    check(rc == 0 and live.is_file() and claim_of(ws).read_text() == str(live) + "\n",
          "positive control: the plain retry publishes and commits the claim")
# the link finds a result that appeared after the lookup: rolled back, the result kept
with tempfile.TemporaryDirectory() as td:
    ws, task, j = fresh_ws(td)
    live = ws / "results" / "task-role-status-v1-0000-a2.txt"
    real_link = os.link

    def link_exists(src, dst, *a, **k):
        os.link = real_link
        live.write_text("[no-send]\n[]\n")           # a hand-written result lands mid-flight
        raise FileExistsError(17, "File exists", dst)

    os.link = link_exists
    try:
        rc, so, se = publish_in(ws, task, j)
    finally:
        os.link = real_link
    check(rc == 2 and "cannot answer: result already present at %s" % live in se
          and live.read_text() == "[no-send]\n[]\n" and not claim_of(ws).exists(),
          "link meets an existing target: exit 2, that result kept, the claim rolled back")
# (b) crash simulation: an empty, unlocked claim and no result anywhere is abandoned -> reused
with tempfile.TemporaryDirectory() as td:
    ws, task, j = fresh_ws(td)
    live = ws / "results" / "task-role-status-v1-0000-a2.txt"
    claim_of(ws).parent.mkdir(parents=True)
    claim_of(ws).touch()
    rc, so, se = publish_in(ws, task, j)
    check(rc == 0 and live.is_file() and se == "" and claim_of(ws).read_text() == str(live) + "\n",
          "an empty unlocked claim with no result anywhere: the publish proceeds and commits it")
# (c) crash simulation: an empty claim while the result already sits in the archive
with tempfile.TemporaryDirectory() as td:
    ws, task, j = fresh_ws(td, FULL[::-1])
    live = ws / "results" / "task-role-status-v1-0000-a2.txt"
    archived = ws / "results" / "archive" / "2026-09" / "task-role-status-v1-0000-a2.txt"
    archived.parent.mkdir(parents=True)
    archived.write_text("[no-send]\n[]\n")
    claim_of(ws).parent.mkdir(parents=True)
    claim_of(ws).touch()
    rc, so, se = publish_in(ws, task, j)
    check(rc == 2 and "result already published (claim %s) -- recovered from %s" % (claim_of(ws), archived) in se
          and not live.exists() and archived.read_text() == "[no-send]\n[]\n",
          "an empty claim with the result in the archive: exit 2, nothing written, archive untouched")
    check(claim_of(ws).read_text() == str(archived) + "\n", "the abandoned claim is committed naming the archived result")
    # an archive copy carrying an epoch suffix is found too
    archived.rename(archived.with_name("task-role-status-v1-0000-a2.1758130000.txt"))
    claim_of(ws).write_text("")
    rc, so, se = publish_in(ws, task, j)
    check(rc == 2 and "recovered from " in se and "a2.1758130000.txt" in se and not live.exists(),
          "an archive copy with an epoch suffix is found by the recovery")
# another publisher holds the claim: refused without touching it
with tempfile.TemporaryDirectory() as td:
    ws, task, j = fresh_ws(td)
    claim_of(ws).parent.mkdir(parents=True)
    holder = pub.claim_open(str(claim_of(ws)))
    try:
        rc, so, se = publish_in(ws, task, j)
    finally:
        os.close(holder)
    check(rc == 2 and "cannot answer: publication in progress (claim %s)" % claim_of(ws) in se
          and claim_of(ws).exists() and os.path.getsize(str(claim_of(ws))) == 0
          and sorted(p.name for p in (ws / "results").iterdir()) == [],
          "a held claim: exit 2 `publication in progress`, the claim left in place, nothing written")
    rc, so, se = publish_in(ws, task, j)
    check(rc == 0, "positive control: once released, the same publish lands")
# the commit write fails after the link: the result stands, the claim is dropped, a retry recovers
with tempfile.TemporaryDirectory() as td:
    ws, task, j = fresh_ws(td)
    live = ws / "results" / "task-role-status-v1-0000-a2.txt"
    real_commit = pub.claim_commit
    pub.claim_commit = lambda fd, note: "No space left on device"
    try:
        rc, so, se = publish_in(ws, task, j)
    finally:
        pub.claim_commit = real_commit
    check(rc == 0 and live.is_file() and "published " in so and "warning: claim not committed" in se
          and not claim_of(ws).exists(), "commit fails after the link: rc 0, result stands, the claim is dropped, warned")
    rc, so, se = publish_in(ws, task, j)
    check(rc == 2 and "recovered from %s" % live in se and claim_of(ws).read_text() == str(live) + "\n",
          "the retry recovers: exit 2, the claim committed against the live result")

# (11) UTF-8 output whatever the locale: a verifier-accepted non-ASCII subject_id and
# row detail survive LC_ALL=C PYTHONUTF8=0 through the real entry point.
C_ENV = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
C_ENV.update({"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"})
UNI = [{"actor_id": ALICE, "working_event_ids": [], "detail": "détail — 工作中",
        "blocked_items": [{"subject_id": "sujet:é✓", "evidence_event_ids": [E_A], "reason_code": "waiting_for_review"}]},
       {"actor_id": BOB, "working_event_ids": [E_B], "blocked_items": []}]
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = Path(td) / "j.json"
    j.write_text(json.dumps(UNI))
    p = subprocess.run([sys.executable, str(PUBLISH), str(task), str(j), "--workspace", str(ws)],
                       capture_output=True, env=C_ENV)
    out = ws / "results" / "task-role-status-v1-0000-a2.txt"
    check(p.returncode == 0 and out.is_file() and "Traceback" not in p.stderr.decode("utf-8", "replace"),
          "LC_ALL=C: a non-ASCII subject_id publishes, rc 0, no traceback (stderr=%r)" % p.stderr[:200])
    body = out.read_bytes().decode("utf-8")
    check(json.loads(body.split("\n", 1)[1]) == UNI, "LC_ALL=C: the result decodes as UTF-8 with every character intact")
    check("sujet:é✓".encode("utf-8") in out.read_bytes(), "LC_ALL=C: the bytes on disk are UTF-8, not escapes")
# a verified array that cannot be encoded: a lone surrogate the JSON parser accepts
# (`"\ud800"`) is exit 2, nothing written -- no result, no claim.
rc, so, se, result, names = run(TASK, '[{"actor_id": "%s", "working_event_ids": ["%s"], "blocked_items": [], "detail": "\\ud800"}]' % (ALICE, E_A))
check(rc == 2 and "cannot answer: result not serializable" in se and result is None and names == [],
      "unencodable verified array: exit 2 `result not serializable`, nothing written")
with tempfile.TemporaryDirectory() as td:
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = Path(td) / "j.json"
    j.write_text('[{"actor_id": "%s", "working_event_ids": ["%s"], "blocked_items": [], "detail": "\\ud800"}]' % (ALICE, E_A))
    so_buf, se_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so_buf), contextlib.redirect_stderr(se_buf):
        rc = pub.main([str(task), str(j), "--workspace", str(ws)])
    check(rc == 2 and not (ws / "state").exists() and not (ws / "results").exists(),
          "unencodable verified array: no claim and no results/ created")
check("fdopen(fd, \"w\")" not in src and 'fdopen(fd, "wb")' in src,
      "publish.py writes bytes it encoded as UTF-8 itself, never a locale-default text writer")

# (12) the launcher: publish.sh runs publish.py through scripts/python-binary.sh's
# resolve_python, never through a bare PATH python3.
check(PUBLISH_SH.is_file() and os.access(str(PUBLISH_SH), os.X_OK), "publish.sh exists and is executable")
sh_src = PUBLISH_SH.read_text()
check("python-binary.sh" in sh_src and "require_python" in sh_src and 'exec "$_py"' in sh_src
      and not re.search(r'exec\s+(?!"\$_py")', sh_src) and not re.search(r"/\S*bin/python", sh_src),
      "publish.sh sources the resolver and execs only its answer; no interpreter path is hardcoded")
with tempfile.TemporaryDirectory() as td:
    stub_dir = Path(td) / "bin"
    stub_dir.mkdir()
    marker = Path(td) / "stub-ran"
    stub = stub_dir / "python3"
    stub.write_text("#!/bin/sh\necho stub >> '%s'\nexit 1\n" % marker)
    stub.chmod(0o755)
    ws = Path(td) / "ws"
    (ws / "tasks").mkdir(parents=True)
    task = ws / "tasks" / "task-role-status-v1-0000-a2.txt"
    task.write_text(TASK)
    j = Path(td) / "j.json"
    j.write_text(json.dumps(FULL))
    base_env = {k: v for k, v in os.environ.items() if k not in ("SUTANDO_PY", "PYTHONPATH")}
    # control: with SUTANDO_PY unset the resolver WOULD hand this PATH the stub
    probe = subprocess.run(["/bin/bash", "-c", ". '%s/scripts/python-binary.sh'; resolve_python '%s'" % (REPO, REPO)],
                           capture_output=True, text=True, env={**base_env, "PATH": str(stub_dir)})
    check(probe.stdout.strip() == str(stub) and not marker.exists(),
          "control: on this PATH resolve_python names the stub (without executing it)")
    p = subprocess.run(["/bin/bash", str(PUBLISH_SH), str(task), str(j), "--workspace", str(ws)],
                       capture_output=True, text=True,
                       env={**base_env, "PATH": str(stub_dir), "SUTANDO_PY": sys.executable})
    check(p.returncode == 0 and (ws / "results" / "task-role-status-v1-0000-a2.txt").is_file()
          and "published " in p.stdout, "publish.sh: publishes through the resolved interpreter (rc 0)")
    check(not marker.exists(), "publish.sh: the PATH python3 stub never executed")
    # negative: nothing resolves -> exit 2 with the message, nothing written
    (ws / "results" / "task-role-status-v1-0000-a2.txt").unlink()
    claim_of(ws).unlink()
    empty = Path(td) / "empty"
    empty.mkdir()
    p = subprocess.run(["/bin/bash", str(PUBLISH_SH), str(task), str(j), "--workspace", str(ws)],
                       capture_output=True, text=True, env={**base_env, "PATH": str(empty)})
    check(p.returncode == 2 and "no runnable python3" in p.stderr and "nothing published" in p.stderr,
          "publish.sh with no resolvable interpreter: exit 2 with the message (stderr=%r)" % p.stderr[:300])
    check(not (ws / "results" / "task-role-status-v1-0000-a2.txt").exists() and not claim_of(ws).exists()
          and not marker.exists(), "publish.sh with no resolvable interpreter: nothing written, no stub run")

print()
print("checks: %d" % CHECKS)
if failures:
    print("FAILED (%d):" % len(failures))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("all checks passed")
