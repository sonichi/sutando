#!/usr/bin/env python3
"""Unit tests for `proactive_routing.should_claim_proactive`.

## What this guards

The owner reported on 2026-05-20:

> Why have you sent me the message on Telegram? Is in response to our
> Discord communication? It looks like a bug — I was only checking
> messages from you on Discord in response to my messages on Discord.

Root cause: `results/proactive-*.txt` files were polled by every
configured bridge, and whichever bridge's polling loop reached the file
first did the atomic-rename claim. Both bridges had matching code
(rename → send → unlink); the race produced unpredictable cross-channel
delivery, with proactive owner-notifications landing on whichever
bridge happened to win that iteration.

Fix: `should_claim_proactive(state_file, this_channel)` consults
`state/last-owner-activity.json` and returns True only when this
bridge is the last-active channel. Default-to-Discord on missing or
malformed state so fresh installs route predictably.

This test file pins every branch of the decision rule so a future
refactor cannot reintroduce the cross-channel-leak class.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import (should_claim_proactive,  # noqa: E402
                               should_claim_proactive_file)


def _with_state(content, fn):
    """Run fn(state_file) with a temp state file written from content.
    If content is None, the file is absent. If content is a string, it
    is written verbatim (lets us test malformed JSON). Otherwise
    `json.dumps(content)` is used."""
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
    """Owner's last activity was on Discord — Discord bridge claims,
    Telegram skips. The headline case from the 2026-05-20 report."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"channel": "discord", "ts": 1779339000}, run)


def test_telegram_active_routes_to_telegram():
    """Symmetric: Telegram-recent activity → Telegram claims, Discord
    skips. Confirms the rule is bidirectional (not Discord-favored)."""

    def run(state):
        assert should_claim_proactive(state, "discord") is False
        assert should_claim_proactive(state, "telegram") is True

    _with_state({"channel": "telegram", "ts": 1779339000}, run)


def test_ag2space_active_routes_to_ag2space():
    """Owner's last activity was in the AG2 Space desktop app (the gateway
    bridge stamps `channel: ag2space` on every owner message) — the gateway
    drain claims, discord and telegram skip. Pre-fix, `ag2space` fell into
    the unrecognized-channel branch and Discord claimed a desktop owner's
    nudges (or stranded them on a discord-less host)."""

    def run(state):
        assert should_claim_proactive(state, "ag2space") is True
        assert should_claim_proactive(state, "discord") is False
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"channel": "ag2space", "ts": 1786608037}, run)


def test_missing_state_file_defaults_to_discord():
    """Fresh install / no activity yet → Discord wins by default. Two
    bridges polling at the same time on a fresh install must NOT both
    claim (the original race). Discord-default ensures exactly one
    bridge claims."""

    def run(state):
        # state file does not exist (passed None to _with_state)
        assert not state.exists()
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state(None, run)


def test_malformed_state_file_defaults_to_discord():
    """Corrupt state file → fail closed (default discord). Must not
    raise — the polling loop would silently die otherwise. Default-
    to-discord matches the missing-file case for predictability."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state("{ this is not json", run)


def test_state_file_missing_channel_field_defaults_to_discord():
    """A state file written by an older bridge version (no `channel`
    field) or by a partial mid-write → default to discord. Don't
    surprise the user by routing to an unexpected channel."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"ts": 1779339000}, run)


def test_state_file_empty_channel_string_defaults_to_discord():
    """`{"channel": ""}` — distinct from missing channel; pin it
    handles the same way. Future writers that emit an empty channel
    string must not silently route to telegram."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"channel": "", "ts": 1779339000}, run)


def test_state_file_non_dict_root_defaults_to_discord():
    """A state file whose root is a list/scalar (corruption) → default.
    The `data.get("channel")` call would AttributeError on a non-dict
    without this guard; pin that the function returns False (skip) for
    non-discord callers."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state(["not a dict"], run)


def test_voice_channel_defaults_to_discord():
    """Per @rickchen007 PR #35 review: `state/last-owner-activity.json`
    is written with channel values beyond `discord`/`telegram` —
    `"voice"` is a real value the voice agent writes on every owner
    utterance. Pre-fix, the strict `last_channel == this_channel`
    rule returned False for BOTH bridges in this case, stranding
    the proactive file in `results/`.

    Post-fix: non-bridge channels default to Discord (canonical
    first-channel install path)."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True, (
            "voice-channel-active must route to Discord — otherwise the "
            "proactive file is stranded until the owner next DMs"
        )
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"channel": "voice", "ts": 1779339000}, run)


def test_github_commits_channel_defaults_to_discord():
    """Same shape: the github-commit auto-poll writes `{"channel":
    "github-commits"}` on every observed commit. Must NOT strand the
    proactive file."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"channel": "github-commits", "ts": 1779339000}, run)


def test_unrecognized_channel_defaults_to_discord():
    """Generalization: an arbitrary non-bridge channel name (e.g.
    `"slack"`, `"matrix"`, future channels not yet implemented) also
    defaults to Discord rather than stranding the message. The pre-
    fix behavior was strict equality which silently dropped the
    proactive — exactly the bug @rickchen007 identified."""

    def run(state):
        assert should_claim_proactive(state, "discord") is True
        assert should_claim_proactive(state, "telegram") is False

    _with_state({"channel": "slack", "ts": 1779339000}, run)


def test_bridge_channels_set_is_documented():
    """Pin the BRIDGE_CHANNELS constant: a future contributor adding
    a new bridge (e.g. matrix) must update both this constant AND
    add a corresponding `test_<channel>_active_routes_to_<channel>`.
    Without this pin, the constant could silently widen and break the
    "non-bridge defaults to Discord" contract."""
    from proactive_routing import BRIDGE_CHANNELS
    assert BRIDGE_CHANNELS == frozenset({"discord", "telegram", "ag2space"}), (
        f"BRIDGE_CHANNELS changed to {BRIDGE_CHANNELS!r}. If you added a "
        f"new bridge, add a corresponding routing test AND update this "
        f"assertion deliberately."
    )


def test_body_leg_discord_claims_its_own_target():
    """A [channel: <discord-id>] body is claimable by discord even when
    activity routing points elsewhere — the 2026-08-23 deadlock fix."""
    body = "[channel: 1535008729106485288]\nhello"
    def fn(state):
        assert should_claim_proactive_file(
            "proactive-1.txt", state, "discord",
            body_reader=lambda: body) is True
        assert should_claim_proactive_file(
            "proactive-1.txt", state, "telegram",
            body_reader=lambda: body) is False
    _with_state({"ts": 9999999999, "channel": "ag2space"}, fn)


def test_body_leg_fallbacks_and_precedence():
    def fn(state):
        # empty body -> activity routing (ag2space active -> discord declines)
        assert should_claim_proactive_file(
            "proactive-1.txt", state, "discord",
            body_reader=lambda: "") is False
        # unreadable body -> activity routing, never raises
        def boom():
            raise OSError("gone")
        assert should_claim_proactive_file(
            "proactive-1.txt", state, "discord", body_reader=boom) is False
        # filename destination tag outranks the body target
        assert should_claim_proactive_file(
            "proactive-1.to-telegram.txt", state, "telegram",
            body_reader=lambda: "[channel: 1535008729106485288]\nx") is True
        # dm-only disarms the redirect -> body leg abstains -> activity routing
        assert should_claim_proactive_file(
            "proactive-1.txt", state, "ag2space",
            body_reader=lambda: "[dm-only]\n[channel: 1535008729106485288]\nx"
        ) is True
    _with_state({"ts": 9999999999, "channel": "ag2space"}, fn)


def main():
    test_discord_active_routes_to_discord()
    test_telegram_active_routes_to_telegram()
    test_ag2space_active_routes_to_ag2space()
    test_missing_state_file_defaults_to_discord()
    test_malformed_state_file_defaults_to_discord()
    test_state_file_missing_channel_field_defaults_to_discord()
    test_state_file_empty_channel_string_defaults_to_discord()
    test_state_file_non_dict_root_defaults_to_discord()
    test_voice_channel_defaults_to_discord()
    test_github_commits_channel_defaults_to_discord()
    test_unrecognized_channel_defaults_to_discord()
    test_bridge_channels_set_is_documented()
    test_body_leg_discord_claims_its_own_target()
    test_body_leg_fallbacks_and_precedence()
    print("All proactive-routing tests passed.")


if __name__ == "__main__":
    main()
