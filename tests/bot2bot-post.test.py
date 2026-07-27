#!/usr/bin/env python3
"""Tests for skills/bot2bot-post/post.py — the fleet-scope guard.

Regression for the 2026-07-27 dead-letter: `bot2bot-post --to <contributor>`
silently posted to the fleet-only #bot2bot (allowFrom = Air/Mini/Pro), where
qingyun/Rui/john aren't members, so the ping went nowhere. bot2bot-post is a
FLEET-coordination tool; the guard makes it REFUSE a recipient who isn't a
member of the #bot2bot channel (and thus the bot-vs-human-owner id mix-up),
instead of dead-lettering. Contributor messaging stays a separate concern.

Run: python3 tests/bot2bot-post.test.py   (exit 0 pass / non-zero on failure)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_POST = Path(__file__).resolve().parents[1] / "skills" / "bot2bot-post" / "post.py"
_spec = importlib.util.spec_from_file_location("b2b_post", _POST)
b2b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b2b)

_fails = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# Numeric ids so main()'s resolve_to_target accepts a raw --to value.
AIR, MINI, PRO = "100", "200", "300"       # fleet bots (in #bot2bot)
QINGYUN, RUI = "900", "901"                # contributors (in #dev, NOT #bot2bot)
ACCESS = {
    "allowFrom": ["1"],
    "groups": {
        "1490_fleet": {"role": "bot2bot", "allowFrom": [AIR, MINI, PRO]},
        "1485_dev": {"requireMention": True, "allowFrom": ["1", QINGYUN, RUI]},
    },
}
FLEET = "1490_fleet"

# --- _recipient_in_channel: the scope check ---
check("fleet member → in channel", b2b._recipient_in_channel(ACCESS, FLEET, MINI) is True)
check("contributor (not in fleet) → NOT in channel", b2b._recipient_in_channel(ACCESS, FLEET, QINGYUN) is False)
check("unknown id → NOT in channel", b2b._recipient_in_channel(ACCESS, FLEET, "nobody") is False)
check("missing channel → False", b2b._recipient_in_channel(ACCESS, "no_such", MINI) is False)
# int allowFrom entry still matches a str recipient
check("int allowFrom matches str recipient",
      b2b._recipient_in_channel({"groups": {"c": {"allowFrom": [42]}}}, "c", "42") is True)

# --- main(): the guard refuses a non-fleet recipient, allows a fleet one ---
_orig = {k: getattr(b2b, k) for k in ("load_token", "load_access", "get_self_id", "post")}
_posted = {}


def _install_mocks():
    b2b.load_token = lambda: "tok"
    b2b.load_access = lambda: ACCESS
    b2b.get_self_id = lambda token: PRO  # this bot is a fleet member
    b2b.post = lambda ch, txt, tok: _posted.update(channel=ch, text=txt) or {"id": "1"}


def _restore():
    for k, v in _orig.items():
        setattr(b2b, k, v)


# resolve_to_target on a raw numeric-like id just returns it; use ids present in fleet allowFrom.
# main() reads sys.argv, so drive it directly.
_install_mocks()
try:
    # guard REFUSES a contributor (in #dev, not #bot2bot)
    sys.argv = ["post.py", "--to", QINGYUN, "ping", "hi"]
    raised = False
    try:
        b2b.main()
    except SystemExit as e:
        raised = True
        msg = str(e)
    check("main: --to contributor → SystemExit (refused, not posted)", raised and "posted" not in _posted)
    check("main: refusal names #bot2bot fleet-scope", raised and "fleet-coordination only" in msg)

    # guard ALLOWS a fleet member
    _posted.clear()
    sys.argv = ["post.py", "--to", MINI, "done", "shipped"]
    b2b.main()
    check("main: --to fleet member → posts to fleet channel", _posted.get("channel") == FLEET)
    check("main: fleet post carries the mention", _posted.get("text", "").startswith(f"<@{MINI}> "))
finally:
    _restore()

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — fleet-scope guard")
