#!/usr/bin/env python3
"""Tests for skills/bot2bot-post/post.py — recipient-first channel routing.

Regression for the 2026-07-27 dead-letter mis-route: contributor pings were
sent to the fleet-only #bot2bot (whose allowFrom is Air/Mini/Pro only), so they
silently went nowhere. `resolve_channel_for_recipient` derives the channel FROM
the recipient's allowFrom membership so that mis-route is impossible by
construction, and refuses (None → caller exits) when the recipient is in no
channel.

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


# Two channels: a fleet #bot2bot (Air/Mini/Pro) and a contributor #dev.
ACCESS = {
    "allowFrom": ["owner1"],
    "groups": {
        "1490_fleet": {"role": "bot2bot", "allowFrom": ["air", "mini", "pro"]},
        "1485_dev": {"requireMention": True, "allowFrom": ["owner1", "qingyun", "rui", "pro"]},
        "1494_susan": {"allowFrom": ["susan", "pro"]},
    },
}

# --- recipient-first routing: post where the recipient actually is ---
check("contributor rui → #dev (not fleet)", b2b.resolve_channel_for_recipient(ACCESS, "rui") == "1485_dev")
check("qingyun → #dev", b2b.resolve_channel_for_recipient(ACCESS, "qingyun") == "1485_dev")
check("fleet-only air → fleet channel", b2b.resolve_channel_for_recipient(ACCESS, "air") == "1490_fleet")

# --- the dead-letter guarantee: unknown recipient → None (caller must refuse) ---
check("john (in NO channel) → None (no dead-letter)", b2b.resolve_channel_for_recipient(ACCESS, "john") is None)
check("empty/garbage recipient → None", b2b.resolve_channel_for_recipient(ACCESS, "nobody") is None)

# --- multi-channel recipient: deterministic pick (any reaches them) ---
# 'pro' is in all three; lowest channel id wins deterministically.
check("multi-channel recipient → deterministic lowest id",
      b2b.resolve_channel_for_recipient(ACCESS, "pro") == "1485_dev")

# --- id type coercion: int allowFrom entry still matches a str recipient ---
ACCESS_INT = {"groups": {"c1": {"allowFrom": [12345, 67890]}}}
check("int allowFrom matches str recipient", b2b.resolve_channel_for_recipient(ACCESS_INT, "12345") == "c1")

# --- legacy fleet-broadcast path still resolves the tagged channel ---
check("resolve_bot2bot_channel still finds role:bot2bot", b2b.resolve_bot2bot_channel(ACCESS) == "1490_fleet")

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — recipient-first routing")
