"""Client-side handling for an authorization-decision envelope on room-op responses.

A room-op endpoint may return an authorization decision inline on the op response, so a
single round-trip carries both the decision and (when allowed) the op result — no separate
pre-flight call, and no gap between a check and the op. When present, the decision rides in
an `authz` block:

    {
      "ok": <bool>,
      "authz": {
        "decision":       "auto-allow" | "approval_required" | "forbidden",
        "reason_code":    "UNAUTHORIZED" | "FORBIDDEN" | null,
        "grant_id":       "<required capability>",
        "policy_version": "...",
        "details":        { "policy": "<short machine reason>" }   # present on forbidden
      },
      "approval": { "approval_id": "..." },   # present ONLY on approval_required
      ...                                     # normal op-result fields on auto-allow
    }

HTTP status mirrors the decision: auto-allow -> 200, forbidden -> 403, approval_required -> 202.

This module is the CLIENT half: turn `(http_status, parsed_body)` into a typed `AuthzOutcome`
and give callers the three branches they must implement — auto-allow -> use the result,
forbidden -> surface the denial (never retry), approval_required -> surface "needs approval"
(a distinct third outcome) and keep the `approval_id`. Pure: no HTTP here; `_gateway.http_json`
feeds it. The per-op wiring (routing every room op through this) is a later increment.

Backward compatibility is deliberate: a response with **no** `authz` block is served by an
endpoint that does not (yet) emit the envelope. A clean 2xx there means the op already
executed, so it is reported as `LEGACY_ALLOW` rather than an error — the client works against
an endpoint whether or not it emits the envelope. Fails closed on an unknown/missing decision
and on a non-2xx response that carries no envelope.
"""
from __future__ import annotations

# Decision tokens — must match what the endpoint emits in `authz.decision`.
AUTO_ALLOW = "auto-allow"
APPROVAL_REQUIRED = "approval_required"
FORBIDDEN = "forbidden"

# Client-only synthetic decision for a response that carries no `authz` block.
#
# LOAD-BEARING INVARIANT (the endpoint's side of the contract): a denial ALWAYS carries an
# `authz` block, so an *absent* envelope on a 2xx unambiguously means the op executed. The
# endpoint must never signal a denial as a bare 2xx with no envelope — if it did, LEGACY_ALLOW
# would mask a real deny. This is why `classify` treats no-envelope-on-2xx as "allowed": it is
# only ever a not-yet-emitting endpoint that ran the op, never a hidden denial.
LEGACY_ALLOW = "legacy-allow"

# reason_code / policy strings the client may branch on (never the human message).
R_UNAUTHORIZED = "UNAUTHORIZED"
R_FORBIDDEN = "FORBIDDEN"

_ALLOW_DECISIONS = frozenset({AUTO_ALLOW, LEGACY_ALLOW})

# The HTTP status a given decision MUST arrive with is a 1:1 contract, NOT "any 2xx":
# auto-allow -> 200, approval_required -> 202, forbidden -> 403. A decision paired with any
# other status is a self-contradicting / malformed response (a buggy endpoint, a proxy error
# page, or tampering) and the client fails closed. The exact-code match matters: a
# `202 + auto-allow`, for instance, is a server claiming "the op ran" on the status code that
# means "accepted, awaiting approval" — trusting it would let an approval-gated op through.
def _status_ok(decision, http_status) -> bool:
    if decision == AUTO_ALLOW:
        return http_status == 200
    if decision == APPROVAL_REQUIRED:
        return http_status == 202
    if decision == FORBIDDEN:
        return http_status == 403
    return False  # unknown decision — caller fails closed regardless


# The recognized decisions that carry a status contract (unknown decisions are handled by the
# fail-closed default at the end of `classify`, so they are deliberately excluded here).
_EXPECTED_STATUS_DECISIONS = frozenset({AUTO_ALLOW, APPROVAL_REQUIRED, FORBIDDEN})


class AuthzOutcome:
    """Normalized result of one `/v1/room` op response. `decision` is the source of truth;
    the `allowed`/`forbidden`/`needs_approval` properties are the three client branches."""

    __slots__ = (
        "decision", "ok", "http_status", "reason_code", "grant_id",
        "policy_version", "policy", "approval_id", "result", "raw",
    )

    def __init__(self, decision, *, ok, http_status, reason_code=None, grant_id=None,
                 policy_version=None, policy=None, approval_id=None, result=None, raw=None):
        self.decision = decision
        self.ok = ok
        self.http_status = http_status
        self.reason_code = reason_code
        self.grant_id = grant_id
        self.policy_version = policy_version
        self.policy = policy
        self.approval_id = approval_id
        self.result = result
        self.raw = raw

    @property
    def allowed(self) -> bool:
        return self.decision in _ALLOW_DECISIONS

    @property
    def forbidden(self) -> bool:
        return self.decision == FORBIDDEN

    @property
    def needs_approval(self) -> bool:
        return self.decision == APPROVAL_REQUIRED

    def __repr__(self) -> str:
        return (f"AuthzOutcome(decision={self.decision!r}, ok={self.ok}, "
                f"http={self.http_status}, reason={self.reason_code!r}, "
                f"policy={self.policy!r}, approval_id={self.approval_id!r})")


class Forbidden(Exception):
    """Raised by `result_or_raise` when the op was denied. Carries the stable reason_code +
    policy so callers branch on codes, not prose."""

    def __init__(self, outcome: "AuthzOutcome"):
        self.outcome = outcome
        self.reason_code = outcome.reason_code
        self.policy = outcome.policy
        super().__init__(f"forbidden: reason={outcome.reason_code} policy={outcome.policy} "
                         f"grant={outcome.grant_id}")


class ApprovalRequired(Exception):
    """Raised by `result_or_raise` when the op needs approval (did NOT execute). Carries the
    `approval_id` handle to poll/await. Distinct from Forbidden — a retry is meaningless, but
    the action is not denied; it is pending a human/approval decision."""

    def __init__(self, outcome: "AuthzOutcome"):
        self.outcome = outcome
        self.approval_id = outcome.approval_id
        super().__init__(f"approval_required: approval_id={outcome.approval_id} "
                         f"grant={outcome.grant_id}")


def classify(http_status, body) -> AuthzOutcome:
    """Turn a `/v1/room` op response `(http_status, parsed_json_body)` into an `AuthzOutcome`.

    Fails closed: an unrecognized `decision` string, a non-2xx response with no envelope, or an
    envelope whose `decision` disagrees with the HTTP status (e.g. `auto-allow` on a 403/500 —
    see `_status_ok`) is reported as `forbidden` rather than allowed. A 2xx response with no
    envelope is `LEGACY_ALLOW` (pre-wiring server; the op already ran)."""
    body = body if isinstance(body, dict) else {}
    authz = body.get("authz")

    if not isinstance(authz, dict):
        # No envelope: pre-2.5 / legacy server.
        if isinstance(http_status, int) and 200 <= http_status < 300:
            return AuthzOutcome(LEGACY_ALLOW, ok=True, http_status=http_status,
                                result=body, raw=body)
        # A non-2xx without an envelope is a transport/error response — fail closed.
        return AuthzOutcome(FORBIDDEN, ok=False, http_status=http_status,
                            policy="http_error", raw=body)

    decision = authz.get("decision")
    details = authz.get("details") if isinstance(authz.get("details"), dict) else {}
    common = dict(
        http_status=http_status,
        reason_code=authz.get("reason_code"),
        grant_id=authz.get("grant_id"),
        policy_version=authz.get("policy_version"),
        policy=details.get("policy"),
        raw=body,
    )

    # Envelope decision and HTTP status must agree (see `_status_ok`). A recognized decision
    # arriving with an incompatible status is fail-closed to FORBIDDEN — otherwise an
    # `auto-allow` body on a 403/500 would be handed back as a successful op.
    if decision in _EXPECTED_STATUS_DECISIONS and not _status_ok(decision, http_status):
        return AuthzOutcome(FORBIDDEN, ok=False,
                            **{**common, "policy": "status_mismatch"})

    if decision == AUTO_ALLOW:
        return AuthzOutcome(AUTO_ALLOW, ok=True, result=body, **common)
    if decision == APPROVAL_REQUIRED:
        approval = body.get("approval") if isinstance(body.get("approval"), dict) else {}
        return AuthzOutcome(APPROVAL_REQUIRED, ok=False,
                            approval_id=approval.get("approval_id"), **common)
    if decision == FORBIDDEN:
        return AuthzOutcome(FORBIDDEN, ok=False, **common)

    # Unknown / missing decision token -> fail closed. Keep the server's policy detail if any,
    # but mark the client-side reason so a mismatch is debuggable rather than silently allowed.
    common["policy"] = common["policy"] or "unknown_decision"
    return AuthzOutcome(FORBIDDEN, ok=False, **common)


def result_or_raise(outcome: "AuthzOutcome"):
    """Three-branch convenience for imperative callers:
      auto-allow / legacy-allow -> return the op result body,
      approval_required        -> raise ApprovalRequired(approval_id),
      forbidden / unknown       -> raise Forbidden(reason_code, policy).
    Callers that prefer to branch explicitly can read the outcome properties instead."""
    if outcome.allowed:
        return outcome.result
    if outcome.needs_approval:
        raise ApprovalRequired(outcome)
    raise Forbidden(outcome)
