"""Poll-stall exit — the handler for a wedged-but-alive bridge.

Every poll failure backs off (capped at 60s) and loops, so a relay that stays
down leaves a live process polling into the void. A supervisor that restarts
only on process exit never sees it. These pin the exit that gives it one.
"""
import contextlib
import io
import os
import tempfile
import time
import importlib
import sys
import pathlib


sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()


def _ask_to_exit(m, *args):
    """What _abort_if_poll_stalled answered, with an in-place exit turned into a failure.

    The code belongs to main(), so run() can bound the exit hooks; an exit taken here would
    otherwise end the whole run with that code and read as a pass.
    """
    try:
        return m._abort_if_poll_stalled(*args)
    except SystemExit as e:
        raise AssertionError(f"the stall exit left in place with code {e.code}") from None


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


def test_abort_asks_for_the_exit_and_records_the_stall():
    import json
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        # The exit is armed only where something restarts us.
        os.environ["SUTANDO_BRIDGE_RESTART_OWNER"] = "launchd"
        m = _load(tmp)
        m.POLL_STALL_EXIT_S = 10
        # within the limit: returns False, asks for nothing
        assert m._abort_if_poll_stalled(time.monotonic()) is False
        # past it: the exit is marked ready for main(), and the sidecar names the stall
        stopped = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            asked = _ask_to_exit(m, time.monotonic() - 60, lambda: stopped.append(1) or True)
        assert asked is True, "a stalled poll loop must ask the caller to leave"
        assert m._STALL_EXIT_READY is True, "main() reads this to return the code"
        assert m.POLL_STALL_EXIT_CODE != 0
        assert "no successful poll" in err.getvalue()
        assert stopped == [1], "the workers must be stopped before the exit"
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["connected"] is False
        assert "stalled" in rec["error"]


def test_the_exit_request_survives_the_loops_catch_all():
    """The call site sits inside `try:` whose last handler is `except Exception`.

    The request is a flag and a return, not an exception, so no handler can eat
    it; pinned because the whole fix is inert if the request can be discarded.
    """
    with tempfile.TemporaryDirectory() as d:
        os.environ["SUTANDO_BRIDGE_RESTART_OWNER"] = "launchd"
        m = _load(pathlib.Path(d))
        m.POLL_STALL_EXIT_S = 1
        with contextlib.redirect_stderr(io.StringIO()):
            asked = _ask_to_exit(m, time.monotonic() - 60)
        assert asked is True
        assert m._STALL_EXIT_READY is True, "the request must outlive the catch-all"


def test_unsupervised_lane_does_NOT_exit_and_keeps_retrying():
    """The reviewer's case: startup-runtime.sh launches the primary and every
    named lane as a bare `&`, so exiting there ends the lane until someone
    reruns startup. With no declared owner the stall is recorded, not fatal."""
    import json
    import time
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        os.environ.pop("SUTANDO_BRIDGE_RESTART_OWNER", None)
        m = _load(tmp)
        m.POLL_STALL_EXIT_S = 10
        assert m.POLL_STALL_RESTART_OWNER == "", "no owner should be declared here"
        # Well past the limit: must return rather than raise.
        m._abort_if_poll_stalled(time.monotonic() - 60)
        rec = json.loads(m.GATEWAY_STATUS_FILE.read_text())
        assert rec["connected"] is False
        assert "stalled" in rec["error"]
        assert "retrying" in rec["error"], rec["error"]
        assert "no restart owner" in rec["error"], rec["error"]


def test_declared_owner_arms_the_exit_and_names_it():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        os.environ["SUTANDO_BRIDGE_RESTART_OWNER"] = "launchd"
        try:
            m = _load(tmp)
            m.POLL_STALL_EXIT_S = 10
            assert m.POLL_STALL_RESTART_OWNER == "launchd"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                asked = _ask_to_exit(m, time.monotonic() - 60)
            assert asked is True, "a declared owner must arm the exit"
            assert m._STALL_EXIT_READY is True
            assert "launchd" in err.getvalue(), err.getvalue()
        finally:
            os.environ.pop("SUTANDO_BRIDGE_RESTART_OWNER", None)


def test_the_unsupervised_stall_is_recorded_once_per_episode():
    """The poll loop calls this every iteration; the sidecar is a file write."""
    import time
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        os.environ.pop("SUTANDO_BRIDGE_RESTART_OWNER", None)
        m = _load(tmp)
        m.POLL_STALL_EXIT_S = 10
        last_ok = time.monotonic() - 60
        m._abort_if_poll_stalled(last_ok)
        first = m.GATEWAY_STATUS_FILE.stat().st_mtime_ns
        m.GATEWAY_STATUS_FILE.write_text("{}")          # would be overwritten on a re-emit
        m._abort_if_poll_stalled(last_ok)               # same episode
        assert m.GATEWAY_STATUS_FILE.read_text() == "{}", "re-emitted within one episode"
        m._abort_if_poll_stalled(time.monotonic() - 120)  # a NEW episode does report
        assert m.GATEWAY_STATUS_FILE.read_text() != "{}"
        assert first > 0


def test_wall_clock_jumps_do_not_count_as_a_stall():
    """A Mac asleep past the limit wakes with wall time far ahead; only the
    monotonic clock, which stops during sleep, may decide."""
    class _WallJumped:
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return time.time() + 3 * 3600

    with tempfile.TemporaryDirectory() as d:
        os.environ["SUTANDO_BRIDGE_RESTART_OWNER"] = "launchd"
        try:
            m = _load(pathlib.Path(d))
            m.POLL_STALL_EXIT_S = 10
            m.time = _WallJumped()
            m._abort_if_poll_stalled(time.monotonic())
        finally:
            os.environ.pop("SUTANDO_BRIDGE_RESTART_OWNER", None)


def test_a_worker_still_sending_holds_the_exit_until_it_stops():
    """Exiting while the outbound worker is mid-send strands its claim, and the
    next process resends it. The stall is held, never abandoned: once the stop is
    requested it is retried even if the poll anchor has since gone fresh."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["SUTANDO_BRIDGE_RESTART_OWNER"] = "launchd"
        try:
            m = _load(pathlib.Path(d))
            m.POLL_STALL_EXIT_S = 10
            stops = [False, False, True]
            calls = []

            def stop():
                calls.append(1)
                return stops.pop(0)

            assert m._abort_if_poll_stalled(time.monotonic() - 60, stop) is True
            assert m._STALL_EXIT_PENDING is True
            # A fresh anchor must not cancel a requested stop.
            assert m._abort_if_poll_stalled(time.monotonic(), stop) is True
            with contextlib.redirect_stderr(io.StringIO()):
                assert _ask_to_exit(m, time.monotonic(), stop) is True
            assert m._STALL_EXIT_READY is True, \
                "the exit must follow once the worker has stopped"
            assert len(calls) == 3, calls
        finally:
            os.environ.pop("SUTANDO_BRIDGE_RESTART_OWNER", None)


def test_an_exit_taken_in_place_is_reported_instead_of_ending_the_run():
    """The guard the cases above call through. Before this fix the stall exit raised SystemExit,
    which ends the run with code 69 and prints nothing, so a regression could read as a pass."""
    class _ExitsInPlace:
        def _abort_if_poll_stalled(self, *_a):
            raise SystemExit(69)

    reported = ""
    try:
        _ask_to_exit(_ExitsInPlace(), 0.0)
    except AssertionError as e:
        reported = str(e)
    assert "left in place with code 69" in reported, reported


def test_the_join_budget_outlasts_every_outbound_request_timeout():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        longest = max(m._REQ_TIMEOUT_S, m._ROOM_POST_TIMEOUT_S, m._PROACTIVE_POST_TIMEOUT_S,
                      m._UPLOAD_TIMEOUT_S, m._ACK_TIMEOUT_S)
        assert m._OUTBOUND_SEND_TIMEOUT_S == longest
        assert m._worker_stop_budget_s() > longest


if __name__ == "__main__":
    test_stalled_only_past_the_limit()
    print("PASS test_stalled_only_past_the_limit")
    test_zero_limit_disables_the_exit()
    print("PASS test_zero_limit_disables_the_exit")
    test_abort_asks_for_the_exit_and_records_the_stall()
    print("PASS test_abort_asks_for_the_exit_and_records_the_stall")
    test_the_exit_request_survives_the_loops_catch_all()
    print("PASS test_the_exit_request_survives_the_loops_catch_all")
    test_unsupervised_lane_does_NOT_exit_and_keeps_retrying()
    print("PASS test_unsupervised_lane_does_NOT_exit_and_keeps_retrying")
    test_declared_owner_arms_the_exit_and_names_it()
    print("PASS test_declared_owner_arms_the_exit_and_names_it")
    test_the_unsupervised_stall_is_recorded_once_per_episode()
    print("PASS test_the_unsupervised_stall_is_recorded_once_per_episode")
    test_wall_clock_jumps_do_not_count_as_a_stall()
    print("PASS test_wall_clock_jumps_do_not_count_as_a_stall")
    test_a_worker_still_sending_holds_the_exit_until_it_stops()
    print("PASS test_a_worker_still_sending_holds_the_exit_until_it_stops")
    test_an_exit_taken_in_place_is_reported_instead_of_ending_the_run()
    print("PASS test_an_exit_taken_in_place_is_reported_instead_of_ending_the_run")
    test_the_join_budget_outlasts_every_outbound_request_timeout()
    print("PASS test_the_join_budget_outlasts_every_outbound_request_timeout")
    print("ALL PASS")
