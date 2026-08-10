#!/usr/bin/env python3
"""Behavioral test for slack-bridge.py's tierMap-driven access_tier
resolution. Mirrors the Discord-bridge tier behavior.

Contract (post-#2161 fail-closed; resolution cases call the REAL
resolve_access_tier from src/slack-bridge.py, not a local copy — #2512):
    1. tierMap[uid] == "team" → "team"; == "other" → "other".
    2. tierMap present, uid missing → "other" (#893: no silent escalation
       for a new allowlist addition).
    3. tierMap empty/unconfirmed → "other" (fail CLOSED, #2161: a legit
       pre-tierMap config is grandfathered into a NON-empty map by the
       seed, so an empty map means the seed failed — never grant owner).
    4. Unknown tier value in config → "other" (fail safe, not "owner").

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
    # Isolate ACCESS_FILE from the REAL slack access.json — this test writes to
    # it (invalid-JSON case) and unlinks it. ACCESS_FILE resolves via
    # CLAUDE_CONFIG_DIR (the SUTANDO_WORKSPACE temp above does NOT cover it), so
    # without this a local run with CLAUDE_CONFIG_DIR set would clobber + DELETE
    # the owner's real allowlist. Same guard the tofu-enroll / mod-judge-buffer
    # tests already use.
    mod.ACCESS_FILE = Path(tempfile.mkdtemp(prefix="sutando-tier-access-")) / "access.json"
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
    expect("Uteam mapped → team", tm.get("Uteam"), "team")
    expect("Uother mapped → other", tm.get("Uother"), "other")
    # load_tier_map() returns raw dict; _write_task handles the split default.
    # When tierMap is non-empty and uid is missing, caller should use "other".
    expect("Uowner in raw tierMap → None (not mapped)", tm.get("Uowner"), None)

    # Case 2: tierMap completely absent — load_tier_map returns {} (the
    # resolution consequence — fail-closed "other" — is case 8 below).
    _write_access(mod, {
        "allowFrom": ["Uolduser"],
        "tofuOwner": "Uolduser",
    })
    tm = load_tier_map()
    expect("absent tierMap returns empty dict", tm, {})

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

    # --- Resolution tests: call the REAL resolve_access_tier (#2512) ---
    # A local re-implementation here is a self-asserting helper: it can't
    # fail when the shipped rule is wrong, and its case 8 historically
    # asserted the pre-#2161 fail-OPEN behavior while the bridge shipped
    # fail-closed. Every case below goes through src/slack-bridge.py's own
    # symbol, so reverting the fail-closed branch fails this suite.
    resolve_access_tier = mod.resolve_access_tier

    # Case 7: tierMap present, uid missing → "other" (the #893 fix)
    expect(
        "tierMap non-empty, uid missing → 'other'",
        resolve_access_tier("Unewguy", {"Uteam": "team"}, True),
        "other",
    )

    # Case 8: tierMap absent/empty → "other" (fail CLOSED, #2161). The seed
    # grandfathers legit pre-tierMap configs into a NON-empty map, so an
    # empty map here means the seed failed — never grant owner off that.
    expect(
        "tierMap absent → 'other' (fail closed, #2161)",
        resolve_access_tier("Uolduser", {}, True),
        "other",
    )

    # Case 8b: seed itself failed (empty map, seeded_ok=False) → still "other".
    expect(
        "seed failure + empty tierMap → 'other' (fail closed, #2161)",
        resolve_access_tier("Uolduser", {}, False),
        "other",
    )

    # Case 9: uid in tierMap → mapped value
    expect(
        "uid mapped to 'team' → 'team'",
        resolve_access_tier("Uteam", {"Uteam": "team"}, True),
        "team",
    )

    # Case 10: unknown tier value → degrade to "other"
    expect(
        "unknown tier value → 'other'",
        resolve_access_tier("Ubad", {"Ubad": "rando"}, True),
        "other",
    )

    # Case 11: tierMap present, multiple uids, one missing
    tm_multi = {"Uteam": "team", "Uother": "other"}
    expect(
        "mixed tierMap, unmapped uid → 'other'",
        resolve_access_tier("Umissing", tm_multi, True),
        "other",
    )
    expect(
        "mixed tierMap, mapped uid → correct tier",
        resolve_access_tier("Uteam", tm_multi, True),
        "team",
    )
    # Owner is a recorded tier like any other — mapped uid → "owner".
    expect(
        "uid mapped to 'owner' → 'owner'",
        resolve_access_tier("Uboss", {"Uboss": "owner"}, True),
        "owner",
    )
    print(f"Results: {passes} passed, {fails} failed")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
