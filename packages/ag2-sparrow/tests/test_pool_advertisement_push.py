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
from pathlib import Path
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

        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
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

        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
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
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        good_key, good_id = m._profile_pushed_identity, m._workers_pushed_identity

        _write_raw(m, '{"workers": {"ts": 2}, "profile_wor', age=5)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
        assert len(calls) == 2, "a corrupt read issues no request"
        assert m._profile_pushed_identity == good_key, "prior card is still the live one"
        assert m._workers_pushed_identity == good_id, "prior snapshot is still live"

        _advertise(m, _record(3), age=10)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
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
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert "workers" in calls[0][2]

        m._POOL_ADVERTISEMENT_FILE.unlink()
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert len(calls) == 1, "the deletion is not a profile replacement"
        print("PASS test_a_deleted_file_after_a_good_push_changes_nothing")


def test_a_one_sided_record_pushes_neither_half():
    """A status map with no label map would POST rows the card never names."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 4, "workers": {"ts": 4, "live_cores": [W1]}})
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False

        _advertise(m, {"ts": 5, "profile_workers": {W1: {"label": "x"}}}, age=5)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
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
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert calls[1][2]["workers"] == {}, "the empty map ships, explicitly"
        print("PASS test_a_valid_empty_map_is_an_intentional_clear_and_ships")


def test_a_file_without_the_keys_pushes_nothing_extra():
    """A half-written or foreign record must not blank the picker."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 7})

        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
        assert calls == []
        print("PASS test_a_file_without_the_keys_pushes_nothing_extra")


def test_a_non_dict_workers_value_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, {"ts": 7, "workers": ["core-1"], "profile_workers": []})

        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
        assert calls == []
        print("PASS test_a_non_dict_workers_value_is_ignored")


class _ReadRaises:
    """A stand-in for the advertisement path whose read fails the way a
    parser does: the exception the loop must survive is not an OSError."""
    name = "pool-advertisement.json"

    def __init__(self, exc):
        self._exc = exc

    def stat(self):
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, 64, 0, 0, 0))

    def read_text(self):
        raise self._exc


def test_a_parser_failure_of_any_kind_is_unavailable_not_a_stalled_poll():
    """The reviewer's input: a record carrying both maps plus an unknown
    1,000-deep array. Older json decoders raise RecursionError on it, and the
    read runs in the loop BEFORE the task poll, where an escaping exception
    backs off and retries the same file forever. Whether or not this
    interpreter's decoder is the recursive one, the read must not raise and
    the unknown key must change nothing; the injected RecursionError then pins
    the UNAVAILABLE path on every interpreter."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        rec = _record(1)
        deep = json.dumps(rec)[:-1] + ', "junk": ' + "[" * 1000 + "]" * 1000 + "}"
        _write_raw(m, deep)
        _, ad = m._read_pool_advertisement()  # must not raise
        pushed = m._maybe_push_workers_snapshot(m._advertisement_or_none())
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is pushed
        if ad is None:
            assert calls == [], "UNAVAILABLE pushes nothing"
        else:
            assert calls[0][2] == rec["workers"], "the unknown key ships nothing"
        n = len(calls)

        real_path = m._POOL_ADVERTISEMENT_FILE
        m._POOL_ADVERTISEMENT_FILE = _ReadRaises(RecursionError("maximum recursion depth exceeded"))
        assert m._read_pool_advertisement() == ("", None), "the UNAVAILABLE sentinel"
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
        assert len(calls) == n, "a parser failure issues no request"

        m._POOL_ADVERTISEMENT_FILE = real_path
        rec = _record(2)
        rec["profile_workers"][W1]["label"] = "after"
        _advertise(m, rec, age=5)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True, "positive control"
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert calls[-1][2]["workers"][W1]["label"] == "after"
        print("PASS test_a_parser_failure_of_any_kind_is_unavailable_not_a_stalled_poll")


def test_an_oversized_record_is_unavailable_before_it_is_parsed():
    """The record is bounded by size before the parse: a runaway file is
    UNAVAILABLE at the stat, not a full read-and-decode on every loop pass."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        rec = _record(1)
        rec["pad"] = "x" * m._POOL_ADVERTISEMENT_MAX_BYTES
        _advertise(m, rec)
        assert m._read_pool_advertisement() == ("", None)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
        assert calls == [], "an oversized record pushes nothing"

        _advertise(m, _record(2), age=5)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True, "positive control"
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        print("PASS test_an_oversized_record_is_unavailable_before_it_is_parsed")


def test_a_later_mtime_repushes_both():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False, "unchanged: no re-post"
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False, "unchanged: no re-put"
        assert len(calls) == 2

        rec = _record(2)
        rec["profile_workers"][W1]["label"] = "shipper"
        _advertise(m, rec, age=5)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert calls[2][2] == rec["workers"]
        assert calls[3][2]["workers"][W1]["label"] == "shipper"
        print("PASS test_a_later_mtime_repushes_both")


def test_a_bumped_mtime_with_unchanged_content_repushes_neither():
    """Content, not mtime, is the change identity — for BOTH halves."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        _advertise(m, _record(1), age=5)
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False, "same content: no re-post"
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False, "same content: no re-put"
        assert len(calls) == 2
        print("PASS test_a_bumped_mtime_with_unchanged_content_repushes_neither")


def test_a_restore_at_or_below_the_prior_mtime_repushes_both():
    """valid -> missing -> restored with a LOWER, then an EQUAL, mtime. Keyed
    on mtime as a high-water mark the workers POST skipped the restore while
    the profile (keyed on content) advanced, so /v1/workers sat on revision 1
    under a card already labelling revision 2 until a later mtime arrived."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = _capture(m)
        _advertise(m, _record(1))
        first_mtime = m._POOL_ADVERTISEMENT_FILE.stat().st_mtime
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True

        m._POOL_ADVERTISEMENT_FILE.unlink()
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False

        rec = _record(2)
        rec["profile_workers"][W1]["label"] = "restored"
        _advertise(m, rec, age=-10)
        assert m._POOL_ADVERTISEMENT_FILE.stat().st_mtime < first_mtime
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True, "lower mtime, new content"
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert [c[2]["ts"] for c in calls if c[0] == "POST"] == [1, 2]
        assert [c[2]["workers"][W1]["label"] for c in calls if c[0] == "PUT"] == [
            "reviewer", "restored"]

        rec = _record(3)
        rec["profile_workers"][W1]["label"] = "again"
        p = _advertise(m, rec)
        os.utime(p, (first_mtime, first_mtime))
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is True, "equal mtime, new content"
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert calls[-2][2]["ts"] == 3 and calls[-1][2]["workers"][W1]["label"] == "again"
        print("PASS test_a_restore_at_or_below_the_prior_mtime_repushes_both")


def test_a_server_error_takes_the_short_retry_not_the_hour():
    """A transient 500 must not leave the picker stale for an hour."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        _advertise(m, _record(1))
        m._req = _raiser(500)
        t0 = time.time()
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
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
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
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
            assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
            assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
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
        assert m._maybe_push_workers_snapshot(m._advertisement_or_none()) is False
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is False
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
            m._maybe_push_workers_snapshot(m._advertisement_or_none())
            m._maybe_push_agent_profile(m._advertisement_or_none())
        assert sum("unavailable" in ln for ln in lines) == 1, lines

        _advertise(m, _record(1))
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert sum("readable again" in ln for ln in lines) == 1, lines
        m._POOL_ADVERTISEMENT_FILE.unlink()
        m._maybe_push_agent_profile(m._advertisement_or_none())
        assert sum("unavailable" in ln for ln in lines) == 2, "re-armed per edge"
        print("PASS test_the_unavailable_edge_logs_once_not_every_pass")


def test_the_production_loop_calls_both_relays():
    """Helper-only tests cannot show the shipped path runs: main() must call
    each relay at least as often as it posts the heartbeat (AST call sites)."""
    import ast
    src = (Path(__file__).resolve().parents[1] / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n.func.id for n in ast.walk(main_fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    beats = calls.count("_post_heartbeat")
    assert beats >= 1, "no heartbeat in main()"
    assert calls.count("_push_pool_advertisement") >= beats, calls
    # and never the two pushers separately: that is the two-reads shape
    assert calls.count("_maybe_push_workers_snapshot") == 0 and calls.count("_maybe_push_agent_profile") == 0, calls


def test_one_beat_publishes_one_revision_even_if_the_file_changes_mid_beat():
    """kewei on #4162: a sibling writer's atomic rename between the two reads
    gave the broker a snapshot from revision 1 and a card from revision 2."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
        (base / "state").mkdir(parents=True, exist_ok=True)
        m = _load(base)
        _advertise(m, _record(ts=1))
        calls = []
        def req(*a, **k):
            calls.append(a)
            if a[0] == "POST":  # the writer lands revision 2 during the first request
                _advertise(m, {**_record(ts=2), "profile_workers": {W1: {"label": "second", "runtime": "codex"}}})
            return {}
        m._req = req
        m._push_pool_advertisement()
        assert [c[0] for c in calls] == ["POST", "PUT"], calls
        assert calls[0][2]["ts"] == 1
        assert calls[1][2]["workers"][W1]["label"] == "reviewer", "card from the same revision as the snapshot"
        # the next beat ships revision 2 as a whole
        calls.clear()
        m._push_pool_advertisement()
        assert calls[0][2]["ts"] == 2 and calls[1][2]["workers"][W1]["label"] == "second"


def test_the_identity_is_one_path_segment():
    """kewei on #4162: a slash in the identity must not become a path separator."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
        (base / "state").mkdir(parents=True, exist_ok=True)
        m = _load(base)
        _advertise(m, _record())
        m._reenroll_identity = lambda: "@agent/name:example.test"
        calls = _capture(m)
        assert m._maybe_push_agent_profile(m._advertisement_or_none()) is True
        assert calls[0][1] == "/v1/agents/%40agent%2Fname%3Aexample.test/profile", calls[0][1]


def test_an_unchanged_pool_is_resent_on_the_cadence_so_a_restarted_broker_heals():
    """Production 2026-09-12: the broker redeployed at 07:43Z after the last
    snapshot push at 22:58Z and the picker stayed empty, because nothing here
    changed. Both halves must go again on the cadence, unchanged or not."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
        (base / "state").mkdir(parents=True, exist_ok=True)
        m = _load(base)
        _advertise(m, _record())
        clock = [1000.0]
        m._now = lambda: clock[0]
        calls = _capture(m)
        m._push_pool_advertisement()
        assert [c[0] for c in calls] == ["POST", "PUT"], calls
        calls.clear()
        clock[0] += 60
        m._push_pool_advertisement()
        assert calls == [], "within the cadence and unchanged: nothing resent"
        clock[0] += m._REPUSH_EVERY_S
        m._push_pool_advertisement()
        assert [c[0] for c in calls] == ["POST", "PUT"], "past the cadence: both halves resent unchanged"


if __name__ == "__main__":
    test_boot_pushes_workers_and_a_card_carrying_them()
    test_a_missing_file_pushes_nothing_at_all()
    test_malformed_json_pushes_nothing_and_keeps_the_prior_snapshot()
    test_a_deleted_file_after_a_good_push_changes_nothing()
    test_a_one_sided_record_pushes_neither_half()
    test_a_valid_empty_map_is_an_intentional_clear_and_ships()
    test_a_file_without_the_keys_pushes_nothing_extra()
    test_a_non_dict_workers_value_is_ignored()
    test_a_parser_failure_of_any_kind_is_unavailable_not_a_stalled_poll()
    test_an_oversized_record_is_unavailable_before_it_is_parsed()
    test_a_later_mtime_repushes_both()
    test_a_bumped_mtime_with_unchanged_content_repushes_neither()
    test_a_restore_at_or_below_the_prior_mtime_repushes_both()
    test_a_server_error_takes_the_short_retry_not_the_hour()
    test_an_auth_error_takes_the_short_retry_not_the_hour()
    test_only_an_unsupported_endpoint_earns_the_hour_backoff()
    test_a_network_error_keeps_the_five_minute_control()
    test_the_unavailable_edge_logs_once_not_every_pass()
    test_the_production_loop_calls_both_relays()
    test_one_beat_publishes_one_revision_even_if_the_file_changes_mid_beat()
    test_the_identity_is_one_path_segment()
    test_an_unchanged_pool_is_resent_on_the_cadence_so_a_restarted_broker_heals()
    print("ALL PASS test_pool_advertisement_push")
