#!/usr/bin/env python3
"""S1 protocol package contract: envelopes, digest stability, error model,
capability grammar + three-set state, present lifecycle validation."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

from device_runtime_protocol import (  # noqa: E402
    ActionEnvelope,
    ExperienceResponseEnvelope,
    PresentEnvelope,
    PresentResult,
    action_digest,
    build_approval_content,
    canonical_json,
    effects_digest,
    fault,
    resolve_capability_state,
    validate_capability_name,
)
from device_runtime_protocol.action import canonical_action  # noqa: E402


def _env(**over):
    base = {"action_id": "act_1", "task_id": "task_1", "device_id": "dev_1",
            "provider": "fake", "capability": "fake.counter.increment",
            "operation": "increment", "arguments": {"by": 2},
            "idempotency_key": "task_1:inc"}
    base.update(over)
    return ActionEnvelope.from_dict(base)


def test_capability_grammar():
    assert validate_capability_name("browser.interact")
    assert validate_capability_name("fake.counter.increment")
    assert not validate_capability_name("Browser.Interact")
    assert not validate_capability_name("noverb")
    assert not validate_capability_name("a.b.c.d")
    assert not validate_capability_name("")


def test_action_envelope_validation():
    for missing in ("action_id", "task_id", "provider", "capability"):
        try:
            _env(**{missing: ""})
            raise AssertionError(f"missing {missing} accepted")
        except ValueError:
            pass
    try:
        _env(attempt=0)
        raise AssertionError("attempt 0 accepted")
    except ValueError:
        pass


def test_digest_stable_across_retries_and_key_order():
    a = _env(attempt=1, trace={"hop": "local"})
    b = _env(attempt=3, trace={"hop": "relay"})
    assert action_digest(a) == action_digest(b), "retry must not change digest"
    c = _env(arguments={"by": 3})
    assert action_digest(a) != action_digest(c), "different args must differ"


def test_error_model_flags():
    p = fault("PRECONDITION_FAILED", "stale")
    assert not p.retryable and p.requires_new_observation
    e = fault("EXECUTION_FAILED", "boom")
    assert e.retryable and not e.requires_new_observation
    u = fault("CAPABILITY_UNAVAILABLE", "no screen recording",
              reason="screen_recording_permission_missing")
    assert u.to_dict()["reason"] == "screen_recording_permission_missing"
    try:
        fault("MADE_UP", "x")
        raise AssertionError("unknown code accepted")
    except ValueError:
        pass


def test_three_set_state():
    s = resolve_capability_state(
        "computer.observe",
        supported={"computer.observe"},
        availability={"computer.observe": "screen_recording_permission_missing"},
        granted={"computer.observe"},
    )
    assert s.supported and not s.available and s.granted
    assert s.reason == "screen_recording_permission_missing"
    assert not s.callable_now
    s2 = resolve_capability_state("computer.observe", supported=set(),
                                  availability={}, granted={"computer.observe"})
    assert not s2.supported and s2.reason is None


def test_present_envelope_lifecycle_validation():
    create = {"presentation_id": "p1", "experience_id": "e1", "task_id": "t1",
              "operation": "create", "intent": "approve"}
    PresentEnvelope.from_dict(create)
    try:
        PresentEnvelope.from_dict({**create, "intent": "nag"})
        raise AssertionError("bad intent accepted")
    except ValueError:
        pass
    try:
        PresentEnvelope.from_dict({"presentation_id": "p2",
                                   "experience_id": "e1", "operation": "update"})
        raise AssertionError("update without expected_version accepted")
    except ValueError:
        pass
    try:
        PresentEnvelope.from_dict({**create, "delivery_policy":
                                   {"disposition": "vanish"}})
        raise AssertionError("bad disposition accepted")
    except ValueError:
        pass


def test_present_result_requires_terminal_disposition():
    try:
        PresentResult(presentation_id="p", experience_id="e",
                      status="completed", disposition=None)
        raise AssertionError("completed without disposition accepted")
    except ValueError:
        pass
    r = PresentResult(presentation_id="p", experience_id="e",
                      status="completed", disposition="suppressed",
                      reason="no_material_change")
    assert r.to_dict()["reason"] == "no_material_change"


def test_approval_content_is_bound_to_the_canonical_action():
    env = _env()
    c = build_approval_content(action=env, summary="push 2 commits",
                               effects=["update remote branch"],
                               preview={"commits": ["a", "b"]},
                               choices=["approve_once", "deny"])
    assert c["type"] == "approval"
    b = c["approval_binding"]
    assert b["action_digest"] == action_digest(env)
    assert b["effects_digest"] == effects_digest(["update remote branch"])
    # a DIFFERENT action or different effects produce a different binding —
    # displayed-vs-approved divergence is detectable by construction
    assert b["action_digest"] != action_digest(_env(arguments={"by": 9}))
    assert b["effects_digest"] != effects_digest(["push to main"])
    for bad_effects in ([], [""], ["ok", 3]):
        try:
            effects_digest(bad_effects)  # type: ignore[arg-type]
            raise AssertionError(f"bad effects accepted: {bad_effects}")
        except ValueError:
            pass
    try:
        build_approval_content(action=env, summary="s", preview={},
                               effects=["e"], choices=[])
        raise AssertionError("empty choices accepted")
    except ValueError:
        pass


def test_protocol_version_enforced():
    _env(protocol_version="1")           # explicit v1 fine
    _env()                               # absent defaults to v1
    for bad in ("999", 2, "0"):
        try:
            _env(protocol_version=bad)
            raise AssertionError(f"version {bad!r} accepted")
        except ValueError:
            pass
    try:
        PresentEnvelope.from_dict({"presentation_id": "p", "experience_id": "e",
                                   "task_id": "t", "operation": "create",
                                   "intent": "inform", "protocol_version": "9"})
        raise AssertionError("present bad version accepted")
    except ValueError:
        pass


def test_canonical_profile_rejects_hostile_shapes():
    for bad_args in ({"x": float("nan")}, {"x": 1.5}, {"x": None},
                     {"x": float("inf")}, {"x": 2**53}):
        try:
            canonical_action(_env(arguments=bad_args))
            raise AssertionError(f"canonical accepted {bad_args}")
        except ValueError:
            pass
    # absent-vs-null: session_id omitted == session_id None (both OMITTED)
    a = _env()
    a.session_id = None
    assert '"session_id"' not in canonical_action(a)


def test_golden_canonicalization_vectors():
    """Fixed envelope → fixed canonical bytes → fixed digest. A second
    implementation (Swift/TS/Kotlin) must reproduce these exactly."""
    env = ActionEnvelope.from_dict({
        "action_id": "act_gold", "task_id": "task_gold",
        "device_id": "dev_gold", "provider": "fake",
        "capability": "fake.counter.increment", "operation": "increment",
        "arguments": {"by": 2, "note": "café"},
        "preconditions": {"counter": 0},
        "idempotency_key": "gold:1",
    })
    expected_canonical = (
        '{"action_id":"act_gold","arguments":{"by":2,"note":"café"},'
        '"capability":"fake.counter.increment","device_id":"dev_gold",'
        '"idempotency_key":"gold:1","operation":"increment",'
        '"preconditions":{"counter":0},"protocol_version":"1",'
        '"provider":"fake","task_id":"task_gold"}'
    )
    assert canonical_action(env) == expected_canonical
    assert action_digest(env) == (
        "sha256:" + __import__("hashlib").sha256(
            expected_canonical.encode("utf-8")).hexdigest())
    assert effects_digest(["update remote branch feat/x"]) == (
        "sha256:" + __import__("hashlib").sha256(
            canonical_json({"effects": ["update remote branch feat/x"]})
            .encode("utf-8")).hexdigest())


def test_primitive_validation_hardened():
    try:
        _env(attempt=True)
        raise AssertionError("bool attempt accepted")
    except ValueError:
        pass
    for bad in ("soon", float("inf"), True):
        try:
            _env(deadline=bad)
            raise AssertionError(f"deadline {bad!r} accepted")
        except ValueError:
            pass
    try:
        _env(arguments=["not", "a", "dict"])
        raise AssertionError("list arguments accepted")
    except ValueError:
        pass
    try:
        _env(provider="other")
        raise AssertionError("provider/capability mismatch accepted")
    except ValueError:
        pass
    try:
        PresentResult(presentation_id="p", experience_id="e", status="pending")
        raise AssertionError("unknown present status accepted")
    except ValueError:
        pass


def test_terminal_ops_require_expected_version():
    for op in ("resolve", "dismiss"):
        try:
            PresentEnvelope.from_dict({"presentation_id": "p",
                                       "experience_id": "e", "operation": op})
            raise AssertionError(f"{op} without expected_version accepted")
        except ValueError:
            pass
    PresentEnvelope.from_dict({"presentation_id": "p", "experience_id": "e",
                               "operation": "resolve", "expected_version": 1})


def test_response_envelope_validation():
    good = {"response_id": "r1", "experience_id": "e1", "expected_version": 1,
            "subject": "qingyun", "surface_id": "watch", "choice": "approve_once",
            "nonce": "n-1"}
    ExperienceResponseEnvelope.from_dict(good)
    for missing in ("response_id", "subject", "surface_id", "nonce"):
        try:
            ExperienceResponseEnvelope.from_dict({**good, missing: ""})
            raise AssertionError(f"missing {missing} accepted")
        except ValueError:
            pass
    try:
        ExperienceResponseEnvelope.from_dict({**good, "expected_version": True})
        raise AssertionError("bool expected_version accepted")
    except ValueError:
        pass


def test_outcome_unknown_posture():
    o = fault("OUTCOME_UNKNOWN", "timeout after send")
    assert not o.retryable and o.requires_new_observation


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
