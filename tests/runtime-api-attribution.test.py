#!/usr/bin/env python3
import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from attribution_claims import AttributionClaimWriter, validate_claim  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402
from ha_adapter import HumanActionAdapter  # noqa: E402
from protocol import ProtocolError  # noqa: E402
from request_store import RequestStore  # noqa: E402

AGENT = "agent:018f0f65-7b4a-7cc1-8f52-8c6ad9a60d7d"
RECEIPT = {
    "provider": "github", "account_id": "account:github:7",
    "resource_id": "owner/example", "object_type": "issue_comment", "object_id": "123",
}
FAILS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}" + ("" if condition else f" — {detail}"))
    if not condition:
        FAILS.append(label)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def fresh(actor=AGENT, writer=True, executor=None):
    root = Path(tempfile.mkdtemp(prefix="runtime-attribution-"))
    store = RequestStore(str(root / "runtime.sqlite"))
    claim_writer = AttributionClaimWriter(root / "claims.jsonl") if writer is True else writer
    disp = RuntimeDispatcher(
        store, HumanActionAdapter(str(root / "ha")), actor,
        executors={"provider.write": executor or (lambda _p: {
            "executed": True, "attributionReceipts": [RECEIPT],
        })}, attribution_writer=claim_writer,
    )
    return disp, store, root


print("── exact receipt publication ──")
disp, store, root = fresh()
params = {"action": "provider.write", "idempotencyKey": "one"}
result = run(disp.handle("capability.execute", params))
check("successful exact receipt is recorded", result["result"]["attribution"]["status"] == "recorded")
lines = (root / "claims.jsonl").read_text().splitlines()
claim = validate_claim(json.loads(lines[0]))
check("daemon actor becomes exact performer", claim["object"] == AGENT)
check("runtime request is receipt evidence", claim["evidence"]["runtime_request_id"] == result["requestId"])
replay = run(disp.handle("capability.execute", params))
check("idempotent replay returns recorded attribution", replay["result"]["attribution"]["status"] == "recorded")
check("idempotent replay does not duplicate claim", len((root / "claims.jsonl").read_text().splitlines()) == 1)

print("── identity and receipt fail closed ──")
legacy, legacy_store, legacy_root = fresh(actor="local-agent")
legacy_result = run(legacy.handle("capability.execute", {"action": "provider.write"}))
check("legacy daemon actor leaves exact attribution unavailable",
      legacy_result["result"]["attribution"]["status"] == "unavailable")
check("legacy daemon actor emits no claim", not (legacy_root / "claims.jsonl").exists())
malformed, malformed_store, malformed_root = fresh(executor=lambda _p: {
    "executed": True, "attributionReceipts": [{**RECEIPT, "account_id": "account:github:8", "extra": "x"}],
})
malformed_result = run(malformed.handle("capability.execute", {"action": "provider.write"}))
check("malformed receipt preserves provider success",
      malformed_result["status"] == "completed" and malformed_store.get(malformed_result["requestId"])["status"] == "completed")
check("malformed receipt emits no claim", not (malformed_root / "claims.jsonl").exists())
failed, failed_store, failed_root = fresh(executor=lambda _p: (_ for _ in ()).throw(RuntimeError("down")))
failed_result = run(failed.handle("capability.execute", {"action": "provider.write"}))
check("failed executor emits no attribution", failed_result["status"] == "failed" and not (failed_root / "claims.jsonl").exists())

print("── append failure reconciliation ──")
class BrokenWriter:
    def append(self, _claim):
        raise OSError("disk unavailable")


pending, pending_store, pending_root = fresh(writer=BrokenWriter())
pending_result = run(pending.handle("capability.execute", {"action": "provider.write"}))
check("claim failure preserves provider completion",
      pending_result["status"] == "completed" and pending_result["result"]["attribution"]["status"] == "pending")
retry = RuntimeDispatcher(
    pending_store, HumanActionAdapter(str(pending_root / "ha2")), AGENT,
    executors={}, attribution_writer=AttributionClaimWriter(pending_root / "claims.jsonl"),
)
retry.recover()
check("recovery publishes only the pending claim append",
      pending_store.attribution_status(pending_result["requestId"])["status"] == "recorded")

print("── execution cancellation race ──")
started = threading.Event()
release = threading.Event()


def slow(_params):
    started.set()
    release.wait(5)
    return {"executed": True}


race, race_store, _ = fresh(executor=slow)


async def exercise_race():
    task = asyncio.create_task(race.handle("capability.execute", {"action": "provider.write"}))
    await asyncio.to_thread(started.wait, 2)
    rid = race_store.pending()[0]["requestId"]
    blocked = False
    try:
        race._cancel({"requestId": rid})
    except ProtocolError:
        blocked = True
    release.set()
    response = await task
    return blocked, response, race_store.get(rid)


blocked, response, durable = run(exercise_race())
check("executing capability cannot be cancelled", blocked)
check("returned and durable terminal states agree",
      response["status"] == "completed" and durable["status"] == "completed")

if FAILS:
    print(f"\nFAIL — {len(FAILS)} runtime attribution check(s): {', '.join(FAILS)}")
    raise SystemExit(1)
print("\nPASS — runtime attribution lifecycle")
