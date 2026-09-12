#!/usr/bin/env python3
"""Focused gateway coverage for backend-owned room-question delivery."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "remote_gateway_bridge_question_delivery",
    REPO / "src" / "remote-gateway-bridge.py",
)
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)

root = Path(tempfile.mkdtemp(prefix="question-delivery-test-"))
gateway.TASKS_DIR = root / "tasks"
gateway.RESULTS_DIR = root / "results"
gateway.ARCHIVE_RESULTS_DIR = root / "results" / "archive"
gateway.LOCAL_TIER = "owner"

assert "question_delivery" in gateway.local_task_protocol.KNOWN_HEADER_KEYS

delivery = {
    "version": 1,
    "delivery_id": "question-delivery-4bd08f",
    "question_id": "q_01H",
    "question_event_id": "$question",
    "revision": 2,
    "status": "resolved",
    "response": {"kind": "choice", "option_id": "minimal", "note": "Ship it."},
    "respondent": "@mark:ag2.space",
    "accepted_at": "2026-09-11T01:05:00Z",
}

written = gateway._write_task({
    "id": "task-question-delivery-4bd08f",
    "source": "ag2space-question-delivery",
    "task": "Continue after the accepted room answer.",
    "access_tier": "guest",
    "interaction_type": "system_event",
    "session_scope": "room",
    "channel_id": "!room:ag2.space",
    "question_delivery": delivery,
})
assert written == ("task-question-delivery-4bd08f", True), written
task_file = gateway.TASKS_DIR / "task-question-delivery-4bd08f.txt"
body = task_file.read_text()
header = next(line for line in body.splitlines() if line.startswith("question_delivery: "))
assert json.loads(header.split(": ", 1)[1]) == delivery
parsed = gateway.local_task_protocol.parse_task_headers_trusted(body)
assert json.loads(parsed.headers["question_delivery"]) == delivery
assert body.count("access_tier: guest") == 1
assert "interaction_type: system_event" in body

# Structured answer metadata never supplies access policy. Missing broker
# attestation remains guest even when the local cap is owner.
second = dict(delivery, delivery_id="question-delivery-no-tier")
gateway._write_task({
    "id": "task-question-delivery-no-tier",
    "source": "ag2space-question-delivery",
    "task": "Continue safely.",
    "question_delivery": second,
})
assert "access_tier: guest" in (
    gateway.TASKS_DIR / "task-question-delivery-no-tier.txt"
).read_text()

# Malformed metadata is not promoted to a trusted header. The ordinary task
# remains guest and executable, allowing broker retry/inspection to recover.
bad = dict(delivery, response={"kind": "runtime_action", "action_id": "allow"})
gateway._write_task({
    "id": "task-question-delivery-invalid",
    "source": "ag2space-question-delivery",
    "task": "Inspect the canonical question before continuing.",
    "question_delivery": bad,
})
invalid = (gateway.TASKS_DIR / "task-question-delivery-invalid.txt").read_text()
assert "question_delivery:" not in invalid
assert "access_tier: guest" in invalid

# The stable broker task identity and the canonical delivery identity must
# agree, otherwise a replay could bypass the gateway's existing task dedup.
gateway._write_task({
    "id": "task-question-delivery-different",
    "source": "ag2space-question-delivery",
    "task": "Inspect the canonical question before continuing.",
    "question_delivery": delivery,
})
mismatch = (gateway.TASKS_DIR / "task-question-delivery-different.txt").read_text()
assert "question_delivery:" not in mismatch

print("PASS — room-question delivery is correlated without local HITL authority")
