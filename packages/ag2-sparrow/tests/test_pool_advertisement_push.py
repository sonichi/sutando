"""The pool advertisement is what the broker draws the worker picker from.

Both halves ship from one file: POST /v1/workers gives each row its STATUS,
the profile card's `workers` map gives it a LABEL. The broker REPLACES the
profile document, so a card sent without `workers` wipes the pool it was
showing — which makes AVAILABILITY, not content, the load-bearing property
these tests pin. The file is read-if-present and its producer ships separately
(#4119): absent, unreadable, half-written and one-sided records are all
UNAVAILABLE and must push NOTHING, while a valid record whose maps are empty
is an intentional clear and must push.

Run: python3 packages/ag2-sparrow/tests/test_pool_advertisement_push.py
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.error

MXID = "@sutando-test:ag2.space"
W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"


def _load(base):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ["AGENT_MXID"] = MXID
    os.environ.pop("SUTANDO_DISPLAY_NAME", None)
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    m = importlib.reload(mod)
    # hermetic: the channel .env fallback must not leak this host's identity
    m._config_from_channel_env = lambda *a, **k: ""
    return m


def _advertise(m, record, age=0.0):
    return _write_raw(m, json.dumps(record), age)


def _write_raw(m, text, age=0.0):
    p = m._POOL_ADVERTISEMENT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if age:
        os.utime(p, (time.time() + age, time.time() + age))
    return p


def _record(ts=1):
    return {"ts": ts,
            "workers": {"ts": ts, "live_cores": [W1], "dead_cores": []},
            "profile_workers": {W1: {"label": "reviewer", "runtime": "codex"}}}


def _capture(m):
    calls = []
    m._req = lambda *a, **k: calls.append(a) or {}
    return calls


def _raiser(code):
    def boom(*a, **k):
        raise urllib.error.HTTPError("https://gw.example/x", code, "e", {}, None)
    return boom


def test_boot_pushes_workers_and_a_card_carrying_them():
    """A fresh process has pushed nothing, so a file that predates it is new."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
        (base / "state").mkdir(parents=True, exist_ok=True)
        (base / "state" / "pool-advertisement.json").write_text(
            json.dumps(_record()))
        m = _load(base)
        calls = _capture(m)

        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        assert calls[0][0] == "POST" and calls[0][1] == "/v1/workers"
        assert calls[0][2] == _record()["workers"], "the body ships verbatim"
        assert calls[1][0] == "PUT" and "/profile" in calls[1][1]
        assert calls[1][2]["workers"] == _record()["profile_workers"]
        assert calls[1][2]["display"]["name"] == "Sutando", "card keeps identity"
        print("PASS test_boot_pushes_workers_and_a_card_carrying_them")


def test_a_missing_file_pushes_nothing_at_all():
    """Read-if-present: with no producer installed the bridge is a no-op, and
    in particular does not PUT a card whose absent `workers` clears the pool."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)

        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert calls == [], "no advertisement = no request of either kind"
        print("PASS test_a_missing_file_pushes_nothing_at_all")


def test_malformed_json_pushes_nothing_and_keeps_the_prior_snapshot():
    """valid -> mid-write truncation -> restored. The corrupt read must not
    push, must not advance the push key, and must not disturb what the broker
    is already holding; the restored read pushes the real map again."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        good_key, good_mtime = m._profile_push_key, m._workers_push_mtime

        _write_raw(m, '{"workers": {"ts": 2}, "profile_wor', age=5)
        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert len(calls) == 2, "a corrupt read issues no request"
        assert m._profile_push_key == good_key, "prior card is still the live one"
        assert m._workers_push_mtime == good_mtime, "prior snapshot is still live"

        _advertise(m, _record(3), age=10)
        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        assert calls[3][2]["workers"] == _record(3)["profile_workers"]
        print("PASS test_malformed_json_pushes_nothing_and_keeps_the_prior_snapshot")


def test_a_deleted_file_after_a_good_push_changes_nothing():
    """valid -> missing. The pre-fix key (mxid + mtime 0.0 + a workers-less
    card) was GUARANTEED to differ from the last one, so the push-on-change
    guard fired exactly on the transition that erased the map."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        assert m._maybe_push_agent_profile() is True
        assert "workers" in calls[0][2]

        m._POOL_ADVERTISEMENT_FILE.unlink()
        assert m._maybe_push_agent_profile() is False
        assert m._maybe_push_workers_snapshot() is False
        assert len(calls) == 1, "the deletion is not a profile replacement"
        print("PASS test_a_deleted_file_after_a_good_push_changes_nothing")


def test_a_one_sided_record_pushes_neither_half():
    """A status map with no label map would POST rows the card never names."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 4, "workers": {"ts": 4, "live_cores": [W1]}})
        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False

        _advertise(m, {"ts": 5, "profile_workers": {W1: {"label": "x"}}}, age=5)
        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert calls == [], "one-sided is unavailable, not half-available"
        print("PASS test_a_one_sided_record_pushes_neither_half")


def test_a_valid_empty_map_is_an_intentional_clear_and_ships():
    """The other side of the tri-state: "the pool is empty" is a fact the
    picker must be told, and is not the same as "cannot read the pool"."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 6, "workers": {"ts": 6, "live_cores": []},
                       "profile_workers": {}})
        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        assert calls[1][2]["workers"] == {}, "the empty map ships, explicitly"
        print("PASS test_a_valid_empty_map_is_an_intentional_clear_and_ships")


def test_a_file_without_the_keys_pushes_nothing_extra():
    """A half-written or foreign record must not blank the picker."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 7})

        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert calls == []
        print("PASS test_a_file_without_the_keys_pushes_nothing_extra")


def test_a_non_dict_workers_value_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 7, "workers": ["core-1"], "profile_workers": []})

        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert calls == []
        print("PASS test_a_non_dict_workers_value_is_ignored")


def test_a_later_mtime_repushes_both():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        assert m._maybe_push_workers_snapshot() is False, "unchanged: no re-post"
        assert m._maybe_push_agent_profile() is False, "unchanged: no re-put"
        assert len(calls) == 2

        rec = _record(2)
        rec["profile_workers"][W1]["label"] = "shipper"
        _advertise(m, rec, age=5)
        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        assert calls[2][2] == rec["workers"]
        assert calls[3][2]["workers"][W1]["label"] == "shipper"
        print("PASS test_a_later_mtime_repushes_both")


def test_mtime_alone_repushes_the_card():
    """The roster is the authority; an identical serialisation still re-puts."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        assert m._maybe_push_agent_profile() is True
        _advertise(m, _record(1), age=5)
        assert m._maybe_push_agent_profile() is True, "mtime is part of the key"
        assert len(calls) == 2
        print("PASS test_mtime_alone_repushes_the_card")


def test_a_server_error_takes_the_short_retry_not_the_hour():
    """A transient 500 must not leave the picker stale for an hour."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        _advertise(m, _record(1))
        m._req = _raiser(500)
        t0 = time.time()
        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert 250 < m._workers_push_retry_at - t0 < 350, "5m, not 1h"
        assert 250 < m._profile_push_retry_at - t0 < 350, "5m, not 1h"
        print("PASS test_a_server_error_takes_the_short_retry_not_the_hour")


def test_an_auth_error_takes_the_short_retry_not_the_hour():
    """401/403 is a live endpoint refusing us; recovery is minutes away."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        _advertise(m, _record(1))
        m._req = _raiser(401)
        t0 = time.time()
        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert 250 < m._workers_push_retry_at - t0 < 350
        assert 250 < m._profile_push_retry_at - t0 < 350
        print("PASS test_an_auth_error_takes_the_short_retry_not_the_hour")


def test_only_an_unsupported_endpoint_earns_the_hour_backoff():
    """404/405/501 is the broker saying the route does not exist here."""
    for code in (404, 405, 501):
        with tempfile.TemporaryDirectory() as d:
            m = _load(pathlib.Path(d))
            _advertise(m, _record(1))
            m._req = _raiser(code)
            t0 = time.time()
            assert m._maybe_push_workers_snapshot() is False
            assert m._maybe_push_agent_profile() is False
            assert 3550 < m._workers_push_retry_at - t0 < 3650, code
            assert 3550 < m._profile_push_retry_at - t0 < 3650, code
    print("PASS test_only_an_unsupported_endpoint_earns_the_hour_backoff")


def test_a_network_error_keeps_the_five_minute_control():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        _advertise(m, _record(1))

        def urlerr(*a, **k):
            raise urllib.error.URLError("connection refused")
        m._req = urlerr
        t0 = time.time()
        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is False
        assert 250 < m._workers_push_retry_at - t0 < 350
        assert 250 < m._profile_push_retry_at - t0 < 350
        print("PASS test_a_network_error_keeps_the_five_minute_control")


def test_the_unavailable_edge_logs_once_not_every_pass():
    """The loop calls these every few seconds; an absent producer would
    otherwise write one line per pass forever."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        _capture(m)
        lines = []
        m._log = lines.append
        for _ in range(5):
            m._maybe_push_workers_snapshot()
            m._maybe_push_agent_profile()
        assert sum("unavailable" in ln for ln in lines) == 1, lines

        _advertise(m, _record(1))
        assert m._maybe_push_agent_profile() is True
        assert sum("readable again" in ln for ln in lines) == 1, lines
        m._POOL_ADVERTISEMENT_FILE.unlink()
        m._maybe_push_agent_profile()
        assert sum("unavailable" in ln for ln in lines) == 2, "re-armed per edge"
        print("PASS test_the_unavailable_edge_logs_once_not_every_pass")


if __name__ == "__main__":
    test_boot_pushes_workers_and_a_card_carrying_them()
    test_a_missing_file_pushes_nothing_at_all()
    test_malformed_json_pushes_nothing_and_keeps_the_prior_snapshot()
    test_a_deleted_file_after_a_good_push_changes_nothing()
    test_a_one_sided_record_pushes_neither_half()
    test_a_valid_empty_map_is_an_intentional_clear_and_ships()
    test_a_file_without_the_keys_pushes_nothing_extra()
    test_a_non_dict_workers_value_is_ignored()
    test_a_later_mtime_repushes_both()
    test_mtime_alone_repushes_the_card()
    test_a_server_error_takes_the_short_retry_not_the_hour()
    test_an_auth_error_takes_the_short_retry_not_the_hour()
    test_only_an_unsupported_endpoint_earns_the_hour_backoff()
    test_a_network_error_keeps_the_five_minute_control()
    test_the_unavailable_edge_logs_once_not_every_pass()
    print("ALL PASS test_pool_advertisement_push")
