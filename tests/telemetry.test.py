#!/usr/bin/env python3
"""Tests for src/telemetry.py — the anonymous, opt-out product telemetry module.

The load-bearing guarantee is that opting out **actually stops all network
sends** — a bug class that has shipped in other OSS telemetry (opt-out flag
read once at import, or checked but not honored). We prove it by monkeypatching
the network sink and asserting it is never touched when opted out, via every
opt-out mechanism (DO_NOT_TRACK, SUTANDO_TELEMETRY=0, disable file).

Also covers: no-op without a key, anonymous distinct-id persistence/format,
and that a normal capture DOES reach the sink when enabled.

Run: python3 tests/telemetry.test.py
"""
import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def _load(state_dir: Path, key: str = "", env: dict | None = None):
    """Import a fresh telemetry module with a temp state dir + clean env."""
    for k in ("DO_NOT_TRACK", "SUTANDO_TELEMETRY", "POSTHOG_API_KEY", "SUTANDO_DEBUG_TELEMETRY"):
        os.environ.pop(k, None)
    os.environ["SUTANDO_STATE_DIR"] = str(state_dir)
    # Force the module's state dir to the temp path even if workspace_default
    # resolves elsewhere: point SUTANDO_STATE_DIR and stub resolve_workspace.
    os.environ.update(env or {})
    if key:
        os.environ["POSTHOG_API_KEY"] = key
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("telemetry_under_test", SRC / "telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Pin the effective key deterministically so tests don't depend on whether a
    # public key is embedded in _EMBEDDED_KEY for distribution.
    mod._KEY = key
    # No monkeypatch needed: _state_dir honors SUTANDO_STATE_DIR (set above),
    # so the real resolver code runs against the temp dir.
    assert mod._state_dir() == state_dir, "SUTANDO_STATE_DIR override should win"
    return mod


def _capture_sync(mod, event, props=None):
    """Call capture and join any spawned sender threads so assertions are
    deterministic (capture posts on a daemon thread)."""
    before = set(threading.enumerate())
    mod.capture(event, props)
    for t in set(threading.enumerate()) - before:
        t.join(timeout=2)


def run():
    passed = 0

    # 1) Opt-out via each mechanism → sink NEVER called, even with a key set.
    for mech in ("DO_NOT_TRACK", "SUTANDO_TELEMETRY", "file"):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            env = {}
            if mech == "DO_NOT_TRACK":
                env["DO_NOT_TRACK"] = "1"
            elif mech == "SUTANDO_TELEMETRY":
                env["SUTANDO_TELEMETRY"] = "0"
            mod = _load(sd, key="phc_test_key", env=env)
            if mech == "file":
                (sd).mkdir(parents=True, exist_ok=True)
                (sd / "telemetry-disabled").write_text("")
            calls = []
            mod._post = lambda payload: calls.append(payload)  # type: ignore
            assert mod.opted_out() is True, f"{mech}: opted_out should be True"
            assert mod.enabled() is False, f"{mech}: enabled should be False"
            _capture_sync(mod, "core_started", {"interval_s": 30})
            assert calls == [], f"{mech}: opt-out MUST send nothing, got {calls}"
            passed += 1
            print(f"ok   opt-out honored via {mech} — zero sends")

    # 2) No key configured → no-op even when not opted out.
    with tempfile.TemporaryDirectory() as td:
        mod = _load(Path(td), key="")
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        assert mod.enabled() is False, "no key → disabled"
        _capture_sync(mod, "core_started")
        assert calls == [], f"no key MUST send nothing, got {calls}"
        passed += 1
        print("ok   no key configured — zero sends")

    # 3) Enabled path → sink IS called, payload is anonymous + well-formed.
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        mod = _load(sd, key="phc_live")
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        assert mod.enabled() is True, "key + not opted out → enabled"
        _capture_sync(mod, "feature_used", {"feature": "morning-briefing"})
        assert len(calls) == 1, f"enabled MUST send once, got {len(calls)}"
        p = calls[0]
        assert p["event"] == "feature_used"
        assert p["api_key"] == "phc_live"
        assert p["properties"]["feature"] == "morning-briefing"
        assert "$process_person_profile" not in p["properties"], \
            "person processing on so installs appear as active users"
        assert p["properties"]["$ip"] == "", "must suppress IP storage"
        assert p["properties"]["$geoip_disable"] is True, "must disable GeoIP"
        did = p["distinct_id"]
        assert did and did != "anonymous" and len(did) == 32, f"distinct_id looks wrong: {did!r}"
        passed += 1
        print("ok   enabled — sends once, anonymous, well-formed payload")

    # 3b) Debug mode prints to stderr before send; still no send when opted out.
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        mod = _load(sd, key="phc_live", env={"SUTANDO_DEBUG_TELEMETRY": "1", "DO_NOT_TRACK": "1"})
        calls = []
        mod._post = lambda payload: calls.append(payload)  # type: ignore
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _capture_sync(mod, "core_started", {"interval_s": 30})
        assert "[telemetry] core_started" in buf.getvalue(), "debug should print the event"
        assert calls == [], "debug + opted-out MUST still send nothing"
        passed += 1
        print("ok   debug mode prints but opt-out still sends nothing")

    # 4) distinct_id persists across calls (stable per install).
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        mod = _load(sd, key="phc_live")
        d1 = mod._distinct_id()
        d2 = mod._distinct_id()
        assert d1 == d2, "distinct_id must be stable"
        assert (sd / "telemetry-id").read_text().strip() == d1
        passed += 1
        print("ok   distinct_id persists across calls")

    print(f"\nALL PASS ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
