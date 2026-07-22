"""in-flight ledger — crash-before-ack recovery (v1 freeze gate #142 item 6).

A task pulled from the broker but not yet completed (`POST /v1/results`) lives in
the persisted in-flight set. If the worker restarts between pull and result-POST,
the set must survive so the result drain re-acks it — otherwise the reply is lost
(the exact restart-safety gap called out in the broker README). The ledger write
must be atomic (no partial JSON) and the read must fail open (a corrupt ledger
starts empty, never crashes the loop).
"""
import os, json, importlib, sys, pathlib, tempfile


def _load(base):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def test_inflight_survives_restart():
    """save → simulated restart (reload) → load returns the same set."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        pulled = {"task-1", "task-2", "task-3"}
        m._save_inflight(pulled)
        assert m.INFLIGHT_FILE.exists()
        # Atomic write — no temp sidecar left behind.
        assert not (m.INFLIGHT_FILE.with_suffix(".json.tmp")).exists()
        # Persisted as a sorted JSON list (deterministic on disk).
        assert json.loads(m.INFLIGHT_FILE.read_text()) == ["task-1", "task-2", "task-3"]
        # Restart: reload the module (same state dir) and read back.
        m2 = _load(base)
        assert m2._load_inflight() == pulled, "in-flight set must survive a restart"
        print("PASS test_inflight_survives_restart")


def test_inflight_load_fails_open_on_corruption():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        m.INFLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
        m.INFLIGHT_FILE.write_text("{ not json at all ")
        assert m._load_inflight() == set(), "corrupt ledger must load empty, not raise"
        # A JSON object (not a list) is also treated as empty, not crashed on.
        m.INFLIGHT_FILE.write_text('{"task-1": true}')
        assert m._load_inflight() == set()
        print("PASS test_inflight_load_fails_open_on_corruption")


def test_inflight_load_missing_file_is_empty():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        if m.INFLIGHT_FILE.exists():
            m.INFLIGHT_FILE.unlink()
        assert m._load_inflight() == set()
        print("PASS test_inflight_load_missing_file_is_empty")


def test_inflight_save_never_raises_on_unwritable():
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        m.INFLIGHT_FILE = pathlib.Path("/proc/nonexistent/dir/inflight.json")
        m._save_inflight({"task-1"})  # must swallow, not raise
        print("PASS test_inflight_save_never_raises_on_unwritable")


def test_inflight_round_trip_after_add_and_discard():
    """The real loop mutates the set as tasks are pulled and acked — verify the
    ledger reflects an add-then-discard across a persist boundary."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        m = _load(base)
        s = m._load_inflight()          # empty
        s.add("task-a")                 # pulled
        s.add("task-b")                 # pulled
        m._save_inflight(s)
        s.discard("task-a")             # acked via /v1/results
        m._save_inflight(s)
        assert _load(base)._load_inflight() == {"task-b"}
        print("PASS test_inflight_round_trip_after_add_and_discard")


if __name__ == "__main__":
    test_inflight_survives_restart()
    test_inflight_load_fails_open_on_corruption()
    test_inflight_load_missing_file_is_empty()
    test_inflight_save_never_raises_on_unwritable()
    test_inflight_round_trip_after_add_and_discard()
    print("ALL PASS test_inflight_recovery")
