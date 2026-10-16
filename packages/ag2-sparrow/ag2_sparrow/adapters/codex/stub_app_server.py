#!/usr/bin/env python3
"""Test double for `codex app-server --listen stdio://`: the same JSON-RPC shapes, no model.

Echoes the first line of each turn's input as the agentMessage. Markers in the input
drive failure modes: `[[approval]]` sends an `item/commandExecution/requestApproval`
request first and echoes the decision; `[[stall]]` never completes until
`turn/interrupt`; `[[stall-hard]]` ignores the interrupt too.
"""
import json
import sys
import time
import uuid

THREADS = {}
REQ_ID = 1000
PENDING = {}   # request id -> (thread id, turn id, item id, reply text)
STALLED = {}   # turn id -> (thread id, hard)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def thread_obj(tid):
    return {"id": tid, "status": {"type": "idle"}, "cwd": THREADS[tid]["cwd"],
            "name": None, "canAcceptDirectInput": True, "cliVersion": "stub"}


def finish_turn(tid, turn_id, item_id, reply):
    # Noise a real server also emits: another thread's event and a non-message item.
    send({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": "other-thread", "turn": {"id": "x"}}})
    send({"jsonrpc": "2.0", "method": "item/completed",
          "params": {"threadId": tid, "turnId": turn_id, "completedAtMs": 0, "item": {"type": "reasoning", "id": "r1"}}})
    for chunk in (reply[:10], reply[10:]):
        send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
              "params": {"threadId": tid, "turnId": turn_id, "itemId": item_id, "delta": chunk}})
        time.sleep(0.01)
    send({"jsonrpc": "2.0", "method": "item/completed",
          "params": {"threadId": tid, "turnId": turn_id, "completedAtMs": int(time.time() * 1000),
                     "item": {"type": "agentMessage", "id": item_id, "text": reply, "phase": "final"}}})
    send({"jsonrpc": "2.0", "method": "turn/completed",
          "params": {"threadId": tid, "turn": {"id": turn_id, "status": "completed", "items": [], "error": None}}})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    meth, rid, p = m.get("method"), m.get("id"), m.get("params") or {}
    if meth is None and rid in PENDING:
        tid, turn_id, item_id, reply = PENDING.pop(rid)
        finish_turn(tid, turn_id, item_id, reply + " approval=" + str((m.get("result") or {}).get("decision")))
    elif meth is None:
        pass   # a response to a request this stub does not track
    elif meth == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "stub-app-server/0", "codexVersion": "stub"}})
    elif meth == "initialized":
        pass
    elif meth == "model/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"data": [{"id": "stub-model", "isDefault": True}], "nextCursor": None}})
    elif meth in ("thread/start", "thread/resume"):
        tid = p.get("threadId") or str(uuid.uuid4())
        if meth == "thread/resume" and tid.startswith("stale-"):
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "no rollout found for thread id " + tid}})
            continue
        if meth == "thread/resume" and tid.startswith("hang-"):
            continue   # never answers: the client's timeout must fall back
        THREADS[tid] = {"cwd": p.get("cwd", "/"), "params": p, "emptied": THREADS.get(tid, {}).get("emptied")}
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": thread_obj(tid), "model": p.get("model") or "stub-model",
                                                      "modelProvider": "stub", "cwd": THREADS[tid]["cwd"],
                                                      "approvalPolicy": "never", "sandbox": "read-only", "approvalsReviewer": None}})
        send({"jsonrpc": "2.0", "method": "thread/started", "params": {"thread": thread_obj(tid)}})
    elif meth == "thread/loaded/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"data": list(THREADS), "nextCursor": None}})
    elif meth == "thread/read":
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": thread_obj(p["threadId"])}})
    elif meth == "turn/start":
        tid = p["threadId"]
        turn_id = str(uuid.uuid4())
        text = next((i.get("text", "") for i in p.get("input", []) if i.get("type") == "text"), "")
        tp = THREADS[tid]["params"]
        # The echo names the thread's settings, so a test can pin sandbox and lifetime.
        reply = "STUB echo: " + (text.strip().split("\n")[0][:80]) + " [sandbox=%s ephemeral=%s cwd=%s]" % (
            tp.get("sandbox"), tp.get("ephemeral"), tp.get("cwd"))
        if "[[empty]]" in text:
            reply = "" if not THREADS[tid].get("emptied") else "STUB second try"
            THREADS[tid]["emptied"] = True
        send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        send({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": tid, "turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        item_id = str(uuid.uuid4())
        if "[[die]]" in text:
            sys.exit(0)
        if "[[warn]]" in text:
            send({"jsonrpc": "2.0", "method": "error", "params": {"threadId": tid, "message": "stub warning"}})
        if "[[ask]]" in text:
            for n, (method, params) in enumerate((("item/tool/requestUserInput", {"questions": []}),
                                                  ("currentTime/read", {}), ("stub/unknownRequest", {}))):
                send({"jsonrpc": "2.0", "id": REQ_ID + 100 + n, "method": method, "params": params})
        if "[[stall" in text:
            STALLED[turn_id] = (tid, "late" if "[[stall-late]]" in text else "[[stall-hard]]" in text)
        elif "[[approval]]" in text:
            REQ_ID += 1
            PENDING[REQ_ID] = (tid, turn_id, item_id, reply)
            send({"jsonrpc": "2.0", "id": REQ_ID, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": tid, "turnId": turn_id, "itemId": item_id, "command": ["rm", "-rf", "x"]}})
        else:
            finish_turn(tid, turn_id, item_id, reply)
    elif meth == "turn/interrupt":
        late = STALLED.get(p.get("turnId"), (None, None))[1] == "late"
        if late:   # completed just before the interrupt landed: finish, then refuse it
            tid = STALLED.pop(p["turnId"])[0]
            finish_turn(tid, p["turnId"], str(uuid.uuid4()), "STUB late finish")
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "no active turn"}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        tid, hard = STALLED.get(p.get("turnId"), (None, True))
        if tid is not None and not hard:
            STALLED.pop(p["turnId"])
            send({"jsonrpc": "2.0", "method": "turn/completed",
                  "params": {"threadId": tid, "turn": {"id": p["turnId"], "status": "interrupted", "items": [],
                                                       "error": {"message": "interrupted"}}}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "stub: unknown method %s" % meth}})
