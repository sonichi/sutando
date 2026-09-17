"""
Result Router — fallback & audit policy (Result Router v1, slice S4).

Companion to `result_markers.py` (#873, which owns marker parsing:
skip/redirect/attach). This module owns the OTHER half of delivery policy from
the Result Router spec (`notes/design-result-router-spec-2026-07-06.md`):
**what happens when the normal delivery can't or didn't land**, and the audit
trail. It is a set of PURE functions — no I/O, no network, no bridge state — so
it is trivially testable and safe to adopt bridge-by-bridge (the wiring is a
separate slice; this PR only lands the policy + tests).

Spec decisions encoded here (§9, owner 2026-07-07):

  §9.1  A live-session (voice/phone) result that outlives its session is NOT
        silently dropped — it is delivered to the owner DM as a *late result*.
  §9.2  Late-result presentation is a PLAIN TEXT prefix, `[late result —
        session ended]`, followed by the original body. No per-surface cards;
        every surface renders the prefix as-is.
  §9.3  A delivery failure of ANY tier produces BOTH a structured audit line
        AND an owner DM naming the task id, tier, surface, and error. (Owner
        chose stronger visibility over audit-only.)

And §4 (fallback trigger conditions) + §7 (audit) as predicates/formatters.

Normative rule zero (spec §1): *exactly one user-visible delivery per user
intent* — every function here exists to enforce **no silent drops** while never
manufacturing a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ── §9.2 late-result prefix ──────────────────────────────────────────────────

# Exact, surface-agnostic. Kept as a module constant so bridges and tests share
# one string — a drift here would fragment how a late result reads per surface.
LATE_RESULT_PREFIX = "[late result — session ended]"


def late_result_body(body: str) -> str:
    """Prepend the §9.2 late-result prefix to a result body.

    Plain text, rendered as-is on every surface. Idempotent: a body that
    already leads with the prefix is returned unchanged (guards against a
    double-prefix if two fallback paths both fire for the same result).
    """
    text = body or ""
    if text.lstrip().startswith(LATE_RESULT_PREFIX):
        return text
    # Blank line keeps the prefix on its own line; an empty late result stays
    # visible (producer bug per §4), never swallowed.
    return f"{LATE_RESULT_PREFIX}\n\n{text}" if text.strip() else LATE_RESULT_PREFIX


# ── §4 fallback trigger conditions ───────────────────────────────────────────

# The four causes that MUST route to the owner-DM fallback (exhaustive, §4):
# session end / API error post-retry / over hard limit post-chunking / channel gated.
FallbackTrigger = Literal[
    "session_closed",
    "delivery_error",
    "over_limit",
    "channel_gated",
]

# NON-triggers (§4): slow delivery / idle user / empty-looking result deliver
# as-is — an empty result is a producer bug and must stay visible.
_NON_TRIGGERS = frozenset({"slow_delivery", "user_idle", "empty_result"})

_FALLBACK_TRIGGERS = frozenset({
    "session_closed", "delivery_error", "over_limit", "channel_gated",
})


def is_fallback_trigger(reason: str) -> bool:
    """True iff `reason` is a spec-§4 fallback trigger.

    Unknown reasons return False (fail-safe: an unrecognized reason does not
    manufacture an owner DM). Explicit non-triggers also return False.
    """
    return reason in _FALLBACK_TRIGGERS


# ── §9.3 delivery-failure owner notice + §7 audit ────────────────────────────

@dataclass(frozen=True)
class DeliveryFailure:
    """The facts of one failed (or fell-back) delivery, for owner-DM + audit."""

    task_id: str
    tier: str            # "owner" | "team" | "guest" (or "" if unknown)
    surface: str         # "discord" | "slack" | "telegram" | "voice" | ...

    error: str  # short human-readable cause


def delivery_failure_notice(f: DeliveryFailure) -> str:
    """The owner-DM text for a §9.3 delivery failure (any tier).

    Names task id, tier, surface, and error so the owner can act without
    grepping bridge logs. One compact multi-line block; safe on every surface.
    """
    tier = f.tier or "unknown"
    return (
        f"⚠️ Result delivery failed — you did not see this.\n"
        f"• task: {f.task_id}\n"
        f"• tier: {tier}\n"
        f"• surface: {f.surface}\n"
        f"• error: {f.error}"
    )


# Audited dispositions (§3/§6/§7): delivered=primary; redirected=[channel:];
# deduped/no_send=silent archive; late_result/failed=owner-DM fallback (+audit).
Disposition = Literal[
    "delivered",
    "redirected",
    "deduped",
    "no_send",
    "late_result",
    "failed",
]


def audit_line(task_id: str, disposition: str, surface: str, ts: str) -> str:
    """One structured audit line per result (spec §7).

    Makes "did the user ever see this?" answerable without grepping four bridge
    logs. Tab-separated so it greps/cuts cleanly; fields are positional:
    `ts \\t task_id \\t disposition \\t surface`. `ts` is caller-supplied (an
    ISO-8601 UTC string) — this module stays pure and does no clock reads.
    """
    return f"{ts}\t{task_id}\t{disposition}\t{surface}"


#: Empty polls before a stuck result is announced. `poll_results()` sleeps 1s, not
#: the neighbours' 3s, so 20 is ~20s: past a write, inside the 7d age-out.
EMPTY_RESULT_POLL_THRESHOLD = 20


def empty_result_notice(
    task_id: str,
    path: str,
    consecutive: int,
    threshold: int = EMPTY_RESULT_POLL_THRESHOLD,
) -> "str | None":
    """Every result file is briefly present-and-empty (`>` truncates at open), so
    persistence, not first sight, separates a stuck result from a racing one."""
    if consecutive != threshold:
        return None
    return (
        f"[result] {task_id}: result file has been PRESENT BUT EMPTY for "
        f"{consecutive} consecutive polls ({path}). The reply is NOT being "
        f"delivered and the task stays pending until the 7-day age-out. "
        f"This is past any partial-write window — the writer likely died "
        f"mid-write or produced no body."
    )


def note_empty_result(
    counters: dict,
    task_id: str,
    path: str,
    threshold: int = EMPTY_RESULT_POLL_THRESHOLD,
) -> "str | None":
    """Counting lives here so the policy is not copied into each bridge; `counters` is
    caller-owned, keeping this module's no-I/O, no-global contract."""
    n = counters.get(task_id, 0) + 1
    counters[task_id] = n
    return empty_result_notice(task_id, path, n, threshold)
