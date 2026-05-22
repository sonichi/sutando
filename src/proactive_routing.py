"""Channel routing for proactive owner-notification messages.

Background: `results/proactive-*.txt` files are polled by ALL
configured bridges (`src/discord-bridge.py:poll_proactive` and
`src/telegram-bridge.py:main` proactive loop). The pre-fix arrangement
relied on a race: whichever bridge's polling loop reached the file
first did an atomic-rename claim (`f.with_suffix(".sending")`) and
delivered the message; the other bridge's next poll found the file
gone and silently skipped.

The race-claim was correct as "deliver at most once" but wrong as
"deliver where the owner expects to read it." A user with both
Discord and Telegram allowlisted would see proactive messages
randomly land on one channel or the other based on poll timing —
on 2026-05-20 a Discord-context follow-up landed on Telegram and
the owner asked "Why have you sent me the message on Telegram? It
looks like a bug — I was only checking messages from you on Discord."

Fix: route proactive messages to the channel where the owner was
**most recently active**. Both bridges already record activity via
`write_owner_activity(channel, summary)` → `state/last-owner-activity.json`.
This module reads that state file and tells the calling bridge
whether it should claim the next proactive file.

Default (no state file yet, or a malformed one): Discord wins.
Discord is the canonical first-channel install path; new installs
without any owner activity yet should route to Discord, not silently
duplicate to every configured bridge.
"""
from __future__ import annotations

import json
from pathlib import Path

# Channels whose bridges actually deliver `proactive-*.txt` files.
# Other producers write `last-owner-activity.json` with channel values
# like `"voice"` (voice agent registered an utterance) or
# `"github-commits"` (auto-poll observed a new commit) — those are
# activity-tracking signals, NOT message-delivery channels. When the
# last activity was on a non-bridge channel, proactive messages must
# still get delivered SOMEWHERE rather than stranded: Discord is the
# default (the canonical first-channel install path).
#
# Per @rickchen007 PR #35 review: pre-fix, only `discord`/`telegram`
# were treated as recognized — and an unrecognized value (`voice`,
# `github-commits`, anything else) returned False for BOTH bridges,
# silently stranding the proactive file in `results/` until the
# next discord/telegram message restored a known activity channel.
BRIDGE_CHANNELS = frozenset({"discord", "telegram"})


def should_claim_proactive(state_file_path: Path, this_channel: str) -> bool:
    """Decide whether this bridge should claim `results/proactive-*.txt`.

    Args:
        state_file_path: Path to `state/last-owner-activity.json`.
        this_channel: Channel identifier for the calling bridge —
            typically ``"discord"`` or ``"telegram"``.

    Returns:
        ``True`` iff the calling bridge is the destination for proactive
        messages right now. The decision rule:

          1. State file says ``data["channel"]`` is a known BRIDGE
             channel (discord / telegram) → claim only when
             ``last_channel == this_channel``. This is the message-
             routing match — owner was last reading there, follow-up
             goes there.
          2. State file missing / unreadable / malformed / no-channel
             / channel-not-a-string / channel-not-a-bridge (e.g.
             ``voice``, ``github-commits``) → default Discord. Owner
             messages must not get stranded when the last activity
             was on a non-bridge surface; Discord is the canonical
             first-channel install path.

    Pure function — no side effects, no logging. Callers handle
    skip/continue control flow.
    """
    try:
        data = json.loads(state_file_path.read_text())
    except FileNotFoundError:
        return this_channel == "discord"
    except (OSError, json.JSONDecodeError):
        return this_channel == "discord"

    if not isinstance(data, dict):
        return this_channel == "discord"

    last_channel = data.get("channel", "")
    if not isinstance(last_channel, str) or not last_channel:
        return this_channel == "discord"

    # If the last-active channel is a known bridge, match strictly so
    # only that bridge claims.
    if last_channel in BRIDGE_CHANNELS:
        return last_channel == this_channel

    # Non-bridge channel (voice / github-commits / etc.): the owner
    # most recently interacted on a surface that doesn't deliver DMs.
    # Default Discord rather than strand the proactive file.
    return this_channel == "discord"
