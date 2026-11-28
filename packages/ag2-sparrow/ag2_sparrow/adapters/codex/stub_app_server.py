#!/usr/bin/env python3
"""Test double for `codex app-server --listen stdio://`: the same JSON-RPC shapes, no model.

Echoes the first line of each turn's input as the agentMessage. Markers in the input
drive failure modes: `[[approval]]` sends an `item/commandExecution/requestApproval`
request first and echoes the decision; `[[stall]]` never completes until
`turn/interrupt`; `[[stall-hard]]` ignores the interrupt too. `[[limit]]` fails the turn on the account's usage
limit (a used-up rate-limit window, an `error` notification, the turn's codexErrorInfo); `[[limit-bare]]` the
same with no rate-limit window; `[[limit-words]]` fails it on another error whose words speak of a usage limit;
`[[noaccess]]` fails a turn that names no model as one the account cannot run; `[[fail]]` fails it on another error. A refused turn ends with no item, unless `[[worked]]` is there too: then a
reasoning item and part of a reply come first.

`thread/resume` fails for a thread id starting `stale-`, never answers for `hang-`, and fails for `nomcp-` only
when its config adds `mcp_servers` (a runtime refusing that override at resume).

`account/rateLimits/read` answers as `stub-limits` under CODEX_HOME says ("out": a window used up; "ok": usage
left); with no such file the method is unknown.

Tokens, as the real server holds them: `stub-token` under CODEX_HOME is read once, at start, and kept in
memory. A sign-in that succeeds writes a new one there and lists the old one in `stub-revoked`; a turn on a
token listed there fails as `unauthorized`, as a server that kept the old login after a switch does.
`[[rotate]]` does the same once, as a sign-in elsewhere; `stub-locked` refuses every turn that way.
The login file is rewritten (a byte appended to `auth.json`), as an account switch does, by `[[touch-login]]`
once a turn is sent, and on every thread start or resume while `stub-touch-on-thread` exists.
"""
import json
import os
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


# What `model/list` answers when asked with a limit (the model card's call): more than a card's six
# buttons, one hidden, one off the connector's allowlist, one whose turns fail and one Codex moves turns from.
MODELS = ([{"id": "gpt-5.6-sol", "displayName": "Stub Model", "isDefault": True},
           {"id": "gpt-6-sol", "displayName": "Stub Large"}, {"id": "gpt-6-hidden", "displayName": "Hidden", "hidden": True},
           {"id": "stub-unlisted", "displayName": "Stub Unlisted"},
           {"id": "gpt-6-astra", "displayName": "Stub Broken"}, {"id": "gpt-6-luna", "displayName": "Stub Moved"}]
          + [{"id": mid, "displayName": "Stub %d" % n} for n, mid in
             ((5, "gpt-5.6-terra"), (6, "gpt-5.6-luna"), (7, "gpt-5.5"), (8, "gpt-5.2"))])
BROKEN, MOVED = "gpt-6-astra", "gpt-6-luna"


def home_file(name, text=None):
    """A sign-in stand-in under CODEX_HOME: `stub-account` holds the address signed in, `stub-login` says
    how the next sign-in ends ("success <address>", "fail", "refuse", "page <address of its page>";
    absent: it never ends)."""
    path = os.path.join(os.environ.get("CODEX_HOME") or ".", name)
    if text is not None:
        with open(path, "a") as f:
            f.write(text)
        return text
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def finish_turn(tid, turn_id, item_id, reply, status="completed", error=None):
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
          "params": {"threadId": tid, "turn": {"id": turn_id, "status": status, "items": [], "error": error}}})


def refuse_turn(tid, turn_id, item_id, error, text):
    """End the turn failed on `error`, as the server does: an `error` notification, then the turn."""
    send({"jsonrpc": "2.0", "method": "error",
          "params": {"threadId": tid, "turnId": turn_id, "error": error, "willRetry": False}})
    if "[[worked]]" in text:
        finish_turn(tid, turn_id, item_id, "partial answer", "failed", error)
        return
    send({"jsonrpc": "2.0", "method": "turn/completed",
          "params": {"threadId": tid, "turn": {"id": turn_id, "status": "failed", "items": [], "error": error}}})


def new_token(value):
    with open(os.path.join(os.environ.get("CODEX_HOME") or ".", "stub-token"), "w") as f:
        f.write(value)


TOKEN = home_file("stub-token")    # read once, at start: a later sign-in does not reach this process
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
    elif meth == "account/read":
        who = home_file("stub-account").split("\n")[-1]
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "account": {"type": "chatgpt", "email": who, "planType": "pro"} if who else None, "requiresOpenaiAuth": True}})
    elif meth == "account/rateLimits/read" and home_file("stub-limits"):
        # `stub-limits` says what the account's usage is now: "out" (a window used up) or "ok" (usage left);
        # absent, the method is unknown, as on a server that cannot say.
        used = 100 if home_file("stub-limits") == "out" else 10
        send({"jsonrpc": "2.0", "id": rid, "result": {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": 300,
                                            "resetsAt": int(time.time()) + 3600}}}})
    elif meth == "account/login/start":
        plan = home_file("stub-login").split()
        if plan[:1] == ["refuse"]:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "login server could not start"}})
            continue
        page = plan[1] if plan[:1] == ["page"] else "https://stub.invalid/sign-in?id=login-1"
        send({"jsonrpc": "2.0", "id": rid, "result": {"type": "chatgpt", "loginId": "login-1", "authUrl": page}})
        if plan[:1] == ["success"]:
            home_file("stub-account", "\n" + plan[1])
            old = home_file("stub-token")
            if old:
                home_file("stub-revoked", old + "\n")
                new_token("tok-" + uuid.uuid4().hex[:8])
        if plan and plan[0] != "page":
            send({"jsonrpc": "2.0", "method": "account/login/completed",
                  "params": {"loginId": "login-1", "success": plan[0] == "success",
                             "error": None if plan[0] == "success" else "access_denied"}})
    elif meth == "account/login/cancel":
        home_file("stub-cancelled", p.get("loginId", "") + "\n")
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif meth == "config/read":
        # The owner's config as the real server reports it: one MCP server a restricted thread must disable.
        send({"jsonrpc": "2.0", "id": rid, "result": {"config": {"mcp_servers": {"owner-mcp": {"command": "x"}},
                                                                 "web_search": None}, "origins": {}}})
    elif meth == "model/list":
        listed = MODELS if p.get("limit") else [{"id": "stub-model", "isDefault": True}]
        send({"jsonrpc": "2.0", "id": rid, "result": {"data": listed, "nextCursor": None}})
    elif meth in ("thread/start", "thread/resume"):
        tid = p.get("threadId") or str(uuid.uuid4())
        if home_file("stub-touch-on-thread"):
            home_file("auth.json", " ")      # the login switched while the turn was being made ready
        if meth == "thread/resume" and tid.startswith("stale-"):
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "no rollout found for thread id " + tid}})
            continue
        if meth == "thread/resume" and tid.startswith("nomcp-") and (p.get("config") or {}).get("mcp_servers"):
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "invalid config: mcp_servers"}})
            continue
        if meth == "thread/resume" and tid.startswith("hang-"):
            continue   # never answers: the client's timeout must fall back
        THREADS[tid] = {"cwd": p.get("cwd", "/"), "params": p, "emptied": THREADS.get(tid, {}).get("emptied")}
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": thread_obj(tid), "model": p.get("model") or "stub-model",
                                                      "modelProvider": "stub", "cwd": THREADS[tid]["cwd"],
                                                      "approvalPolicy": "never", "sandbox": "read-only", "approvalsReviewer": None,
                                                      # As 0.167.0: the profile is active only with no `sandbox` param.
                                                      "activePermissionProfile": (
                                                          {"id": (p.get("config") or {}).get("default_permissions"), "extends": None}
                                                          if (p.get("config") or {}).get("default_permissions")
                                                          and "sandbox" not in p else None)}})
        send({"jsonrpc": "2.0", "method": "thread/started", "params": {"thread": thread_obj(tid)}})
    elif meth == "thread/loaded/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"data": list(THREADS), "nextCursor": None}})
    elif meth == "thread/read":
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": thread_obj(p["threadId"])}})
    elif meth == "turn/start":
        tid = p["threadId"]
        turn_id = str(uuid.uuid4())
        text = next((i.get("text", "") for i in p.get("input", []) if i.get("type") == "text"), "")
        if "[[touch-login]]" in text:
            home_file("auth.json", " ")          # the login switched after this turn was sent
        tp = THREADS[tid]["params"]
        # The echo names the thread's settings, so a test can pin sandbox and lifetime; the cwd is the
        # turn's own when the turn carries one (the turn/start override), else the thread's.
        reply = "STUB echo: " + (text.strip().split("\n")[0][:80]) + " [sandbox=%s ephemeral=%s cwd=%s]" % (
            tp.get("sandbox"), tp.get("ephemeral"), p.get("cwd", tp.get("cwd")))
        if p.get("model"):
            reply += " model=%s" % p["model"]
        if "[[askmodel]]" in text:
            reply = "[model-card]"
        if "[[askaccount]]" in text:
            reply = "[account-card]"
        if "[[config]]" in text:      # what the thread was started with, for a test to pin its overrides
            reply += " config=" + json.dumps(tp.get("config"), sort_keys=True)
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
        if "[[rotate]]" in text and TOKEN and not home_file("stub-rotated"):
            # Another sign-in rotated the login while this server ran: the disk has a new token, memory the old.
            home_file("stub-rotated", "1")
            home_file("stub-revoked", TOKEN + "\n")
            new_token("tok-rotated")
        if "[[stall" in text:
            STALLED[turn_id] = (tid, "late" if "[[stall-late]]" in text else "[[stall-hard]]" in text)
        elif "[[approval]]" in text:
            REQ_ID += 1
            PENDING[REQ_ID] = (tid, turn_id, item_id, reply)
            send({"jsonrpc": "2.0", "id": REQ_ID, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": tid, "turnId": turn_id, "itemId": item_id, "command": ["rm", "-rf", "x"]}})
        elif home_file("stub-locked") or (TOKEN and TOKEN in home_file("stub-revoked").split()):
            refuse_turn(tid, turn_id, item_id, {
                "message": "Your access token could not be refreshed because your refresh token was revoked. "
                           "Please log out and sign in again.", "codexErrorInfo": "unauthorized",
                "additionalDetails": None}, text)
        elif "[[limit-words]]" in text:
            refuse_turn(tid, turn_id, item_id, {"message": "You've hit your usage limit. Try again at 3:05 PM.",
                                                "codexErrorInfo": "other", "additionalDetails": None}, text)
        elif "[[limit" in text:
            if "[[limit]]" in text:
                send({"jsonrpc": "2.0", "method": "account/rateLimits/updated", "params": {"rateLimits": {
                    "limitId": "codex", "primary": {"usedPercent": 100, "windowDurationMins": 300,
                                                    "resetsAt": int(time.time()) + 3600}}}})
            refuse_turn(tid, turn_id, item_id, {
                "message": "You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM.",
                "codexErrorInfo": "usageLimitExceeded", "additionalDetails": None}, text)
        elif "[[noaccess]]" in text and not p.get("model"):    # the account cannot run the thread's own model
            refuse_turn(tid, turn_id, item_id, {
                "message": "The 'stub-model' model is not supported when using Codex with a ChatGPT account.",
                "codexErrorInfo": "other", "additionalDetails": None}, text)
        elif "[[fail]]" in text:
            refuse_turn(tid, turn_id, item_id, {"message": "stream disconnected before completion",
                                                "codexErrorInfo": "responseStreamDisconnected"}, text)
        elif p.get("model") == BROKEN:
            finish_turn(tid, turn_id, item_id, "", "failed", {"message": "the model is not available"})
        else:
            if p.get("model") == MOVED:
                send({"jsonrpc": "2.0", "method": "model/rerouted",
                      "params": {"threadId": tid, "turnId": turn_id, "fromModel": MOVED, "toModel": "gpt-5.6-sol",
                                 "reason": "highRiskCyberActivity"}})
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
