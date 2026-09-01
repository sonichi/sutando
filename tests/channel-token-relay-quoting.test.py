#!/usr/bin/env python3
"""`clean_relay_token` repairs a re-rendered relay token WITHOUT loosening the
one-matching-layer contract every other channel token relies on.

A desktop writer that quoted an already-quoted value produced
`''\''<url>|<secret>''\''` on disk; the bridge then presented a secret carrying
quote bytes and every relay answered 401. Peeling is safe only for the
`url|hex` relay shape — a bot token may legitimately contain quotes, which
tests/discord-token-delegation.test.py pins.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from channel_token import _clean, clean_relay_token  # noqa: E402

_checks = []


def check(label, cond):
    _checks.append(bool(cond))
    print(("  ok   " if cond else "  FAIL ") + label)


REAL = "https://chat.ag2.space/relay|8e58fbf183fdf1f9bcfc3760a829e381"

check("observed corruption is repaired",
      clean_relay_token("''\\''" + REAL + "''\\''") == REAL)
check("single quoted layer (the normal write) is stripped",
      clean_relay_token("'" + REAL + "'") == REAL)
check("a clean value is unchanged",
      clean_relay_token(REAL) == REAL)
check("idempotent",
      clean_relay_token(clean_relay_token("''\\''" + REAL + "''\\''")) == REAL)
check("a non-relay value keeps the one-layer contract",
      clean_relay_token('""abc""') == '"abc"')
check("_clean itself is untouched: doubled quotes lose exactly one layer",
      _clean('""abc""') == '"abc"')
check("_clean itself is untouched: mismatched quotes kept verbatim",
      _clean("\"abc'") == "\"abc'")
check("non-str is not usable", clean_relay_token(None) == "")

print()
if all(_checks):
    print(f"channel-token relay quoting: {len(_checks)}/{len(_checks)} passed")
else:
    print("FAILED")
    sys.exit(1)
