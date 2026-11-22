#!/usr/bin/env python3
"""A stand-in for `agy acp`: an ACP agent over stdio for the Antigravity adapter tests.

`initialize` and a signed-out `session/new` answer as agy 1.5.9 did on a real host: the sign-in methods are
listed signed in or not, and `session/new` fails with -32000 "Authentication required". The permission
requests (generic option ids picked by `kind`, `locations`, no `rawInput`) are assumed: no signed-in agy turn
has been measured yet. Its sign-in, session
and log files live under $HOME (the one variable the launcher's allowlist always passes): `signed-in` marks it
signed in, `stub-sessions.json` keeps its sessions, `stub.log` records each request and permission outcome.
A prompt marker picks the turn: STUB:TEXT, STUB:NOREPLY, STUB:MARKER, STUB:EDIT, STUB:SHELL, STUB:WEB,
STUB:AGYWRITE, STUB:ODD, STUB:HANG, STUB:ASKMODEL. STUB:AGYWRITE's rawInput carries agy's own argument names (Path, Content,
Overwrite; Target, Replacement, Edits) as its file tools declare them; the key order (Content first) is the one
a real owner turn's refusal named ("unrecognised key Content").

Measured on a signed-in agy (2026-11-19/20, three owner turns): a prompt that carries the gateway's "write the
result to results/<task>.txt" step makes agy ask to write that file (title `File results/<task>.txt`, a
rawInput with a `Content` key), and once refused it ends the turn (`end_turn`) with no message text. The stub
does the same; the `tool_call` update before the request is assumed.

Its first log line reports the agy home it would use (`--gemini-dir`, else $HOME/.gemini), whether that
home holds a settings.json (and its text) or any global context file, and its process directory.
`session/new` offers the modes default, accept-edits and plan as agy's `--mode` names them (assumed: no
signed-in agy has listed its ACP modes yet); `no-modes` under $HOME offers none, `set-mode-fails` makes
`session/set_mode` fail.
"""
import json
import os
import re
import sys

HOME = os.environ.get("HOME", "")
STATE = os.path.join(HOME, "stub-sessions.json")
LOG = os.path.join(HOME, "stub.log")
_next = [1000]


# ACP's `models` on session/new, with ids as `agy models` prints them (assumed shape: the ACP schema's
# SessionModelState; no signed-in agy has been asked yet).
MODELS = {"currentModelId": "gemini-3.8-flash-medium",
          "availableModels": [{"modelId": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash"},
                              {"modelId": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6"}]}


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
    told = re.search(r"write the result to (results/\S+?\.txt)", text)
    if told:
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
            "sessionUpdate": "tool_call", "toolCallId": "c-file", "title": "File " + told.group(1), "kind": "edit"}}})
        ask(sid, {"title": "File " + told.group(1), "kind": "edit", "rawInput": {"Content": "hello"}})
    elif "STUB:ASKMODEL" in text:
        # What agy is told to answer when asked about its model, provided the hint reached it.
        chunk(sid, "[model-card]" if "reply with exactly [model-card]" in text else "no hint")
    elif "STUB:NOREPLY" in text:
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
    elif "STUB:MCP" in text:
        # Assumed shape (no signed-in agy MCP call measured): the MCP server and tool named in `_meta`.
        for title, meta, raw in MCP_CALLS:
            kind = "execute" if "command" in raw else "other"
            ask(sid, dict({"title": title, "kind": kind, "rawInput": raw}, **({"_meta": meta} if meta else {})))
        chunk(sid, "tried the S.space tools")
    elif "STUB:WEB" in text:
        ask(sid, {"title": "date", "kind": "execute",
                  "rawInput": {"CommandLine": "date", "Cwd": cwd, "WaitMsBeforeAsync": 0}})
        ask(sid, {"title": "Fetch https://example.com/", "kind": "fetch", "rawInput": {"Url": "https://example.com/"}})
        ask(sid, {"title": "Search the web: news today", "kind": "search", "rawInput": {"query": "news today"}})
        chunk(sid, "ran and searched")
    elif "STUB:AGYWRITE" in text:
        inside = os.path.join(cwd, "notes.txt")
        ask(sid, {"title": "File notes.txt", "kind": "edit", "locations": [{"path": inside}],
                  "rawInput": {"Content": "x", "Overwrite": False, "Path": inside}})
        ask(sid, {"title": "Edit notes.txt", "kind": "edit", "rawInput": {
            "Path": inside, "Edits": [{"Target": "x", "Replacement": "y", "AllowMultiple": False}]}})
        ask(sid, {"title": "File /etc/stub-outside", "kind": "edit", "locations": [{"path": "/etc/stub-outside"}],
                  "rawInput": {"Content": "x", "Path": "/etc/stub-outside"}})
        ask(sid, {"title": "File notes.txt (unknown key)", "kind": "edit",
                  "rawInput": {"Content": "x", "Path": inside, "Mystery": "?"}})
        chunk(sid, "wrote")
    elif "STUB:ODD" in text:
        for kind in ("delete", "move", "other", "think", None):
            ask(sid, {"title": "odd %s" % kind, "kind": kind, "locations": [{"path": os.path.join(cwd, "a")}]})
        chunk(sid, "tried odd ones")
    else:
        chunk(sid, "hello from agy-stub")
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


PAGE = {"room_id": "!room:s.space", "action": "commons.page.create", "input": {"title": "Notes"}}
MCP_CALLS = [
    ("room.action.execute", {"serverName": "ag2-space", "toolName": "room.action.execute"}, PAGE),
    ("room_actions_search", {"serverName": "ag2-space", "toolName": "room_actions_search"}, {"query": "page"}),
    ("agent.unlink", {"serverName": "ag2-space", "toolName": "agent.unlink"}, {}),
    ("room.action.execute", {"serverName": "other-server", "toolName": "room.action.execute"}, PAGE),
    ("ag2-space room.action.execute", None, PAGE),
    ("rm -rf ~", {"serverName": "ag2-space", "toolName": "ag2.whoami"}, {"command": "rm -rf ~"}),
]


def launch(servers, loading=False):
    """With `launch-mcp` under $HOME, start each stdio MCP server as agy does (its command, args and only its env),
    ask for its tools and log their names; on session/load only with `launch-on-load` (agy launched none then)."""
    if not os.path.exists(os.path.join(HOME, "launch-mcp")) or (loading and not os.path.exists(os.path.join(HOME, "launch-on-load"))):
        return
    import subprocess
    for s in servers or []:
        if "command" not in s:
            continue
        env = {e["name"]: e["value"] for e in s.get("env") or []}
        try:
            p = subprocess.Popen([s["command"]] + list(s.get("args") or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, env=env, text=True)
        except OSError as e:
            log({"mcp_launch": s["name"], "error": type(e).__name__})
            continue
        try:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
        except OSError:
            line = ""
        tools = [t.get("name") for t in ((json.loads(line).get("result") or {}).get("tools") or [])] if line else []
        log({"mcp_launch": s["name"], "tools": tools})
        p.stdin.close()
        p.wait(30)


GLOBAL_CONTEXT = ("GEMINI.md", "AGENTS.md", "config/GEMINI.md", "config/AGENTS.md")
MODES = {"currentModeId": "default", "availableModes": [
    {"id": "default", "name": "Default"}, {"id": "accept-edits", "name": "Accept edits"},
    {"id": "plan", "name": "Plan"}]}


def agy_home(argv):
    if "--gemini-dir" in argv[:-1]:
        return argv[argv.index("--gemini-dir") + 1]
    return os.path.join(HOME, ".gemini")


def boot_record(argv):
    home = agy_home(argv)
    settings = os.path.join(home, "antigravity-cli", "settings.json")
    try:
        with open(settings, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = None
    return {"argv": argv, "agy_home": home, "settings": text, "process_cwd": os.getcwd(),
            "global_context": [n for n in GLOBAL_CONTEXT if os.path.exists(os.path.join(home, n))]}


def main():
    log(boot_record(sys.argv[1:]))
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
                "agentCapabilities": dict({"loadSession": True, "sessionCapabilities": {"load": {}, "resume": {}}},
                                          # `mcp-http` under $HOME declares HTTP MCP (assumed: 1.5.9 declared none)
                                          **({"mcpCapabilities": {"http": True, "sse": False}}
                                             if os.path.exists(os.path.join(HOME, "mcp-http")) else {})),
                "authMethods": [{"id": "oauth-personal", "name": "Log in with Google"},
                                {"id": "oauth-business", "name": "Log in with Gemini Enterprise"},
                                {"id": "gemini-api-key", "name": "Gemini API key"},
                                {"id": "agent-platform", "name": "Gemini Enterprise Agent Platform"}]}})
        elif method == "session/new":
            if not os.path.exists(os.path.join(HOME, "signed-in")):
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "Authentication required"}})
                continue
            launch(params.get("mcpServers"))
            s = sessions()
            sid = "s-%d" % (len(s) + 1)
            s[sid] = params.get("cwd")
            save(s)
            result = {"sessionId": sid}
            if not os.path.exists(os.path.join(HOME, "no-modes")):
                result["modes"] = MODES
            if os.path.exists(os.path.join(HOME, "models")):
                result["models"] = MODELS
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        elif method == "session/set_model":
            if os.path.exists(os.path.join(HOME, "set-model-fails")):
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown model"}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/set_mode":
            if os.path.exists(os.path.join(HOME, "set-mode-fails")):
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "mode not allowed"}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/load":
            if params.get("sessionId") in sessions():
                launch(params.get("mcpServers"), loading=True)
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
