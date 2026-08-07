#!/usr/bin/env python3
"""Mediated capability layer — policy-as-data + the decision function.

Authorization core of docs/design-mediated-capability-layer.md (RFC #2632):
the capability x tier matrix, the inbound-content classifier, and
``decide(capability, principal, grants, prohibited_overlay) -> Decision``.

This module holds NO transport and executes NOTHING — dispatcher.py and the
PreToolUse hook consume it so a capability decision is made in exactly one place
(RFC "Relationship to the runtime-API dispatcher"). First slice per resolved
open-question 1: credential:* + github:* are wired; the rest are matrix entries
awaiting their consumers.

Two functions are TOTAL by contract (RFC "Totality is required at two levels"):
- ``decide()`` returns a decision for every (capability-class, tier) cell.
- ``classify()`` maps ANY inbound content to a CapabilityRequest or the explicit
  terminal ``UNCLASSIFIED`` (fail-closed + observable) — never raises, never None.

Never raises: an unknown capability, unknown tier, or malformed input resolves
to the most restrictive defined outcome, not an exception.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


# ── Tiers (the access-tier taxonomy is the INPUT; RFC non-goal: don't replace it)
OWNER, TEAM, OTHER, AMBIENT = "owner", "team", "other", "ambient"
TIERS = (OWNER, TEAM, OTHER, AMBIENT)


def normalize_tier(access_tier: Optional[str]) -> str:
    """Map a task's ``access_tier`` to a matrix column.

    A MISSING/empty tier is ``owner`` — the existing contract (CLAUDE.md: "only
    access_tier: owner, or tasks without an access_tier field, get full
    processing"). A present-but-unrecognized value fails CLOSED to ``other``
    (the most restrictive real tier), never silently to owner — strict parsing,
    no auth bypass via a junk tier string.
    """
    if access_tier is None:
        return OWNER
    t = str(access_tier).strip().lower()
    if not t:
        return OWNER
    return t if t in TIERS else OTHER


# ── Capability classes (matrix rows, RFC "Model")
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

# ── Decisions (RFC decision set + PROHIBITED for the human-only overlay rows)
ALLOW = "allow"
DENY = "deny"
NEEDS_AUTH = "needs-authorization"
DELEGATE = "delegate-sandboxed"
PROHIBITED = "prohibited"   # human-only, all tiers incl. owner — never automated
DECISIONS = (ALLOW, DENY, NEEDS_AUTH, DELEGATE, PROHIBITED)

# ── The capability x tier matrix as DATA (RFC "Model" table). Every cell decided;
#    tested total by tests/capability-policy.test.py. The two prohibited-overlay
#    classes are ``PROHIBITED`` for ALL tiers incl. owner (the layer directs the
#    human to do it); they are ALSO checked ahead of the matrix in decide() so an
#    overlay membership can never be satisfied by a grant.
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

# Classes that are prohibited-overlay members by default in the reference
# deployment (RFC paragraph-symbol). decide() checks the overlay FIRST; no grant
# (standing or fresh) can satisfy an overlay capability.
DEFAULT_PROHIBITED_OVERLAY = frozenset({FINANCIAL_MOVE, CREDENTIAL_ENTRY})

# ── Concrete capability verb -> class. Verbs are "verb:scope"; the class lookup
#    keys on the bare verb (scope is carried on the request for grant matching).
#    First slice: credential:* + github:* are real; others map for the matrix.
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
    """Derived by the mediator from its trusted context handle — NEVER submitted
    by a caller (RFC "Trust root"). ``tier`` is already normalized."""
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
    """True iff a covering grant exists, BOUND to the authenticated principal
    (RFC "authorization grants": a grant binds the owner identity and source on
    which approval arrived). A grant covers only when it matches the verb, the
    principal's **tier AND user_id** (and source, if the grant pins one), and
    either the exact args_digest (fresh single-use grant) or a scope pattern
    (standing grant).

    Fail-closed identity binding — an approval record is NOT a bearer token: a
    grant that omits ``tier`` or ``user_id``, or names a different principal,
    never covers, and a principal with no ``user_id`` can never be covered. This
    is what stops one principal replaying another same-tier principal's grant.

    This module only CONSULTS grants; minting, nonce consumption, and expiry live
    in the mediator. Grant objects are mappings/attr-bags; unknown shapes never
    match. Observed-content 'authorization' is not a grant and can never appear.
    """
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
    """The decision function. Total over (class, tier).

    Order (RFC "Model"):
      1. prohibited_overlay is checked FIRST — an overlay class is PROHIBITED for
         every tier incl. owner and NO grant can satisfy it.
      2. matrix[class][tier] gives the base decision.
      3. a NEEDS_AUTH cell resolves to ALLOW iff a covering grant exists, else
         stays NEEDS_AUTH (escalate). This is CLAUDE.md's "confirm unless standing
         approval" made enforceable.
    Unknown verb or unknown tier fails CLOSED to DENY.
    """
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


# ── Inbound-content classification (RFC "Totality is required at two levels").
#    classify() is TOTAL: every input yields a Classification, and anything not
#    matched is the explicit terminal UNCLASSIFIED (fail-closed + observable), not
#    a silent no-op. Recognizers are intentionally conservative; a miss is
#    UNCLASSIFIED, never a guessed privileged action.
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
    (_has("use the", "sign the request", "call the api"), "credential:use"),
    (_has("purchase", "buy ", "check out", "place the order"), "purchase"),
    (_has("transfer funds", "send money", "sell ", "withdraw"), "financial:move"),
)


def classify(inbound_content) -> Classification:
    """Map inbound content to a CapabilityRequest, or the terminal UNCLASSIFIED.

    Total: returns a Classification for ANY input (None, non-str, empty, prose)
    — never raises, never None. UNCLASSIFIED is fail-closed (callers must not
    resolve it to a privileged action) and observable (callers emit an audit
    record + escalate on it), so the class is countable instead of silent (RFC
    motivating case: a peer bot's ``done:`` status matches no action and must be
    a defined outcome, not an invisible no-op).
    """
    if inbound_content is None:
        return Classification(None, UNCLASSIFIED, "no content")
    text = str(inbound_content).strip().lower()
    if not text:
        return Classification(None, UNCLASSIFIED, "empty content")
    for pred, verb in _RECOGNIZERS:
        if pred(text):
            return Classification(CapabilityRequest(verb=verb), verb, f"matched recognizer -> {verb}")
    return Classification(None, UNCLASSIFIED, "no recognizer matched — terminal unclassified")
