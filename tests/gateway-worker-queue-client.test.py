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
         "advertise": True, "strict": False, "rejected": [],
         # a 200 whose body is not JSON, and a 200 that explicitly declines
         "heartbeat_garbage": False, "results_decline": False,
         # strict_wire models a LEGACY relay that REJECTS (not ignores) unknown
         # heartbeat keys and unknown query params.
         "strict_wire": False, "advertise_routing": False,
         "hb_keys": [], "get_qkeys": [], "wire_rejected": []}
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
            from urllib.parse import urlparse, parse_qs
            qk = sorted(parse_qs(urlparse(self.path).query).keys())
            STATE["get_qkeys"].append(qk)
            _okq = {"wait"} | ({"worker"} if STATE["advertise_routing"] else set())
            if STATE["strict_wire"] and set(qk) - _okq:
                STATE["wire_rejected"].append(("get-query", qk))
                self._json(400, {"error": "unknown query parameter"}); return
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
            STATE["results"].append(body)
            if STATE["results_decline"]:
                self._json(200, {"ok": False, "error": "declined"}); return
            self._json(200, {"ok": True}); return
        if self.path.startswith("/v1/tasks/") and self.path.endswith("/ack"):
            STATE["acks"].append((self.path, self._body())); self._json(200, {}); return
        if self.path == "/v1/heartbeat":
            if STATE["heartbeat_404"]:
                # True keeps the historical 404; an int lets a case pick 405.
                _hb = STATE["heartbeat_404"]
                self.send_response(404 if _hb is True else int(_hb))
                self.end_headers(); return
            _hb = self._body()
            STATE["heartbeats"].append(_hb)
            STATE["hb_keys"].append(sorted(_hb.keys()))
            _legacy = {"client", "protocol_version", "provider", "tier",
                       "inflight", "capabilities", "status", "step"}
            if STATE["advertise_routing"]:      # advertising it means accepting it
                _legacy |= {"worker_id", "location"}
            if STATE["strict_wire"] and set(_hb) - _legacy:
                STATE["wire_rejected"].append(("heartbeat", sorted(set(_hb) - _legacy)))
                self._json(422, {"error": "unknown field"}); return
            if STATE["heartbeat_garbage"]:
                raw = b"<html>not json</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw); return
            _adv = (["worker-metadata"] if STATE["advertise"] else []) + \
                   (["worker-routing"] if STATE["advertise_routing"] else [])
            caps = {"capabilities": _adv} if _adv else {}
            self._json(200, caps); return
        if self.path == "/v1/workers":
            _w = self._body()
            STATE["workers"].append(_w)
            # A legacy relay REJECTS unknown keys on this endpoint too; without
            # this the "parent envelope accepted" check could not fail.
            _wlegacy = {"writer", "live_cores", "bindings", "ts", "seats", "lead"}
            # A broker that ADVERTISED worker-routing must also accept the routed
            # keys; a stub that advertises then rejects is an incoherent broker.
            if STATE["advertise_routing"]:
                _wlegacy |= {"worker_id", "location"}
            if STATE["strict_wire"] and set(_w) - _wlegacy:
                STATE["wire_rejected"].append(("workers", sorted(set(_w) - _wlegacy)))
                self._json(422, {"error": "unknown fields"}); return
            self._json(200, {}); return
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
    # The suffix is now NEGOTIATED, so assert the shape under an advertisement
    # rather than at import; unconditional was keweichen's blocker 1.
    check(home._seat_qs() == "" and named._seat_qs() == "",
          "no advertisement -> the poll carries no seat (legacy shape)")
    for _m in (home, named):
        _m._note_broker_capabilities({"capabilities": ["worker-routing"]})
    check(home._seat_qs() == "&worker=home" and named._seat_qs() == "&worker=worker-9",
          "once advertised, the suffix is &worker=<seat> (bare id for the legal charset)")
    for _m in (home, named):
        _m._revoke_broker_capabilities()

    # ── 2. heartbeat + workers snapshot carry the seat ──────────────────────
    print("2. heartbeat / workers snapshot payloads")
    STATE["heartbeats"].clear()
    STATE["advertise_routing"] = True          # identity is negotiated now
    home._note_broker_capabilities({"capabilities": ["worker-routing"]})
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
    # Identity is negotiated: teach this module the advertisement, then re-send.
    named._note_broker_capabilities({"capabilities": ["worker-routing"]})
    named._post_heartbeat(set(), force=True)
    check(STATE["heartbeats"][-1].get("worker_id") == "worker-9"
          and STATE["heartbeats"][-1].get("location") == "cloud",
          "heartbeat carries the configured seat + cloud location")
    print(f"     heartbeat payload (cloud): {json.dumps(STATE['heartbeats'][-1], sort_keys=True)}")
    snap = ws1 / "state" / "pool-status.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    blob = {"ts": 1, "writer": "pool-lead", "live_cores": ["core-1"], "bindings": {}}
    snap.write_text(json.dumps(blob))
    # CHANGED with the routing capability: identity rides /v1/workers only after
    # the broker advertises `worker-routing`, exactly like the heartbeat and poll.
    STATE["advertise_routing"] = True
    named._post_heartbeat(set(), force=True)
    named._workers_push_mtime = 0.0; named._workers_push_retry_at = 0.0
    check(named._maybe_push_workers_snapshot() is True
          and STATE["workers"][-1] == {**blob, "worker_id": "worker-9", "location": "cloud"},
          "workers-snapshot push stamps worker_id/location ONCE routing is advertised")
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
          f"poll URL carries the seat once routing is advertised: {STATE['gets'][:1]}")
    check(STATE["heartbeats"] and "worker_id" not in STATE["heartbeats"][0],
          "the seat's FIRST heartbeat is legacy — it has not seen an advertisement yet")
    check(all(h.get("worker_id") == "cloud-1" and h.get("location") == "cloud"
              for h in STATE["heartbeats"][1:]) and len(STATE["heartbeats"]) > 1,
          "every heartbeat AFTER the advertising reply names the cloud seat")
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

    # The ordering only pays off if a LATER process finishes the close, so the
    # control has to cross a real process boundary, not drain in the same one.
    print("\n10. crash after a confirmed ack -> a fresh process closes it exactly once")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    STATE["advertise"] = True
    ws10 = root / "ws10"; ws10.mkdir()
    seat10 = _load("wqc_pin_restart", ws10, port, SUTANDO_WORKER_ID="cloud-2",
                   SUTANDO_WORKER_LOCATION="cloud")
    PIN = "worker-pin-909-d0d0"

    def _die(*_a, **_k):
        raise RuntimeError("process loss immediately after the confirmed ack")

    seat10._retry_review_control_results = _die
    try:
        seat10._consume_worker_pin({"id": PIN, "task": "pin"})
    except RuntimeError:
        pass
    check(STATE["acks"] == [(f"/v1/tasks/{PIN}/ack", {"id": PIN})],
          f"the ack was CONFIRMED before the loss: {STATE['acks']}")
    check(STATE["results"] == [],
          f"the dying process did NOT close the lease: {STATE['results']}")
    check(seat10._control_result_path(PIN).is_file(),
          "the close intent survives on disk for whoever runs next")

    # restart: a brand-new module over the same workspace, no in-process carry-over
    seat10b = _load("wqc_pin_restart_b", ws10, port, SUTANDO_WORKER_ID="cloud-2",
                    SUTANDO_WORKER_LOCATION="cloud")
    check(seat10b is not seat10,
          "control: a genuinely fresh module, not the crashed one")
    check(seat10b._control_result_path(PIN).is_file(),
          "the fresh process finds the pending close on DISK, not in memory")
    seat10b._retry_review_control_results()
    check(STATE["results"] == [{"id": PIN, "body": "[no-send]"}],
          f"restart closes the lease exactly once: {STATE['results']}")
    check(not seat10b._control_result_path(PIN).is_file(),
          "and consumes the journal, so nothing can repeat it")
    seat10b._retry_review_control_results()
    check(STATE["results"] == [{"id": PIN, "body": "[no-send]"}],
          f"a second drain after restart posts nothing more: {STATE['results']}")

    # A 200 that cannot be decoded never reaches _note_broker_capabilities, so
    # there is no fresh advertisement and the extension must not stay latched.
    print("\n11. undecodable 200 heartbeat -> extension revoked, not latched")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    STATE["rejected"].clear(); STATE["advertise"] = True
    STATE["heartbeat_404"] = False; STATE["heartbeat_garbage"] = False
    STATE["strict"] = True
    ws11 = root / "ws11"; ws11.mkdir()
    d11 = _load("garbage11", ws11, port,
                SUTANDO_WORKER_ID="home-1", SUTANDO_WORKER_LOCATION="home")
    check(d11._post_heartbeat(set(), force=True) is True
          and d11._broker_worker_metadata is True,
          "advertising broker -> extension ON")
    STATE["heartbeat_garbage"] = True
    STATE["advertise"] = False
    check(d11._post_heartbeat(set(), force=True) is False,
          "an undecodable 200 does not raise out of the heartbeat")
    check(d11._broker_worker_metadata is False,
          "extension REVOKED after an undecodable reply (not latched on)")
    d11.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (d11.RESULTS_DIR / "task-GARBAGE.txt").write_text("answer after a bad reply\n")
    d11._save_inflight({"task-GARBAGE"})
    d11._post_ready_results(d11._load_inflight())
    check(STATE["rejected"] == [],
          f"the strict relay refused nothing: {STATE['rejected']}")
    check(len(STATE["results"]) == 1 and sorted(STATE["results"][0]) == ["body", "id"],
          f"wire-keys stay the documented envelope: "
          f"{sorted(STATE['results'][0]) if STATE['results'] else None}")
    STATE["heartbeat_garbage"] = False; STATE["strict"] = False

    # A declining 2xx means the lease did NOT close; deleting the journal there
    # strands it with nothing on disk to retry from.
    print("\n12. a declining 2xx keeps the pin-close journal for the next drain")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    STATE["advertise"] = True
    ws12 = root / "ws12"; ws12.mkdir()
    d12 = _load("decline12", ws12, port,
                SUTANDO_WORKER_ID="cloud-3", SUTANDO_WORKER_LOCATION="cloud")
    PIN12 = "worker-pin-424-b0b0"
    STATE["results_decline"] = True
    check(d12._consume_worker_pin({"id": PIN12, "task": "pin"}) is True,
          "the pin is consumed")
    check(len(STATE["results"]) == 1,
          f"one close attempt was made: {STATE['results']}")
    check(d12._control_result_path(PIN12).is_file(),
          "the broker DECLINED, so the journal is KEPT for a later drain")
    STATE["results_decline"] = False
    STATE["results"].clear()
    d12._retry_review_control_results()
    check(STATE["results"] == [{"id": PIN12, "body": "[no-send]"}],
          f"the next drain closes it exactly once: {STATE['results']}")
    check(not d12._control_result_path(PIN12).is_file(),
          "and an ACCEPTED close does consume the journal")

    # keweichen blocker 1: the worker-aware shape must be NEGOTIATED, never
    # unconditional — a strict legacy relay rejects both requests otherwise.
    print("\n13. worker routing is gated on the broker advertising `worker-routing`")
    for k in ("gets", "acks", "results", "heartbeats", "other"):
        STATE[k].clear()
    for k in ("hb_keys", "get_qkeys", "wire_rejected"):
        STATE[k].clear()
    STATE["advertise"] = False; STATE["advertise_routing"] = False
    STATE["heartbeat_404"] = False; STATE["heartbeat_garbage"] = False
    STATE["strict_wire"] = True
    ws13 = root / "ws13"; ws13.mkdir()
    d13 = _load("routing13", ws13, port,
                SUTANDO_WORKER_ID="cloud-9", SUTANDO_WORKER_LOCATION="cloud")

    # (a) FIRST exchange must be byte-for-byte legacy against a rejecting relay
    check(d13._post_heartbeat(set(), force=True) is True,
          "first heartbeat is ACCEPTED by a relay that rejects unknown keys")
    check(STATE["wire_rejected"] == [],
          f"nothing was rejected on the first exchange: {STATE['wire_rejected']}")
    check("worker_id" not in STATE["hb_keys"][0] and "location" not in STATE["hb_keys"][0],
          f"first heartbeat carries NO seat identity: {STATE['hb_keys'][0]}")
    check(d13._broker_worker_routing is False,
          "no advertisement -> routing stays off")
    check(d13._seat_qs() == "",
          f"pre-advertisement poll suffix is empty: {d13._seat_qs()!r}")

    # (b) broker advertises -> routing appears on BOTH surfaces
    STATE["advertise_routing"] = True
    check(d13._post_heartbeat(set(), force=True) is True, "second heartbeat sent")
    check(d13._broker_worker_routing is True, "advertisement enables routing")
    check("worker_id" not in STATE["hb_keys"][1],
          "the ADVERTISING heartbeat is itself still legacy — the flag comes from its REPLY")
    check(d13._post_heartbeat(set(), force=True) is True, "third heartbeat sent")
    check("worker_id" in STATE["hb_keys"][2] and "location" in STATE["hb_keys"][2],
          f"identity rides the request AFTER the advertisement: {STATE['hb_keys'][2]}")
    check(d13._seat_qs().startswith("&worker="),
          f"poll suffix carries the seat once advertised: {d13._seat_qs()!r}")

    # (c) withdrawal returns BOTH shapes to legacy
    STATE["advertise_routing"] = False
    check(d13._post_heartbeat(set(), force=True) is False,
          "the FIRST post-withdrawal heartbeat still carries identity and is 422'd")
    check(("heartbeat", ["location", "worker_id"]) in STATE["wire_rejected"],
          f"and the relay names exactly the routed keys: {STATE['wire_rejected']}")
    check(d13._broker_worker_routing is False,
          "that rejection REVOKES routing — one wasted round trip, then self-healed")
    check(d13._seat_qs() == "", "poll suffix is back to legacy")
    check(d13._post_heartbeat(set(), force=True) is True,
          "and the next heartbeat is legacy-shaped and accepted again")

    # (d) the two capabilities are INDEPENDENT, not aliases
    STATE["advertise"] = True; STATE["advertise_routing"] = False
    d13._post_heartbeat(set(), force=True)
    check(d13._broker_worker_metadata is True and d13._broker_worker_routing is False,
          "worker-metadata alone does NOT enable routing")
    STATE["advertise"] = False; STATE["advertise_routing"] = True
    d13._post_heartbeat(set(), force=True)
    check(d13._broker_worker_metadata is False and d13._broker_worker_routing is True,
          "worker-routing alone does NOT enable result metadata")
    STATE["strict_wire"] = False
    STATE["advertise"] = True; STATE["advertise_routing"] = False


    # ---- 14. blockers 3 + 4: /v1/workers is the THIRD body carrying seat
    # identity; ungated, a strict relay 422s it and the push backs off 1h.
    import http.client as _hc
    ws14 = root / "ws14"; (ws14 / "state").mkdir(parents=True, exist_ok=True)
    d14 = _load("d14", ws14, port, SUTANDO_WORKER_ID="core-4",
                SUTANDO_WORKER_LOCATION="cloud")
    snap_file = ws14 / "state" / "pool-status.json"
    snap_file.write_text(json.dumps({"writer": "lead", "live_cores": 2,
                                     "bindings": {}, "ts": 1}))
    STATE["strict_wire"] = True
    STATE["advertise"] = False; STATE["advertise_routing"] = False
    d14._post_heartbeat(set(), force=True)
    check(d14._broker_worker_routing is False, "routing OFF for the snapshot control")
    d14._workers_push_mtime = 0.0; d14._workers_push_retry_at = 0.0
    check(d14._maybe_push_workers_snapshot() is True,
          "un-advertised: the snapshot posts the EXACT parent envelope and is accepted")
    check(all(r[0] != "workers" for r in STATE["wire_rejected"]),
          f"the strict relay rejected nothing on /v1/workers: {STATE['wire_rejected']}")
    check("worker_id" not in sorted(STATE["workers"][-1])
          and "location" not in sorted(STATE["workers"][-1]),
          f"and carries no seat keys: {sorted(STATE['workers'][-1])}")

    STATE["advertise_routing"] = True
    d14._post_heartbeat(set(), force=True)
    check(d14._broker_worker_routing is True, "routing advertised")
    snap_file.write_text(json.dumps({"writer": "lead", "live_cores": 3,
                                     "bindings": {}, "ts": 2}))
    d14._workers_push_mtime = 0.0; d14._workers_push_retry_at = 0.0
    check(d14._maybe_push_workers_snapshot() is True, "advertised: snapshot posts")
    check("worker_id" in sorted(STATE["workers"][-1])
          and "location" in sorted(STATE["workers"][-1]),
          f"and identity rides it only now: {sorted(STATE['workers'][-1])}")

    # (3) a TRUNCATED successful heartbeat escapes during resp.read(); it carries
    # no advertisement, so it must revoke exactly like a 404 or a malformed 200.
    check(d14._broker_worker_metadata is True or d14._broker_worker_metadata is False,
          "metadata flag readable before the truncation control")
    STATE["advertise"] = True; STATE["advertise_routing"] = True
    d14._post_heartbeat(set(), force=True)
    check(d14._broker_worker_routing is True, "capabilities ON before truncation")
    _real_urlopen = d14.urllib.request.urlopen

    def _truncating(*a, **k):
        raise _hc.IncompleteRead(b"{\"ok\": tr", 42)
    d14.urllib.request.urlopen = _truncating
    try:
        check(d14._post_heartbeat(set(), force=True) is False,
              "a truncated 200 does not raise out of the heartbeat")
    finally:
        d14.urllib.request.urlopen = _real_urlopen
    check(d14._broker_worker_routing is False and d14._broker_worker_metadata is False,
          "and it REVOKES both capabilities — absence of a reply is absence of evidence")
    STATE["strict_wire"] = False

    srv.shutdown()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})"); return 1
    print("\nPASS — worker-aware queue client: all checks green"); return 0


if __name__ == "__main__":
    sys.exit(main())
