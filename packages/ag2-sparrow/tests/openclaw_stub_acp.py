#!/usr/bin/env python3
"""A stand-in for `openclaw acp`: an ACP agent over stdio for the OpenClaw adapter tests.

It keeps its sessions in STUB_STATE (as OpenClaw keeps them in its profile), logs every request and
permission outcome to STUB_LOG, and acts on a marker in the prompt: STUB:REPLY runs the prompt's reply
command (after asking permission), STUB:NOREPLY its no-reply, STUB:TEXT answers in text only,
STUB:OUTSIDE asks to write outside the working directory, STUB:HANG never answers.
"""
import json
import os
import re
import subprocess
import sys

STATE = os.environ.get("STUB_STATE", "")
LOG = os.environ.get("STUB_LOG", "")
LOAD = os.environ.get("STUB_LOAD", "1") == "1"
_next = [1000]


def log(rec):
    if LOG:
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
    """Ask the client, wait for its answer (answering nothing else meanwhile); True when allowed."""
    _next[0] += 1
    rid = _next[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
          "params": {"sessionId": session_id, "toolCall": tool_call,
                     "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                                 {"optionId": "no", "name": "Deny", "kind": "reject_once"}]}})
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("id") == rid and "method" not in m:
            outcome = (m.get("result") or {}).get("outcome") or {}
            allowed = outcome.get("optionId") == "yes"
            log({"permission": tool_call.get("title"), "allowed": allowed})
            return allowed
    return False


def chunk(session_id, text):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session_id,
                     "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def run_command(session_id, command):
    if ask(session_id, {"title": "exec", "kind": "execute", "rawInput": {"command": command}}):
        r = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
        log({"ran": command.split("\n")[0], "rc": r.returncode, "out": r.stdout + r.stderr})


def prompt(rid, params):
    sid = params["sessionId"]
    text = "".join(b.get("text", "") for b in params.get("prompt") or [])
    log({"method": "session/prompt", "sessionId": sid, "text": text})
    if "STUB:HANG" in text:
        return
    post = re.search(r"to the file (\S+), then run exactly this command line, alone and unchanged:\n(.*)\n", text)
    skip = re.search(r"for no post\), run exactly:\n(.*)\n", text)
    if "STUB:REPLY" in text and post:
        if ask(sid, {"title": "write-draft", "kind": "edit", "locations": [{"path": post.group(1)}],
                     "rawInput": {"path": post.group(1), "content": "stub reply"}}):
            os.makedirs(os.path.dirname(post.group(1)), exist_ok=True)
            with open(post.group(1), "w", encoding="utf-8") as f:
                f.write("stub reply\n")
        run_command(sid, post.group(2))
    elif "STUB:NOREPLY" in text and skip:
        run_command(sid, skip.group(1))
    elif "STUB:OUTSIDE" in text:
        ask(sid, {"title": "write", "kind": "edit", "locations": [{"path": "/etc/stub-outside"}]})
        ask(sid, {"title": "write-inside", "kind": "edit", "locations": [{"path": "notes.txt"}]})
    chunk(sid, "text from the stub")
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def main():
    for line in sys.stdin:
        m = json.loads(line)
        method, rid, params = m.get("method"), m.get("id"), m.get("params") or {}
        if method is None:
            continue
        if method != "session/prompt":
            log({"method": method, "params": params})
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentInfo": {"name": "openclaw-stub", "version": "0.0.1"},
                "agentCapabilities": {"loadSession": LOAD}, "authMethods": []}})
        elif method == "session/new":
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
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "unknown session"}})
        elif method == "session/prompt":
            prompt(rid, params)
        elif method == "session/cancel":
            pass
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}})


if __name__ == "__main__":
    main()
