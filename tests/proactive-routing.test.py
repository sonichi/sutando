#!/usr/bin/env python3
"""Unit tests for `proactive_routing.should_claim_proactive`.

`results/proactive-*.txt` files were polled by every configured bridge,
and whichever bridge's polling loop reached the file first did the
atomic-rename claim. The race produced unpredictable cross-channel
delivery, with proactive owner-notifications landing on whichever
bridge happened to win that iteration.

Fix: `should_claim_proactive(state_file, this_channel)` consults
`state/last-owner-activity.json` and returns True only when this
bridge is the last-active channel. Default-to-Discord on missing or
malformed state so fresh installs route predictably.

This test file pins every branch of the decision rule.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import should_claim_proactive  # noqa: E402


def _with_state(content, fn):
    tmp = Path(tempfile.mkdtemp(prefix="sutando-proactive-test-"))
    state = tmp / "last-owner-activity.json"
    if content is not None:
        if isinstance(content, str):
            state.write_text(content)
        else:
            state.write_text(json.dumps(content))
    try:
        fn(state)
    finally:
        if state.exists():
            state.unlink()
        tmp.rmdir()


def test_discord_active_routes_to_discord():
    """Owner's last activity was on Discord — Discord claims, Telegram
    skips. The headline case."""
    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False
    _with_state({"channel": "discord", "ts": 1779339000}, run)


def test_telegram_active_routes_to_telegram():
    """Symmetric: Telegram-recent activity -> Telegram claims, Discord
    skips."""
    def run(state):
        assert should_claim_proactive(state, "discord") is False
        assert should_claim_proactive(state, "telegram") is True
    _with_state({"channel": "telegram", "ts": 1779339000}, run)


def test_missing_state_file_defaults_to_discord():
    """Fresh install -> Discord wins by default. Exactly one bridge
    claims (the race-leak class is closed)."""
    def run(state):
        assert not state.exists()
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False
    _with_state(None, run)


def test_malformed_state_file_defaults_to_discord():
    """Corrupt state file -> fail closed (default discord). Must not
    raise — the polling loop would silently die otherwise."""
    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False
    _with_state("{ this is not json", run)


def test_state_file_missing_channel_field_defaults_to_discord():
    """A state file written by an older bridge version (no `channel`
    field) or by a partial mid-write -> default to discord."""
    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False
    _with_state({"ts": 1779339000}, run)


def test_state_file_empty_channel_string_defaults_to_discord():
    """`{"channel": ""}` — distinct from missing channel; pin it
    handles the same way."""
    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False
    _with_state({"channel": "", "ts": 1779339000}, run)


def test_state_file_non_dict_root_defaults_to_discord():
    """A state file whose root is a list/scalar (corruption) ->
    default. Pin the type guard."""
    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False
    _with_state(["not a dict"], run)


def test_unknown_channel_value_does_not_match_anyone():
    """`{"channel": "slack"}` — channel value not "discord" or
    "telegram". Neither bridge should claim. Strict equality, no
    fallback to Discord in this case — silence is correct."""
    def run(state):
        assert should_claim_proactive(state, "discord") is False
        assert should_claim_proactive(state, "telegram") is False
    _with_state({"channel": "slack", "ts": 1779339000}, run)


def main():
    test_discord_active_routes_to_discord()
    test_telegram_active_routes_to_telegram()
    test_missing_state_file_defaults_to_discord()
    test_malformed_state_file_defaults_to_discord()
    test_state_file_missing_channel_field_defaults_to_discord()
    test_state_file_empty_channel_string_defaults_to_discord()
    test_state_file_non_dict_root_defaults_to_discord()
    test_unknown_channel_value_does_not_match_anyone()
    print("All proactive-routing tests passed.")


if __name__ == "__main__":
    main()
