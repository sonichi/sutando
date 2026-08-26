#!/usr/bin/env python3
"""In-process suite for the runtime-api daemon internals (coverage-visible).

The E2E suite (tests/runtime-api-e2e.test.py) proves behavior through the REAL
daemon — but as a subprocess the coverage recorder cannot see, so server.py /
protocol.py / ha_adapter.py read as uncovered (the same gap the store suite
closed for request_store.py). This suite drives the SAME contracts in-process:
RuntimeServer.handle() end to end (issue/elicit/capability/get/wait/cancel,
governed gating, binding, idempotency incl. the duplicate-key race branch),
the settle/resolver mapping, the Unix-socket serve() loop with a live client
(frame errors, oversized frames, stale-socket takeover), main() wiring, the
protocol frame codec, and the ha adapter's mapping + resolution branches.

Run: python3 tests/runtime-api-server.test.py   (stdlib only)
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

_spec = importlib.util.spec_from_file_location(
    "rt_server", REPO / "src" / "runtime-api" / "server.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)
# EXECUTORS moved to dispatcher.py with the request-domain layer. It is the same
# dict object the dispatcher holds (constructor default), so mutating it here
# still injects test executors into a live RuntimeServer.
import dispatcher as rt_dispatcher  # noqa: E402

import protocol as proto  # noqa: E402
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run(coro):
    return asyncio.run(coro)


def _srv(tmp: Path) -> "rt.RuntimeServer":
    return rt.RuntimeServer(socket_path=str(tmp / "s.sock"),
                            db_path=str(tmp / "s.sqlite"),
                            ha_dir=str(tmp / "ha"))


def _resolve_ha(srv, request_id, answers):
    aid = ha_action_id(request_id)
    srv.ha.store.resolve(aid, answers, "@owner:hs")


def main() -> int:  # noqa: PLR0915 — one linear conformance script
    tmp = Path(tempfile.mkdtemp(prefix="rt-srv-"))
    # instance lock + run dir must not collide with a live daemon's default
    os.environ["SUTANDO_RUN_DIR"] = str(tmp / "run")
    srv = _srv(tmp)

    # ── protocol codec ──────────────────────────────────────────────────────
    rid, method, params = proto.parse_line(
        b'{"jsonrpc":"2.0","id":"x1","method":"request.get","params":{"a":1}}')
    check((rid, method, params) == ("x1", "request.get", {"a": 1}),
          "protocol: valid frame parses")
    for raw, code in ((b"\xff\xfe", -32700),
                      (b'"not an object"', -32600),
                      (b'{"jsonrpc":"1.0","method":"request.get"}', -32600),
                      (b'{"jsonrpc":"2.0","method":"nope.method"}', -32601),
                      (b'{"jsonrpc":"2.0","method":"request.get","params":[1]}', -32602),
                      (b"x" * (proto.MAX_LINE_BYTES + 1), -32600)):
        try:
            proto.parse_line(raw)
            check(False, f"protocol: {code} raised")
        except proto.ProtocolError as e:
            check(e.code == code, f"protocol: bad frame → {code}")
    out = json.loads(proto.result_frame("i", {"ok": 1}).decode())
    err = json.loads(proto.error_frame("i", -32000, "boom").decode())
    check(out["result"] == {"ok": 1} and err["error"]["code"] == -32000,
          "protocol: result/error frames encode")

    # ── issue + elicitation validation ──────────────────────────────────────
    ra = run(srv.dispatcher.handle("approval.request",
                        {"action": "message.send",
                         "resource": {"roomId": "!r:hs"}, "reason": "why"}))
    check(ra["status"] == "pending", "approval.request issues pending")
    ha_file = tmp / "ha" / (ha_action_id(ra["requestId"]) + ".json")
    rec = json.loads(ha_file.read_text())
    check("Approve: message.send" in rec["questions"][0]["question"]
          and "Reason: why" in rec["questions"][0]["question"],
          "ha mirror carries action + reason in the card question")
    for bad_params, frag in (
            ({"question": "q", "type": "bogus"}, "type must be"),
            ({"question": "q", "type": "free_text"}, "not supported in v0"),
            ({"question": "q", "type": "single_select"}, "requires options"),
            ({"type": "confirmation"}, "missing required param")):
        try:
            run(srv.dispatcher.handle("elicitation.request", bad_params))
            check(False, f"elicitation rejects: {frag}")
        except rt.ProtocolError as e:
            check(frag in e.message, f"elicitation rejects: {frag}")
    re1 = run(srv.dispatcher.handle("elicitation.request",
                         {"question": "Which?", "type": "multi_select",
                          "options": ["a", "b", "c"]}))
    rec1 = json.loads((tmp / "ha" / (ha_action_id(re1["requestId"]) + ".json")).read_text())
    check(rec1["questions"][0].get("multiSelect") is True,
          "multi_select sets the multiSelect card flag")
    try:
        run(srv.dispatcher.handle("no.such", {}))
        check(False, "unknown method rejected")
    except rt.ProtocolError as e:
        check(e.code == -32601, "unknown method rejected")

    # ── capability gating + binding + idempotency ───────────────────────────
    sent: list = []

    def ok_exec(p):
        sent.append(p)
        return {"executed": True, "eventId": f"$e{len(sent)}"}

    rt_dispatcher.EXECUTORS["test.echo"] = ok_exec
    rt_dispatcher.GOVERNED_ACTIONS = frozenset({"test.echo"})
    try:
        for p, frag in (({}, "missing required param"),
                        ({"action": "test.echo"}, "is governed"),
                        ({"action": "test.echo", "approvalRequestId": "nope-1"},
                         "unknown approval"),
                        ({"action": "test.echo",
                          "approvalRequestId": ra["requestId"]}, "not approved")):
            try:
                run(srv.dispatcher.handle("capability.execute", p))
                check(False, f"capability rejects: {frag}")
            except rt.ProtocolError as e:
                check(frag in e.message, f"capability rejects: {frag}")

        # approve via the ha lifecycle → settle → approved (action matches
        # the governed test executor so binding passes on the happy path)
        rb = run(srv.dispatcher.handle("approval.request",
                            {"action": "test.echo",
                             "resource": {"roomId": "!r:hs"},
                             "input": {"body": "hi"}}))
        _resolve_ha(srv, rb["requestId"], {"1": [1]})
        w = run(srv.dispatcher.handle("request.wait", {"requestId": rb["requestId"],
                                            "timeoutS": 5}))
        check(w["status"] == "approved" and w["resolvedBy"] == "@owner:hs",
              "wait settles ha Approve → approved(by owner)")

        # binding: wrong action / wrong resource
        for p, frag in (({"action": "other.act",
                          "approvalRequestId": rb["requestId"]}, "authorizes action"),
                        ({"action": "test.echo", "resource": {"roomId": "!other"},
                          "approvalRequestId": rb["requestId"]}, "different resource")):
            try:
                run(srv.dispatcher.handle("capability.execute", p))
                check(False, f"binding rejects: {frag}")
            except rt.ProtocolError as e:
                check(frag in e.message, f"binding rejects: {frag}")

        good = {"action": "test.echo", "resource": {"roomId": "!r:hs"},
                "input": {"body": "hi"}, "idempotencyKey": "k1"}
        r1 = run(srv.dispatcher.handle("capability.execute",
                            {**good, "approvalRequestId": rb["requestId"]}))
        check(r1["status"] == "completed" and r1["result"]["eventId"] == "$e1",
              "governed execute completes via executor")
        r2 = run(srv.dispatcher.handle("capability.execute",
                            {**good, "approvalRequestId": rb["requestId"]}))
        check(r2.get("idempotentReplay") is True and len(sent) == 1,
              "idempotency key replays without re-executing")
        try:
            run(srv.dispatcher.handle("capability.execute",
                           {**good, "input": {"body": "DIFFERENT"}}))
            check(False, "fingerprint mismatch rejected")
        except rt.ProtocolError as e:
            check("different action/resource/input" in e.message,
                  "fingerprint mismatch rejected")
        try:
            run(srv.dispatcher.handle("capability.execute",
                           {**good, "idempotencyKey": "k2",
                            "approvalRequestId": rb["requestId"]}))
            check(False, "consumed approval rejected")
        except rt.ProtocolError as e:
            check("already consumed" in e.message, "consumed approval rejected")

        # duplicate-key RACE branch: replay check misses, unique index wins
        ra2 = run(srv.dispatcher.handle("approval.request",
                             {"action": "test.echo",
                              "resource": {"roomId": "!r:hs"},
                              "input": {"body": "hi"}}))
        _resolve_ha(srv, ra2["requestId"], {"1": [1]})
        run(srv.dispatcher.handle("request.wait", {"requestId": ra2["requestId"], "timeoutS": 5}))
        real_lookup = srv.store.by_idempotency_key
        calls = {"n": 0}

        def racey(key):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_lookup(key)

        srv.store.by_idempotency_key = racey
        rr = run(srv.dispatcher.handle("capability.execute",
                            {**good, "approvalRequestId": ra2["requestId"]}))
        srv.store.by_idempotency_key = real_lookup
        a2 = srv.store.get(ra2["requestId"])
        check(rr.get("idempotentReplay") is True and a2["consumedAt"] is None,
              "same-key race replays winner; loser's approval NOT consumed")

        # ungoverned + executor failure + unknown action
        rt_dispatcher.GOVERNED_ACTIONS = frozenset()

        def boom(_p):
            raise RuntimeError("exec boom")

        rt_dispatcher.EXECUTORS["test.boom"] = boom
        rf = run(srv.dispatcher.handle("capability.execute", {"action": "test.boom"}))
        check(rf["status"] == "failed" and "exec boom" in rf["result"]["error"],
              "executor exception → failed request")
        ru = run(srv.dispatcher.handle("capability.execute", {"action": "no.executor"}))
        check(ru["status"] == "failed" and "no executor" in ru["result"]["error"],
              "unknown action → clean failed request")
    finally:
        rt_dispatcher.EXECUTORS.pop("test.echo", None)
        rt_dispatcher.EXECUTORS.pop("test.boom", None)
        rt_dispatcher.GOVERNED_ACTIONS = frozenset({"message.send"})

    # ── settle mapping: deny + elicitation answer + expiry + cancel ─────────
    rd = run(srv.dispatcher.handle("approval.request", {"action": "x.y"}))
    _resolve_ha(srv, rd["requestId"], {"1": [2]})  # option 2 = Deny
    wd = run(srv.dispatcher.handle("request.wait", {"requestId": rd["requestId"], "timeoutS": 5}))
    check(wd["status"] == "denied", "ha Deny → denied")
    re2 = run(srv.dispatcher.handle("elicitation.request",
                         {"question": "Pick", "options": ["red", "blue"]}))
    _resolve_ha(srv, re2["requestId"], {"Pick": "blue"})
    we = run(srv.dispatcher.handle("request.wait", {"requestId": re2["requestId"], "timeoutS": 5}))
    check(we["status"] == "resolved" and we["result"]["answer"] == "blue",
          "elicitation answer maps to resolved(answer)")
    rw = run(srv.dispatcher.handle("approval.request", {"action": "x.y"}))
    wt = run(srv.dispatcher.handle("request.wait", {"requestId": rw["requestId"], "timeoutS": 0.6}))
    check(wt.get("timedOut") is True and wt["status"] == "pending",
          "wait timeout reports timedOut, stays pending")
    rc = run(srv.dispatcher.handle("request.cancel", {"requestId": rw["requestId"]}))
    check(rc["status"] == "cancelled", "cancel transitions")
    try:
        run(srv.dispatcher.handle("request.get", {"requestId": "nope"}))
        check(False, "unknown requestId rejected")
    except rt.ProtocolError:
        check(True, "unknown requestId rejected")
    # expiry through the ha mirror
    rx = run(srv.dispatcher.handle("approval.request", {"action": "x.y"}))
    xf = tmp / "ha" / (ha_action_id(rx["requestId"]) + ".json")
    xr = json.loads(xf.read_text())
    xr["expires_at"] = time.time() - 5
    xf.write_text(json.dumps(xr))
    srv.dispatcher._settle(rx["requestId"])
    check(srv.store.get(rx["requestId"])["status"] == "expired",
          "expired ha action settles the request expired")

    # recover() re-links pending approvals/elicitations after a restart
    srv2 = _srv(tmp)
    srv2.dispatcher.recover()
    check(any(k for k in srv2.dispatcher._ha_of), "recover() re-links pending requests")

    # ── serve(): live unix socket, frame errors, stale-socket takeover ──────
    async def drive_socket():
        # short OWNED dir (AF_UNIX 104-char path cap; serve() chmods the
        # socket's parent, so it must belong to us — /tmp itself does not)
        sdir = tempfile.mkdtemp(prefix="rt", dir="/tmp")
        sock = f"{sdir}/s.sock"
        Path(sock).touch()  # stale non-socket file → takeover path
        s3 = rt.RuntimeServer(socket_path=sock, db_path=str(tmp / "s3.sqlite"),
                              ha_dir=str(tmp / "ha3"))
        task = asyncio.ensure_future(s3.serve())
        await asyncio.sleep(0.3)
        r, w = await asyncio.open_unix_connection(sock)
        w.write(b'{"jsonrpc":"2.0","id":"c1","method":"approval.request",'
                b'"params":{"action":"a.b"}}\n')
        await w.drain()
        ok = json.loads((await r.readline()).decode())
        w.write(b'not json\n')
        await w.drain()
        bad = json.loads((await r.readline()).decode())
        w.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        Path(sock).unlink(missing_ok=True)
        return ok, bad

    ok_f, bad_f = run(drive_socket())
    check(ok_f.get("result", {}).get("status") == "pending",
          "serve(): live socket round trip issues a request")
    check(bad_f.get("error", {}).get("code") == -32700,
          "serve(): malformed frame answers a parse error")

    # ── negative control: an ordinary Unix client cannot approve its own ────
    # governed action (P1 — the requester must never be its own approver)
    async def drive_self_approval():
        sdir = tempfile.mkdtemp(prefix="rt", dir="/tmp")
        sock = f"{sdir}/g.sock"
        side_effects = []
        s6 = rt.RuntimeServer(socket_path=sock, db_path=str(tmp / "s6.sqlite"),
                              ha_dir=str(tmp / "ha6"))
        s6.dispatcher.executors = {
            "message.send": lambda p: side_effects.append("SEND") or {"executed": True}}
        task = asyncio.ensure_future(s6.serve())
        await asyncio.sleep(0.3)
        r, w = await asyncio.open_unix_connection(sock)

        async def rpc(i, method, params):
            w.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                 "params": params}) + "\n").encode())
            await w.drain()
            return json.loads((await r.readline()).decode())

        req = await rpc("g1", "approval.request", {"action": "message.send"})
        rid = req["result"]["requestId"]
        resp = await rpc("g2", "approval.respond",
                         {"requestId": rid, "decision": "approve"})
        execu = await rpc("g3", "capability.execute",
                          {"action": "message.send",
                           "resource": {"roomId": "!r:x"},
                           "input": {"body": "hi"},
                           "approvalRequestId": rid})
        row = s6.store.get(rid)
        w.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        Path(sock).unlink(missing_ok=True)
        return resp, execu, row, side_effects

    resp_f, exec_f, row_f, effects = run(drive_self_approval())
    check(resp_f.get("error", {}).get("code") == -32601
          and "grant" in resp_f.get("error", {}).get("message", ""),
          "unix client: approval.respond is not callable (no grant)")
    check(row_f["status"] == "pending",
          "unix client: the approval remains pending after the respond attempt")
    check("error" in exec_f,
          "unix client: governed execute without approval is refused")
    check(effects == [],
          "unix client: the executor is never called (no side effect)")

    # main() wiring (env → construction), run patched out
    os.environ["SUTANDO_RUNTIME_SOCKET"] = str(tmp / "m.sock")
    os.environ["SUTANDO_RUNTIME_DB"] = str(tmp / "m.sqlite")
    os.environ["SUTANDO_HA_DIR"] = str(tmp / "ha-m")
    real_run = rt.asyncio.run
    rt.asyncio.run = lambda *_a, **_k: None
    try:
        rt.main()
        check(True, "main() constructs from env (run patched out)")
    finally:
        rt.asyncio.run = real_run
        for k in ("SUTANDO_RUNTIME_SOCKET", "SUTANDO_RUNTIME_DB", "SUTANDO_HA_DIR"):
            os.environ.pop(k, None)

    # ── ha_adapter remaining branches ───────────────────────────────────────
    ad = HumanActionAdapter(str(tmp / "ha4"))
    aid = ad.open_elicitation({"requestId": "elicitation-cafe01234567",
                               "params": {"type": "confirmation", "question": "Go?"}})
    got = ad.store.get(aid)
    check([o["label"] for o in got["questions"][0]["options"]] == ["Yes", "No"],
          "confirmation without options defaults Yes/No")
    check(ad.poll_resolution("ha_missing000000") is None,
          "poll: unknown action → None")
    check(ad.poll_resolution(aid) is None, "poll: pending → None")
    rec = ad.store.get(aid)
    rec["expires_at"] = time.time() - 1
    ad.store.update(rec)
    check(ad.poll_resolution(aid)[0] == "expired", "poll: past deadline → expired")
    opts = [{"label": "A"}, {"label": "B"}]
    check(HumanActionAdapter.first_answer({"1": [2, 99]}, opts) == ["B"],
          "first_answer: index list maps labels, bad index skipped")
    check(HumanActionAdapter.first_answer({"q": "text"}, opts) == "text",
          "first_answer: non-'1' key falls back to first value")

    # ── _exec_message_send against an in-process HTTP stub ──────────────────
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits: list = []

    class GW(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            hits.append(json.loads(self.rfile.read(n).decode()))
            body = (b"{}" if len(hits) > 1
                    else b'{"ok": true, "event_id": "$sent-1"}')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    gw = HTTPServer(("127.0.0.1", 0), GW)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    os.environ["REMOTE_TASK_URL"] = f"http://127.0.0.1:{gw.server_address[1]}"
    os.environ["REMOTE_TASK_TOKEN"] = "test-bearer-not-real"
    try:
        good_send = {"resource": {"roomId": "!r:hs"}, "input": {"body": "hi"}}
        out = rt_dispatcher._exec_message_send(good_send)
        check(out == {"executed": True, "eventId": "$sent-1", "roomId": "!r:hs"}
              and hits[0]["op"] == "message",
              "_exec_message_send delivers and returns the event id")
        try:
            rt_dispatcher._exec_message_send(good_send)  # stub now answers 200 w/o event_id
            check(False, "swallowed 200 fails closed")
        except RuntimeError as e:
            check("not confirmed" in str(e), "swallowed 200 fails closed")
        try:
            rt_dispatcher._exec_message_send({"resource": {}, "input": {}})
            check(False, "missing room/body rejected")
        except RuntimeError as e:
            check("needs resource.roomId" in str(e), "missing room/body rejected")
        os.environ.pop("REMOTE_TASK_TOKEN")
        try:
            rt_dispatcher._exec_message_send(good_send)
            check(False, "unconfigured gateway rejected")
        except RuntimeError as e:
            check("not configured" in str(e), "unconfigured gateway rejected")
    finally:
        gw.shutdown()
        os.environ.pop("REMOTE_TASK_URL", None)
        os.environ.pop("REMOTE_TASK_TOKEN", None)

    # _state_dir explicit override branch
    os.environ["SUTANDO_RUNTIME_STATE"] = "/tmp/rt-state-x"
    check(rt._state_dir() == Path("/tmp/rt-state-x"),
          "_state_dir honors SUTANDO_RUNTIME_STATE")
    os.environ.pop("SUTANDO_RUNTIME_STATE")

    # request.get success path
    g = run(srv.dispatcher.handle("request.get", {"requestId": rd["requestId"]}))
    check(g["status"] == "denied", "request.get returns the public record")

    # duplicate-key race where NO winner row exists → original error re-raised
    import sqlite3 as _sq
    dup = {"action": "x.y", "idempotencyKey": "k1"}  # k1 already used above
    real2 = srv.store.by_idempotency_key
    srv.store.by_idempotency_key = lambda _k: None
    try:
        run(srv.dispatcher.handle("capability.execute", dup))
        check(False, "raceless duplicate key re-raises")
    except _sq.IntegrityError:
        check(True, "raceless duplicate key re-raises")
    finally:
        srv.store.by_idempotency_key = real2

    # _settle early-outs: terminal request; pending with no ha mapping
    srv.dispatcher._settle(rd["requestId"])  # already denied — returns at the top
    check(srv.store.get(rd["requestId"])["status"] == "denied",
          "_settle no-ops on a terminal request")
    srv3 = _srv(tmp)  # same DB, NO recover() → pending rows without ha mapping
    pend = [r for r in srv3.store.pending()][:1]
    if pend:
        srv3.dispatcher._settle(pend[0]["requestId"])
        check(srv3.store.get(pend[0]["requestId"])["status"] == "pending",
              "_settle without an ha mapping leaves the request pending")

    # single_select index-shaped answer flattens to one label
    rs = run(srv.dispatcher.handle("elicitation.request",
                        {"question": "One?", "type": "single_select",
                         "options": ["left", "right"]}))
    _resolve_ha(srv, rs["requestId"], {"1": [2]})
    ws2 = run(srv.dispatcher.handle("request.wait", {"requestId": rs["requestId"], "timeoutS": 5}))
    check(ws2["status"] == "resolved" and ws2["result"]["answer"] == "right",
          "single_select index answer flattens to its label")

    # resolver_loop: settles pending requests; isolates a store error
    async def drive_resolver():
        rr = await srv.dispatcher.handle("approval.request", {"action": "loop.test"})
        real_pending = srv.store.pending
        state = {"raised": False}

        def flaky():
            if not state["raised"]:
                state["raised"] = True
                raise RuntimeError("transient store error")
            return real_pending()

        srv.store.pending = flaky
        task = asyncio.ensure_future(srv.dispatcher.resolver_loop())
        await asyncio.sleep(0.05)
        _resolve_ha(srv, rr["requestId"], {"1": [1]})
        for _ in range(80):
            if srv.store.get(rr["requestId"])["status"] != "pending":
                break
            await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        srv.store.pending = real_pending
        return srv.store.get(rr["requestId"])["status"], state["raised"]

    old_poll = rt_dispatcher.RESOLVER_POLL_S
    rt_dispatcher.RESOLVER_POLL_S = 0.05
    status, raised = run(drive_resolver())
    rt_dispatcher.RESOLVER_POLL_S = old_poll
    check(status == "approved" and raised,
          "resolver_loop survives a store error and settles the approval")

    # serve(): oversized frame closes the connection; generic handler error
    # answers -32000; a second daemon on a LIVE socket refuses to start
    async def drive_socket_2():
        sdir = tempfile.mkdtemp(prefix="rt", dir="/tmp")
        sock = f"{sdir}/s.sock"
        s4 = rt.RuntimeServer(socket_path=sock, db_path=str(tmp / "s4.sqlite"),
                              ha_dir=str(tmp / "ha5"))
        task = asyncio.ensure_future(s4.serve())
        await asyncio.sleep(0.3)
        # generic handler error: timeoutS float() blows up inside _wait
        r, w = await asyncio.open_unix_connection(sock)
        w.write(b'{"jsonrpc":"2.0","id":"g1","method":"request.wait",'
                b'"params":{"requestId":"nope","timeoutS":1}}\n')
        await w.drain()
        notfound = json.loads((await r.readline()).decode())
        ra4 = json.loads((await _rpc(r, w, "approval.request", {"action": "a"})).decode())
        bad_wait = json.loads((await _rpc(
            r, w, "request.wait",
            {"requestId": ra4["result"]["requestId"], "timeoutS": "NaNs"})).decode())
        # oversized frame → server closes the connection
        w.write(b"x" * (rt.MAX_LINE_BYTES + 2048))
        await w.drain()
        w.close()
        # second daemon on the LIVE socket must refuse (SystemExit)
        s5 = rt.RuntimeServer(socket_path=sock, db_path=str(tmp / "s5.sqlite"),
                              ha_dir=str(tmp / "ha6"))
        refused = False
        try:
            await s5.serve()
        except SystemExit:
            refused = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return notfound, bad_wait, refused

    async def _rpc(r, w, method, params):
        w.write((json.dumps({"jsonrpc": "2.0", "id": "i", "method": method,
                             "params": params}) + "\n").encode())
        await w.drain()
        return await r.readline()

    nf, bw, refused = run(drive_socket_2())
    check(nf.get("error", {}).get("code") == -32602,
          "serve(): protocol error from handle() answers as error frame")
    check(bw.get("error", {}).get("code") == -32000,
          "serve(): non-protocol handler crash answers -32000, daemon lives")
    check(refused, "serve(): live-socket probe refuses a second daemon")

    # main(): KeyboardInterrupt exits quietly
    os.environ["SUTANDO_RUNTIME_SOCKET"] = str(tmp / "m2.sock")
    os.environ["SUTANDO_RUNTIME_DB"] = str(tmp / "m2.sqlite")
    os.environ["SUTANDO_HA_DIR"] = str(tmp / "ha-m2")
    real_run2 = rt.asyncio.run

    def kb(*_a, **_k):
        raise KeyboardInterrupt

    rt.asyncio.run = kb
    try:
        rt.main()
        check(True, "main() swallows KeyboardInterrupt (clean ^C)")
    finally:
        rt.asyncio.run = real_run2
        for k in ("SUTANDO_RUNTIME_SOCKET", "SUTANDO_RUNTIME_DB", "SUTANDO_HA_DIR"):
            os.environ.pop(k, None)

    # ── INPUT binding (review P1): approval binds the exact effect ──────────
    rt_dispatcher.EXECUTORS["test.echo"] = lambda p2: {"executed": True, "eventId": "$x"}
    rt_dispatcher.GOVERNED_ACTIONS = frozenset({"test.echo"})
    try:
        rbi = run(srv.dispatcher.handle("approval.request",
                             {"action": "test.echo",
                              "resource": {"roomId": "!r:hs"},
                              "input": {"body": "benign"}}))
        card = json.loads((tmp / "ha" / (ha_action_id(rbi["requestId"]) + ".json"))
                          .read_text())
        check("benign" in card["questions"][0]["question"],
              "approval card shows the governed input")
        _resolve_ha(srv, rbi["requestId"], {"1": [1]})
        run(srv.dispatcher.handle("request.wait", {"requestId": rbi["requestId"], "timeoutS": 5}))
        try:
            run(srv.dispatcher.handle("capability.execute",
                           {"action": "test.echo", "resource": {"roomId": "!r:hs"},
                            "input": {"body": "SUBSTITUTED"},
                            "approvalRequestId": rbi["requestId"]}))
            check(False, "substituted input rejected")
        except rt.ProtocolError as e:
            check("different resource/input" in e.message, "substituted input rejected")
        rok = run(srv.dispatcher.handle("capability.execute",
                             {"action": "test.echo", "resource": {"roomId": "!r:hs"},
                              "input": {"body": "benign"},
                              "approvalRequestId": rbi["requestId"]}))
        check(rok["status"] == "completed",
              "refusal did not consume — the exact approved effect executes")
    finally:
        rt_dispatcher.EXECUTORS.pop("test.echo", None)
        rt_dispatcher.GOVERNED_ACTIONS = frozenset({"message.send"})

    # ── restart-strand (review P1): consumed-approval capability row must not
    # stay pending forever after a daemon restart ───────────────────────────
    apx = run(srv.dispatcher.handle("approval.request", {"action": "x.y"}))
    _resolve_ha(srv, apx["requestId"], {"1": [1]})
    run(srv.dispatcher.handle("request.wait", {"requestId": apx["requestId"], "timeoutS": 5}))
    strand = srv.store.create_consuming(apx["requestId"], "capability",
                                        "capability.execute", "@a:hs",
                                        {"action": "x.y"})
    check(strand["status"] == "pending"
          and srv.store.get(apx["requestId"])["consumedAt"] is not None,
          "simulated crash window: row pending, approval consumed")
    srv_r = _srv(tmp)
    srv_r.dispatcher.recover()
    got_r = srv_r.store.get(strand["requestId"])
    check(got_r["status"] == "failed"
          and "interrupted by daemon restart" in (got_r["result"] or {}).get("error", "")
          and got_r["resolvedBy"] == "daemon-recovery",
          "recover() fails the stranded row honestly (no silent replay)")
    check((got_r["result"] or {}).get("outcome") == "unknown"
          and "executed" not in (got_r["result"] or {}),
          "recovery asserts outcome UNKNOWN — never executed:false (a crash "
          "may be post-send; false would invite a duplicate retry)")
    check(srv_r.store.get(apx["requestId"])["consumedAt"] is not None,
          "the spent approval stays spent — retry requires a fresh approval")
    wr = run(srv_r.dispatcher.handle("request.wait", {"requestId": strand["requestId"],
                                           "timeoutS": 2}))
    check(wr["status"] == "failed" and wr.get("timedOut") is None,
          "wait on the recovered row returns failed, not a pending timeout")

    print(f"\n{'PASS — in-process daemon suite green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
