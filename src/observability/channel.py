"""Channel ingress/egress observability for the chat bridges.

The ONE place that builds ``channel.<surface>.<in|out>`` observability events
(discord / telegram / slack / whatsapp). Bridges call ``emit_channel(...)``
instead of hand-rolling the envelope so the taxonomy + actor/field mapping live
in a single source of truth -- the same single-constructor discipline as
``result_channel_key``. Best-effort: this never raises into the bridge.

    from observability.channel import emit_channel
    # inbound — a message was accepted and a task file written
    emit_channel("discord", "in", user_id=str(author_id),
                 channel_id=str(channel_id), access_tier=tier,
                 data={"task_id": task_id, "is_dm": is_dm})
    # outbound — a reply was delivered
    emit_channel("slack", "out", channel_id=channel,
                 data={"task_id": task_id, "file_count": n})

``direction`` is ``"in"`` for an accepted inbound message and ``"out"`` for a
delivered reply. The event is written by the default jsonl-file sink (which
owns path resolution via the canonical workspace resolver), where the
collector + dashboard read it.
"""

from __future__ import annotations

from typing import Any

from .obs import emit

# surface -> the emitting process `source` (matches the Source union in events.ts)
_SOURCE = {
    "discord": "discord-bridge",
    "telegram": "telegram-bridge",
    "slack": "slack-bridge",
    "whatsapp": "whatsapp-bridge",
}

# The bridges' access-control vocabulary (owner/team/other) onto the obs schema's
# AccessTier (events.ts: owner/team/public/unknown). "other" is the bridges' name
# for the schema's "public" (non-owner external); anything unrecognized — incl.
# the fail-safe sentinel — is "unknown", never "owner". Keeps every emitted
# channel event in-schema for downstream TS consumers.
_TIER_TO_SCHEMA = {
    "owner": "owner",
    "team": "team",
    "other": "public",
    "public": "public",
    "unknown": "unknown",
}


def _normalize_tier(tier: str) -> str:
    return _TIER_TO_SCHEMA.get(tier, "unknown")


def emit_channel(
    surface: str,
    direction: str,
    *,
    user_id: str = "",
    channel_id: str = "",
    access_tier: str = "unknown",
    outcome: str = "ok",
    trace_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Emit one ``channel.<surface>.<in|out>`` event. Silent on any failure --
    observability must never crash a bridge."""
    try:
        ev: dict[str, Any] = {
            "source": _SOURCE.get(surface, f"{surface}-bridge"),
            "actor": {
                "user_id": str(user_id),
                "channel": surface,
                "access_tier": _normalize_tier(access_tier),
            },
            "kind": f"channel.{surface}.{direction}",
            "outcome": outcome,
        }
        if trace_id:
            ev["trace_id"] = trace_id
        payload = dict(data or {})
        if channel_id:
            payload.setdefault("channel_id", str(channel_id))
        if payload:
            ev["data"] = payload
        emit(ev)
    except Exception:
        pass
