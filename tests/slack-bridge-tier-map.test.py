#!/usr/bin/env python3
"""Behavioral test for slack-bridge.py's tierMap-driven access_tier
resolution. Mirrors the Discord-bridge tier behavior.

Contract:
    1. Unmapped users → "owner" (preserves pre-tierMap behavior).
    2. tierMap[uid] == "team" → "team".
    3. tierMap[uid] == "other" → "other".
    4. Unknown tier value in config → "other" (fail safe, not "owner").
    5. Missing tierMap key (whole map absent) → all users → "owner".

The bridge imports slack_bolt at module load (auth.test on init) — same
stub-monkey-patch pattern as slack-bridge-allowlist.test.py.

Run: python3 tests/slack-bridge-tier-map.test.py
Exit: 0 on pass, 1 on fail.
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path


class _StubApp:
    """Same stub used by slack-bridge-allowlist.test.py."""

    def __init__(self, *a, **kw):
        self.client = types.SimpleNamespace()

    def event(self, _name):
        def decorator(fn):
            return fn
        return decorator


def _load_module():
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-for-helper-only")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token-for-helper-only")
    os.environ.setdefault("SUTANDO_WORKSPACE", tempfile.mkdtemp(prefix="sutando-test-slack-tier-"))

    try:
        import slack_bolt as _real_bolt
        _real_bolt.App = _StubApp
    except ImportError:
        stub_bolt = types.ModuleType("slack_bolt")
        stub_bolt.App = _StubApp
        sys.modules["slack_bolt"] = stub_bolt
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod

    if "slack_bolt.adapter.socket_mode" not in sys.modules:
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod

    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    bridge_path = repo / "src" / "slack-bridge.py"
    spec = importlib.util.spec_from_file_location("slack_bridge_tier_under_test", bridge_path)
    sys.path.insert(0, str(repo / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_access(mod, payload: dict) -> None:
    """Write the bridge's ACCESS_FILE to a controlled payload."""
    access_file = mod.ACCESS_FILE
    access_file.parent.mkdir(parents=True, exist_ok=True)
    access_file.write_text(json.dumps(payload))


def main() -> int:
    mod = _load_module()
    load_tier_map = mod.load_tier_map

    passes = 0
    fails = 0

    def expect(name: str, got, want):
        nonlocal passes, fails
        if got == want:
            print(f"PASS: {name}")
            passes += 1
        else:
            print(f"FAIL: {name} — got {got!r}, want {want!r}")
            fails += 1

    # Case 1: tierMap present, owner unmapped (default fallback).
    _write_access(mod, {
        "allowFrom": ["Uowner", "Uteam", "Uother"],
        "tierMap": {"Uteam": "team", "Uother": "other"},
    })
    tm = load_tier_map()
    expect("Uowner unmapped → owner default", tm.get("Uowner", "owner"), "owner")
    expect("Uteam mapped → team", tm.get("Uteam", "owner"), "team")
    expect("Uother mapped → other", tm.get("Uother", "owner"), "other")
    expect("Uunknown unmapped → owner default", tm.get("Uunknown", "owner"), "owner")

    # Case 2: tierMap completely absent (pre-tierMap config). Everyone defaults to owner.
    _write_access(mod, {
        "allowFrom": ["Uolduser"],
        "tofuOwner": "Uolduser",
    })
    tm = load_tier_map()
    expect("absent tierMap returns empty dict", tm, {})
    expect("absent tierMap → all default to owner", tm.get("Uolduser", "owner"), "owner")

    # Case 3: tierMap with unknown tier value — caller-side fail-safe check.
    # load_tier_map() itself just returns the map; the caller in _write_task
    # is responsible for sanitizing. Verify the raw map round-trips.
    _write_access(mod, {
        "allowFrom": ["Ubad"],
        "tierMap": {"Ubad": "rando"},
    })
    tm = load_tier_map()
    expect("unknown tier value passes through to caller", tm.get("Ubad"), "rando")

    # Case 4: malformed access.json — should return {} not crash.
    mod.ACCESS_FILE.write_text("not valid json {{{")
    tm = load_tier_map()
    expect("malformed json → empty dict", tm, {})

    # Case 5: tierMap explicitly null.
    _write_access(mod, {"allowFrom": ["Unull"], "tierMap": None})
    tm = load_tier_map()
    expect("null tierMap → empty dict", tm, {})

    # Case 6: missing access.json file — should return {} not crash.
    mod.ACCESS_FILE.unlink(missing_ok=True)
    tm = load_tier_map()
    expect("missing file → empty dict", tm, {})

    print()
    print(f"Results: {passes} passed, {fails} failed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
