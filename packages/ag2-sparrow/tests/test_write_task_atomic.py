"""_write_task — atomic publish + idempotency (v1 freeze gate #142 item 5).

The transport writer must publish a task file so the core's watcher never sees a
partial file, and must be idempotent under gateway redelivery (the relay replays
its un-acked pool on reconnect — the 2026-06-30/07-01 500-task floods). These are
the two properties the at-least-once broker contract leans on at the worker edge.
"""
import os, importlib, sys, pathlib, tempfile


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
        "timestamp": "2026-07-20T00:00:00Z",
    }


def test_write_task_publishes_atomically_and_completely():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        tid = m._write_task(_task())
        dest = m.TASKS_DIR / f"{tid}.txt"
        assert tid == "task-1784500000000"
        assert dest.exists(), "task file must be published"
        # No temp sidecar left behind — the tmp+rename must have completed.
        assert not (dest.with_suffix(".txt.tmp")).exists()
        # The watcher globs task-*.txt; the tmp name (…​.txt.tmp) must not match it.
        assert list(m.TASKS_DIR.glob("task-*.txt")) == [dest]
        body = dest.read_text()
        assert body.endswith("\n")
        # access_tier is written LAST so a last-occurrence parser can't be tricked.
        assert body.rstrip().splitlines()[-1].startswith("access_tier:")
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
        orig_rename = pathlib.Path.rename

        def boom(self, target):
            raise OSError("simulated crash at publish")

        pathlib.Path.rename = boom
        try:
            raised = False
            try:
                m._write_task(_task(tid))
            except OSError:
                raised = True
            assert raised, "a failed publish must surface, not silently succeed"
        finally:
            pathlib.Path.rename = orig_rename
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
        tid1 = m._write_task(t)
        first = (m.TASKS_DIR / f"{tid1}.txt").read_text()
        # Redeliver the SAME id with a DIFFERENT body: the guard must skip the
        # rewrite entirely (return the id, leave the queued file byte-for-byte).
        # A modified body proves the guard fired — an identical body could pass
        # even with no guard at all.
        redelivered = dict(t, task="[AG2Space qingyun] TAMPERED redelivery body")
        tid2 = m._write_task(redelivered)
        assert tid1 == tid2
        assert list(m.TASKS_DIR.glob("task-*.txt")) == [m.TASKS_DIR / f"{tid1}.txt"]
        # Untouched — the original queued content wins, not the redelivery.
        assert (m.TASKS_DIR / f"{tid1}.txt").read_text() == first
        assert "TAMPERED" not in (m.TASKS_DIR / f"{tid1}.txt").read_text()
        print("PASS test_write_task_is_idempotent_under_redelivery")


def test_write_task_drops_unsafe_and_idless():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        assert m._write_task({"task": "no id"}) is None
        assert m._write_task({"id": "../etc/passwd", "task": "x"}) is None
        assert list(m.TASKS_DIR.glob("*")) == []
        print("PASS test_write_task_drops_unsafe_and_idless")


if __name__ == "__main__":
    test_write_task_publishes_atomically_and_completely()
    test_write_task_never_leaves_partial_file_on_publish_crash()
    test_write_task_is_idempotent_under_redelivery()
    test_write_task_drops_unsafe_and_idless()
    print("ALL PASS test_write_task_atomic")
