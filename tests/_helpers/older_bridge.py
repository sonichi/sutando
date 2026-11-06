"""What a bridge from before the voice-outcome contract does with a relay ask's answer it
cannot deliver, replayed with the real outbox and quarantine code at that behaviour.

`park` is its drain (`remote_gateway_bridge.py` at f7cb1131, `_post_ready_results` and
`_deliver_result_payload`): the answer goes out with the `[dm-only]` line stripped and no
contract, through the bounded outbox (`RetrySchedule()`, 10 minutes); with the broker down
past the window the item parks, and the body is quarantined to `results/undelivered/`.
`restart` is the first drain pass of a restarted older bridge: it settles each id in flight
whose result it finds disposed of. The functions it calls (`_result_disposed`,
`_settle_task`, `_quarantine_undelivered`, `_delivery_tid`, `_broker_tid`) are unchanged
since then, so they stand for the older bridge here.

    from _helpers.older_bridge import park, restart
    parked = park(gw, answered_local_id)      # {"broker", "payload", "reason"}
    settled = restart(gw)                     # the ids it settled
"""
from __future__ import annotations

import json
import time
import urllib.error

from ag2_sparrow import outbox
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider
from ag2_sparrow.result_markers import parse_markers


def _broker_down(*_a, **_k):
    raise urllib.error.URLError("connection refused")


def park(gw, answered: str) -> dict:
    """The older bridge's drain of `results/<answered>.txt` while the broker is unavailable."""
    rfile = gw.RESULTS_DIR / f"{answered}.txt"
    raw, generation = gw._read_ready_generation(rfile)
    broker = gw._broker_tid(gw._delivery_tid(answered))
    body, _withheld = gw._guarded_result_body(answered, raw)
    parsed = parse_markers(body, skip_after_channel=gw.DELIVERY_SKIP_AFTER_CHANNEL)
    payload = json.dumps({"id": broker, "body": parsed.body}).encode("utf-8")
    now = [time.time()]
    backend = DesignAClaimBackend(gw.gateway_instance.outbox_root(gw.RESULTS_DIR, gw.GATEWAY_INSTANCE),
                                  retry_schedule=outbox.RetrySchedule(), republish_delivered=False,
                                  clock=lambda: now[0])
    core = DeliveryCore(backend, AG2SpaceResultProvider(_broker_down),
                        policy=RetryPolicy(max_attempts=5, defer_idempotent_resend=True),
                        worker="gateway-result-drain")
    backend.publish(broker, payload, source_ready_sha256=outbox.source_digest(raw))
    for _attempt in range(100):
        core.deliver_one(broker, payload)
        if outbox.read_item(backend.root, broker)["status"] == "PARKED":
            break
        now[0] += 31.0                     # the broker stays unavailable past the window
    record = outbox.read_item(backend.root, broker)
    gw._quarantine_undelivered(rfile, answered, f"outbox item is terminal: {record['reason']}",
                               outbox_item_id=broker, generation=generation)
    return {"broker": broker, "payload": payload.decode("utf-8"), "reason": record["reason"]}


def restart(gw) -> "list[str]":
    """A restarted older bridge's first drain pass over the ids it restores in flight."""
    inflight = gw._load_inflight()
    gw._load_settle_pending()
    settled = []
    for tid in sorted(inflight):
        if (gw.RESULTS_DIR / f"{tid}.txt").exists():
            continue
        if (tid in gw._SETTLE_PENDING or gw._result_disposed(tid)) and gw._settle_task(tid, inflight):
            settled.append(tid)
    gw._save_inflight(inflight)
    return settled
