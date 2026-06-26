"""The usage record (Python twin of types.ts).

Durable, billable/attributable primitive — distinct from an obs event. Emitted
as a plain dict; TypedDicts here are for editor help only.
"""

from __future__ import annotations

from typing import Any, TypedDict


class UsageAttrs(TypedDict, total=False):
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    cost_usd: float  # ADVISORY estimate only — never the billed figure


class UsageRecord(TypedDict, total=False):
    schema: int
    usage_id: str
    ts: float
    tenant_id: str | None
    trace_id: str
    actor: dict[str, Any]
    source: str
    source_file: str
    meter: str
    quantity: float
    unit: str
    provider: str
    provider_ref: str | None
    attrs: dict[str, Any]
