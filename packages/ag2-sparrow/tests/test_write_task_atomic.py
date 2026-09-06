"""_write_task — atomic publish + idempotency (v1 freeze gate #142 item 5).

The transport writer must publish a task file so the core's watcher never sees a
partial file, and must be idempotent under gateway redelivery (the relay replays
its un-acked pool on reconnect — the 2026-06-30/07-01 500-task floods). These are
the two properties the at-least-once broker contract leans on at the worker edge.
"""
import os
import importlib
import shlex
import sys
import pathlib
import tempfile


def _load(base):
    """Reload the bridge with task/result/state dirs pointed under `base`."""
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def _task(tid="task-1784500000000"):
    return {
        "id": tid,
        "task": "[AG2Space qingyun] hello there",
        "source": "ag2space",
        "channel_id": "!room:ag2.space",
        "user_id": "@qingyun:ag2.space",
        "access_tier": "owner",
        "timestamp": "2026-07-20T00:00:00Z",
    }


def test_write_task_publishes_atomically_and_completely():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        tid, durable = m._write_task(_task())
        dest = m.TASKS_DIR / f"{tid}.txt"
        assert tid == "task-1784500000000"
        assert durable, "a committed queue write must be ackable as durable"
        assert dest.exists(), "task file must be published"
        # No temp sidecar left behind — the tmp+rename must have completed.
        assert not list(m.TASKS_DIR.glob(f"{tid}.txt.*.tmp"))
        # The watcher globs task-*.txt; the staged name (.txt.<pid>.<uuid>.tmp) must not match.
        assert list(m.TASKS_DIR.glob("task-*.txt")) == [dest]
        body = dest.read_text()
        assert body.endswith("\n")
        # access_tier stays the ONLY header-shaped access_tier line even with the
        # owner-tier block appended — a last-occurrence parser still can't be tricked.
        lines = body.rstrip().splitlines()
        tier_at = [i for i, ln in enumerate(lines) if ln.startswith("access_tier:")]
        assert len(tier_at) == 1
        tail = lines[tier_at[0] + 1:]
        assert any(ln.startswith("===SKILL INSTRUCTIONS") for ln in tail)
        # Completeness: the block's final line (the result path) is the file tail —
        # a truncated write cannot produce it.
        assert lines[-1].endswith(f"results/{tid}.txt")
        assert "id: task-1784500000000" in body
        assert "source: ag2space" in body
        print("PASS test_write_task_publishes_atomically_and_completely")


def test_write_task_never_leaves_partial_file_on_publish_crash():
    """If the process dies at the rename, the watcher-visible .txt must not exist —
    the reader only ever sees the fully-formed file or nothing (atomic publish)."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        tid = "task-1784500000001"
        dest = m.TASKS_DIR / f"{tid}.txt"
        orig_replace = m.os.replace

        def boom(src, target):
            raise OSError("simulated crash at publish")

        m.os.replace = boom
        try:
            assert m._write_task(_task(tid)) is None, (
                "a failed publish must report failure, not a queued task")
        finally:
            m.os.replace = orig_replace
        # The watcher-visible .txt must NOT exist — only nothing or a .tmp sidecar.
        assert not dest.exists(), "partial task must never be visible to the watcher"
        print("PASS test_write_task_never_leaves_partial_file_on_publish_crash")


def test_write_task_is_idempotent_under_redelivery():
    """Re-writing an already-queued task returns its id without duplicating the
    file — the guard that survives the relay replaying its un-acked pool."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        t = _task("task-1784500000002")
        tid1, _ = m._write_task(t)
        first = (m.TASKS_DIR / f"{tid1}.txt").read_text()
        # Redeliver the SAME id with a DIFFERENT body: the guard must skip the
        # rewrite entirely (return the id, leave the queued file byte-for-byte).
        # A modified body proves the guard fired — an identical body could pass
        # even with no guard at all.
        redelivered = dict(t, task="[AG2Space qingyun] TAMPERED redelivery body")
        tid2, _ = m._write_task(redelivered)
        assert tid1 == tid2
        assert list(m.TASKS_DIR.glob("task-*.txt")) == [m.TASKS_DIR / f"{tid1}.txt"]
        # Untouched — the original queued content wins, not the redelivery.
        assert (m.TASKS_DIR / f"{tid1}.txt").read_text() == first
        assert "TAMPERED" not in (m.TASKS_DIR / f"{tid1}.txt").read_text()
        print("PASS test_write_task_is_idempotent_under_redelivery")


def test_write_task_does_not_reexecute_a_completed_task():
    """v1 freeze gate item 3: same task_id never starts two local executions.
    The broker re-serves a task on lease expiry; if the worker already handled it,
    `_write_task` must NOT re-queue it (no second run for the watcher to pick up) —
    it drops a `[no-send]` result so the drain re-acks it upstream instead."""
    # (a) the task was already processed + archived
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        tid = "task-1784500000010"
        arch = m.TASKS_DIR / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / f"{tid}.txt").write_text(f"id: {tid}\ntask: handled earlier\n")
        assert m._write_task(_task(tid)) == (tid, True)
        # NOT re-queued — nothing live for the watcher to execute a second time.
        assert list(m.TASKS_DIR.glob("task-*.txt")) == []
        rfile = m.RESULTS_DIR / f"{tid}.txt"
        assert rfile.exists() and rfile.read_text().startswith("[no-send]")
        print("PASS test_write_task_does_not_reexecute_a_completed_task (archived task)")

    # (b) the reply was already delivered + archived (result archive, ts-suffixed)
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        tid = "task-1784500000011"
        m.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (m.ARCHIVE_RESULTS_DIR / f"{tid}-1784500000999.txt").write_text("earlier reply\n")
        assert m._write_task(_task(tid)) == (tid, True)
        assert list(m.TASKS_DIR.glob("task-*.txt")) == []
        rfile = m.RESULTS_DIR / f"{tid}.txt"
        assert rfile.exists() and rfile.read_text().startswith("[no-send]")
        print("PASS test_write_task_does_not_reexecute_a_completed_task (archived result)")


def test_write_task_drops_unsafe_and_idless():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        assert m._write_task({"task": "no id"}) is None
        assert m._write_task({"id": "../etc/passwd", "task": "x"}) is None
        assert list(m.TASKS_DIR.glob("*")) == []
        print("PASS test_write_task_drops_unsafe_and_idless")


def test_write_task_shell_quotes_channel_id_in_skill_instructions():
    """A malicious channel_id must not break the shell commands embedded in
    the SKILL INSTRUCTIONS block; shlex must round-trip it as one argument."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        malicious_chan = "!room'; touch /tmp/ag2sparrow_pwned; #"
        t = _task("task-1784500000020")
        t["channel_id"] = malicious_chan
        tid, _ = m._write_task(t)
        body = (m.TASKS_DIR / f"{tid}.txt").read_text()
        context_line = next(ln for ln in body.splitlines() if "room_ops.py read" in ln)
        context_cmd = context_line.split("`python3 ", 1)[1].split("`", 1)[0]
        context_args = shlex.split("python3 " + context_cmd)
        assert malicious_chan in context_args
        notify_line = next(ln for ln in body.splitlines() if "--channel-id" in ln)
        notify_cmd = notify_line.split("2. NOTIFY FIRST (if task takes >60s): ", 1)[1]
        notify_args = shlex.split(notify_cmd)
        assert malicious_chan in notify_args
        print("PASS test_write_task_shell_quotes_channel_id_in_skill_instructions")


if __name__ == "__main__":
    test_write_task_publishes_atomically_and_completely()
    test_write_task_never_leaves_partial_file_on_publish_crash()
    test_write_task_is_idempotent_under_redelivery()
    test_write_task_does_not_reexecute_a_completed_task()
    test_write_task_drops_unsafe_and_idless()
    test_write_task_shell_quotes_channel_id_in_skill_instructions()
    print("ALL PASS test_write_task_atomic")
