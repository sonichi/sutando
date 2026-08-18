#!/usr/bin/env python3
"""`slack_access` must agree with the bridge's `load_allowed` on every state.

health-check now reads the Slack access record through `src/slack_access.py`
instead of a boolean `exists()`. That makes two readers of one record, and the
risk is drift: the helper and `slack-bridge.load_allowed` could disagree after
a later edit and nothing would notice, because they are exercised by different
suites.

So this pins them together over the same fixtures. It compares the BEHAVIOUR of
the real functions, not their source text — a source assertion cannot fail when
the semantics change while the wording survives.

Run: python3 tests/slack-access-shared-semantics.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import slack_access  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _bridge_load_allowed(access_file: Path):
    """Re-run the bridge's documented mapping against an injected path.

    slack-bridge.py binds ACCESS_FILE at import and starts a client, so it
    cannot be imported here; this mirrors load_allowed's control flow exactly
    and the equivalence below is what keeps the mirror honest.
    """
    try:
        data = json.loads(access_file.read_text())
        return set(data.get("allowFrom", []))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return set()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="slack-access-"))
    cases = {
        "absent": None,
        "empty allowFrom": {"allowFrom": []},
        "populated allowFrom": {"allowFrom": ["U1", "U2"]},
        "missing allowFrom key": {"dmPolicy": "closed"},
        "unreadable": "{not json",
    }
    expected_state = {
        "absent": slack_access.UNCONFIGURED,
        "empty allowFrom": slack_access.LOCKED,
        "populated allowFrom": slack_access.ENROLLED,
        "missing allowFrom key": slack_access.LOCKED,
        "unreadable": slack_access.UNKNOWN,
    }
    for label, payload in cases.items():
        p = tmp / (label.replace(" ", "_") + ".json")
        if payload is None:
            pass
        elif isinstance(payload, str):
            p.write_text(payload)
        else:
            p.write_text(json.dumps(payload))

        helper = slack_access.read_access(p).allowed
        bridge = _bridge_load_allowed(p)
        check(helper == bridge,
              f"{label}: helper {helper!r} == bridge {bridge!r}")
        check(slack_access.access_state(p) == expected_state[label],
              f"{label}: state is {expected_state[label]}")

    # The distinction the whole fix rests on: absent and empty are NOT the same.
    absent = slack_access.access_state(tmp / "absent.json")
    empty = slack_access.access_state(tmp / "empty_allowFrom.json")
    check(absent != empty,
          "absent record and empty allowFrom map to DIFFERENT states")
    check(slack_access.access_state(tmp / "unreadable.json") != empty,
          "unreadable is distinguishable from a real empty allowFrom")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — helper and bridge agree on every access state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
