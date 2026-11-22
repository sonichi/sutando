"""ag2-sparrow MCP name shim: a stdio MCP server that runs another one and renames its tools for clients that
only take `[A-Za-z0-9_-]{1,64}` tool names.

Antigravity CLI (agy) drops every MCP tool whose name has a dot: through the desktop's `ag2-mcp-proxy` it saw
only `ui_describe`, `ui_request` and `ui_result` of AG2 Space's tools (`room.action.execute`, `ag2.whoami`, ...
were not offered). This shim sits between agy and that proxy and changes nothing but names: a `tools/list`
answer's tool names become `room_action_execute` and so on (`alias`), and a `tools/call` for an alias is sent
on under the server's own name. Every other message passes through as it came, and the child's environment is
this process's (the proxy scrubs its own).

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
    """The aliases handed out so far: alias -> server name (a later tools/list may add or change them)."""

    def __init__(self):
        self.to_server = {}
        self.lock = threading.Lock()

    def rename_tools(self, tools):
        """Rename in place; an alias once handed out keeps its server tool across pages and later lists."""
        with self.lock:
            taken = dict(self.to_server)
            for tool in tools:
                name = tool.get("name") if isinstance(tool, dict) else None
                if not isinstance(name, str) or not name:
                    continue
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


def _messages(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    return msg if isinstance(msg, (dict, list)) else None


def _each(msg):
    return msg if isinstance(msg, list) else [msg]


def to_server(line, names, pending):
    """A client line as the server should get it: a `tools/call` for an alias under the server's name; the id of
    each `tools/list` request remembered so its answer is renamed."""
    msg = _messages(line)
    if msg is None:
        return line
    changed = False
    for m in _each(msg):
        if not isinstance(m, dict):
            continue
        if m.get("method") == "tools/list" and "id" in m:
            pending.add(json.dumps(m["id"]))
        elif m.get("method") == "tools/call" and isinstance(m.get("params"), dict):
            name = m["params"].get("name")
            if isinstance(name, str) and names.server_name(name) != name:
                m["params"]["name"] = names.server_name(name)
                changed = True
    return json.dumps(msg) + "\n" if changed else line


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
    names, pending, out_lock = Names(), set(), threading.Lock()

    def pump_out():
        for line in child.stdout:
            with out_lock:
                stdout.write(to_client(line, names, pending, status))
                stdout.flush()

    reader = threading.Thread(target=pump_out, daemon=True)
    reader.start()
    try:
        for line in stdin:
            try:
                child.stdin.write(to_server(line, names, pending))
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
