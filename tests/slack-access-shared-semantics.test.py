#!/usr/bin/env python3
"""`slack_access` must agree with the bridge's `load_allowed` on every state.

health-check reads the Slack access record through `src/slack_access.py`. That
makes two readers of one record, and the risk is drift: the helper and
`slack-bridge.load_allowed` could disagree after a later edit and nothing would
notice, because they are exercised by different suites.

This runs the SHIPPED `load_allowed`, extracted from the bridge's own AST. A
mirrored copy cannot detect drift — it drifts with whoever edits the test, and
it hid exactly that: on a malformed `allowFrom` the copy raised while the real
function fails closed to set().

Run: python3 tests/slack-access-shared-semantics.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

# Isolate BEFORE anything reads channel config. This test compiles one AST node
# rather than importing, but the isolation must not depend on that staying true.
_CFG = tempfile.mkdtemp(prefix="slack-access-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_cfg_slack = Path(_CFG) / "channels" / "slack"
_cfg_slack.mkdir(parents=True, exist_ok=True)
(_cfg_slack / "access.json").write_text(json.dumps({"allowFrom": ["U-fixture"]}))

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import slack_access  # noqa: E402

BRIDGE = REPO / "src" / "slack-bridge.py"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _guard(fn):
    """Turn a propagated exception into a comparable value so the suite reports
    it as a FAIL instead of exploding on the first fixture."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — the raise IS the finding
        return f"RAISED {type(exc).__name__}: {exc}"


def _bridge_load_allowed(access_file: Path):
    """Run the SHIPPED `load_allowed` against an injected path.

    The module cannot be imported — `app = App(token=BOT_TOKEN)` constructs a
    Slack client at import and the header mkdir()s into the live workspace — so
    only its AST node is compiled, under the production filename so coverage
    attributes it to the bridge rather than to a copy in this test.
    """
    src = BRIDGE.read_text()
    tree = ast.parse(src, filename=str(BRIDGE))
    fn_node = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "load_allowed")
    cached: list = []
    ns = {"json": json, "ACCESS_FILE": access_file,
          "slack_access": slack_access, "_update_access_cache": cached.append}
    module = ast.Module(body=[fn_node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(BRIDGE), "exec"), ns)
    return ns["load_allowed"]()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="slack-access-"))
    cases = {
        "absent": None,
        "empty allowFrom": {"allowFrom": []},
        "populated allowFrom": {"allowFrom": ["U1", "U2"]},
        "missing allowFrom key": {"dmPolicy": "closed"},
        "unreadable": "{not json",
        # Valid JSON, wrong shape: `.get()` raises, which must read as UNKNOWN
        # rather than as an empty allowFrom.
        "not a mapping": "[]",
        # Syntactically valid records whose allowFrom is the wrong TYPE. The
        # bridge fails closed on both; a helper that raises is not equivalent.
        "allowFrom is a scalar": {"allowFrom": 42},
        "allowFrom holds objects": {"allowFrom": [{"id": "U1"}]},
    }
    # Formerly exempted from value-equality because the bridge splayed them into
    # characters. It delegates now, so they carry the SAME contract as the rest.
    permissive = {
        "allowFrom is a string": {"allowFrom": "U123"},
        "allowFrom holds numbers": {"allowFrom": [7]},
        "allowFrom is mixed": {"allowFrom": ["U1", 7]},
    }
    cases.update(permissive)
    # A list of STRINGS passes the type check and can still authorize nobody.
    blanks = {
        "allowFrom is one empty string": {"allowFrom": [""]},
        "allowFrom is whitespace only": {"allowFrom": ["  "]},
    }
    cases.update(blanks)
    expected_state = {
        "absent": slack_access.UNCONFIGURED,
        "empty allowFrom": slack_access.LOCKED,
        "populated allowFrom": slack_access.ENROLLED,
        "missing allowFrom key": slack_access.LOCKED,
        "unreadable": slack_access.UNKNOWN,
        "not a mapping": slack_access.UNKNOWN,
        "allowFrom is a scalar": slack_access.UNKNOWN,
        "allowFrom holds objects": slack_access.UNKNOWN,
    }
    expected_state.update(dict.fromkeys(permissive, slack_access.UNKNOWN))
    expected_state.update(dict.fromkeys(blanks, slack_access.LOCKED))
    for label, payload in cases.items():
        p = tmp / (label.replace(" ", "_") + ".json")
        if payload is None:
            pass
        elif isinstance(payload, str):
            p.write_text(payload)
        else:
            p.write_text(json.dumps(payload))

        # A raise is a RESULT here, not a crash: the bridge fails closed on every
        # fixture, so a helper that propagates is already non-equivalent.
        helper = _guard(lambda: slack_access.read_access(p).allowed)
        bridge = _guard(lambda: _bridge_load_allowed(p))
        check(helper == bridge,
              f"{label}: helper {helper!r} == bridge {bridge!r}")
        state = _guard(lambda: slack_access.access_state(p))
        check(state == expected_state[label],
              f"{label}: state is {expected_state[label]} (got {state!r})")

    # The distinction the whole fix rests on: absent and empty are NOT the same.
    absent = slack_access.access_state(tmp / "absent.json")
    empty = slack_access.access_state(tmp / "empty_allowFrom.json")
    check(absent != empty,
          "absent record and empty allowFrom map to DIFFERENT states")
    check(slack_access.access_state(tmp / "unreadable.json") != empty,
          "unreadable is distinguishable from a real empty allowFrom")

    # A malformed allowFrom is not an admin lockout: the operator must not be
    # told "an admin locked it down" about a record nobody can classify.
    for label in ("allowFrom is a scalar", "allowFrom holds objects"):
        p = tmp / (label.replace(" ", "_") + ".json")
        check(_guard(lambda: slack_access.access_state(p)) != slack_access.LOCKED,
              f"{label}: does NOT read as a deliberate lockout")

    # Value-equality now covers these (they are in `cases`); this keeps the
    # property that motivated them — neither side lets a real user in.
    PROBE = "U9REALUSER"
    for label, payload in permissive.items():
        q = tmp / (label.replace(" ", "_") + ".json")
        q.write_text(json.dumps(payload))
        helper = _guard(lambda: slack_access.read_access(q).allowed)
        bridge = _guard(lambda: _bridge_load_allowed(q))
        state = _guard(lambda: slack_access.access_state(q))
        check(state == slack_access.UNKNOWN,
              f"{label}: reads UNKNOWN, not ENROLLED (got {state!r})")
        check(isinstance(helper, set) and PROBE not in helper,
              f"{label}: the helper enrols no real user (got {helper!r})")
        check(isinstance(bridge, set) and PROBE not in bridge,
              f"{label}: the bridge admits no real user either (got {bridge!r})")
    # The one that motivated this: "U123" becomes a set of CHARACTERS, which the
    # old equality contract would have called agreement.
    q = tmp / "allowFrom_is_a_string.json"
    check(_bridge_load_allowed(q) == set(),
          "the bridge no longer splays a string into characters")
    check(slack_access.read_access(q).allowed == set(),
          "and it refuses the record for the same reason the helper does")

    # The extraction must be running the real thing. If the bridge ever stops
    # defining load_allowed, every equivalence above would pass vacuously.
    bridge_src = BRIDGE.read_text()
    check("def load_allowed()" in bridge_src,
          "the bridge still defines the function this test extracts")
    check(_bridge_load_allowed(tmp / "populated_allowFrom.json") == {"U1", "U2"},
          "the extracted function returns the bridge's real allowlist")

    # TOFU rests on None-vs-empty-set, previously pinned by grepping the bridge
    # for `return None`. Delegation deletes that text, so assert the behaviour.
    check(_bridge_load_allowed(tmp / "no_such_file.json") is None,
          "a MISSING record is None (TOFU-eligible), never an empty set")
    check(_bridge_load_allowed(tmp / "empty_allowFrom.json") == set(),
          "an empty allowFrom is a set (deliberate lockout), never None")
    check(_bridge_load_allowed(tmp / "unreadable.json") == set(),
          "an unreadable record fails CLOSED to a set, never to None")

    # A blank entry must not survive beside a real one, and must not take the
    # real one down with it.
    q2 = tmp / "mixed_blank.json"
    q2.write_text(json.dumps({"allowFrom": ["U0123ABCD", "", "  "]}))
    check(slack_access.read_access(q2).allowed == {"U0123ABCD"},
          "a blank entry is dropped and the real id keeps working")
    check(slack_access.access_state(q2) == slack_access.ENROLLED,
          "so the record stays ENROLLED rather than being rejected wholesale")
    check(_bridge_load_allowed(q2) == {"U0123ABCD"},
          "and the bridge gate agrees, because it reads through the same helper")
    q3 = tmp / "allowFrom_is_one_empty_string.json"
    check(slack_access.access_state(q3) == slack_access.LOCKED,
          "a bare [\"\"] reads LOCKED — whose remedy is 'add an allowed user id'")
    check(slack_access.access_state(q3) != slack_access.ENROLLED,
          "and never ENROLLED, which would send the operator to Event Subscriptions")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — helper and bridge agree on every access state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
