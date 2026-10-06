"""Owner profile labels reach Sutando without changing task delivery.

The broker's effective workers list may include our own published labels. Only
its explicit owner override map is rename intent; the stable ID is the key.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error


PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
MXID = "@worker-label-test:ag2.space"
WID = "274cb60d473744dba54040a9de119877"


def _module(base: Path):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ["AGENT_MXID"] = MXID
    os.environ["REMOTE_TASK_URL"] = "https://gw.example/relay"
    os.environ["REMOTE_TASK_TOKEN"] = "dummy-secret"
    from ag2_sparrow import remote_gateway_bridge as bridge
    m = importlib.reload(bridge)
    m._reenroll_identity = lambda: MXID
    m._WORKER_LABEL_APPLIER = None
    m._profile_labels_checked_at = 0.0
    m._profile_labels_retry_at = 0.0
    return m


def _profile(labels, *, mxid=MXID, version=7):
    return {"schema_version": 1, "mxid": mxid,
            "config": {"version": version},
            "display": {"worker_labels": labels},
            "workers": [{"worker_id": WID, "label": "stale published label"}]}


def _ready(m):
    record = ("digest-1", {"workers": {}, "profile_workers": {}})
    m._profile_pushed_identity = f"{MXID}\ndigest-1"
    return record


def test_only_owner_overrides_are_applied_by_id():
    with tempfile.TemporaryDirectory() as d:
        m = _module(Path(d))
        seen = []
        m._WORKER_LABEL_APPLIER = lambda labels, version, mxid: (
            seen.append((labels, version, mxid)) or {"changed": True})
        calls = []
        m._req = lambda *a, **k: calls.append(a) or _profile({WID: "kc-reviewer-ryan"})

        assert m._maybe_pull_worker_labels(_ready(m)) is True
        assert seen == [({WID: "kc-reviewer-ryan"}, 7, MXID)]
        assert calls == [("GET", "/v1/agents/%40worker-label-test%3Aag2.space/profile")]
        assert m._maybe_pull_worker_labels(_ready(m)) is False, "refresh is bounded"


def test_profile_is_published_before_owner_labels_are_read():
    with tempfile.TemporaryDirectory() as d:
        m = _module(Path(d))
        m._POOL_ADVERTISEMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        m._POOL_ADVERTISEMENT_FILE.write_text(json.dumps({
            "workers": {"live_cores": [WID], "dead_cores": []},
            "profile_workers": {WID: {"label": "local alias"}},
        }))
        seen = []
        calls = []
        m._WORKER_LABEL_APPLIER = lambda labels, version, mxid: (
            seen.append((labels, version, mxid)) or {"changed": True})

        def request(method, path, *args, **kwargs):
            calls.append(method)
            return _profile({WID: "owner name"}) if method == "GET" else {}

        m._req = request
        m._push_pool_advertisement_now()
        assert calls == ["POST", "PUT", "GET"]
        assert seen == [({WID: "owner name"}, 7, MXID)]


def test_empty_override_map_clears_but_missing_map_never_does():
    with tempfile.TemporaryDirectory() as d:
        m = _module(Path(d))
        seen = []
        m._WORKER_LABEL_APPLIER = lambda labels, version, mxid: (
            seen.append((labels, version, mxid)) or {"changed": True})
        m._req = lambda *a, **k: _profile({}, version=8)
        assert m._maybe_pull_worker_labels(_ready(m)) is True
        assert seen == [({}, 8, MXID)]

        m._profile_labels_checked_at = 0.0
        m._req = lambda *a, **k: {**_profile({}, version=9), "display": {}}
        assert m._maybe_pull_worker_labels(_ready(m)) is False
        assert seen == [({}, 8, MXID)], "absence is not an explicit clear"


def test_wrong_identity_or_unpublished_card_cannot_rename():
    with tempfile.TemporaryDirectory() as d:
        m = _module(Path(d))
        seen = []
        m._WORKER_LABEL_APPLIER = lambda labels, version, mxid: seen.append(labels)
        calls = []
        m._req = lambda *a, **k: calls.append(a) or _profile({WID: "wrong"}, mxid="@other:x")
        record = ("digest-1", {"workers": {}, "profile_workers": {}})
        assert m._maybe_pull_worker_labels(record) is False
        assert calls == [], "read waits for our profile publication"

        assert m._maybe_pull_worker_labels(_ready(m)) is False
        assert seen == []


def test_identity_change_during_profile_read_cannot_apply_old_owner_labels():
    with tempfile.TemporaryDirectory() as d:
        m = _module(Path(d))
        seen = []
        current = [MXID]
        m._reenroll_identity = lambda: current[0]
        m._WORKER_LABEL_APPLIER = lambda labels, version, mxid: seen.append(labels)

        def request(*_args, **_kwargs):
            current[0] = "@new-agent:ag2.space"
            return _profile({WID: "old owner name"})

        m._req = request
        assert m._maybe_pull_worker_labels(_ready(m)) is False
        assert seen == []


def test_invalid_label_and_unsupported_endpoint_leave_pool_untouched():
    with tempfile.TemporaryDirectory() as d:
        m = _module(Path(d))
        seen = []
        m._WORKER_LABEL_APPLIER = lambda labels, version, mxid: seen.append(labels)
        m._req = lambda *a, **k: _profile({WID: "bad\nname"})
        assert m._maybe_pull_worker_labels(_ready(m)) is False
        assert seen == []

        m._profile_labels_checked_at = 0.0
        m._profile_labels_retry_at = 0.0

        def unsupported(*_a, **_k):
            raise urllib.error.HTTPError("https://gw.example/x", 404, "no route", {}, None)

        m._req = unsupported
        assert m._maybe_pull_worker_labels(_ready(m)) is False
        assert m._profile_labels_retry_at > time.time() + 3500
        assert seen == []


if __name__ == "__main__":
    test_only_owner_overrides_are_applied_by_id()
    test_profile_is_published_before_owner_labels_are_read()
    test_empty_override_map_clears_but_missing_map_never_does()
    test_wrong_identity_or_unpublished_card_cannot_rename()
    test_identity_change_during_profile_read_cannot_apply_old_owner_labels()
    test_invalid_label_and_unsupported_endpoint_leave_pool_untouched()
    print("PASS test_worker_profile_labels")
