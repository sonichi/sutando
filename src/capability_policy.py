#!/usr/bin/env python3
"""Mediated capability layer — capability x tier matrix, classify(), decide().
Opt-in DORMANT scaffolding: no production caller registers, imports, or consumes it; decide()/classify() are total and fail-closed to the most restrictive outcome."""
from __future__ import annotations

from typing import NamedTuple, Optional


# Tiers — matrix columns (the access-tier taxonomy is the input, not replaced).
OWNER, TEAM, OTHER, AMBIENT = "owner", "team", "other", "ambient"
TIERS = (OWNER, TEAM, OTHER, AMBIENT)


def normalize_tier(access_tier: Optional[str]) -> str:
    """Map a task's access_tier to a matrix column. Missing/empty -> owner (the
    existing contract); a present-but-unrecognized value fails CLOSED to other, never owner."""
    if access_tier is None:
        return OWNER
    t = str(access_tier).strip().lower()
    if not t:
        return OWNER
    return t if t in TIERS else OTHER


# Capability classes (matrix rows).
INFO_READ = "info-read"
CREDENTIAL_USE = "credential-use"       # exercise a vaulted secret; value never surfaced
CREDENTIAL_READ = "credential-read"     # raw stored value handed back
WRITE_REVERSIBLE = "write-reversible"
WRITE_IRREVERSIBLE = "write-irreversible"  # send / merge / publish / config-write / delete
PURCHASE = "purchase"                   # goods/services on a method on file
FINANCIAL_MOVE = "financial-move"       # trade/transfer of funds — prohibited overlay
CREDENTIAL_ENTRY = "credential-entry"   # typing a NEW secret into a field — prohibited overlay

CLASSES = (
    INFO_READ, CREDENTIAL_USE, CREDENTIAL_READ, WRITE_REVERSIBLE,
    WRITE_IRREVERSIBLE, PURCHASE, FINANCIAL_MOVE, CREDENTIAL_ENTRY,
)

# Decisions; PROHIBITED marks the human-only overlay rows (no tier, no grant).
ALLOW = "allow"
DENY = "deny"
NEEDS_AUTH = "needs-authorization"
DELEGATE = "delegate-sandboxed"
PROHIBITED = "prohibited"   # human-only, all tiers incl. owner — never automated
DECISIONS = (ALLOW, DENY, NEEDS_AUTH, DELEGATE, PROHIBITED)

# Capability x tier matrix as DATA; every cell decided (totality tested). The
# prohibited-overlay classes are checked ahead of the matrix so no grant satisfies them.
_MATRIX = {
    INFO_READ:          {OWNER: ALLOW, TEAM: ALLOW,     OTHER: DENY, AMBIENT: DELEGATE},
    CREDENTIAL_USE:     {OWNER: ALLOW, TEAM: ALLOW,     OTHER: DENY, AMBIENT: DENY},
    CREDENTIAL_READ:    {OWNER: ALLOW, TEAM: DENY,      OTHER: DENY, AMBIENT: DENY},
    WRITE_REVERSIBLE:   {OWNER: ALLOW, TEAM: DELEGATE,  OTHER: DENY, AMBIENT: DENY},
    WRITE_IRREVERSIBLE: {OWNER: NEEDS_AUTH, TEAM: NEEDS_AUTH, OTHER: DENY, AMBIENT: DENY},
    PURCHASE:           {OWNER: NEEDS_AUTH, TEAM: DENY, OTHER: DENY, AMBIENT: DENY},
    FINANCIAL_MOVE:     {OWNER: PROHIBITED, TEAM: PROHIBITED, OTHER: PROHIBITED, AMBIENT: PROHIBITED},
    CREDENTIAL_ENTRY:   {OWNER: PROHIBITED, TEAM: PROHIBITED, OTHER: PROHIBITED, AMBIENT: PROHIBITED},
}

# Prohibited-overlay classes: decide() checks the overlay FIRST, so no grant
# (standing or fresh) can ever satisfy one.
DEFAULT_PROHIBITED_OVERLAY = frozenset({FINANCIAL_MOVE, CREDENTIAL_ENTRY})

# Concrete verb -> class; the class lookup keys on the bare verb (scope rides on
# the request for grant matching).
_VERB_CLASS = {
    "info:read": INFO_READ,
    "github:read": INFO_READ,
    "credential:use": CREDENTIAL_USE,
    "credential:read": CREDENTIAL_READ,
    "secret:read": CREDENTIAL_READ,
    "credential:entry": CREDENTIAL_ENTRY,
    "fs:write": WRITE_REVERSIBLE,
    "config:write": WRITE_IRREVERSIBLE,
    "fs:delete": WRITE_IRREVERSIBLE,
    "github:comment": WRITE_IRREVERSIBLE,
    "github:merge": WRITE_IRREVERSIBLE,
    "email:send": WRITE_IRREVERSIBLE,
    "publish": WRITE_IRREVERSIBLE,
    "purchase": PURCHASE,
    "payment:charge": PURCHASE,
    "payment:transfer": FINANCIAL_MOVE,
    "financial:move": FINANCIAL_MOVE,
}


def capability_class(verb: str) -> Optional[str]:
    """Class for a capability verb (``"github:merge"`` -> WRITE_IRREVERSIBLE), or
    None for an unrecognized verb — decide() treats None as fail-closed DENY."""
    return _VERB_CLASS.get((verb or "").strip().lower())


class Principal(NamedTuple):
    """Derived by the mediator from its trusted context handle — never submitted
    by a caller. ``tier`` is already normalized."""
    tier: str
    source: str = ""
    user_id: str = ""


class CapabilityRequest(NamedTuple):
    verb: str
    scope: str = ""          # e.g. the repo, path, or vendor the verb acts on
    args_digest: str = ""    # exact digest of normalized args (grant binding)


class Decision(NamedTuple):
    decision: str            # one of DECISIONS
    capability_class: str    # the resolved class (or "unknown")
    rule: str                # human-readable citation for the audit row


def _covered_by_grant(req: CapabilityRequest, principal: Principal, grants) -> bool:
    """True iff a live grant covers the request: matches verb, tier AND user_id
    (+source if pinned), and args_digest (fresh) or scope (standing). Fail-closed
    — a grant missing tier/user_id or a principal with no user_id never covers."""
    if not grants or not principal.user_id:
        return False
    for g in grants:
        if _g(g, "verb") != req.verb:
            continue
        gt = _g(g, "tier")
        if not gt or gt != principal.tier:          # missing/mismatched tier -> no cover
            continue
        gu = _g(g, "user_id")
        if not gu or gu != principal.user_id:       # grant must name THIS principal
            continue
        gs = _g(g, "source")
        if gs and gs != principal.source:           # if pinned, source must match too
            continue
        gd = _g(g, "args_digest")
        if gd and req.args_digest and gd == req.args_digest:
            return True
        gp = _g(g, "scope_pattern")
        if gp and _scope_matches(gp, req.scope):
            return True
    return False


def _g(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _scope_matches(pattern: str, scope: str) -> bool:
    """Minimal standing-grant scope match: exact, or a single trailing '*' prefix
    (e.g. 'github:merge' scope 'owner/*'). Deliberately narrow — a permissive
    matcher would widen authority silently."""
    if pattern == scope or pattern == "*":
        return True
    if pattern.endswith("*"):
        return scope.startswith(pattern[:-1])
    return False


def decide(req: CapabilityRequest, principal: Principal, grants=None,
           prohibited_overlay=DEFAULT_PROHIBITED_OVERLAY) -> Decision:
    """Total over (class, tier): prohibited_overlay first (no grant satisfies it), then
    matrix; a NEEDS_AUTH cell allows only with a covering grant; unknown fails closed to DENY."""
    tier = principal.tier if principal.tier in TIERS else OTHER
    cls = capability_class(req.verb)
    if cls is None:
        return Decision(DENY, "unknown", f"unknown capability verb {req.verb!r} -> fail-closed deny")

    overlay = prohibited_overlay or frozenset()
    if cls in overlay:
        return Decision(PROHIBITED, cls,
                        f"{cls} is in the prohibited overlay — human-only, no grant satisfies it")

    base = _MATRIX[cls][tier]
    if base == NEEDS_AUTH:
        if _covered_by_grant(req, principal, grants):
            return Decision(ALLOW, cls, f"{cls}/{tier} needs-authorization, satisfied by a covering grant")
        return Decision(NEEDS_AUTH, cls, f"{cls}/{tier} requires owner authorization (no covering grant)")
    return Decision(base, cls, f"{cls}/{tier} -> {base} (matrix)")


# Inbound-content classification: classify() is TOTAL — every input yields a
# Classification; an unmatched input is the explicit UNCLASSIFIED terminal (fail-closed).
UNCLASSIFIED = "unclassified"


class Classification(NamedTuple):
    request: Optional[CapabilityRequest]  # None iff unclassified
    outcome: str                          # a verb string, or UNCLASSIFIED
    reason: str


# (predicate, verb) recognizers, first match wins. Predicates take lowered text.
def _has(*subs):
    return lambda t: any(s in t for s in subs)


_RECOGNIZERS = (
    (_has("review the pr", "review this pr", "review pr", "pr diff", "diff of"), "github:read"),
    (_has("merge the pr", "merge pr", "merge this pr"), "github:merge"),
    (_has("approve the pr", "comment on the pr", "post a comment", "approve pr"), "github:comment"),
    (_has("send an email", "send email", "email to"), "email:send"),
    (_has("read the secret", "read credential", "get the api key", "read .env"), "credential:read"),
    # Narrow to EXPLICIT secret/token/key usage — a bare "use the ..." is ordinary
    # prose and must fall through to UNCLASSIFIED, not the credential lane.
    (_has("use the api key", "use the token", "use the credential", "use the secret",
          "use the vault", "sign the request", "authenticate with the"), "credential:use"),
    (_has("purchase", "buy ", "check out", "place the order"), "purchase"),
    (_has("transfer funds", "send money", "sell ", "withdraw"), "financial:move"),
)


def classify(inbound_content) -> Classification:
    """Map inbound content to a CapabilityRequest, or the terminal UNCLASSIFIED.
    Total (never raises/None); UNCLASSIFIED is fail-closed and observable, not a silent no-op."""
    if inbound_content is None:
        return Classification(None, UNCLASSIFIED, "no content")
    text = str(inbound_content).strip().lower()
    if not text:
        return Classification(None, UNCLASSIFIED, "empty content")
    for pred, verb in _RECOGNIZERS:
        if pred(text):
            return Classification(CapabilityRequest(verb=verb), verb, f"matched recognizer -> {verb}")
    return Classification(None, UNCLASSIFIED, "no recognizer matched — terminal unclassified")
