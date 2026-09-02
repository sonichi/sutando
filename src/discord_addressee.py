"""
Shared-channel addressee gate (pure) — companion to `discord-bridge.py`.

A single, testable decision for one question: in a *shared* channel — one
configured `requireMention:false` and NOT `role:"bot2bot"` — is a given message
addressed to THIS bot, or to someone else in the room?

Why this exists (owner-reported 2026-07-18): `requireMention:false` was being
read as "process every message in the channel". In a multi-agent room that is
wrong — it made this core answer:

  * the owner *replying to another agent* (Discord auto-adds the reply-target to
    `message.mentions`; the old bridge gate then *excluded* that target from its
    "addressed elsewhere?" check — backwards — so the reply slipped through), and
  * *another agent's own posts* (e.g. a sibling bot's "⏳ working…" status),

as if they were addressed to us. `requireMention:false` should mean only "the
owner needn't @-mention us", never "grab everything".

Kept as a pure function (no discord.py, no I/O) so the truth table is unit-tested
directly — same shape as `result_router.py` / `result_markers.py`. The bridge
resolves the concrete discord objects into these primitives and gates on
`requireMention:false` + `role != "bot2bot"` before calling in.
"""

from __future__ import annotations


def reference_is_reply(has_reference: bool, reference_type_name) -> bool:
    """Forwards carry `reference` too, matched by enum NAME so renumbering is safe;
    a missing type is a reply, the pre-forward default."""
    if not has_reference:
        return False
    name = getattr(reference_type_name, "name", reference_type_name)
    return name != "forward"


def is_addressed_in_shared_channel(
    *,
    author_is_bot: bool,
    bot_mentioned: bool,
    role_mentioned: bool,
    is_reply: bool,
    reply_author_id,
    self_id,
    author_id=None,
    other_agent_mentioned: bool = False,
) -> bool:
    """Return True iff this message should be processed as addressed to us.

    Only meaningful for a shared channel (`requireMention:false`, non-bot2bot);
    callers gate on that before consulting this.

    Addressed to us (→ True) when any of:
      * we are @-mentioned (`bot_mentioned`) or role-mentioned (`role_mentioned`);
      * the message is a reply to one of OUR messages
        (`is_reply and reply_author_id == self_id`);
      * a HUMAN replies to their OWN message (`reply_author_id == author_id`).
        Requires `author_id`; omitted (legacy callers) turns the exemption off.

    Otherwise the message is addressed elsewhere (→ False) when it is:
      * another agent's own post/reply — `author_is_bot` and not addressed to
        us; or
      * a HUMAN reply to a DIFFERENT author — `is_reply` whose target is neither
        us nor the sender; or
      * a human message that @-mentions a DIFFERENT agent and not us
        (`other_agent_mentioned`) — it addressed that agent, not everyone.

    A fresh (non-reply) message from a human that addresses NOBODY is still
    processed (→ True): the owner posting directly is for whoever is listening —
    that is the pre-existing `requireMention:false` behavior and is out of scope
    for this fix.

    `other_agent_mentioned` is resolved by the adapter (which mentioned users are
    bots other than us); the policy decision stays here.
    """
    replying_to_me = bool(
        is_reply and reply_author_id is not None and reply_author_id == self_id
    )
    if bot_mentioned or role_mentioned or replying_to_me:
        return True

    # Another agent's own post/reply is never for us — checked before the
    # self-reply exemption so a sibling bot replying to itself stays skipped.
    if author_is_bot:
        return False

    replying_to_self = bool(
        is_reply and reply_author_id is not None
        and author_id is not None and reply_author_id == author_id
    )
    replying_to_other = bool(is_reply) and not replying_to_me and not replying_to_self
    if replying_to_other:
        return False

    # A human who @-mentioned another agent addressed THAT agent, not everyone.
    if other_agent_mentioned:
        return False

    return True
