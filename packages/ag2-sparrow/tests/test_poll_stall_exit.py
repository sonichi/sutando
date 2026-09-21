"""Poll-stall exit — the handler for a wedged-but-alive bridge.

Every poll failure backs off (capped at 60s) and loops, so a relay that stays
down leaves a live process polling into the void. A supervisor that restarts
only on process exit never sees it. These pin the exit that gives it one.
"""
import os
import tempfile
import importlib
import sys
import pathlib


def _load(tmp):
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(tmp)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def test_stalled_only_past_the_limit():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        assert m._poll_stalled(1000.0, 1000.0 + 9, limit=10) is False
        assert m._poll_stalled(1000.0, 1000.0 + 10, limit=10) is False  # boundary is exclusive
        assert m._poll_stalled(1000.0, 1000.0 + 11, limit=10) is True


def test_zero_limit_disables_the_exit():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        assert m._poll_stalled(0.0, 1e9, limit=0) is False


def test_abort_exits_nonzero_and_records_the_stall():
    import json
    import time
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        m = _load(tmp)
        m.POLL_STALL_EXIT_S = 10
        # within the limit: returns, does not exit
        m._abort_if_poll_stalled(time.time())
        # past it: SystemExit, and the status sidecar names the stall
        try:
            m._abort_if_poll_stalled(time.time() - 60)
        except SystemExit as e:
            assert e.code != 0, "must exit non-zero so the supervisor restarts"
            assert "no successful poll" in str(e.code)
        else:
            raise AssertionError("a stalled poll loop must exit")
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["connected"] is False
        assert "stalled" in rec["error"]


def test_systemexit_is_not_swallowed_by_the_loops_catch_all():
    """The call site sits inside `try:` whose last handler is `except Exception`.

    SystemExit derives from BaseException, so it escapes — pinned because the
    whole fix is inert if a future handler widens to BaseException.
    """
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        m.POLL_STALL_EXIT_S = 1
        raised = False
        try:
            try:
                m._abort_if_poll_stalled(0.0)
            except Exception:  # noqa: BLE001 — mirrors the poll loop's catch-all
                raise AssertionError("SystemExit must not be caught as Exception")
        except SystemExit:
            raised = True
        assert raised
