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
import re
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
#
# `ag2space` is the desktop gateway bridge (remote-gateway-bridge's
# owner-DM drain); its owner-activity writer stamps that channel on
# every owner message from the AG2 Space app.
BRIDGE_CHANNELS = frozenset({"discord", "telegram", "ag2space"})


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
             channel (discord / telegram / ag2space) → claim only when
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


# The .to-<channel> tag rides between stem and suffix (globs/claims keep it).
# Unlike the [channel:] BODY marker (room redirect), it selects WHICH BRIDGE.

# Slack is deliberately a destination but NOT a BRIDGE_CHANNEL: it races
# without activity routing — aimable, never the undestined-activity winner.
PROACTIVE_DESTINATIONS = frozenset(BRIDGE_CHANNELS | {"slack"})

_DESTINATION_RE = re.compile(r"\.to-([a-z0-9_-]+)\.txt\Z")


def proactive_filename(ts, channel: "str | None" = None) -> str:
    """Typed constructor — the only way to spell a destined proactive name.
    Writer and every claiming reader share this grammar (phone-key precedent:
    a private spelling on either side re-creates the cross-bridge race)."""
    if channel is None:
        return f"proactive-{ts}.txt"
    if channel not in PROACTIVE_DESTINATIONS:
        raise ValueError(f"unknown proactive destination {channel!r}")
    return f"proactive-{ts}.to-{channel}.txt"


def proactive_destination(name) -> "str | None":
    """The declared destination, or None for legacy/undestined names. An
    unrecognized tag still reads as a destination: a file aimed at a channel
    this install lacks must strand visibly, never fall into another
    bridge's race."""
    m = _DESTINATION_RE.search(Path(name).name)
    return m.group(1) if m else None


def should_claim_proactive_file(name, state_file_path: Path,
                                this_channel: str) -> bool:
    """Per-FILE claim decision: destination outranks activity routing."""
    dest = proactive_destination(name)
    if dest is not None:
        return dest == this_channel
    return should_claim_proactive(state_file_path, this_channel)


def fallback_claims_name(name, this_channel: str) -> bool:
    """Per-file gate for a channel's catch-all fallback (no activity routing):
    a foreign or unknown .to-<channel> tag is never claimed — an explicit
    destination strands visibly rather than falling into another channel's
    fallback sweep."""
    return proactive_destination(name) in (None, this_channel)


# The [channel:] BODY marker names a channel but IMPLIES a bridge, and that
# implication is what each adapter re-derived — two of them Discord-only.

# Discord snowflake / Matrix room-or-alias / Slack channel id. Anchored whole:
# a substring match would classify `#room:server` off its leading character.
_TARGET_KINDS = (
    ("discord", re.compile(r"\d{17,20}\Z")),
    ("ag2space", re.compile(r"[!#][^\s:]+:[^\s:]+\Z")),
    ("slack", re.compile(r"[CDG][A-Z0-9]{6,}\Z")),
)


def target_channel_kind(target) -> "str | None":
    """The bridge a resolved `[channel:]` target belongs to, or None.

    None means "not recognised as any bridge's address" — deliberately NOT
    "foreign". See body_claimable_by for why that distinction is load-bearing.

    Telegram is absent by decision, not omission: its bridge DROPS a `[channel:]`
    redirect outright, so classifying a telegram chat id would route the file to
    a bridge guaranteed never to deliver it — the strand this module prevents.
    """
    value = str(target or "").strip()
    for kind, pattern in _TARGET_KINDS:
        if pattern.fullmatch(value):
            return kind
    return None


def body_target_channel(body) -> "str | None":
    """The bridge the body addresses, or None when no redirect will EXECUTE.

    Reads the shared parser's redirect action rather than matching the text: a
    private regex sees `[channel:]` that `[dm-only]` has already disarmed, and
    routing on a disarmed address strands the file at a bridge that will not
    deliver it. Re-deriving the grammar here is the defect this module fixes.
    """
    from result_markers import parse_markers  # noqa: PLC0415 — see module note
    redirect = next(
        (a for a in parse_markers(str(body or "")).actions if a.kind == "redirect"),
        None)
    return target_channel_kind(redirect.value) if redirect is not None else None


def body_claimable_by(body, this_channel: str) -> bool:
    """False only when the body names ANOTHER bridge's address.

    An unrecognised target stays claimable, which is the pre-existing behaviour
    of every body-marker gate and is deliberately not changed here: stranding a
    briefing on a malformed target is a worse failure than delivering it with a
    stray marker line, and it is a separate judgement from this one.
    """
    kind = body_target_channel(body)
    return kind is None or kind == this_channel


def redirect_target_is_foreign(target, this_channel: str) -> bool:
    """Strict form: anything not POSITIVELY this bridge's address is foreign.

    The default destination uses this — an unrecognised target must not fall
    into the default's delivery, or every malformed marker lands in one DM.
    """
    return target_channel_kind(target) != this_channel
