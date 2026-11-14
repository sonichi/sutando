"""_post_task_ack — self-healing retry when the gateway lacks /v1/tasks/<id>/ack.

The worker acks each pulled task so the broker can surface a "received" state.
When the broker lacks the endpoint it 404s; the worker must back off — but NOT
permanently, or a broker that later *deploys* the endpoint is never picked up
until the worker restarts. This pins the time-gated retry (self-heal on deploy).
"""
import os
import io
import importlib
import sys
import pathlib
import tempfile
import urllib.error


sys.path.append(str(pathlib.Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

def _load(base, cooldown="300"):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ["REMOTE_ACK_RETRY_COOLDOWN"] = cooldown
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def _http404():
    return urllib.error.HTTPError("https://gw/relay", 404, "no route", {}, None)


def _http404_not_leased():
    # The DEPLOYED broker's per-task 404: this lease expired / was re-served /
    # isn't ours. Body carries the documented marker.
    fp = io.BytesIO(b'{"error":"not leased to you"}')
    return urllib.error.HTTPError("https://gw/relay", 404, "not leased", {}, fp)


def _http404_stale_generation():
    # The broker's answer to an ack naming a superseded lease generation.
    fp = io.BytesIO(b'{"error":"not leased to you","reason":"stale lease generation"}')
    return urllib.error.HTTPError("https://gw/relay", 404, "not leased", {}, fp)


def _ack_bodies(m):
    """Capture every /ack body `_post_task_ack` sends, answering 200."""
    bodies = []
    m._req = lambda method, path, body=None, **k: bodies.append(body) or {"ok": True}
    return bodies


def test_ack_success_no_backoff():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {"ok": True}
        assert m._post_task_ack("task-1") is True
        assert len(calls) == 1
        assert m._ack_disabled_until == 0.0
        print("PASS test_ack_success_no_backoff")


def test_ack_invalid_tid_never_hits_network():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = []
        m._req = lambda *a, **k: calls.append(a)
        assert m._post_task_ack("../evil") is False
        assert calls == []
        print("PASS test_ack_invalid_tid_never_hits_network")


def test_ack_404_backs_off_within_cooldown():
    """A 404 arms a cooldown; a second ack *within* it is skipped (no network)."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d), cooldown="300")
        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise _http404()

        m._req = boom
        assert m._post_task_ack("task-1") is False
        assert len(calls) == 1
        assert m._ack_disabled_until > 0, "404 must arm a cooldown"
        # Second ack within the cooldown: skipped entirely, no network call.
        assert m._post_task_ack("task-2") is False
        assert len(calls) == 1, "within cooldown the worker must NOT hit /ack"
        print("PASS test_ack_404_backs_off_within_cooldown")


def test_ack_self_heals_after_cooldown():
    """The cooldown the 404 sets must be FINITE — after it lapses the ack retries
    and a now-deployed endpoint succeeds, with no worker restart. Uses cooldown=0
    so the retry is driven by the handler's own value, NOT a manual timer reset:
    a permanent latch (∞) fails this; a finite cooldown passes it."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d), cooldown="0")
        # First ack 404s → handler arms _ack_disabled_until = now + 0 (already lapsed).
        def boom(*a, **k):
            raise _http404()
        m._req = boom
        assert m._post_task_ack("task-1") is False
        # Endpoint has since deployed; next ack must retry (cooldown already lapsed),
        # driven purely by the finite value the 404 handler set — no manual reset.
        ok = []
        m._req = lambda *a, **k: ok.append(a) or {"ok": True}
        assert m._post_task_ack("task-2") is True, "finite cooldown must let the ack retry"
        assert len(ok) == 1, "self-heal: a deployed endpoint is picked up without a restart"
        assert m._ack_disabled_until == 0.0, "success clears the backoff"
        print("PASS test_ack_self_heals_after_cooldown")


def test_ack_per_task_404_not_leased_keeps_acking_others():
    """A DEPLOYED-broker per-task `404 {"error":"not leased to you"}` (this lease
    expired / re-served / foreign) must NOT arm the global cooldown — else one
    stale lease silences /ack for EVERY other task on the host, blinding the
    `received` state during churn. It's a single-task negative ack: skip this one,
    leave global acking enabled. (Blocking finding from qingyun-001, broker author.)"""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d), cooldown="300")
        calls = []

        def not_leased(*a, **k):
            calls.append(a)
            raise _http404_not_leased()

        m._req = not_leased
        assert m._post_task_ack("task-stale") is False
        assert len(calls) == 1
        assert m._ack_disabled_until == 0.0, "per-task 404 must NOT arm the global cooldown"
        # An unrelated task's ack still hits the network — acking stays enabled.
        ok = []
        m._req = lambda *a, **k: ok.append(a) or {"ok": True}
        assert m._post_task_ack("task-other") is True
        assert len(ok) == 1, "acking must stay enabled after a per-task not-leased 404"
        print("PASS test_ack_per_task_404_not_leased_keeps_acking_others")


def test_ack_bare_404_still_arms_cooldown():
    """A bare no-route 404 (pre-/ack broker, no body) is still 'endpoint
    unsupported' → arms the cooldown (self-heal path unchanged)."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d), cooldown="300")
        m._req = lambda *a, **k: (_ for _ in ()).throw(_http404())
        assert m._post_task_ack("task-x") is False
        assert m._ack_disabled_until > 0, "bare no-route 404 must arm the cooldown"
        print("PASS test_ack_bare_404_still_arms_cooldown")


def test_ack_echoes_the_served_lease_generation():
    """A task served with `lease_id` acks with that generation: the broker
    accepts only the live one, so the ack must name the serve it answers."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        bodies = _ack_bodies(m)
        assert m._post_task_ack("task-1", True, "lease-gen-2") is True
        assert bodies == [{"id": "task-1", "lease_id": "lease-gen-2", "durable": True}]
        print("PASS test_ack_echoes_the_served_lease_generation")


def test_ack_without_a_lease_omits_the_key():
    """An older broker serves no `lease_id`; the ack body must not carry the key
    at all (a null or empty value would be a generation the broker cannot match)."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        bodies = _ack_bodies(m)
        assert m._post_task_ack("task-1") is True
        assert m._post_task_ack("task-2", True, None) is True
        assert bodies == [{"id": "task-1"}, {"id": "task-2", "durable": True}]
        assert all("lease_id" not in b for b in bodies)
        print("PASS test_ack_without_a_lease_omits_the_key")


def test_ack_stale_generation_404_is_terminal_and_logged():
    """`404 {"error":"not leased to you","reason":"stale lease generation"}` means
    the broker re-served the task under a newer generation: the ack is retired
    (no retry, no global cooldown) and one log line names the task."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d), cooldown="300")
        assert m._record_pending_acks([("task-old", True, "lease-gen-1")]) is True
        calls, logs = [], []
        m._log = lambda msg, *a, **k: logs.append(str(msg))

        def stale(*a, **k):
            calls.append(a)
            raise _http404_stale_generation()

        m._req = stale
        assert m._post_task_ack("task-old", True, "lease-gen-1") is False
        assert len(calls) == 1
        assert m._ack_disabled_until == 0.0, "a stale generation is per-task, not a cooldown"
        assert m._load_pending_acks() == {}, "a stale ack is retired, never retried"
        m._retry_pending_acks({"task-old"})
        assert len(calls) == 1, "the retry loop must not re-post a retired ack"
        stale_lines = [l for l in logs if "task-old" in l and "lease generation" in l]
        assert len(stale_lines) == 1, logs
        print("PASS test_ack_stale_generation_404_is_terminal_and_logged")


def test_pending_ack_ledger_keeps_the_lease_across_a_restart():
    """The ack ledger survives a restart; a retried ack must carry the generation
    the task was served under, while a lease-less entry keeps today's bool shape."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        assert m._record_pending_acks([("task-leased", True, "lease-gen-3"),
                                       ("task-plain", False)]) is True
        assert m._load_pending_acks() == {"task-leased": True, "task-plain": False}
        fresh = _load(pathlib.Path(d))          # a restart re-reads the ledger
        assert fresh._load_pending_leases() == {"task-leased": "lease-gen-3"}
        bodies = _ack_bodies(fresh)
        fresh._retry_pending_acks({"task-leased", "task-plain"})
        assert bodies == [{"id": "task-leased", "lease_id": "lease-gen-3", "durable": True},
                          {"id": "task-plain"}]
        assert fresh._load_pending_acks() == {}, "confirmed acks are retired"
        print("PASS test_pending_ack_ledger_keeps_the_lease_across_a_restart")


def test_a_served_task_dict_yields_its_lease_or_none():
    """`_lease_id_of` reads the served task: a usable string is echoed, anything
    else (missing, non-string, empty, over-long) means no generation to echo."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        assert m._lease_id_of({"id": "t", "lease_id": "gen-1"}) == "gen-1"
        assert m._lease_id_of({"id": "t"}) is None
        assert m._lease_id_of({"id": "t", "lease_id": 7}) is None
        assert m._lease_id_of({"id": "t", "lease_id": ""}) is None
        assert m._lease_id_of({"id": "t", "lease_id": "x" * (m.LEASE_ID_MAX + 1)}) is None
        print("PASS test_a_served_task_dict_yields_its_lease_or_none")


def _dropped_then(m, outcomes):
    """Answer each /ack with the next outcome (an exception is raised); record every body."""
    bodies = []

    def req(method, path, body=None, **k):
        bodies.append(body)
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out
    m._req = req
    m.time.sleep = lambda s: bodies.append(("slept", s))
    return bodies


def test_an_ack_on_a_dropped_connection_is_retried_once_with_the_same_lease():
    import http.client
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        real_sleep = m.time.sleep
        try:
            for err in (http.client.RemoteDisconnected("Remote end closed connection without response"),
                        http.client.IncompleteRead(b"", 29),
                        ConnectionResetError(54, "Connection reset by peer"),
                        urllib.error.URLError(ConnectionResetError(54, "reset"))):
                bodies = _dropped_then(m, [err, {"ok": True}])
                assert m._post_task_ack("task-1", True, "lease-a") is True, err
                sent = [b for b in bodies if isinstance(b, dict)]
                assert sent == [{"id": "task-1", "lease_id": "lease-a", "durable": True}] * 2, sent
                assert [b for b in bodies if isinstance(b, tuple)] == [("slept", m.ACK_DROP_RETRY_S)]
                assert 0 < m.ACK_DROP_RETRY_S <= 2
        finally:
            m.time.sleep = real_sleep
        print("PASS test_an_ack_on_a_dropped_connection_is_retried_once_with_the_same_lease")


def test_a_second_drop_gives_up_and_keeps_the_pending_ack_for_the_ledger_retry():
    import http.client
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        real_sleep = m.time.sleep
        try:
            assert m._record_pending_acks([("task-1", True, "lease-a")])
            drop = http.client.RemoteDisconnected("gone")
            bodies = _dropped_then(m, [drop, http.client.RemoteDisconnected("gone again")])
            assert m._post_task_ack("task-1", True, "lease-a") is False
            assert len([b for b in bodies if isinstance(b, dict)]) == 2, "one retry, not a loop"
            assert m._load_pending_acks() == {"task-1": True}
            assert m._load_pending_leases() == {"task-1": "lease-a"}
        finally:
            m.time.sleep = real_sleep
        print("PASS test_a_second_drop_gives_up_and_keeps_the_pending_ack_for_the_ledger_retry")


def test_other_network_errors_are_not_retried_inline():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        real_sleep = m.time.sleep
        try:
            for err in (urllib.error.URLError("no route"), TimeoutError("timed out")):
                bodies = _dropped_then(m, [err, {"ok": True}])
                assert m._post_task_ack("task-1", True, "lease-a") is False
                assert len(bodies) == 1, err
        finally:
            m.time.sleep = real_sleep
        print("PASS test_other_network_errors_are_not_retried_inline")


if __name__ == "__main__":
    test_ack_success_no_backoff()
    test_ack_invalid_tid_never_hits_network()
    test_ack_404_backs_off_within_cooldown()
    test_ack_self_heals_after_cooldown()
    test_ack_per_task_404_not_leased_keeps_acking_others()
    test_ack_bare_404_still_arms_cooldown()
    test_ack_echoes_the_served_lease_generation()
    test_ack_without_a_lease_omits_the_key()
    test_ack_stale_generation_404_is_terminal_and_logged()
    test_pending_ack_ledger_keeps_the_lease_across_a_restart()
    test_a_served_task_dict_yields_its_lease_or_none()
    test_an_ack_on_a_dropped_connection_is_retried_once_with_the_same_lease()
    test_a_second_drop_gives_up_and_keeps_the_pending_ack_for_the_ledger_retry()
    test_other_network_errors_are_not_retried_inline()
    print("ALL PASS test_ack_retry")
