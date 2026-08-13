#!/usr/bin/env python3
"""S1 acceptance (owner-specced): the same action submitted locally or
relay-carried dispatches only to a declared/granted fake capability, emits
progress and a terminal result, rejects stale preconditions
deterministically, and deduplicates redelivery without executing twice.
The fake approval Experience renders on two capability-different surfaces,
updates in place, accepts exactly one response, resolves — and a suppressed
presentation completes with NO delivery for any bridge/relay to retry."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "shared"))

spec = importlib.util.spec_from_file_location(
    "runtime_dispatch", REPO / "src" / "runtime-api" / "runtime_dispatch.py")
rd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rd)

from device_runtime_protocol import build_approval_content  # noqa: E402

GRANTED = {"fake.counter.increment"}


def _action(**over):
    base = {"action_id": "act_1", "task_id": "task_1", "device_id": "dev_1",
            "provider": "fake", "capability": "fake.counter.increment",
            "operation": "increment", "arguments": {"by": 1},
            "preconditions": {"counter": 0},
            "idempotency_key": "task_1:inc_once"}
    base.update(over)
    return base


def _relay_carry(raw: dict) -> dict:
    """The relay leg: the envelope survives a serialize→file→parse round trip
    (what the gateway task/result plane does) before hitting THE SAME
    dispatch. Transport must not change semantics."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"type": "action", "payload": raw}, f)
        p = Path(f.name)
    carried = json.loads(p.read_text())
    p.unlink()
    assert carried["type"] == "action"
    return carried["payload"]


def test_local_and_relay_transports_agree():
    for transport in ("local", "relay"):
        provider = rd.FakeCounterProvider()
        progress = []
        raw = _action()
        if transport == "relay":
            raw = _relay_carry(raw)
        res = rd.dispatch_action(raw, providers={"fake": provider},
                                 granted=GRANTED,
                                 emit_progress=progress.append)
        assert res["status"] == "completed", (transport, res)
        assert res["result"]["counter"] == 1
        assert progress and progress[0]["stage"] == "incrementing"
        assert res["started_at"] and res["completed_at"]


def test_dispatch_gates():
    provider = rd.FakeCounterProvider()
    ungranted = rd.dispatch_action(_action(), providers={"fake": provider},
                                   granted=set())
    assert ungranted["fault"]["code"] == "PERMISSION_DENIED"
    unsupported = rd.dispatch_action(
        _action(capability="fake.counter.decrement"),
        providers={"fake": provider}, granted={"fake.counter.decrement"})
    assert unsupported["fault"]["code"] == "CAPABILITY_UNSUPPORTED"
    provider.unavailable_reason = "counter_hardware_missing"
    unavailable = rd.dispatch_action(_action(), providers={"fake": provider},
                                     granted=GRANTED)
    assert unavailable["fault"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert unavailable["fault"]["reason"] == "counter_hardware_missing"
    no_provider = rd.dispatch_action(_action(provider="ghost"),
                                     providers={"fake": provider},
                                     granted=GRANTED)
    assert no_provider["fault"]["code"] == "CAPABILITY_UNSUPPORTED"
    assert provider.executions == 0, "gated dispatches must never execute"


def test_stale_precondition_rejected_deterministically():
    provider = rd.FakeCounterProvider()
    provider.counter = 5
    for _ in range(2):  # deterministic: same rejection every time
        res = rd.dispatch_action(_action(), providers={"fake": provider},
                                 granted=GRANTED)
        assert res["fault"]["code"] == "PRECONDITION_FAILED"
        assert res["fault"]["requires_new_observation"] is True
        assert res["fault"]["retryable"] is False
    assert provider.executions == 0


def test_redelivery_deduplicates_without_double_execution():
    provider = rd.FakeCounterProvider()
    first = rd.dispatch_action(_action(attempt=1),
                               providers={"fake": provider}, granted=GRANTED)
    redelivered = rd.dispatch_action(_action(attempt=2),
                                     providers={"fake": provider},
                                     granted=GRANTED)
    assert first["status"] == redelivered["status"] == "completed"
    assert redelivered["result"]["counter"] == first["result"]["counter"] == 1
    assert provider.executions == 1, "redelivery must not re-execute"


def test_deadline_expired_never_executes():
    provider = rd.FakeCounterProvider()
    res = rd.dispatch_action(_action(deadline=100.0),
                             providers={"fake": provider}, granted=GRANTED,
                             now=200.0)
    assert res["fault"]["code"] == "DEADLINE_EXCEEDED"
    assert provider.executions == 0


def _surfaces():
    wrist = rd.FakeRenderer("fake-watch", {"text", "approval"}, max_actions=2)
    chat = rd.FakeRenderer("fake-chat", {"text", "approval", "table"},
                           max_actions=6)
    return wrist, chat


def _approval_create(**over):
    base = {"presentation_id": "p1", "experience_id": "exp_1",
            "task_id": "task_1", "operation": "create", "intent": "approve",
            "audience": {"subjects": ["qingyun"], "scope": "private"},
            "content": {**build_approval_content(
                summary="push 2 commits to feat/runtime",
                effects=["update remote branch", "trigger CI"],
                preview={"commits": ["abc", "def"]},
                choices=["approve_once", "deny"]),
                "table": {"rows": 3}},
            "interaction": {"actions": ["approve_once", "deny", "explain",
                                        "defer"]}}
    base.update(over)
    return base


def test_approval_experience_full_lifecycle():
    wrist, chat = _surfaces()
    reg = rd.ExperienceRegistry()
    created = rd.dispatch_present(_approval_create(), registry=reg,
                                  renderers=[wrist, chat])
    assert created["status"] == "completed"
    assert created["disposition"] == "delivered"
    assert created["experience_version"] == 1
    assert len(created["delivery_refs"]) == 2
    # capability-different rendering: wrist dropped the table + capped actions
    assert "table" not in wrist.delivered[0]["content"]
    assert len(wrist.delivered[0]["actions"]) == 2
    assert "table" in chat.delivered[0]["content"]
    assert len(chat.delivered[0]["actions"]) == 4

    dup = rd.dispatch_present(_approval_create(presentation_id="p1b"),
                              registry=reg, renderers=[wrist, chat])
    assert dup["fault"]["code"] == "CONFLICT"

    stale = rd.dispatch_present(
        {"presentation_id": "p2", "experience_id": "exp_1",
         "operation": "update", "expected_version": 9, "content": {}},
        registry=reg, renderers=[wrist, chat])
    assert stale["fault"]["code"] == "CONFLICT"

    updated = rd.dispatch_present(
        {"presentation_id": "p3", "experience_id": "exp_1",
         "operation": "update", "expected_version": 1,
         "content": {"type": "approval", "summary": "now 3 commits"}},
        registry=reg, renderers=[wrist, chat])
    assert updated["disposition"] == "updated"
    assert updated["experience_version"] == 2

    reg.respond("exp_1", "qingyun", "approve_once")
    try:
        reg.respond("exp_1", "qingyun", "deny")
        raise AssertionError("second response accepted")
    except ValueError:
        pass
    # the recorded response is INPUT — nothing here mints authorization
    assert reg.get("exp_1")["responses"][0]["choice"] == "approve_once"

    resolved = rd.dispatch_present(
        {"presentation_id": "p4", "experience_id": "exp_1",
         "operation": "resolve"}, registry=reg, renderers=[wrist, chat])
    assert resolved["disposition"] == "resolved"
    after = rd.dispatch_present(
        {"presentation_id": "p5", "experience_id": "exp_1",
         "operation": "update", "expected_version": 2, "content": {}},
        registry=reg, renderers=[wrist, chat])
    assert after["fault"]["code"] == "CONFLICT"


def test_suppressed_presentation_is_terminal_and_undelivered():
    wrist, chat = _surfaces()
    reg = rd.ExperienceRegistry()
    res = rd.dispatch_present(
        _approval_create(
            presentation_id="p9", experience_id="exp_quiet",
            delivery_policy={"disposition": "suppress",
                             "reason": "no_material_change"}),
        registry=reg, renderers=[wrist, chat])
    assert res["status"] == "completed", "suppression is SUCCESS, not failure"
    assert res["disposition"] == "suppressed"
    assert res["reason"] == "no_material_change"
    assert res["delivery_refs"] == []
    assert wrist.delivered == [] and chat.delivered == []
    assert reg.get("exp_quiet") is None, "suppressed create stores nothing"


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
