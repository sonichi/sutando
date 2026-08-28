#!/usr/bin/env python3
"""E2E test for the runtime API v0 (owner acceptance: the E2E loop must be testable through).

Boots the REAL daemon on a temp socket, drives it through the REAL CLI, and
resolves requests through the REAL human-action store — the only simulated
piece is the owner's answer, written exactly the way DecisionHandler writes it
(ActionStore.resolve with 1-based option indexes). This is the local half of
the design doc's acceptance scenario; slice ② swaps the fake capability
executor for the governed gateway send.

Covered:
  1. approval.request → pending → card record visible to CardPoster's sweep
     source (pending + no card_event_id) → owner approves → request.wait
     returns approved.
  2. approval denied path.
  3. elicitation single_select → owner picks option 2 → wait returns the
     chosen label.
  4. capability.execute (fake executor) → completed immediately.
  5. request.get unknown id → protocol error (exit 1).
  6. wait timeout on an unanswered request → timedOut: true, still pending.
  7. daemon restart recovery: pending request survives, resolves after boot.
  8. cancel → cancelled; a late owner answer does NOT overwrite (CAS).

Run: python3 tests/runtime-api-e2e.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "src" / "runtime-api" / "server.py"
CLI = REPO / "src" / "runtime-cli" / "sutando-runtime.py"

# Under the coverage gate, subprocesses must self-instrument or their
# execution (daemon + CLI) counts as zero — same pattern as voice-lock.
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def cli(*args, expect_rc=0, timeout=30):
    p = subprocess.run([*PYBASE, str(CLI), *args],
                       capture_output=True, text=True, timeout=timeout,
                       env=ENV)
    if p.returncode != expect_rc:
        raise AssertionError(f"cli {args} rc={p.returncode} err={p.stderr}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def wait_socket(path, timeout=10):
    import socket as _s
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        try:
            s.connect(path)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def start_daemon():
    proc = subprocess.Popen([*PYBASE, str(SERVER)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    if not wait_socket(ENV["SUTANDO_RUNTIME_SOCKET"]):
        proc.kill()
        print(proc.stdout.read())
        raise AssertionError("daemon socket never came up")
    return proc


def ha_store():
    spec = importlib.util.spec_from_file_location(
        "ha", REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "human_action.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ActionStore(ENV["SUTANDO_HA_DIR"])


def pending_action_for(request_id, store, timeout=5):
    aid = "ha_" + request_id.split("-", 1)[-1][:24]
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = store.get(aid)
        if rec is not None:
            return rec
        time.sleep(0.1)
    return None


# Mock gateway: /v1/room returns {ok, event_id} (the #207 broker contract);
# togglable swallowed-send mode returns bare {} — must read as NOT delivered.
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GW = {"posts": [], "swallow": False, "slow": False}


class _GwHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if GW["slow"]:
            time.sleep(1.5)
        n = int(self.headers.get("Content-Length") or 0)
        GW["posts"].append(json.loads(self.rfile.read(n).decode()))
        body = b"{}" if GW["swallow"] else json.dumps(
            {"ok": True, "event_id": f"$evt-{len(GW['posts'])}"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


_gw_srv = ThreadingHTTPServer(("127.0.0.1", 0), _GwHandler)
threading.Thread(target=_gw_srv.serve_forever, daemon=True).start()
GW_URL = f"http://127.0.0.1:{_gw_srv.server_address[1]}"

TMP = tempfile.mkdtemp(prefix="runtime-api-e2e-")
ENV = {**os.environ,
       # instance lock + run dir must not collide with a live daemon's default
       "SUTANDO_RUN_DIR": str(Path(TMP) / "run"),
       "SUTANDO_RUNTIME_SOCKET": str(Path(TMP) / "rt.sock"),
       "SUTANDO_RUNTIME_DB": str(Path(TMP) / "runtime-state.sqlite"),
       "SUTANDO_HA_DIR": str(Path(TMP) / "human-actions"),
       "SUTANDO_RUNTIME_STATE": str(Path(TMP) / "state"),
       "SUTANDO_RUNTIME_RESOLVE_POLL": "0.3",
       "SUTANDO_AGENT_ID": "@test-agent:example.org",
       "SUTANDO_HOST_LABEL": "e2e-host",  # runtime.* reads its own beat by label
       "SUTANDO_INSTANCE_REGISTRY": str(Path(TMP) / "instances"),
       "SUTANDO_TMUX_SOCKET": "/tmp/e2e-tmux.sock",
       "SUTANDO_TMUX_SESSION": "e2e-core",
       "REMOTE_TASK_URL": "",  # set per-phase: capability tests point at the mock
       "REMOTE_TASK_TOKEN": "test-bearer"}


def main() -> int:
    ENV["REMOTE_TASK_URL"] = GW_URL
    daemon = start_daemon()
    store = ha_store()
    try:
        # 1. approval → approve
        r = cli("approval", "request", "--task-id", "task-e2e",
                "--action", "message.send",
                "--resource", '{"roomId":"!room:example.org"}',
                "--reason", "post the summary")
        check(r["status"] == "pending" and r["requestId"].startswith("approval-"),
              "approval.request issues pending immediately")
        act = pending_action_for(r["requestId"], store)
        # DecisionHandler answer-grammar compatibility: the REAL owner answers
        # `answer <action_id> N` and _ANSWER_RE only matches ha_ + HEX. A
        # non-hex id silently strands the card (live finding 2026-07-26).
        import re as _re
        _answer_re = _re.compile(r"\banswer\s+(ha_[0-9a-f]{6,})\s+([0-9])")
        check(act is not None and _answer_re.search(f"answer {act['action_id']} 1") is not None,
              "ha action id matches DecisionHandler's answer grammar (hex-only)")
        check(act is not None and act["status"] == "pending"
              and not act.get("card_event_id")
              and "Approve" in json.dumps(act["questions"]),
              "pending ha action exists for CardPoster to sweep (card lifecycle wired)")
        # owner answers exactly as DecisionHandler writes it: option 1 = Approve
        check(store.resolve(act["action_id"], {"1": [1]}, "@owner:example.org"),
              "owner resolution lands (DecisionHandler write shape)")
        w = cli("request", "wait", r["requestId"], "--timeout", "10")
        check(w["status"] == "approved" and w["resolvedBy"] == "@owner:example.org",
              "wait returns approved with resolver identity")

        # 2. approval → deny (option 2)
        r2 = cli("approval", "request", "--action", "repo.force_push")
        act2 = pending_action_for(r2["requestId"], store)
        store.resolve(act2["action_id"], {"1": [2]}, "@owner:example.org")
        w2 = cli("request", "wait", r2["requestId"], "--timeout", "10")
        check(w2["status"] == "denied", "Deny option resolves to denied")

        # 3. elicitation single_select
        r3 = cli("elicitation", "request", "--question", "Deploy where?",
                 "--type", "single_select", "--options", '["staging","production"]')
        act3 = pending_action_for(r3["requestId"], store)
        store.resolve(act3["action_id"], {"1": [2]}, "@owner:example.org")
        w3 = cli("request", "wait", r3["requestId"], "--timeout", "10")
        check(w3["status"] == "resolved"
              and w3["result"]["answer"] == "production",
              "elicitation returns the chosen option label")

        # 3b. multi_select: multiSelect flag makes the comma grammar resolve
        r3b = cli("elicitation", "request", "--question", "Which envs?",
                  "--type", "multi_select", "--options", '["dev","staging","prod"]')
        act3b = pending_action_for(r3b["requestId"], store)
        check((act3b.get("questions") or [{}])[0].get("multiSelect") is True,
              "multi_select sets the multiSelect flag on the card")
        store.resolve(act3b["action_id"], {"1": [1, 3]}, "@owner:example.org")
        w3b = cli("request", "wait", r3b["requestId"], "--timeout", "10")
        check(w3b["status"] == "resolved" and w3b["result"]["answer"],
              "multi_select resolves with the chosen options")
        # 3c. free_text is a clean v0 rejection (dead path would strand forever)
        p3c = subprocess.run([*PYBASE, str(CLI), "elicitation", "request",
                              "--question", "Say anything", "--type", "free_text"],
                             capture_output=True, text=True, env=ENV)
        check(p3c.returncode == 1 and "not supported in v0" in p3c.stderr,
              "free_text elicitation rejected loudly in v0 (no stranded request)")

        # 4. capability.execute — REAL message.send against the mock gateway,
        #    gated by a consumed-once approval (the full acceptance chain).
        ra = cli("approval", "request", "--action", "message.send",
                 "--resource", '{"roomId":"!room:example.org"}',
                 "--input", '{"body":"hello"}',
                 "--reason", "post the summary")
        acta = pending_action_for(ra["requestId"], store)
        store.resolve(acta["action_id"], {"1": [1]}, "@owner:example.org")
        wa = cli("request", "wait", ra["requestId"], "--timeout", "10")
        check(wa["status"] == "approved", "gating approval approved")
        r4 = cli("capability", "execute", "--action", "message.send",
                 "--resource", '{"roomId":"!room:example.org"}',
                 "--input", '{"body":"hello"}',
                 "--approval", ra["requestId"])
        check(r4["status"] == "completed"
              and r4["result"]["eventId"] == "$evt-1"
              and GW["posts"][-1]["body"] == "hello"
              and GW["posts"][-1]["room_id"] == "!room:example.org",
              "capability.execute delivers via gateway, verified by event_id")
        # one-time consumption: the same approval cannot authorize a second send
        p4 = subprocess.run([*PYBASE, str(CLI), "capability", "execute",
                             "--action", "message.send",
                             "--resource", '{"roomId":"!room:example.org"}',
                             "--input", '{"body":"hello"}',
                             "--approval", ra["requestId"]],
                            capture_output=True, text=True, env=ENV)
        check(p4.returncode == 1 and "already consumed" in p4.stderr,
              "an approval authorizes exactly ONE execution (consumed)")
        # UNGATED governed action → refused BEFORE any gateway contact
        posts_before = len(GW["posts"])
        p4u = subprocess.run([*PYBASE, str(CLI), "capability", "execute",
                              "--action", "message.send",
                              "--resource", '{"roomId":"!room:example.org"}',
                              "--input", '{"body":"sneaky"}'],
                             capture_output=True, text=True, env=ENV)
        check(p4u.returncode == 1 and "governed" in p4u.stderr
              and len(GW["posts"]) == posts_before,
              "governed message.send without approval is refused pre-gateway")

        # idempotency: same key replays the recorded result — no second send,
        # no 'already consumed' failure on retry
        ra2 = cli("approval", "request", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"once"}')
        acta2 = pending_action_for(ra2["requestId"], store)
        store.resolve(acta2["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", ra2["requestId"], "--timeout", "10")
        rk1 = cli("capability", "execute", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"once"}',
                  "--approval", ra2["requestId"],
                  "--idempotency-key", "task-e2e:final")
        posts_after_first = len(GW["posts"])
        rk2 = cli("capability", "execute", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"once"}',
                  "--approval", ra2["requestId"],
                  "--idempotency-key", "task-e2e:final")
        check(rk1["status"] == "completed"
              and rk2["requestId"] == rk1["requestId"]
              and rk2.get("idempotentReplay") is True
              and len(GW["posts"]) == posts_after_first,
              "idempotency key replays the first result — no duplicate send")

        # same key + a DIFFERENT approval → replay, and the fresh approval is
        # NOT consumed (review P1: record + approval consumption are one
        # atomic step; a replayed key must never spend a second approval)
        ra3 = cli("approval", "request", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"fresh-after-replay"}')
        acta3 = pending_action_for(ra3["requestId"], store)
        store.resolve(acta3["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", ra3["requestId"], "--timeout", "10")
        rk3 = cli("capability", "execute", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"once"}',
                  "--approval", ra3["requestId"],
                  "--idempotency-key", "task-e2e:final")
        check(rk3.get("idempotentReplay") is True
              and rk3["requestId"] == rk1["requestId"]
              and len(GW["posts"]) == posts_after_first,
              "same key + different approval replays without a second send")
        rk4 = cli("capability", "execute", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"fresh-after-replay"}',
                  "--approval", ra3["requestId"],
                  "--idempotency-key", "task-e2e:fresh")
        check(rk4["status"] == "completed"
              and GW["posts"][-1]["body"] == "fresh-after-replay",
              "the replay did not consume the approval — still spendable once")

        # INPUT binding (review P1): the owner approved the card's exact
        # effect — an execute that substitutes a different input (the message
        # body!) after approval must be refused, pre-consume, pre-gateway.
        rbi = cli("approval", "request", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"benign approved body"}')
        actbi = pending_action_for(rbi["requestId"], store)
        card_bi = json.loads((Path(ENV["SUTANDO_HA_DIR"]) / (actbi["action_id"] + ".json")).read_text())
        check('"body": "benign approved body"' in card_bi["questions"][0]["question"]
              or 'benign approved body' in card_bi["questions"][0]["question"],
              "approval card SHOWS the governed input (the body the owner approves)")
        store.resolve(actbi["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", rbi["requestId"], "--timeout", "10")
        posts_bi = len(GW["posts"])
        pbi = subprocess.run([*PYBASE, str(CLI), "capability", "execute",
                              "--action", "message.send",
                              "--resource", '{"roomId":"!room:example.org"}',
                              "--input", '{"body":"SUBSTITUTED-UNSHOWN-BODY"}',
                              "--approval", rbi["requestId"]],
                             capture_output=True, text=True, env=ENV)
        check(pbi.returncode == 1 and "different resource/input" in pbi.stderr
              and len(GW["posts"]) == posts_bi,
              "substituted input after approval is refused pre-gateway")
        rbi_ok = cli("capability", "execute", "--action", "message.send",
                     "--resource", '{"roomId":"!room:example.org"}',
                     "--input", '{"body":"benign approved body"}',
                     "--approval", rbi["requestId"])
        check(rbi_ok["status"] == "completed"
              and GW["posts"][-1]["body"] == "benign approved body",
              "the refusal did not consume the approval — exact effect still executes")

        # approval BINDING: an approval for another action cannot authorize
        # message.send — and the mismatch must not consume it.
        rb = cli("approval", "request", "--action", "repo.force_push",
                 "--resource", '{"repository":"o/r"}')
        actb = pending_action_for(rb["requestId"], store)
        store.resolve(actb["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", rb["requestId"], "--timeout", "10")
        pb = subprocess.run([*PYBASE, str(CLI), "capability", "execute",
                             "--action", "message.send",
                             "--resource", '{"roomId":"!room:example.org"}',
                             "--input", '{"body":"cross"}',
                             "--approval", rb["requestId"]],
                            capture_output=True, text=True, env=ENV)
        check(pb.returncode == 1 and "authorizes action" in pb.stderr,
              "approval for another action cannot authorize message.send")
        # resource binding: same action, different resource → refused
        rb2 = cli("approval", "request", "--action", "message.send",
                  "--resource", '{"roomId":"!OTHER:example.org"}')
        actb2 = pending_action_for(rb2["requestId"], store)
        store.resolve(actb2["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", rb2["requestId"], "--timeout", "10")
        pb2 = subprocess.run([*PYBASE, str(CLI), "capability", "execute",
                              "--action", "message.send",
                              "--resource", '{"roomId":"!room:example.org"}',
                              "--input", '{"body":"cross2"}',
                              "--approval", rb2["requestId"]],
                             capture_output=True, text=True, env=ENV)
        check(pb2.returncode == 1 and "different resource" in pb2.stderr,
              "approval bound to another resource cannot authorize this send")

        # idempotency-key REUSE with a different fingerprint → rejected
        pk = subprocess.run([*PYBASE, str(CLI), "capability", "execute",
                             "--action", "message.send",
                             "--resource", '{"roomId":"!room:example.org"}',
                             "--input", '{"body":"DIFFERENT"}',
                             "--idempotency-key", "task-e2e:final"],
                            capture_output=True, text=True, env=ENV)
        check(pk.returncode == 1 and "different action/resource/input" in pk.stderr,
              "idempotency key reuse with a different fingerprint is rejected")

        # concurrency: a slow gateway send must not stall other requests
        import threading as _th
        ra4 = cli("approval", "request", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"slowpoke"}')
        acta4 = pending_action_for(ra4["requestId"], store)
        store.resolve(acta4["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", ra4["requestId"], "--timeout", "10")
        GW["slow"] = True
        slow_done = {}
        def _slow_send():
            slow_done["r"] = cli("capability", "execute", "--action", "message.send",
                                 "--resource", '{"roomId":"!room:example.org"}',
                                 "--input", '{"body":"slowpoke"}',
                                 "--approval", ra4["requestId"], timeout=30)
        t = _th.Thread(target=_slow_send)
        t.start()
        time.sleep(0.3)  # let the slow send enter the executor
        t0 = time.monotonic()
        g = cli("request", "get", ra4["requestId"])
        dt = time.monotonic() - t0
        t.join(timeout=30)
        GW["slow"] = False
        check(dt < 1.0 and g is not None
              and slow_done.get("r", {}).get("status") == "completed",
              f"slow executor does not stall other requests (get took {dt:.2f}s)")

        # swallowed-send 200 (no event_id) → failed, never falsely completed
        ra3 = cli("approval", "request", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"ghost"}')
        acta3 = pending_action_for(ra3["requestId"], store)
        store.resolve(acta3["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", ra3["requestId"], "--timeout", "10")
        GW["swallow"] = True
        r4b = cli("capability", "execute", "--action", "message.send",
                  "--resource", '{"roomId":"!room:example.org"}',
                  "--input", '{"body":"ghost"}',
                  "--approval", ra3["requestId"])
        GW["swallow"] = False
        check(r4b["status"] == "failed" and "not confirmed" in r4b["result"]["error"],
              "send without event_id fails closed (no false completed)")
        # unknown action → failed with a clear error
        r4c = cli("capability", "execute", "--action", "no.such_action")
        check(r4c["status"] == "failed" and "no executor" in r4c["result"]["error"],
              "unknown capability action fails cleanly")

        # 5. unknown id → error
        p = subprocess.run([*PYBASE, str(CLI), "request", "get", "nope-1"],
                           capture_output=True, text=True, env=ENV)
        check(p.returncode == 1 and "unknown requestId" in p.stderr,
              "unknown requestId is a clean protocol error")

        # 6. wait timeout leaves the request pending
        r6 = cli("approval", "request", "--action", "slow.thing")
        w6 = cli("request", "wait", r6["requestId"], "--timeout", "1")
        check(w6.get("timedOut") is True and w6["status"] == "pending",
              "wait timeout reports timedOut and stays pending")

        # 7. restart recovery: r6 survives and resolves after a daemon restart
        daemon.terminate()
        daemon.wait(timeout=5)
        daemon = start_daemon()
        act6 = pending_action_for(r6["requestId"], store)
        store.resolve(act6["action_id"], {"1": [1]}, "@owner:example.org")
        w7 = cli("request", "wait", r6["requestId"], "--timeout", "10")
        check(w7["status"] == "approved",
              "pending request survives daemon restart and resolves (recovery)")

        # 7b. rolling-update migration: a pre-idempotency v0 DB gains the new
        # columns on boot instead of crashing (the live acceptance created one).
        import sqlite3 as _sq
        old_db = str(Path(TMP) / "old-schema.sqlite")
        con = _sq.connect(old_db)
        con.executescript("""
CREATE TABLE runtime_requests (
  request_id TEXT PRIMARY KEY, request_type TEXT NOT NULL, task_id TEXT,
  execution_id TEXT, actor_id TEXT NOT NULL, method TEXT NOT NULL,
  params_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
  created_at REAL NOT NULL, expires_at REAL, resolved_at REAL,
  resolved_by TEXT, consumed_at REAL);
INSERT INTO runtime_requests VALUES ('approval-old1','approval','t',NULL,
  '@a:hs','approval.request','{}','pending',NULL,1,NULL,NULL,NULL,NULL);
""")
        con.commit(); con.close()
        spec_rs = importlib.util.spec_from_file_location(
            "rs", REPO / "src" / "runtime-api" / "request_store.py")
        rs = importlib.util.module_from_spec(spec_rs)
        spec_rs.loader.exec_module(rs)
        migrated = rs.RequestStore(old_db)
        rec_old = migrated.get("approval-old1")
        check(rec_old is not None and rec_old["status"] == "pending"
              and migrated.by_idempotency_key("nope") is None,
              "pre-idempotency DB migrates on boot (old rows readable, index usable)")
        migrated.close()

        # 8. cancel wins over a late answer (CAS terminal immutability)
        r8 = cli("approval", "request", "--action", "late.answer")
        act8 = pending_action_for(r8["requestId"], store)
        c8 = cli("request", "cancel", r8["requestId"])
        check(c8["status"] == "cancelled", "cancel transitions to cancelled")
        store.resolve(act8["action_id"], {"1": [1]}, "@owner:example.org")
        time.sleep(1.0)  # give the resolver loop a pass to (not) clobber
        g8 = cli("request", "get", r8["requestId"])
        check(g8["status"] == "cancelled",
              "late owner answer cannot overwrite a terminal state (CAS)")

        # 9. agent discovery through the REAL daemon + CLI (Sutando Server
        # slice 1): cores heartbeats surface as identity+liveness; a stale
        cores = Path(ENV["SUTANDO_RUNTIME_STATE"]) / "cores"
        cores.mkdir(parents=True, exist_ok=True)
        (cores / "e2e-host.alive").write_text(json.dumps(
            {"host": "e2e-host", "pid": 42, "status": "running"}))
        stale = cores / "stale-host.alive"
        stale.write_text(json.dumps({"host": "stale-host"}))
        _old = time.time() - 300
        os.utime(stale, (_old, _old))
        al = cli("agent", "list")
        by_id = {a["agentId"]: a for a in al["agents"]}
        check(by_id.get("e2e-host", {}).get("alive") is True
              and by_id.get("stale-host", {}).get("alive") is False,
              "agent list: fresh beat alive, stale beat present-but-dead")
        st9 = cli("agent", "status", "e2e-host")
        check(st9["alive"] is True and st9["pid"] == 42,
              "agent status resolves identity + heartbeat metadata via CLI")
        cli("agent", "status", "no-such-agent", expect_rc=1)
        check(True, "agent status for unknown id exits 1 (loud, not empty)")

        # 10. identity surface (sutando.*) through the real daemon + CLI.
        # The daemon booted before core-status.json existed — identity reads
        Path(ENV["SUTANDO_RUNTIME_STATE"], "core-status.json").write_text(
            json.dumps({"status": "running", "step": "e2e", "ts": 1}))
        s10 = cli("sutando", "status")
        check(s10.get("status") == "running" and s10.get("step") == "e2e",
              "sutando status reflects live core-status.json")
        i10 = cli("sutando", "info")
        check(i10.get("agentId") == "@test-agent:example.org",
              "sutando info reports the daemon-resolved actor id")
        a10 = cli("sutando", "allowlist")
        check(isinstance(a10.get("channels"), dict),
              "sutando allowlist answers with a channels map")

        # 11. task pipeline (task.*) through the real daemon + CLI: submit
        # lands a canonical task file, status tracks it, a result completes
        t11 = cli("task", "submit", "e2e: do the thing", "--priority", "low")
        tid11 = t11["taskId"]
        check(t11["state"] == "pending", "task submit returns pending")
        tf = Path(TMP) / "tasks" / f"{tid11}.txt"
        check(tf.is_file() and "access_tier: owner" in tf.read_text()
              and "task: e2e: do the thing" in tf.read_text(),
              "submit wrote a canonical owner-tier task file")
        d11 = cli("task", "details", tid11)
        check(d11["task"] == "e2e: do the thing" and d11["priority"] == "low",
              "task details round-trips through the real parser")
        Path(TMP, "results").mkdir(exist_ok=True)
        Path(TMP, "results", f"{tid11}.txt").write_text("all done")
        s11 = cli("task", "status", tid11)
        check(s11["state"] == "done", "a result file completes the task")
        r11 = cli("task", "get-result", tid11)
        check(r11["result"] == "all done", "task get-result returns the body")
        t12 = cli("task", "submit", "cancel me")
        c12 = cli("task", "cancel", t12["taskId"])
        check(c12["cancelled"] == "requested" and c12.get("cancelTaskId"),
              "cancel emits a CANCEL_INSTRUCTION signal task")

        # 13. runtime surface (runtime.*): health is the coarse end-user
        # readout (fresh e2e-host beat from section 9 + live core-status
        (cores / "e2e-host.alive").write_text(json.dumps(
            {"host": "e2e-host", "pid": 42, "socket": "/tmp/e2e-tmux.sock"}))
        h13 = cli("runtime", "health")
        check(h13["state"] == "online" and h13.get("currentActivity") == "e2e",
              "runtime health: online + current activity from core-status")
        d13 = cli("runtime", "details")
        check(d13.get("pid") == 42 and d13.get("socket") == "/tmp/e2e-tmux.sock"
              and d13.get("runtimeSocket", "").endswith("rt.sock"),
              "runtime details: pid + tmux socket + daemon runtime socket")
        i13 = cli("sutando", "info")
        check("pid" not in i13 and "socket" not in i13
              and "runtimeSocket" not in i13,
              "sutando info no longer leaks runtime internals")

        # 14. human_action.* (third HITL type) through the real daemon + CLI:
        # request mirrors a Done/Decline card; the owner's card answer
        h14 = cli("human-action", "request", "--action", "Sign the e2e form",
                  "--instructions", "Review it first")
        act14 = pending_action_for(h14["requestId"], store)
        check(act14 is not None
              and "Sign the e2e form" in json.dumps(act14["questions"])
              and [o["label"] for o in act14["questions"][0]["options"]] == ["Done", "Decline"],
              "human_action card carries the act + Done/Decline options")
        store.resolve(act14["action_id"], {"1": [1]}, "@owner:example.org")
        w14 = cli("request", "wait", h14["requestId"], "--timeout", "10")
        check(w14["status"] == "completed" and w14["resolvedBy"] == "@owner:example.org",
              "owner card answer Done resolves the request to completed")
        # Live negative control over the REAL socket: the plain Unix client
        # that raised the request must not be able to settle it (review P1).
        h15 = cli("human-action", "request", "--action", "Plug in the drive")
        cli("human-action", "complete", h15["requestId"], "--note", "done irl",
            expect_rc=1)
        s15 = cli("human-action", "status", h15["requestId"])
        check(s15["status"] == "pending",
              "ungranted CLI complete leaves the durable row pending")
        act15 = pending_action_for(h15["requestId"], store)
        check(act15 is not None and act15["status"] == "pending",
              "ungranted CLI complete leaves the card open for the human")
        store.resolve(act15["action_id"], {"1": [1]}, "@owner:example.org")
        w15 = cli("request", "wait", h15["requestId"], "--timeout", "10")
        check(w15["status"] == "completed",
              "the human's own card answer still settles the action")

        # 15. task waiting_for_* weave: a live task with a pending HITL
        # request is parked in its waiting state; resolving the request
        t16 = cli("task", "submit", "e2e: needs a signature")
        tid16 = t16["taskId"]
        h16 = cli("human-action", "request", "--action", "Sign it",
                  "--task-id", tid16)
        st16 = cli("task", "status", tid16)
        check(st16["state"] == "waiting_for_human_action"
              and st16["waitingOn"] == ["waiting_for_human_action"],
              "pending human_action parks the task in waiting_for_human_action")
        act16 = pending_action_for(h16["requestId"], store)
        store.resolve(act16["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", h16["requestId"], "--timeout", "10")
        st16b = cli("task", "status", tid16)
        check(st16b["state"] == "pending",
              "resolving the request returns the task to the normal lifecycle")

        # 15b. enumeration (acceptance-test gap 1): a client with NO known
        # ids lists live tasks and pending human requests.
        tl = cli("task", "list")
        ids15 = [t["taskId"] for t in tl["tasks"]]
        check(tid16 in ids15 and t12["taskId"] in ids15,
              "task list enumerates live tasks without prior ids")
        h15b = cli("human-action", "request", "--action", "List me")
        rl = cli("request", "list")
        rl_ids = [r["requestId"] for r in rl["requests"]]
        check(h15b["requestId"] in rl_ids
              and any(r.get("action") == "List me" for r in rl["requests"]),
              "request list enumerates pending human requests with summaries")
        act15b = pending_action_for(h15b["requestId"], store)
        store.resolve(act15b["action_id"], {"1": [1]}, "@owner:example.org")
        cli("request", "wait", h15b["requestId"], "--timeout", "10")
        rl2 = cli("request", "list")
        check(h15b["requestId"] not in [r["requestId"] for r in rl2["requests"]],
              "resolved requests leave the pending list")

        # 16. instance manifest registry (M1): the daemon registered itself at
        # boot; file-based discovery answers through the CLI; the manifest is
        l17 = cli("instance", "list")
        inst = [m for m in l17["instances"]
                if m.get("identity", {}).get("agent_id") == "@test-agent:example.org"]
        check(len(inst) == 1 and inst[0]["status"] == "running"
              and inst[0]["endpoint"]["path"].endswith("rt.sock"),
              "daemon wrote its instance manifest at boot (status running)")
        mtext = Path(inst[0]["_file"]).read_text().lower()
        check(all(n not in mtext for n in ("token", "secret", "password")),
              "manifest carries no secrets")
        rt18 = inst[0].get("runtime", {})
        check(rt18.get("tmux_socket") == "/tmp/e2e-tmux.sock"
              and rt18.get("session") == "e2e-core",
              "manifest records the tmux attach coords (socket + session)")
        at18 = subprocess.run(
            [*PYBASE, str(CLI), "instance", "attach",
             "@test-agent:example.org", "--print"],
            capture_output=True, text=True, env=ENV)
        check(at18.stdout.strip() ==
              "tmux -S /tmp/e2e-tmux.sock attach-session -t e2e-core",
              "attach resolves the tmux argv from the manifest")
        check(inst[0].get("launcher", {}).get("args") == ["serve"]
              and inst[0]["launcher"]["executable"].endswith("bin/sutando"),
              "manifest carries a structured launcher (no shell strings)")

        # 16b. same-instance double start is refused by the instance lock
        # (different instances may run in parallel; this one may not fork).
        dup = subprocess.run(
            [sys.executable, str(REPO / "src" / "runtime-api" / "server.py")],
            env=ENV, capture_output=True, text=True, timeout=15)
        check(dup.returncode != 0 and "refusing double start" in
              (dup.stderr + dup.stdout),
              "second server for the SAME instance exits loudly (lock held)")
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
    # 17. clean shutdown (SIGTERM) marks the manifest stopped — discovery
    # still lists the instance with the daemon fully down.
    _mf = Path(TMP) / "instances"
    _stopped = [json.loads(f.read_text()) for f in _mf.glob("*.json")
                if "test-agent" in f.name]
    check(len(_stopped) == 1 and _stopped[0]["status"] == "stopped",
          "SIGTERM shutdown marked the instance manifest stopped")

    # 18. the start verb: with the daemon fully down, `instance start` reads
    # the manifest, execs the recorded launcher and waits for the endpoint —
    st18 = cli("instance", "start", "@test-agent:example.org")
    check(st18.get("ok") is True and st18.get("state") == "started",
          "start verb boots a stopped instance via its manifest launcher")
    i18 = cli("sutando", "info")
    check(i18.get("agentId") == "@test-agent:example.org",
          "restarted instance answers with the same identity")
    st18b = cli("instance", "start", "@test-agent:example.org")
    check(st18b.get("state") == "already_running",
          "start verb is idempotent on a live instance (attachable, not just socket)")
    # the started instance must be ATTACHABLE: identity verified over its socket
    i18b = cli("sutando", "info")
    check(i18b.get("agentId") == "@test-agent:example.org",
          "started instance is attachable — identity verified over its socket")
    import signal as _signal
    os.kill(st18["pid"], _signal.SIGTERM)
    time.sleep(1.5)

    print(f"\n{'PASS — runtime-api v0 E2E green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
