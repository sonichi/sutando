"""ag2-sparrow MCP name shim: a stdio MCP server that runs another one and renames its tools for clients that
only take `[A-Za-z0-9_-]{1,64}` tool names.

Antigravity CLI (agy) drops every MCP tool whose name has a dot: through the desktop's `ag2-mcp-proxy` it saw
only `ui_describe`, `ui_request` and `ui_result` of AG2 Space's tools (`room.action.execute`, `ag2.whoami`, ...
were not offered). This shim sits between agy and that proxy and changes nothing but names: a `tools/list`
answer's tool names become `room_action_execute` and so on (`alias`), and a `tools/call` for an alias is sent
on under the server's own name. It also enforces the turn's grant (TOOLS_ENV, the server names the connector
grants): a `tools/list` answer keeps only granted tools, and a `tools/call` for any other name, or a malformed
one, is answered with a JSON-RPC error and never forwarded. No grant named means none granted. With GATE_ENV (a
command the connector names, for a session more than one sender's turns share), every `tools/call` also runs
that command first and is forwarded only when it answers {"allowed": true}. Only METHODS and
`notifications/*` reach the server, plus the client's answers to the server's own requests; any other request,
and a line that is not a JSON-RPC object, is answered with an error, any other notification dropped. Server
lines pass through as they came, and the child's environment is this process's (the proxy scrubs its own).

    python -m ag2_sparrow.mcp_name_shim <command> [args...]

Stdlib only.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time

# agy checks `mcp_<server>_<tool>` against ^[a-zA-Z0-9_-]{1,64}$; `mcp_ag2-space_` leaves the tool 50.
ALIAS_MAX = 50
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,%d}" % ALIAS_MAX)
_BAD = re.compile(r"[^A-Za-z0-9_-]")
LOG_ENV = "AG2_MCP_LOG"
STATUS_ENV, TURN_ENV = "AG2_MCP_SHIM_STATUS", "AG2_MCP_SHIM_TURN"
TOOLS_ENV = "AG2_MCP_SHIM_TOOLS"
GATE_ENV = "AG2_MCP_SHIM_GATE"
GATE_TIMEOUT_S = 10.0
GATE_UNCHECKED = "the turn's sender could not be checked, so no room tool is called"
NOT_GRANTED = -32601
PARSE_ERROR, INVALID_REQUEST = -32700, -32600
# The client methods forwarded; any other request is answered with an error, any other notification dropped.
METHODS = frozenset({"initialize", "ping", "tools/list", "tools/call"})


def granted(environ=None):
    """The server tool names this turn may list and call (TOOLS_ENV, comma-separated); none when unset."""
    value = (os.environ if environ is None else environ).get(TOOLS_ENV) or ""
    return frozenset(n for n in value.split(",") if n)


def turn_gate(environ=None, run=subprocess.run):
    """None, or the check every `tools/call` passes first: '' to forward it, else why it is refused. GATE_ENV
    holds a command (a JSON list of words) that prints one JSON object: {"allowed": true}, or `why` it is not.
    A command that cannot run, times out, exits non-zero or prints anything else refuses (fail closed)."""
    raw = (os.environ if environ is None else environ).get(GATE_ENV)
    if not raw:
        return None

    def check():
        try:
            argv = json.loads(raw)
            if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
                raise ValueError("not a command")
            done = run(argv, capture_output=True, text=True, timeout=GATE_TIMEOUT_S, stdin=subprocess.DEVNULL)
            if done.returncode != 0:    # whatever it printed: a gate that failed allowed nothing
                raise ValueError("exit %s" % done.returncode)
            out = json.loads(done.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            note("the turn gate could not run (%s)" % type(e).__name__)
            return GATE_UNCHECKED
        if isinstance(out, dict) and out.get("allowed") is True:
            return ""
        why = out.get("why") if isinstance(out, dict) else None
        return why[:300] if isinstance(why, str) and why else GATE_UNCHECKED
    return check


class Status:
    """The status file the connector reads at turn end (STATUS_ENV): this turn's id (TURN_ENV) and `state`
    started | listed (with `tools`, the count the server listed) | exited (with `code`, before any list)."""

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.path, self.turn = env.get(STATUS_ENV), env.get(TURN_ENV) or ""
        self.listed = False
        self.lock = threading.Lock()

    def write(self, **fields):
        if not self.path:
            return
        with self.lock, contextlib.suppress(OSError):
            tmp = "%s.%d.tmp" % (self.path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(fields, turn=self.turn), f)
            os.replace(tmp, self.path)

    def tools(self, count):
        self.listed = True
        self.write(state="listed", tools=count)

    def exited(self, code):
        if not self.listed:
            self.write(state="exited", code=code)


def alias(name):
    """The client-safe name for a server tool name: itself when already safe, else every other character as
    `_`, cut to ALIAS_MAX."""
    return name if NAME_RE.fullmatch(name) else (_BAD.sub("_", name)[:ALIAS_MAX] or "_")


def note(line, environ=None):
    """One line in the proxy's own log (AG2_MCP_LOG), so a run shows whether the shim started; best effort."""
    path = (os.environ if environ is None else environ).get(LOG_ENV)
    if path:
        with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as f:
            f.write("%s pid=%d mcp_name_shim: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), line))


class Names:
    """The aliases handed out so far: alias -> server name (a later tools/list may add or change them), and the
    server names this turn grants (`allowed`)."""

    def __init__(self, allowed=frozenset(), gate=None):
        self.to_server = {}
        self.allowed = frozenset(allowed)
        self.gate = gate
        self.lock = threading.Lock()

    def rename_tools(self, tools):
        """Keep only granted tools, renamed in place; an alias once handed out keeps its server tool across pages
        and later lists."""
        with self.lock:
            tools[:] = [t for t in tools if isinstance(t, dict) and t.get("name") in self.allowed]
            taken = dict(self.to_server)
            for tool in tools:
                name = tool["name"]
                safe, n = alias(name), 2
                while safe in taken and taken[safe] != name:
                    suffix = "_%d" % n
                    safe, n = alias(name)[:ALIAS_MAX - len(suffix)] + suffix, n + 1
                taken[safe] = name
                tool["name"] = safe
            self.to_server = taken

    def server_name(self, name):
        with self.lock:
            return self.to_server.get(name, name)

    def call_ok(self, name):
        """A `tools/call` name (an alias or a server name) that resolves to a granted tool."""
        return isinstance(name, str) and self.server_name(name) in self.allowed


def _messages(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, (dict, list)) else None


def _each(msg):
    return msg if isinstance(msg, list) else [msg]


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _method_ok(method):
    return method in METHODS or (isinstance(method, str) and method.startswith("notifications/"))


def _refused(m, names):
    """The error code and message for a client message the server must not get, or None to forward it."""
    if not isinstance(m, dict):
        return INVALID_REQUEST, "invalid request"
    method = m.get("method")
    if "method" not in m:   # an answer to one of the server's own requests
        return None if "id" in m and ("result" in m or "error" in m) else (INVALID_REQUEST, "invalid request")
    if not _method_ok(method):
        return NOT_GRANTED, "method not allowed"
    if method == "tools/call":
        params = m.get("params")
        if not names.call_ok(params.get("name") if isinstance(params, dict) else None):
            return NOT_GRANTED, "tool not granted on this turn"
        why = names.gate() if names.gate else ""
        if why:
            return NOT_GRANTED, why
    return None


def to_server(line, names, pending):
    """(the line for the server or None, the line for the client or None) for a client line. A message `_refused`
    names is never forwarded: a request (or a line that is not a JSON-RPC message) is answered with its error, a
    notification dropped. A granted `tools/call` goes on under the server's name; each `tools/list` id is
    remembered so its answer is filtered."""
    try:
        msg = json.loads(line)
    except ValueError:
        return None, json.dumps(_error(None, PARSE_ERROR, "parse error")) + "\n"
    batch = isinstance(msg, list)
    if batch and not msg:
        return None, json.dumps(_error(None, INVALID_REQUEST, "invalid request")) + "\n"
    changed, kept, refused = False, [], []
    for m in _each(msg):
        why = _refused(m, names)
        if why is not None:
            changed = True
            if not isinstance(m, dict) or "id" in m or "method" not in m:
                refused.append(_error(m.get("id") if isinstance(m, dict) else None, *why))
            continue
        if m.get("method") == "tools/call":
            name = m["params"]["name"]
            if names.server_name(name) != name:
                m["params"]["name"] = names.server_name(name)
        elif m.get("method") == "tools/list" and "id" in m:
            pending.add(json.dumps(m["id"]))
        kept.append(m)
    reply = (json.dumps(refused if batch else refused[0]) + "\n") if refused else None
    if not kept:
        return None, reply
    if not changed and all(m.get("method") != "tools/call" for m in kept):
        return line, None
    return json.dumps(kept if batch else kept[0]) + "\n", reply


def to_client(line, names, pending, status=None):
    """A server line as the client should get it: the tools of an answer to a remembered `tools/list` renamed."""
    msg = _messages(line)
    if msg is None:
        return line
    changed = False
    for m in _each(msg):
        if not isinstance(m, dict) or "id" not in m or "method" in m:
            continue
        key = json.dumps(m["id"])
        if key in pending:
            pending.discard(key)
            tools = (m.get("result") or {}).get("tools") if isinstance(m.get("result"), dict) else None
            if isinstance(tools, list):
                names.rename_tools(tools)
                changed = True
                if status is not None:
                    status.tools(len(tools))
    return json.dumps(msg) + "\n" if changed else line


def main(argv=None, stdin=None, stdout=None, popen=subprocess.Popen):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: mcp_name_shim <command> [args...]", file=sys.stderr)
        return 2
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    note("start, running %s" % os.path.basename(argv[0]))   # never its arguments: they can carry a secret
    status = Status()
    status.write(state="started")
    try:
        child = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        note("the server did not start (%s)" % type(e).__name__)
        status.exited(127)
        return 127
    names, pending, out_lock = Names(granted(), turn_gate()), set(), threading.Lock()

    def pump_out():
        for line in child.stdout:
            with out_lock:
                stdout.write(to_client(line, names, pending, status))
                stdout.flush()

    reader = threading.Thread(target=pump_out, daemon=True)
    reader.start()
    try:
        for line in stdin:
            forward, reply = to_server(line, names, pending)
            if reply is not None:
                with out_lock:
                    stdout.write(reply)
                    stdout.flush()
            if forward is None:
                continue
            try:
                child.stdin.write(forward)
                child.stdin.flush()
            except (BrokenPipeError, OSError):
                break
    finally:
        with contextlib.suppress(OSError):
            child.stdin.close()
    reader.join()
    rc = child.wait()
    note("exit, the server exited %s" % rc)
    status.exited(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
