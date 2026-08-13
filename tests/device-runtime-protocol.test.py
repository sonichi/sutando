#!/usr/bin/env python3
"""S1 protocol package contract: envelopes, digest stability, error model,
capability grammar + three-set state, present lifecycle validation."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

from device_runtime_protocol import (  # noqa: E402
    ActionEnvelope,
    PresentEnvelope,
    PresentResult,
    action_digest,
    build_approval_content,
    fault,
    resolve_capability_state,
    validate_capability_name,
)


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


def test_approval_content_needs_effects_and_choices():
    c = build_approval_content(summary="push 2 commits",
                               effects=["update remote branch"],
                               preview={"commits": ["a", "b"]},
                               choices=["approve_once", "deny"])
    assert c["type"] == "approval"
    for bad in ({"effects": []}, {"choices": []}):
        try:
            build_approval_content(summary="s", preview={},
                                   effects=bad.get("effects", ["e"]),
                                   choices=bad.get("choices", ["c"]))
            raise AssertionError("empty effects/choices accepted")
        except ValueError:
            pass


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
