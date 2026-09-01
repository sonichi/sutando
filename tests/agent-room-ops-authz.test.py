#!/usr/bin/env python3
"""Tests for skills/agent-room-ops/authz.py — client-side authz-envelope handling.
Pure, no I/O. Lives under tests/ so CI auto-discovers it (find tests -name '*.test.py')
and the coverage gate exercises authz.py. Run: python3 tests/agent-room-ops-authz.test.py"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "skills", "agent-room-ops"))
import authz as A

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _envelope(decision, *, reason_code=None, grant="message.send", policy=None,
              approval_id=None, extra=None):
    authz = {"decision": decision, "reason_code": reason_code, "grant_id": grant,
             "policy_version": "0.1.0"}
    if policy is not None:
        authz["details"] = {"policy": policy}
    body = {"ok": decision == A.AUTO_ALLOW, "authz": authz}
    if approval_id is not None:
        body["approval"] = {"approval_id": approval_id}
    if extra:
        body.update(extra)
    return body


def test_auto_allow():
    o = A.classify(200, _envelope(A.AUTO_ALLOW, extra={"event_id": "$e"}))
    check(o.allowed and not o.forbidden and not o.needs_approval, "auto-allow -> allowed only")
    check(o.ok is True and o.decision == A.AUTO_ALLOW, "auto-allow -> ok, decision preserved")
    check(o.result.get("event_id") == "$e", "auto-allow -> op result body carried through")
    check(A.result_or_raise(o).get("event_id") == "$e", "result_or_raise(auto-allow) -> body")
    check("auto-allow" in repr(o), "outcome repr renders the decision")


def test_forbidden():
    o = A.classify(403, _envelope(A.FORBIDDEN, reason_code=A.R_FORBIDDEN, grant="member.invite",
                                  policy="platform_grant"))
    check(o.forbidden and not o.allowed and not o.needs_approval, "forbidden -> forbidden only")
    check(o.reason_code == A.R_FORBIDDEN and o.policy == "platform_grant",
          "forbidden -> reason_code + policy exposed")
    check(o.grant_id == "member.invite", "forbidden -> names the required grant")
    try:
        A.result_or_raise(o); check(False, "result_or_raise(forbidden) should raise")
    except A.Forbidden as e:
        check(e.reason_code == A.R_FORBIDDEN and e.policy == "platform_grant",
              "result_or_raise(forbidden) -> Forbidden(reason_code, policy)")


def test_approval_required():
    o = A.classify(202, _envelope(A.APPROVAL_REQUIRED, grant="room.create", approval_id="apr-1"))
    check(o.needs_approval and not o.allowed and not o.forbidden,
          "approval_required -> needs_approval only (distinct third branch)")
    check(o.approval_id == "apr-1", "approval_required -> approval_id captured")
    check(o.ok is False, "approval_required -> ok:false (did NOT execute)")
    try:
        A.result_or_raise(o); check(False, "result_or_raise(approval) should raise")
    except A.ApprovalRequired as e:
        check(e.approval_id == "apr-1", "result_or_raise(approval) -> ApprovalRequired(id)")


def test_legacy_no_envelope_2xx_is_allow():
    # Pre-2.5 server: no authz block, op executed the old way -> LEGACY_ALLOW (not an error).
    o = A.classify(200, {"event_id": "$legacy", "ok": True})
    check(o.allowed and o.decision == A.LEGACY_ALLOW, "no-envelope 2xx -> legacy-allow (compat)")
    check(A.result_or_raise(o).get("event_id") == "$legacy", "legacy-allow -> body passthrough")


def test_no_envelope_non2xx_fails_closed():
    o = A.classify(500, {"error": "boom"})
    check(o.forbidden and o.policy == "http_error",
          "no-envelope non-2xx -> forbidden/http_error (fail closed, not a false allow)")


def test_unknown_decision_fails_closed():
    o = A.classify(200, _envelope("teleport"))  # not a real decision token
    check(o.forbidden and o.policy == "unknown_decision",
          "unknown decision token -> forbidden/unknown_decision (fail closed)")


def test_missing_decision_fails_closed():
    o = A.classify(200, {"authz": {"grant_id": "message.send"}})  # authz present, no decision
    check(o.forbidden, "authz block without a decision -> forbidden (fail closed)")


def test_approval_without_id_is_still_approval():
    # Malformed server: approval_required but no approval handle. Still the approval branch,
    # approval_id just None — caller can detect the missing handle without misrouting to allow.
    o = A.classify(202, {"authz": {"decision": A.APPROVAL_REQUIRED, "grant_id": "room.create"}})
    check(o.needs_approval and o.approval_id is None,
          "approval_required w/o approval_id -> still needs_approval, id None")


def test_auto_allow_on_non_2xx_fails_closed():
    # THE reported fail-open: an auto-allow envelope on a status that says the op did NOT
    # succeed must never yield an allowed outcome. Both 403 and 500 fail closed.
    for status in (403, 500, 401, 0):
        o = A.classify(status, _envelope(A.AUTO_ALLOW))
        check(o.forbidden and not o.allowed,
              f"auto-allow + {status} -> forbidden (fail closed, not a false allow)")
        check(o.policy == "status_mismatch", f"auto-allow + {status} -> policy status_mismatch")


def test_auto_allow_requires_exactly_200():
    # 1:1 contract, NOT "any 2xx": auto-allow must arrive with 200. A 202 (approval's code) or
    # any other 2xx paired with auto-allow is a self-contradicting envelope -> fail closed.
    for status in (202, 201, 204, 299):
        o = A.classify(status, _envelope(A.AUTO_ALLOW))
        check(o.forbidden and not o.allowed,
              f"auto-allow + {status} (2xx but not 200) -> forbidden (fail closed)")
        check(o.policy == "status_mismatch", f"auto-allow + {status} -> policy status_mismatch")


def test_decision_status_compatible_pairs_pass():
    # Contract-matching pairs are honored unchanged (regression guard against over-failing).
    check(A.classify(200, _envelope(A.AUTO_ALLOW)).allowed, "auto-allow + 200 -> allowed")
    check(A.classify(202, _envelope(A.APPROVAL_REQUIRED, approval_id="a")).needs_approval,
          "approval_required + 202 -> needs_approval")
    check(A.classify(403, _envelope(A.FORBIDDEN, reason_code=A.R_FORBIDDEN)).forbidden,
          "forbidden + 403 -> forbidden")


def test_other_mismatched_pairs_fail_closed():
    # approval_required and forbidden on the wrong status also fail closed. Neither can produce
    # an allowed outcome, but a status-inconsistent envelope is malformed -> forbidden.
    o1 = A.classify(500, _envelope(A.APPROVAL_REQUIRED, approval_id="a"))
    check(o1.forbidden and o1.policy == "status_mismatch",
          "approval_required + 500 -> forbidden/status_mismatch (fail closed)")
    o2 = A.classify(200, _envelope(A.APPROVAL_REQUIRED, approval_id="a"))
    check(o2.forbidden and not o2.needs_approval,
          "approval_required + 200 -> forbidden (not offered on inconsistent status)")
    o3 = A.classify(200, _envelope(A.FORBIDDEN, reason_code=A.R_FORBIDDEN))
    check(o3.forbidden and o3.policy == "status_mismatch",
          "forbidden + 200 -> forbidden/status_mismatch (still a deny)")


def test_status_ok_unknown_decision_defensive_false():
    # _status_ok's defensive default: an unknown decision has no compatible status.
    check(A._status_ok("teleport", 200) is False, "_status_ok(unknown) -> False (defensive)")


def test_non_dict_body_fails_closed():
    o = A.classify(200, None)
    check(o.decision == A.LEGACY_ALLOW, "None body + 200 -> legacy-allow (empty result)")
    o2 = A.classify(403, "not json")
    check(o2.forbidden, "non-dict body + 403 -> forbidden (fail closed)")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            fn()
        except Exception:
            print(f"FAIL  {fn.__name__} (exception)")
            traceback.print_exc()
            FAILS.append(fn.__name__)
    print(f"\n{'FAILED ('+str(len(FAILS))+')' if FAILS else 'PASS — all authz checks green'}")
    raise SystemExit(1 if FAILS else 0)
