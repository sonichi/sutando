#!/usr/bin/env python3
"""Combined integration E2E for the ag2-sparrow connection stack (PR #2245).

Component tests (src/remote-gateway-bridge.test.py) assert each primitive in
isolation. This harness runs the REAL bridge module through ONE end-to-end
sequence against an in-process mock gateway — the reviewer-requested combined
run — and prints gateway / task / result log excerpts:

  1. connected gateway              — a real HTTP poll returns a task
  2. inbound task written ONCE      — exactly one tasks/<id>.txt + one /ack
  3. result returned ONCE           — exactly one /v1/results POST, files archived
  4. forced bridge reconnect        — gateway REDELIVERS the same task-id
                                       (what a real reconnect does: it re-serves
                                       an in-flight/unacked task)
  5. inflight recovery, no dup/loss — the redelivery is deduped (already
                                       archived) → NOT re-queued, NO second local
                                       task, NO duplicate result; a [no-send] is
                                       dropped so the drain re-acks it upstream.

The mock speaks the real gateway HTTP contract, so the bridge is genuinely
"connected"; the reconnect is bridge-side and forced deterministically (a real
gateway can't be told to drop on command, and a one-shot prod run isn't
reproducible for a reviewer — this is). Exits 0 on pass, 1 on fail.

Run: python3 tests/sparrow-integration-e2e.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def log(section: str, line: str) -> None:
    print(f"    [{section}] {line}")


# ── mock gateway: serves ONE task, re-serves it after a "reconnect" ──────────
STATE = {"tasks_served": 0, "results": [], "acks": [], "serve_enabled": True}
TASK = {"id": "task-E2E1", "timestamp": "2026-07-21T00:00:00Z",
        "task": "integration e2e probe", "source": "remote-gateway",
        "channel_id": "!room:ag2.space", "user_id": "@qingyun:ag2.space",
        "access_tier": "owner", "priority": "normal"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/tasks"):
            tasks = [TASK] if STATE["serve_enabled"] else []
            STATE["tasks_served"] += len(tasks)
            body = json.dumps({"tasks": tasks}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else "{}"
        if self.path == "/v1/results":
            STATE["results"].append(json.loads(raw))
            self.send_response(200); self.end_headers()
        elif self.path.startswith("/v1/tasks/") and self.path.endswith("/ack"):
            STATE["acks"].append(self.path)
            self.send_response(200); self.end_headers()
        else:
            self.send_response(200); self.end_headers()


def _boot_bridge():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="sparrow-e2e-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = tmp
    Path(tmp, ".notes-migrated").touch()
    Path(tmp, ".build_log-migrated").touch()
    os.environ["REMOTE_TASK_URL"] = f"http://127.0.0.1:{port}"
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
    os.environ["REMOTE_TASK_TIER"] = "owner"
    spec = importlib.util.spec_from_file_location(
        "rtc", Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py")
    rtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rtc)
    return rtc, srv


def drive_poll(rtc, tasks):
    """Run ONE iteration of the bridge's task-handling block, exactly as
    ``remote_gateway_bridge.main()`` runs it.

    Every phase must go through this. The earlier revision hand-called
    ``_write_task`` and then jumped straight to ``_post_ready_results`` on the
    redelivery leg, skipping inflight-add, persist, and ack — so it measured a
    control flow the bridge never executes and published ``acks=1`` where the
    real loop produces ``acks=2``. A faithful E2E has to drive the loop, not a
    convenient subset of it.

    Mirrors main() line-for-line: note that ``pending_ack.append`` sits OUTSIDE
    the ``tid not in inflight`` guard, so an already-handled redelivery is still
    acked upstream — that is deliberate (``_write_task`` returns the id for work
    it deduped, precisely so the broker gets its ack and stops replaying).
    """
    inflight = rtc._load_inflight()
    added = False
    pending_ack = []
    for task in tasks:
        written = rtc._write_task(task)
        if written:
            tid, durable = written
            if tid not in inflight:
                inflight.add(tid)
                added = True
            pending_ack.append((tid, durable))
    # Ack only after task file, in-flight state and ack ledger are durable
    # (main()'s ordering).
    committed = rtc._save_inflight(inflight) if pending_ack else True
    if pending_ack and committed:
        committed = rtc._record_pending_acks(pending_ack)
    for tid, durable in pending_ack if committed else ():
        rtc._post_task_ack(tid, durable)
    rtc._post_ready_results(inflight)
    return [tid for tid, _ in pending_ack]


def main() -> int:
    rtc, srv = _boot_bridge()
    print("=== ag2-sparrow integration E2E (real bridge module ↔ mock gateway) ===\n")

    # ── 1. connected gateway ────────────────────────────────────────────────
    print("1. CONNECTED — poll the gateway")
    resp = rtc._req("GET", "/v1/tasks?wait=0")
    served = resp.get("tasks", []) if isinstance(resp, dict) else []
    check(len(served) == 1 and served[0]["id"] == "task-E2E1",
          "gateway poll returned the task (HTTP 200, connected)")
    log("gateway", f"GET /v1/tasks → 200, tasks=[{served[0]['id']}]  (tasks_served={STATE['tasks_served']})")

    # ── 2. inbound task written ONCE + acked ────────────────────────────────
    print("\n2. INBOUND — write the task locally, ack it")
    pending = drive_poll(rtc, served)             # same loop body as main()
    wid = pending[0] if pending else None
    tfiles = list((rtc.TASKS_DIR).glob("task-E2E1*.txt"))
    check(wid == "task-E2E1" and len(tfiles) == 1, "task written exactly once to tasks/")
    check(STATE["acks"] == ["/v1/tasks/task-E2E1/ack"], "task acked exactly once")
    check(rtc._load_inflight() == {"task-E2E1"}, "task recorded in persisted inflight")
    log("task-bridge", f"wrote {tfiles[0].name}; inflight={sorted(rtc._load_inflight())}")
    log("gateway", f"POST /v1/tasks/task-E2E1/ack → 200  (acks={len(STATE['acks'])})")

    # ── 3. result returned ONCE ─────────────────────────────────────────────
    print("\n3. RESULT — core produces a result; bridge POSTs it once, archives")
    (rtc.RESULTS_DIR).mkdir(parents=True, exist_ok=True)
    (rtc.RESULTS_DIR / "task-E2E1.txt").write_text("the reply to the probe\n")
    rtc._post_ready_results({"task-E2E1"})
    real_results = [r for r in STATE["results"] if not str(r.get("body", "")).startswith("[no-send]")]
    check(len(STATE["results"]) == 1 and STATE["results"][0]["id"] == "task-E2E1",
          "result POSTed exactly once (id + body)")
    check(not (rtc.RESULTS_DIR / "task-E2E1.txt").exists()
          and (rtc.TASKS_DIR / "archive" / "task-E2E1.txt").exists(),
          "result + task archived after delivery (no pile-up)")
    check(rtc._load_inflight() == set(), "delivered task cleared from persisted inflight")
    log("result", f"POST /v1/results → 200  body={STATE['results'][0]['body']!r}")
    log("task-bridge", "archived tasks/archive/task-E2E1.txt + results file")

    # ── 4. forced bridge reconnect — gateway REDELIVERS the same task ────────
    print("\n4. RECONNECT — gateway redelivers the same in-flight task-id "
          "(simulates a reconnect that replays unacked work)")
    results_before = len(STATE["results"])
    tasks_before = len(list((rtc.TASKS_DIR).glob("task-E2E1*.txt")))
    resp2 = rtc._req("GET", "/v1/tasks?wait=0")   # reconnect poll → same task again
    redelivered = resp2.get("tasks", [])
    log("gateway", f"reconnect GET /v1/tasks → 200, tasks=[{redelivered[0]['id']}]  (REDELIVERED)")
    acks_before = len(STATE["acks"])

    # ── 5. inflight recovery — NO dup, NO loss ──────────────────────────────
    print("\n5. RECOVERY — the redelivery must dedup: no second task, no duplicate result")
    # Drive the SAME loop body main() runs — write → inflight → persist → ack →
    # drain. Hand-calling _write_task here (the earlier revision) skipped the ack
    # and under-reported the real tally by one.
    pending2 = drive_poll(rtc, redelivered)
    rid = pending2[0] if pending2 else None
    tasks_after = len(list((rtc.TASKS_DIR).glob("task-E2E1*.txt")))
    check(rid == "task-E2E1", "redelivered id returned (so the drain re-acks it upstream)")
    check(tasks_after == tasks_before == 0,
          "redelivery did NOT re-queue a local task file (deduped against archive)")
    # The ack is the point of returning the id: the broker must learn the work is
    # handled or it replays forever. main() acks every pending id, redelivery
    # included — so the faithful tally is 2, not 1.
    check(len(STATE["acks"]) == acks_before + 1 == 2
          and STATE["acks"] == ["/v1/tasks/task-E2E1/ack"] * 2,
          "redelivery RE-ACKED upstream (acks=2) — matches main()'s pending_ack loop")
    real_after = [r for r in STATE["results"] if not str(r.get("body", "")).startswith("[no-send]")]
    marker_after = [r for r in STATE["results"] if str(r.get("body", "")).startswith("[no-send]")]
    # The raw skip marker still POSTs to close the redelivered lease; the
    # server suppresses its user-facing delivery.
    check(len(real_after) == 1 and len(STATE["results"]) == results_before + 1,
          "exactly ONE real result across the whole cycle — no duplicate delivery")
    check(len(marker_after) == 1 and "[no-send]" in str(marker_after[0].get("body", "")),
          "redelivery marker POSTed raw exactly once — closes the lease, server suppresses")
    check(rtc._load_inflight() == set(), "inflight empty after recovery (no leaked in-flight)")
    log("gateway", f"final: tasks_served={STATE['tasks_served']}, acks={len(STATE['acks'])}, "
                    f"real_results={len(real_after)}, marker_results={len(marker_after)}")
    log("result", "[no-send] redelivery marker POSTed (lease closed) then archived")

    srv.shutdown()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)})")
        return 1
    print("PASS — connected → task-once → result-once → forced reconnect → "
          "inflight recovery, no duplicate, no loss.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
