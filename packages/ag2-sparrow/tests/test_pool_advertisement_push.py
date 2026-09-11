"""The pool advertisement is what the broker draws the worker picker from.

Both halves ship from one file: POST /v1/workers gives each row its STATUS,
the profile card's `workers` map gives it a LABEL. A restart must re-send both
— the broker REPLACES the profile document, so a card without `workers` wipes
the pool it was showing.

Run: python3 packages/ag2-sparrow/tests/test_pool_advertisement_push.py
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import time

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
    p = m._POOL_ADVERTISEMENT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record))
    if age:
        os.utime(p, (time.time() + age, time.time() + age))
    return p


def _record(ts=1):
    return {"ts": ts,
            "workers": {"ts": ts, "live_cores": [W1], "dead_cores": []},
            "profile_workers": {W1: {"label": "reviewer", "runtime": "codex"}}}


def test_boot_pushes_workers_and_a_card_carrying_them():
    """A fresh process has pushed nothing, so a file that predates it is new."""
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
        (base / "state").mkdir(parents=True, exist_ok=True)
        (base / "state" / "pool-advertisement.json").write_text(
            json.dumps(_record()))
        m = _load(base)
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {}

        assert m._maybe_push_workers_snapshot() is True
        assert m._maybe_push_agent_profile() is True
        assert calls[0][0] == "POST" and calls[0][1] == "/v1/workers"
        assert calls[0][2] == _record()["workers"], "the body ships verbatim"
        assert calls[1][0] == "PUT" and "/profile" in calls[1][1]
        assert calls[1][2]["workers"] == _record()["profile_workers"]
        assert calls[1][2]["display"]["name"] == "Sutando", "card keeps identity"
        print("PASS test_boot_pushes_workers_and_a_card_carrying_them")


def test_no_advertisement_file_pushes_a_card_without_workers():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {}

        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is True
        assert len(calls) == 1, "no pool = no /v1/workers request"
        assert "workers" not in calls[0][2]
        print("PASS test_no_advertisement_file_pushes_a_card_without_workers")


def test_a_file_without_the_keys_pushes_nothing_extra():
    """A half-written or foreign record must not blank the picker."""
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {}
        _advertise(m, {"ts": 7})

        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is True
        assert [c[1] for c in calls] == [
            f"/v1/agents/{m.urllib.parse.quote(MXID)}/profile"]
        assert "workers" not in calls[0][2]
        print("PASS test_a_file_without_the_keys_pushes_nothing_extra")


def test_a_non_dict_workers_value_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {}
        _advertise(m, {"ts": 7, "workers": ["core-1"], "profile_workers": []})

        assert m._maybe_push_workers_snapshot() is False
        assert m._maybe_push_agent_profile() is True
        assert len(calls) == 1 and "workers" not in calls[0][2]
        print("PASS test_a_non_dict_workers_value_is_ignored")


def test_a_later_mtime_repushes_both():
    with tempfile.TemporaryDirectory() as d:
        m = _load(pathlib.Path(d))
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {}
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
        calls = []
        m._req = lambda *a, **k: calls.append(a) or {}
        _advertise(m, _record(1))
        assert m._maybe_push_agent_profile() is True
        _advertise(m, _record(1), age=5)
        assert m._maybe_push_agent_profile() is True, "mtime is part of the key"
        assert len(calls) == 2
        print("PASS test_mtime_alone_repushes_the_card")


if __name__ == "__main__":
    test_boot_pushes_workers_and_a_card_carrying_them()
    test_no_advertisement_file_pushes_a_card_without_workers()
    test_a_file_without_the_keys_pushes_nothing_extra()
    test_a_non_dict_workers_value_is_ignored()
    test_a_later_mtime_repushes_both()
    test_mtime_alone_repushes_the_card()
    print("ALL PASS test_pool_advertisement_push")
