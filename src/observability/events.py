"""The universal observability event envelope (Python twin of types.ts).

Defined as TypedDicts for editor help; ``obs.py`` builds and emits plain dicts
(matching ``event_log.py``) so the JSONL bytes stay format-consistent with the
TS twin. The envelope is thin and stable; kind-specific data lives in the open
``data`` bag and the open dotted ``kind`` namespace.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Outcome = Literal["ok", "error", "denied"]
# `public` = canonical non-owner-external (bridges' "other" maps onto it);
# `unknown` = fail-safe when a tier can't be resolved (never assume "owner").
AccessTier = Literal["owner", "team", "public", "unknown"]


class Actor(TypedDict, total=False):
    user_id: str
    channel: str
    access_tier: str
    tenant_id: str | None


class UsageAdvisory(TypedDict, total=False):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    audio_seconds: float
    cost_usd: float


class ObsEvent(TypedDict, total=False):
    schema: int
    ts: float
    trace_id: str
    span_id: str
    parent_span_id: str
    node: str
    source: str
    source_file: str
    actor: Actor
    kind: str
    outcome: str
    duration_ms: float
    usage: UsageAdvisory
    data: dict[str, Any]
