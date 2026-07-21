"""_post_task_ack — self-healing retry when the gateway lacks /v1/tasks/<id>/ack.

The worker acks each pulled task so the broker can surface a "received" state.
When the broker lacks the endpoint it 404s; the worker must back off — but NOT
permanently, or a broker that later *deploys* the endpoint is never picked up
until the worker restarts. This pins the time-gated retry (self-heal on deploy).
"""
import os, io, importlib, sys, pathlib, tempfile
import urllib.error


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


if __name__ == "__main__":
    test_ack_success_no_backoff()
    test_ack_invalid_tid_never_hits_network()
    test_ack_404_backs_off_within_cooldown()
    test_ack_self_heals_after_cooldown()
    test_ack_per_task_404_not_leased_keeps_acking_others()
    test_ack_bare_404_still_arms_cooldown()
    print("ALL PASS test_ack_retry")
