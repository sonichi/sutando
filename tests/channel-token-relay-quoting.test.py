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

import tempfile  # noqa: E402

from channel_token import (  # noqa: E402
    RELAY_TOKEN_VARS,
    _clean,
    clean_relay_token,
    resolve_channel_token,
    token_from_env_file,
)

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


# Through the REAL readers: a peel nothing calls is not a fix, so drive the
# corruption through the readers a bridge uses, not clean_relay_token directly.
with tempfile.TemporaryDirectory() as d:
    env = os.path.join(d, ".env")
    with open(env, "w") as fh:
        fh.write("REMOTE_TASK_TOKEN=''\\''" + REAL + "''\\''\n")
        fh.write("DISCORD_BOT_TOKEN=\"\"abc\"\"\n")
    from pathlib import Path

    check("token_from_env_file heals the on-disk corruption",
          token_from_env_file("REMOTE_TASK_TOKEN", Path(env)) == REAL)
    check("resolve_channel_token heals it via the .env layer",
          resolve_channel_token("REMOTE_TASK_TOKEN", env_file=Path(env),
                                environ={}, vault_get=lambda _v: "") == REAL)
    check("a NON-relay var read by the same reader keeps one-layer",
          token_from_env_file("DISCORD_BOT_TOKEN", Path(env)) == '"abc"')

check("an exported (env-layer) relay token is healed too",
      resolve_channel_token("REMOTE_TASK_TOKEN",
                            environ={"REMOTE_TASK_TOKEN": "''\\''" + REAL + "''\\''"},
                            vault_get=lambda _v: "") == REAL)
check("the vault layer is healed too",
      resolve_channel_token("AG2_REMOTE_TOKEN", environ={},
                            vault_get=lambda _v: "''\\''" + REAL + "''\\''") == REAL)
check("both gateway spellings are covered",
      tuple(RELAY_TOKEN_VARS) == ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"))

print()
if all(_checks):
    print(f"channel-token relay quoting: {len(_checks)}/{len(_checks)} passed")
else:
    print("FAILED")
    sys.exit(1)
