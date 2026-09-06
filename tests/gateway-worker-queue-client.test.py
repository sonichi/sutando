#!/usr/bin/env python3
"""The gateway client is a worker-aware queue client, backward compatible.

Covers: the seat identity from env (home default, pool seat, cloud seat, bad
value refused at import); heartbeat and workers-snapshot carry worker_id +
location; the poll URL carries `worker=`; a legacy broker that ignores the
param or 404s heartbeat is served exactly as before; a cloud seat with NO
state/pool* and NO state/cores runs pull → tasks/ → results/ → POST end to end
against a stub broker; a task id re-delivered while its first copy is still
in flight (pending, assigned or claimed) is never queued or answered twice; and
the broker's `worker-pin-*` compat task is consumed as a control message (no
task file, one ack, one [no-send] lease close, nothing else on the wire).

Run: python3 tests/gateway-worker-queue-client.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOADER = REPO / "src" / "remote-gateway-bridge.py"
FAILS: list[str] = []
SEAT_ENV = ("SUTANDO_WORKER_ID", "SUTANDO_WORKER_SEAT", "SUTANDO_CORE_ID",
            "SUTANDO_WORKER_LOCATION")


def check(cond: bool, msg: str) -> None:
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ── stub broker: records every call; serves TASK on the first `serve_n` polls,
# ignores the worker= query entirely (a broker that predates seats) ──────────
STATE = {"gets": [], "acks": [], "results": [], "heartbeats": [], "workers": [],
         "other": [], "serve_n": 0, "heartbeat_404": False, "task": None,
         # A modern broker advertises the extension; `strict` models a legacy
         # relay that REJECTS unknown result keys instead of ignoring them.
         "advertise": True, "strict": False, "rejected": []}
TASK = {"id": "task-CLOUD1", "timestamp": "2026-09-03T00:00:00Z",
        "task": "hello cloud seat", "source": "remote-gateway",
        "channel_id": "!room:example.org", "user_id": "@qingyun:example.org",
        "access_tier": "owner", "priority": "normal"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode()) if n else {}

    def do_GET(self):
        if self.path.startswith("/v1/tasks"):
            STATE["gets"].append(self.path)
            served = STATE["serve_n"] > 0
            if served:
                STATE["serve_n"] -= 1
            self._json(200, {"tasks": [STATE["task"] or TASK] if served else []})
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/v1/results":
            body = self._body()
            allowed = {"id", "body"} | ({"metadata"} if STATE["advertise"] else set())
            if STATE["strict"] and set(body) - allowed:
                STATE["rejected"].append(sorted(body))
                self._json(422, {"error": "unknown field"}); return
            STATE["results"].append(body); self._json(200, {"ok": True}); return
        if self.path.startswith("/v1/tasks/") and self.path.endswith("/ack"):
            STATE["acks"].append((self.path, self._body())); self._json(200, {}); return
        if self.path == "/v1/heartbeat":
            if STATE["heartbeat_404"]:
                # True keeps the historical 404; an int lets a case pick 405.
                _hb = STATE["heartbeat_404"]
                self.send_response(404 if _hb is True else int(_hb))
                self.end_headers(); return
            STATE["heartbeats"].append(self._body())
            caps = {"capabilities": ["worker-metadata"]} if STATE["advertise"] else {}
            self._json(200, caps); return
        if self.path == "/v1/workers":
            STATE["workers"].append(self._body()); self._json(200, {}); return
        STATE["other"].append((self.path, self._body()))
        self.send_response(404); self.end_headers()


def _load(name: str, ws: Path, port: int, **seat: str):
    """Fresh module under a fresh env: the client reads its seat at import."""
    for k in SEAT_ENV:
        os.environ.pop(k, None)
    os.environ.update(seat)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = str(ws)
    Path(ws, ".notes-migrated").touch()
    Path(ws, ".build_log-migrated").touch()
    os.environ["REMOTE_TASK_URL"] = f"http://127.0.0.1:{port}"
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
    os.environ["REMOTE_TASK_TIER"] = "owner"
    os.environ["REMOTE_TASK_POLL_WAIT"] = "0"
    os.environ["REMOTE_OUTBOUND_WATCHER"] = "off"
    os.environ.pop("GATEWAY_INSTANCE", None)
    # Hermetic: the tier map resolves from the config dir — never the host's.
    os.environ.pop("AG2_DEVICE_ENV", None)
    os.environ["CLAUDE_CONFIG_DIR"] = str(ws / "cfg")
    spec = importlib.util.spec_from_file_location(name, LOADER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pool_state_absent(ws: Path) -> bool:
    st = ws / "state"
    return (not (st / "pool").exists() and not (st / "pool-status.json").exists()
            and not (st / "cores").exists())


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = Path(tempfile.mkdtemp(prefix="wqc-test-"))

    # ── 1. seat identity from env ───────────────────────────────────────────
    print("1. seat identity")
    ws1 = root / "ws1"; ws1.mkdir()
    home = _load("wqc_home", ws1, port)
    check((home.WORKER_ID, home.WORKER_LOCATION) == ("home", "local"),
          "no seat env → the home seat, local")
    pool = _load("wqc_pool", ws1, port, SUTANDO_CORE_ID="2")
    check((pool.WORKER_ID, pool.WORKER_LOCATION) == ("worker-2", "local"),
          "SUTANDO_CORE_ID=2 derives worker-2 (pool seat convention)")
    named = _load("wqc_named", ws1, port, SUTANDO_CORE_ID="2",
                  SUTANDO_WORKER_ID="worker-9", SUTANDO_WORKER_LOCATION=" Cloud ")
    check((named.WORKER_ID, named.WORKER_LOCATION) == ("worker-9", "cloud"),
          "SUTANDO_WORKER_ID wins over the seat number; location normalises to cloud")
    typo = _load("wqc_typo", ws1, port, SUTANDO_WORKER_LOCATION="remote")
    check(typo.WORKER_LOCATION == "local",
          "an unknown location degrades to local (today's path), never to cloud")
    try:
        _load("wqc_bad", ws1, port, SUTANDO_WORKER_ID="../evil")
        check(False, "a path-shaped SUTANDO_WORKER_ID refuses at import")
    except SystemExit:
        check(True, "a path-shaped SUTANDO_WORKER_ID refuses at import")
    check(home._SEAT_QS == "&worker=home" and named._SEAT_QS == "&worker=worker-9",
          "the poll query suffix is &worker=<seat> (bare id for the legal charset)")

    # ── 2. heartbeat + workers snapshot carry the seat ──────────────────────
    print("2. heartbeat / workers snapshot payloads")
    STATE["heartbeats"].clear()
    check(home._post_heartbeat({"task-A", "task-B"}, force=True)
          and STATE["heartbeats"][-1].get("worker_id") == "home"
          and STATE["heartbeats"][-1].get("location") == "local",
          "heartbeat carries worker_id=home location=local for the home seat")
    hb = STATE["heartbeats"][-1]
    check(hb.get("client") == "sutando-gateway-client" and hb.get("protocol_version") == 1
          and hb.get("provider") == "remote-gateway" and hb.get("tier") == "owner"
          and hb.get("inflight") == 2
          and hb.get("capabilities") == ["task-ack", "heartbeat", "result-skip-markers",
                                         "core-status", "team-collaborator"],
          "every pre-existing heartbeat field is byte-identical (additive change only)")
    print(f"     heartbeat payload (home): {json.dumps(hb, sort_keys=True)}")
    named._last_heartbeat_at = 0.0
    named._post_heartbeat(set(), force=True)
    check(STATE["heartbeats"][-1].get("worker_id") == "worker-9"
          and STATE["heartbeats"][-1].get("location") == "cloud",
          "heartbeat carries the configured seat + cloud location")
    print(f"     heartbeat payload (cloud): {json.dumps(STATE['heartbeats'][-1], sort_keys=True)}")
    snap = ws1 / "state" / "pool-status.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    blob = {"ts": 1, "writer": "pool-lead", "live_cores": ["core-1"], "bindings": {}}
    snap.write_text(json.dumps(blob))
    check(named._maybe_push_workers_snapshot() is True
          and STATE["workers"][-1] == {**blob, "worker_id": "worker-9", "location": "cloud"},
          "workers-snapshot push stamps worker_id/location beside the lead's fields")
    snap.unlink()

    # ── 3. legacy broker: 404 heartbeat, ignored worker= ─────────────────────
    print("3. legacy broker")
    STATE["heartbeat_404"] = True
    legacy = _load("wqc_legacy", ws1, port)
    legacy._last_heartbeat_at = 0.0
    check(legacy._post_heartbeat(set(), force=True) is False and legacy._heartbeat_disabled,
          "heartbeat 404 still disables heartbeat quietly — the seat fields change nothing")
    STATE["heartbeat_404"] = False

    # ── 4. cloud seat, no pool state on this host: full cycle via main() ────
    print("4. cloud seat end-to-end (no state/pool*, no state/cores)")
    ws4 = root / "ws4"; ws4.mkdir()
    cloud = _load("wqc_cloud", ws4, port, SUTANDO_WORKER_ID="cloud-1",
                  SUTANDO_WORKER_LOCATION="cloud")
    check(_pool_state_absent(ws4), "precondition: no pool-status.json / pool / cores")
    check(cloud._worker_of("task-CLOUD1") == "", "no state/cores → attribution empty, no error")
    check(cloud._maybe_push_workers_snapshot() is False, "no pool snapshot → push is a no-op")
    for k in ("gets", "acks", "results", "heartbeats"):
        STATE[k].clear()
    STATE["serve_n"] = 2  # poll 1 serves the task; poll 2 RE-DELIVERS it (lease re-front)
    real_hb = cloud._post_heartbeat
    calls = {"n": 0}

    def _bounded(inflight_arg):
        # Heartbeats bracket each iteration: 1,2 = iteration 1; 3,4 = iteration 2;
        # 5 = top of iteration 3 → stop. Two full polls ran by then.
        calls["n"] += 1
        if calls["n"] >= 5:
            raise KeyboardInterrupt
        return real_hb(inflight_arg, force=True)

    cloud._post_heartbeat = _bounded
    try:
        cloud.main()
    except KeyboardInterrupt:
        pass
    finally:
        cloud._post_heartbeat = real_hb
        cloud._OUTBOUND_STOP.set(); cloud._OUTBOUND_WAKE.set()
    check(calls["n"] == 5, "main: two full loop iterations ran")
    check(len(STATE["gets"]) == 2
          and all(g == "/v1/tasks?wait=0&worker=cloud-1" for g in STATE["gets"]),
          f"poll URL carries the seat: {STATE['gets'][:1]}")
    check(all(h.get("worker_id") == "cloud-1" and h.get("location") == "cloud"
              for h in STATE["heartbeats"]) and STATE["heartbeats"],
          "every heartbeat from the loop names the cloud seat")
    tfile = cloud.TASKS_DIR / "task-CLOUD1.txt"
    tfiles = sorted(p.name for p in cloud.TASKS_DIR.glob("task-CLOUD1*"))
    check(tfiles == ["task-CLOUD1.txt"], f"exactly one task file after redelivery: {tfiles}")
    body = tfile.read_text() if tfile.exists() else ""
    check("task: hello cloud seat" in body and body.startswith("id: task-CLOUD1"),
          "task serialised into this seat's own tasks/ (same schema as every bridge)")
    check(cloud._load_inflight() == {"task-CLOUD1"},
          "persisted inflight holds the id once, not twice")
    check([a[0] for a in STATE["acks"]] == ["/v1/tasks/task-CLOUD1/ack"] * 2,
          "re-delivered id is re-acked (broker learns the seat still holds it)")
    check(STATE["results"] == [], "nothing answered yet — the seat has not produced a result")
    check(_pool_state_absent(ws4), "the cycle created no pool-status.json / pool / cores")

    # Re-delivery against the other live shapes: the seat has taken the file.
    claimed = cloud.TASKS_DIR / "task-CLOUD1.claimed-cloud-1.txt"
    tfile.rename(claimed)
    check(cloud._write_task(dict(TASK)) == "task-CLOUD1"
          and not tfile.exists() and claimed.exists(),
          "redelivery while CLAIMED: id returned, no second task file")
    assigned = cloud.TASKS_DIR / "task-CLOUD1.assigned-cloud-1.txt"
    claimed.rename(assigned)
    check(cloud._write_task(dict(TASK)) == "task-CLOUD1"
          and not tfile.exists() and assigned.exists(),
          "redelivery while ASSIGNED: id returned, no second task file")
    assigned.rename(tfile)
    check(sorted(p.name for p in cloud.TASKS_DIR.glob("task-CLOUD1*")) == ["task-CLOUD1.txt"],
          "task file content untouched across three redeliveries")

    # The seat answers → one POST, one archive, inflight cleared.
    cloud.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (cloud.RESULTS_DIR / "task-CLOUD1.txt").write_text("answer from the cloud seat\n")
    inflight = cloud._load_inflight()
    cloud._post_ready_results(inflight)
    check(len(STATE["results"]) == 1
          and STATE["results"][0].get("id") == "task-CLOUD1"
          and STATE["results"][0].get("body") == "answer from the cloud seat"
          and STATE["results"][0].get("metadata")
          == {"worker_id": "cloud-1", "location": "cloud"},
          "result POSTed once with the broker id; attributed to the cloud seat itself "
          "(no done-flag on this host → the seat's own WORKER_ID + location)")
    print(f"     result payload: {json.dumps(STATE['results'][0], sort_keys=True)}")
    check(not (cloud.RESULTS_DIR / "task-CLOUD1.txt").exists()
          and (cloud.TASKS_DIR / "archive" / "task-CLOUD1.txt").exists(),
          "result + task archived after delivery")
    check(cloud._load_inflight() == set(), "inflight empty after delivery")
    check(_pool_state_absent(ws4), "post-delivery: still no pool state on this host")

    # ── 5. worker-pin compat task: a control message, never a user task ─────
    print("5. worker-pin compat task is consumed in-client")
    ws5 = root / "ws5"; ws5.mkdir()
    seat = _load("wqc_pin", ws5, port, SUTANDO_WORKER_ID="cloud-1",
                 SUTANDO_WORKER_LOCATION="cloud")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    STATE["task"] = {"id": "worker-pin-123-abc", "timestamp": "2026-09-03T00:00:01Z",
                     "task": "pin cloud-1", "source": "remote-gateway",
                     "channel_id": "!room:example.org", "user_id": "@broker:example.org",
                     "access_tier": "owner", "priority": "normal"}
    STATE["serve_n"] = 1
    logs: list[str] = []
    seat._log = lambda m: logs.append(str(m))
    real_hb = seat._post_heartbeat
    calls = {"n": 0}

    def _one_iteration(inflight_arg):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt
        return real_hb(inflight_arg, force=True)

    seat._post_heartbeat = _one_iteration
    try:
        seat.main()
    except KeyboardInterrupt:
        pass
    finally:
        seat._post_heartbeat = real_hb
        seat._OUTBOUND_STOP.set(); seat._OUTBOUND_WAKE.set()
        STATE["task"] = None
    files = sorted(p.name for p in seat.TASKS_DIR.glob("*")) if seat.TASKS_DIR.exists() else []
    check(files == [], f"no tasks/*.txt written for the pin: {files}")
    check(STATE["acks"] == [("/v1/tasks/worker-pin-123-abc/ack", {"id": "worker-pin-123-abc"})],
          f"exactly one ack, on the pin id: {STATE['acks']}")
    check(STATE["results"] == [{"id": "worker-pin-123-abc", "body": "[no-send]"}],
          f"exactly one [no-send] lease close: {STATE['results']}")
    check(STATE["other"] == [], f"nothing else reached the broker (no room post): {STATE['other']}")
    check(seat._load_inflight() == set(), "the pin never entered inflight")
    check(not list((seat._STATE / "withheld-review-control-results").glob("*.json")),
          "no deferred control result left behind")
    target = "archived worker-pin-123-abc (marker no-send, lease closed, not sent)"
    check(target in logs, f"log line matches the live-run target: {[l for l in logs if 'pin' in l]}")
    print(f"     result payload: {json.dumps(STATE['results'][0], sort_keys=True)}")

    # ── 6. pin classifier: full grammar, and ONE normalized id ──────────────
    print("6. pin classifier rejects look-alikes and normalizes the id once")
    ws6 = root / "ws6"; ws6.mkdir()
    seat6 = _load("wqc_pin_edge", ws6, port, SUTANDO_WORKER_ID="cloud-1",
                  SUTANDO_WORKER_LOCATION="cloud")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    look_alike = {"id": "worker-pin-user-message", "task": "an ordinary ask",
                  "timestamp": "2026-09-03T00:00:01Z", "source": "remote-gateway"}
    check(seat6._consume_worker_pin(look_alike) is False,
          "worker-pin-user-message is ordinary work, not a control message")
    check(STATE["acks"] == [] and STATE["results"] == [],
          f"the look-alike was neither acked nor closed: {STATE['acks']} {STATE['results']}")
    for k in ("acks", "results"):
        STATE[k].clear()
    check(seat6._consume_worker_pin({"id": " worker-pin-123-abc ", "task": "pin"}) is True,
          "a padded pin id is still a pin")
    check(STATE["acks"] == [("/v1/tasks/worker-pin-123-abc/ack",
                             {"id": "worker-pin-123-abc"})],
          f"ack uses the normalized id: {STATE['acks']}")
    check(STATE["results"] == [{"id": "worker-pin-123-abc", "body": "[no-send]"}],
          f"lease close uses the normalized id: {STATE['results']}")
    left = sorted(p.name for p in
                  (seat6._STATE / "withheld-review-control-results").glob("*.json"))
    check(left == [], f"no journal file stranded under a raw-id path: {left}")

    # ── 7. journal-before-ACK: a failed durable write must not leave an ack ──
    print("7. the close intent is durable before the ack")
    ws7 = root / "ws7"; ws7.mkdir()
    seat7 = _load("wqc_pin_durable", ws7, port, SUTANDO_WORKER_ID="cloud-1",
                  SUTANDO_WORKER_LOCATION="cloud")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()

    def _explode(*_a, **_k):
        raise RuntimeError("process loss at the durability boundary")

    real_queue = seat7._queue_review_control_result
    seat7._queue_review_control_result = _explode
    try:
        seat7._consume_worker_pin({"id": "worker-pin-777-beef", "task": "pin"})
    except RuntimeError:
        pass
    finally:
        seat7._queue_review_control_result = real_queue
    check(STATE["acks"] == [],
          f"no ack when the close intent could not be persisted: {STATE['acks']}")
    check(not seat7._control_result_path("worker-pin-777-beef").is_file(),
          "and no journal file either — the pin stays redeliverable")

    for k in ("acks", "results"):
        STATE[k].clear()
    check(seat7._consume_worker_pin({"id": "worker-pin-778-cafe", "task": "pin"}) is True,
          "the normal path still consumes the pin")
    check(STATE["acks"] == [("/v1/tasks/worker-pin-778-cafe/ack",
                             {"id": "worker-pin-778-cafe"})],
          f"normal path acks once: {STATE['acks']}")
    check(STATE["results"] == [{"id": "worker-pin-778-cafe", "body": "[no-send]"}],
          f"normal path closes the lease exactly once: {STATE['results']}")
    seat7._retry_review_control_results()
    check(STATE["results"] == [{"id": "worker-pin-778-cafe", "body": "[no-send]"}],
          f"a second drain does not re-post the close: {STATE['results']}")

    # A relay that 422s un-advertised keys must still receive the documented
    # {id, body}; before the fix `metadata` rode every result and was refused.
    print("\n8. legacy relay (does NOT advertise worker-metadata) — envelope preserved")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    STATE["rejected"].clear()
    STATE["advertise"] = False
    STATE["strict"] = True
    ws8 = root / "ws8"; ws8.mkdir()
    legacy8 = _load("legacy8", ws8, port,
                    SUTANDO_WORKER_ID="home-1", SUTANDO_WORKER_LOCATION="home")
    check(legacy8._post_heartbeat(set(), force=True) is True,
          "heartbeat reaches the legacy broker")
    check(legacy8._broker_worker_metadata is False,
          "no advertisement → the client does NOT enable the metadata extension")
    legacy8.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (legacy8.RESULTS_DIR / "task-LEGACY1.txt").write_text("answer for a legacy relay\n")
    legacy8._save_inflight({"task-LEGACY1"})
    legacy8._post_ready_results(legacy8._load_inflight())
    check(STATE["rejected"] == [],
          f"the strict relay refused nothing: {STATE['rejected']}")
    check(len(STATE["results"]) == 1 and sorted(STATE["results"][0]) == ["body", "id"],
          f"wire-keys are exactly the documented envelope: "
          f"{sorted(STATE['results'][0]) if STATE['results'] else None}")
    check(not (legacy8.RESULTS_DIR / "task-LEGACY1.txt").exists(),
          "the result was delivered, not left behind for a retry")
    check(not list((legacy8._STATE.parent / "results" / "undelivered").glob("*"))
          if (legacy8._STATE.parent / "results" / "undelivered").exists() else True,
          "nothing was quarantined as undelivered")
    print(f"     legacy payload: {json.dumps(STATE['results'][0], sort_keys=True)}"
          if STATE["results"] else "     legacy payload: NONE")

    # control: the SAME strict relay, now advertising → the extension returns.
    for k in ("results", "heartbeats"):
        STATE[k].clear()
    STATE["advertise"] = True
    check(legacy8._post_heartbeat(set(), force=True) is True, "second heartbeat sent")
    check(legacy8._broker_worker_metadata is True,
          "control: an advertising broker re-enables the extension")
    (legacy8.RESULTS_DIR / "task-LEGACY2.txt").write_text("answer once advertised\n")
    legacy8._save_inflight({"task-LEGACY2"})
    legacy8._post_ready_results(legacy8._load_inflight())
    check(len(STATE["results"]) == 1
          and STATE["results"][0].get("metadata")
          == {"worker_id": legacy8.WORKER_ID, "location": legacy8.WORKER_LOCATION},
          f"control: attribution rides again once advertised: {STATE['results']}")
    STATE["strict"] = False

    # A broker that advertised and is then rolled back must not leave the
    # extension latched: 404/405 disables heartbeat, so no reply can revoke it.
    print("\n9. advertised -> heartbeat 404/405 -> extension REVOKED, not latched")
    for _code in (404, 405):
        for k in ("gets", "acks", "results", "heartbeats", "other"):
            STATE[k].clear()
        STATE["rejected"].clear()
        STATE["heartbeat_404"] = False
        STATE["advertise"] = True
        STATE["strict"] = True
        ws9 = root / f"ws9_{_code}"; ws9.mkdir()
        d9 = _load(f"down{_code}", ws9, port,
                   SUTANDO_WORKER_ID="home-1", SUTANDO_WORKER_LOCATION="home")
        check(d9._post_heartbeat(set(), force=True) is True
              and d9._broker_worker_metadata is True,
              f"[{_code}] a modern broker advertises -> extension ON")
        # the broker is rolled back / re-routed: heartbeat now answers 404/405
        STATE["heartbeat_404"] = _code
        STATE["advertise"] = False
        check(d9._post_heartbeat(set(), force=True) is False and d9._heartbeat_disabled,
              f"[{_code}] heartbeat {_code} disables heartbeating for the process")
        check(d9._broker_worker_metadata is False,
              f"[{_code}] extension REVOKED on definitive absence (not latched on)")
        d9.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (d9.RESULTS_DIR / f"task-DOWN{_code}.txt").write_text("answer after downgrade\n")
        d9._save_inflight({f"task-DOWN{_code}"})
        d9._post_ready_results(d9._load_inflight())
        check(STATE["rejected"] == [],
              f"[{_code}] the strict relay refused nothing: {STATE['rejected']}")
        check(len(STATE["results"]) == 1
              and sorted(STATE["results"][0]) == ["body", "id"],
              f"[{_code}] wire-keys after downgrade are exactly the envelope: "
              f"{sorted(STATE['results'][0]) if STATE['results'] else None}")
    STATE["heartbeat_404"] = False
    STATE["strict"] = False

    srv.shutdown()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})"); return 1
    print("\nPASS — worker-aware queue client: all checks green"); return 0


if __name__ == "__main__":
    sys.exit(main())
