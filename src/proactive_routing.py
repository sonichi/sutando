"""Channel routing for proactive owner-notification messages.

`results/proactive-*.txt` files are polled by ALL configured bridges
(`src/discord-bridge.py:poll_proactive` and `src/telegram-bridge.py`'s
proactive loop). The pre-fix arrangement relied on a race: whichever
bridge's polling loop reached the file first did an atomic-rename
claim and delivered the message; the other bridge's next poll found
the file gone and silently skipped.

The race-claim was correct as "deliver at most once" but wrong as
"deliver where the owner expects to read it." A user with both
Discord and Telegram allowlisted saw proactive messages randomly
land on one channel or the other based on poll timing — a Discord-
context follow-up could land on Telegram and vice versa.

Fix: route proactive messages to the channel where the owner was
**most recently active**. Both bridges already record activity via
`write_owner_activity(channel, summary)` ->
`state/last-owner-activity.json`. This module reads that state file
and tells the calling bridge whether it should claim the next
proactive file.

Default (no state file yet, or a malformed one): Discord wins.
Discord is the canonical first-channel install path; new installs
without any owner activity yet should route to Discord, not silently
duplicate to every configured bridge.
"""
from __future__ import annotations

import json
from pathlib import Path


def should_claim_proactive(state_file_path: Path, this_channel: str) -> bool:
    """Decide whether this bridge should claim `results/proactive-*.txt`.

    Args:
        state_file_path: Path to `state/last-owner-activity.json`.
        this_channel: Channel identifier for the calling bridge —
            typically ``"discord"`` or ``"telegram"``.

    Returns:
        ``True`` iff the calling bridge is the destination for proactive
        messages right now. The decision rule:

          1. State file present and parseable -> claim only when
             ``data["channel"] == this_channel``.
          2. State file missing, unreadable, or malformed -> claim only
             when ``this_channel == "discord"``. Discord is the default
             so a fresh install (no activity history yet) doesn't
             silently duplicate to every configured bridge.
          3. State file present but ``data["channel"]`` is empty or
             absent -> same default as (2).

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

    return last_channel == this_channel
