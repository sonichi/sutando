#!/usr/bin/env python3
"""Behavioral tests for the core-model + token-usage telemetry.

Asserts the actual emitted payloads (not source structure): core_model rides on
every event + as a person property, and token_usage carries bucketed utilization.
Run: python3 tests/telemetry-usage.test.py
"""
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class _SyncThread:
    """Thread shim that runs the target synchronously so capture() is testable."""
    def __init__(self, target=None, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        if self._t:
            self._t(*self._a)


def _load(**env):
    for k in ("SUTANDO_TELEMETRY", "DO_NOT_TRACK"):
        os.environ.pop(k, None)
    os.environ["POSTHOG_API_KEY"] = "phc_test"  # enable
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import telemetry
    importlib.reload(telemetry)
    telemetry.threading = type("M", (), {"Thread": _SyncThread})()  # sync dispatch
    sent = []
    telemetry._post = lambda payload: sent.append(payload)  # capture, no network
    return telemetry, sent


def test_core_model_env_override_on_every_event():
    t, sent = _load(SUTANDO_CORE_MODEL="claude-opus-4-8")
    t.capture("core_started", {"interval_s": 30})
    assert sent, "no event dispatched"
    props = sent[-1]["properties"]
    assert props["core_model"] == "claude-opus-4-8", props
    assert props["$set"]["core_model"] == "claude-opus-4-8", props["$set"]
    print("ok: core_model env override on every event + $set")


def test_core_model_unknown_when_unset():
    t, sent = _load(SUTANDO_CORE_MODEL=None, ANTHROPIC_MODEL=None,
                    CLAUDE_CONFIG_DIR="/nonexistent-xyz")
    t.capture("feature_used", {"feature": "morning_briefing"})
    assert sent[-1]["properties"]["core_model"] == "unknown", sent[-1]["properties"]
    print("ok: core_model falls back to 'unknown'")


def test_token_usage_buckets_and_shape():
    t, sent = _load(SUTANDO_CORE_MODEL="claude-sonnet-5")
    t.token_usage(41.2, 3.9, status="allowed")
    p = sent[-1]
    assert p["event"] == "token_usage", p["event"]
    props = p["properties"]
    assert props["util_5h_pct"] == 40, props   # 41.2 → nearest 5 → 40
    assert props["util_7d_pct"] == 5, props    # 3.9 → nearest 5 → 5
    assert props["status"] == "allowed", props
    assert props["core_model"] == "claude-sonnet-5", props  # model still attached
    print("ok: token_usage bucketed to nearest 5% + carries model")


def test_bucket_pct_edges():
    t, _ = _load()
    assert t._bucket_pct(0) == 0
    assert t._bucket_pct(100) == 100
    assert t._bucket_pct(97.5) == 100
    assert t._bucket_pct(150) == 100
    assert t._bucket_pct(-5) == 0
    assert t._bucket_pct(None) == -1
    assert t._bucket_pct("x") == -1
    assert t._bucket_pct(float("nan")) == -1
    assert t._bucket_pct(float("inf")) == -1
    assert t._bucket_pct(float("-inf")) == -1
    print("ok: _bucket_pct clamps + rounds + sentinels")


if __name__ == "__main__":
    test_core_model_env_override_on_every_event()
    test_core_model_unknown_when_unset()
    test_token_usage_buckets_and_shape()
    test_bucket_pct_edges()
    print("\nALL PASS")
