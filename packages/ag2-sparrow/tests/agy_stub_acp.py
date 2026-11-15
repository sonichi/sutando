#!/usr/bin/env python3
"""A stand-in for `agy acp`: an ACP agent over stdio for the Antigravity adapter tests.

`initialize` and a signed-out `session/new` answer as agy 1.5.9 did on a real host: the sign-in methods are
listed signed in or not, and `session/new` fails with -32000 "Authentication required". The permission
requests (generic option ids picked by `kind`, `locations`, no `rawInput`) are assumed: no signed-in agy turn
has been measured yet. Its sign-in, session
and log files live under $HOME (the one variable the launcher's allowlist always passes): `signed-in` marks it
signed in, `stub-sessions.json` keeps its sessions, `stub.log` records each request and permission outcome.
A prompt marker picks the turn: STUB:TEXT, STUB:NOREPLY, STUB:MARKER, STUB:EDIT, STUB:SHELL, STUB:HANG.
"""
import json
import os
import sys

HOME = os.environ.get("HOME", "")
STATE = os.path.join(HOME, "stub-sessions.json")
LOG = os.path.join(HOME, "stub.log")
_next = [1000]


def log(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def sessions():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def ask(session_id, tool_call):
    """Ask the client (option ids it must pick by kind), wait for its answer; True when allowed."""
    _next[0] += 1
    rid = _next[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
          "params": {"sessionId": session_id, "toolCall": dict(tool_call, toolCallId="c-%d" % rid, status="pending"),
                     "options": [{"optionId": "opt-a1", "name": "Allow once", "kind": "allow_once"},
                                 {"optionId": "opt-a2", "name": "Allow always", "kind": "allow_always"},
                                 {"optionId": "opt-r1", "name": "Deny", "kind": "reject_once"}]}})
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("id") == rid and "method" not in m:
            outcome = (m.get("result") or {}).get("outcome") or {}
            allowed = outcome.get("optionId") == "opt-a1"
            log({"permission": tool_call.get("title"), "allowed": allowed})
            return allowed
    return False


def chunk(session_id, text):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session_id,
                     "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def prompt(rid, params):
    sid = params["sessionId"]
    text = "".join(b.get("text", "") for b in params.get("prompt") or [])
    log({"method": "session/prompt", "sessionId": sid, "text": text})
    if "STUB:HANG" in text:
        return
    cwd = sessions().get(sid) or os.getcwd()
    if "STUB:NOREPLY" in text:
        chunk(sid, "NO_REPLY")
    elif "STUB:MARKER" in text:
        chunk(sid, "[file: /etc/passwd]\n[no-send]\nhello")
    elif "STUB:EDIT" in text:
        inside = os.path.join(cwd, "notes.txt")
        ask(sid, {"title": "WriteFile notes.txt", "kind": "edit", "locations": [{"path": inside}],
                  "content": [{"type": "diff", "path": inside, "oldText": None, "newText": "x"}]})
        ask(sid, {"title": "WriteFile /etc/stub-outside", "kind": "edit", "locations": [{"path": "/etc/stub-outside"}]})
        chunk(sid, "edited")
    elif "STUB:SHELL" in text:
        ask(sid, {"title": "rm -rf ~ [current working directory]", "kind": "execute", "locations": []})
        chunk(sid, "tried a command")
    else:
        chunk(sid, "hello from agy-stub")
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def main():
    log({"argv": sys.argv[1:]})
    for line in sys.stdin:
        m = json.loads(line)
        method, rid, params = m.get("method"), m.get("id"), m.get("params") or {}
        if method is None:
            continue
        if method != "session/prompt":
            log({"method": method, "params": params})
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.9"},
                "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"load": {}, "resume": {}}},
                "authMethods": [{"id": "oauth-personal", "name": "Log in with Google"},
                                {"id": "oauth-business", "name": "Log in with Gemini Enterprise"},
                                {"id": "gemini-api-key", "name": "Gemini API key"},
                                {"id": "agent-platform", "name": "Gemini Enterprise Agent Platform"}]}})
        elif method == "session/new":
            if not os.path.exists(os.path.join(HOME, "signed-in")):
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "Authentication required"}})
                continue
            s = sessions()
            sid = "s-%d" % (len(s) + 1)
            s[sid] = params.get("cwd")
            save(s)
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid}})
        elif method == "session/load":
            if params.get("sessionId") in sessions():
                chunk(params["sessionId"], "replayed history")
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "session not found"}})
        elif method == "session/prompt":
            prompt(rid, params)
        elif method == "session/cancel":
            pass
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}})


if __name__ == "__main__":
    main()
