#!/usr/bin/env python3
"""Slack reply leg binds the shared outbox (Slack strangler; mirrors #3290).

Direct contract tests on src/slack_result_delivery.py — claim lifecycle plus
the three-state mapping for Slack send outcomes — and wiring assertions that
the bridge delegates at its choke points instead of improvising delivery state.
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import outbox  # noqa: E402
from outbox import DeliveryOutcome as RO  # noqa: E402

import slack_result_delivery as srd  # noqa: E402

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


class _Exc(Exception):
    def __init__(self, msg, response=None):
        super().__init__(msg)
        if response is not None:
            self.response = response


class _Resp:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self.data = data


with tempfile.TemporaryDirectory() as td:
    rd = Path(td) / "results"
    rd.mkdir()

    # happy path: claim -> confirm -> delivered; second cycle refuses
    tok = srd.claim_for_send(rd, "task-A")
    check(tok is not None, "fresh item: claim_for_send returns a token")
    check(srd.confirm(rd, tok, "C0AAAAAAA") is True, "confirm records CONFIRMED")
    check(srd.is_delivered(rd, "task-A"), "delivered: is_delivered True")
    check(srd.claim_for_send(rd, "task-A") is None,
          "delivered item is never re-claimed (crash-window idempotency)")
    check(srd.is_delivered(rd, "task-Z") is False, "unknown item: not delivered")

    # double-claim race: a held claim yields None for the second caller
    t1 = srd.claim_for_send(rd, "task-B")
    check(t1 is not None and srd.claim_for_send(rd, "task-B") is None,
          "held claim: second claim_for_send is refused")

    # ambiguous outcome: parked as outcome-unknown, never auto-retried
    check(srd.unknown(rd, t1) is True and
          outbox.item_status(srd.result_backend(rd).root, "task-B") == "PARKED",
          "OUTCOME_UNKNOWN parks — a maybe-received reply is never re-sent")
    check(srd.is_parked(rd, "task-B"), "parked item: is_parked True")
    check(srd.claim_for_send(rd, "task-B") is None,
          "parked item refuses a fresh cycle")
    check(not srd.is_delivered(rd, "task-B"),
          "parked is not conflated with delivered")

# ── refusal: retried on the outbox's wall-clock window, not 5 bare attempts ─
with tempfile.TemporaryDirectory() as td:
    rd = Path(td) / "results"
    rd.mkdir()
    b = srd.result_backend(rd)
    check(b.retry_schedule is srd.RETRY_SCHEDULE and srd.RETRY_SCHEDULE.window_s == 600.0,
          "the Slack backend carries the outbox RetrySchedule (600 s window)")
    clock = [1000.0]
    b.clock = lambda: clock[0]
    t0 = clock[0]
    # The watcher's cadence: a claim every second, a refusal on every send.
    sends, parked_at = 0, None
    for sec in range(0, 700):
        clock[0] = t0 + sec
        tok = srd.claim_for_send(rd, "task-C")
        if tok is not None:
            sends += 1
            completed = srd.failed(rd, tok)
            if sends == 1:
                check(completed is True, "failed() completes (re-ready)")
        if sec == 1:
            check(tok is None and not srd.is_parked(rd, "task-C"),
                  "1 s after a refusal: claim waits out the backoff, item stays live")
        if sec == 2:
            check(tok is not None, "refused item is re-claimable once the backoff elapses")
        if srd.is_parked(rd, "task-C"):
            parked_at = sec
            break
    item = outbox._read_item(b.root, "task-C")
    failures_seen = item.get("retry", {}).get("failures", item.get("attempts"))
    print(f"   evidence: {sends} sends, parked at t0+{parked_at}s, "
          f"reason={item.get('reason')!r}, failures={failures_seen}")
    check(parked_at is not None and parked_at >= 600,
          f"never parked inside the 600 s window (parked at t0+{parked_at}s)")
    check(item.get("reason") == "retry-window-exhausted" and failures_seen >= 5,
          "parks retry-window-exhausted once the window closed with >= 5 failures")
    check(5 <= sends <= 30,
          f"backoff honoured: {sends} sends across ~600 one-second passes")

# ── three-state mapping for Slack send outcomes ────────────────────────────
r = srd.receipt_for_send(True, _Resp(200, {"ok": True, "ts": "111.222"}))
check(r.outcome is RO.CONFIRMED and r.receipt_id == "111.222",
      "ok:true with ts -> CONFIRMED, ts is the receipt id")
r = srd.receipt_for_send(True, {"ok": True, "ts": "3.4"})
check(r.outcome is RO.CONFIRMED, "bare-dict response with ts -> CONFIRMED")
r = srd.receipt_for_send(True, _Resp(200, {"ok": True}))
check(r.outcome is RO.OUTCOME_UNKNOWN,
      "2xx without ts -> OUTCOME_UNKNOWN (accepted, unproven)")
r = srd.receipt_for_send(False, None,
                         _Exc("refused", _Resp(200, {"ok": False, "error": "channel_not_found"})))
check(r.outcome is RO.NOT_DELIVERED,
      "SlackApiError at HTTP 200 / ok:false -> NOT_DELIVERED (definite refusal)")
r = srd.receipt_for_send(False, None, _Exc("refused", _Resp(404, {"ok": False})))
check(r.outcome is RO.NOT_DELIVERED, "4xx -> NOT_DELIVERED")
r = srd.receipt_for_send(False, None, _Exc("boom", _Resp(500, {"ok": False})))
check(r.outcome is RO.OUTCOME_UNKNOWN, "5xx -> OUTCOME_UNKNOWN (may have applied)")
r = srd.receipt_for_send(False, None, TimeoutError("t"))
check(r.outcome is RO.OUTCOME_UNKNOWN,
      "raise without a response (timeout/transport) -> OUTCOME_UNKNOWN")
r = srd.receipt_for_send(False)
check(r.outcome is RO.NOT_DELIVERED,
      "helper reported failure with no provider material -> NOT_DELIVERED")
r = srd.receipt_for_send(True)
check(r.outcome is RO.CONFIRMED, "attachments-only success -> CONFIRMED")
# the ts key is pinned; an error trace id must not read as a receipt
r = srd.receipt_for_send(True, _Resp(200, {"ok": True, "id": "trace-1"}))
check(r.outcome is RO.OUTCOME_UNKNOWN,
      "a non-ts id key is not proof (SLACK_ID_KEYS pinned to ts)")

# ── settle(): receipt -> outbox transition ─────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    rd = Path(td) / "results"
    rd.mkdir()
    tok = srd.claim_for_send(rd, "task-S1")
    check(srd.settle(rd, tok, srd.receipt_for_send(True, {"ok": True, "ts": "1.2"}),
                     "D123") == "delivered" and srd.is_delivered(rd, "task-S1"),
          "settle(CONFIRMED) -> delivered + outbox DELIVERED")
    tok = srd.claim_for_send(rd, "task-S2")
    clock = [srd.result_backend(rd).clock()]
    srd.result_backend(rd).clock = lambda: clock[0]
    check(srd.settle(rd, tok, srd.receipt_for_send(False), "D123") == "retry"
          and srd.claim_for_send(rd, "task-S2") is None,
          "settle(NOT_DELIVERED) inside the window -> retry, backing off")
    clock[0] += srd.RETRY_SCHEDULE.initial_delay_s
    check(srd.claim_for_send(rd, "task-S2") is not None,
          "settle(NOT_DELIVERED) -> item re-claimable after the backoff")
    tok = srd.claim_for_send(rd, "task-S3")
    check(srd.settle(rd, tok, srd.receipt_for_send(False, None, TimeoutError()),
                     "D123") == "parked" and srd.is_parked(rd, "task-S3"),
          "settle(OUTCOME_UNKNOWN) -> parked, never auto-retried")

# ── wiring: the bridge delegates at its choke points ───────────────────────
src = (REPO / "src" / "slack-bridge.py").read_text()
tree = ast.parse(src)
calls = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "_srd"):
        calls.add(node.attr)
for fn in ("is_delivered", "is_parked", "claim_for_send", "settle",
           "failed", "receipt_for_send"):
    check(fn in calls, f"bridge calls _srd.{fn}() (delegation wired)")

watcher = src[src.find("def result_watcher"):src.find("def _no_events_hint_thread")]
delivered_pos = watcher.find("_srd.is_delivered(RESULTS_DIR, task_id)")
claim_pos = watcher.find("_srd.claim_for_send(RESULTS_DIR, task_id)")
send_pos = watcher.find("_send_reply(target[")
settle_pos = watcher.find("_srd.settle(RESULTS_DIR, _send_tok")
check(0 < delivered_pos < claim_pos < send_pos < settle_pos,
      "order: is_delivered -> claim_for_send -> _send_reply -> settle "
      f"({delivered_pos}, {claim_pos}, {send_pos}, {settle_pos})")
claim_none = watcher[claim_pos:]
claim_none = claim_none[claim_none.find("if _send_tok is None:"):]
claim_none = claim_none[:claim_none.find("continue")]
check("print(" not in claim_none,
      "a claim refused during backoff is skipped quietly (no per-second log)")
check("receipt_out=_rcpt" in watcher,
      "the reply send passes receipt_out so the outcome is provider-derived")
parked_branch = watcher[watcher.find("_srd.is_parked(RESULTS_DIR, task_id)"):]
parked_branch = parked_branch[:parked_branch.find("continue")]
check("archive_file" in parked_branch,
      "bridge archives the pair inside the is_parked branch")
check("outbox_adapter" not in src and "classify_response" not in src,
      "bridge holds no private outcome classification (policy stays in the module)")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
