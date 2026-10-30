#!/usr/bin/env python3
"""The agent API's owner-history gate is `local_api_gate.private_history_request_allowed`,
and `AgentHandler.check_private_history_auth` only carries its verdict to the wire.

Contract: the handler passes the client address, the Origin header and the Authorization
header to the shared gate with the module's API_TOKEN, sends the returned status and
`{"error": <reason>}` through `send_private_json` on refusal, and sends nothing on 200. Over a
real socket, /tasks/history answers 200 to a silent loopback client, 403 to a loopback client
carrying an Origin, 401 to a wrong or missing bearer once a token is configured and 200 to the
right one — the bodies the route has always sent.

Run: python3 tests/agent-api-private-history-gate.test.py
"""
import http.client
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

_WS = tempfile.mkdtemp(prefix="agent-api-private-history-")
# Must be set BEFORE agent-api.py is imported: its WORKSPACE_DIR is read at import.
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = _WS
os.environ.setdefault("SUTANDO_TELEMETRY", "0")
os.environ["SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER"] = "1"
os.environ.pop("SUTANDO_API_TOKEN", None)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
assert api.WORKSPACE_DIR == Path(_WS).resolve(), api.WORKSPACE_DIR
api.API_TOKEN = ""

REFUSED = {"error": "task history requires a local proxy or API token"}
UNAUTHORIZED = {"error": "unauthorized"}
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def handler(client_ip, headers):
    """A Handler with no socket: the gate's verdicts are collected from send_private_json."""
    h = api.Handler.__new__(api.Handler)
    h.client_address = (client_ip, 1)
    h.headers = headers
    h.sent = []
    h.send_private_json = lambda status, body: h.sent.append((status, body))
    return h


print("== delegation: the handler forwards the request and sends the verdict ==")
for token, verdict in (("", (200, None)), ("", (403, "task history requires a local proxy or API token")),
                       ("secret", (401, "unauthorized")), ("secret", (200, None))):
    api.API_TOKEN = token
    h = handler("198.51.100.7", {"Origin": "https://page.example", "Authorization": "Bearer presented"})
    with mock.patch.object(api.local_api_gate, "private_history_request_allowed", return_value=verdict) as gate:
        allowed = h.check_private_history_auth()
    check(f"token={token!r} verdict={verdict}: the shared gate is called with the request's values",
          gate.call_args == mock.call("198.51.100.7", "https://page.example", "Bearer presented",
                                      configured_token=token), gate.call_args)
    if verdict[0] == 200:
        check("a 200 verdict returns True and sends nothing", allowed is True and h.sent == [], h.sent)
    else:
        check(f"a {verdict[0]} verdict returns False and sends its status and reason",
              allowed is False and h.sent == [(verdict[0], {"error": verdict[1]})], h.sent)

api.API_TOKEN = ""
h = handler("127.0.0.1", {})
with mock.patch.object(api.local_api_gate, "private_history_request_allowed", return_value=(200, None)) as gate:
    h.check_private_history_auth()
check("an absent Origin is forwarded as None and an absent Authorization as the empty string",
      gate.call_args == mock.call("127.0.0.1", None, "", configured_token=""), gate.call_args)

print("== the real gate through the handler, without a socket ==")
h = handler("203.0.113.9", {})
check("a non-loopback client without a token is 403",
      h.check_private_history_auth() is False and h.sent == [(403, REFUSED)], h.sent)
h = handler("not-an-ip", {})
check("an unparsable client address is 403", h.check_private_history_auth() is False and h.sent == [(403, REFUSED)], h.sent)
h = handler("::1", {})
check("IPv6 loopback without an Origin is allowed", h.check_private_history_auth() is True and h.sent == [], h.sent)

print("== end to end on /tasks/history ==")
server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
PORT = server.server_address[1]


def serve_while(fn):
    out = {}

    def go():
        out["value"] = fn()
    t = threading.Thread(target=go)
    t.start()
    while t.is_alive():
        server.handle_request()
    t.join()
    return out["value"]


def req(path, headers=None, method="GET"):
    def go():
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else None)
    return serve_while(go)


api.API_TOKEN = ""
s, b = req("/tasks/history")
check("no token: a silent loopback client reads history", s == 200 and "tasks" in b, (s, b))
s, b = req("/tasks/history", {"Origin": "http://127.0.0.1:5173"})
check("no token: a loopback Origin is 403 with the route's body", (s, b) == (403, REFUSED), (s, b))
s, b = req("/tasks/history", {"Authorization": "Bearer anything"})
check("no token: a bearer does not open a page's request", s == 200, (s, b))
s, b = req("/tasks/history", {"Origin": "https://evil.example", "Authorization": "Bearer anything"})
check("no token: a bearer does not override the Origin refusal", (s, b) == (403, REFUSED), (s, b))

api.API_TOKEN = "secret"
s, b = req("/tasks/history")
check("token: a missing bearer is 401 with the route's body", (s, b) == (401, UNAUTHORIZED), (s, b))
s, b = req("/tasks/history", {"Authorization": "Bearer wrong"})
check("token: a wrong bearer is 401", (s, b) == (401, UNAUTHORIZED), (s, b))
s, b = req("/tasks/history", {"Authorization": "Bearer secret"})
check("token: the right bearer reads history", s == 200 and "tasks" in b, (s, b))
s, b = req("/tasks/history", {"Authorization": "Bearer secret", "Origin": "https://page.example"})
check("token: the bearer decides alone, an Origin does not refuse", s == 200, (s, b))
s, b = req("/tasks/workstreams/infer", {"Authorization": "Bearer wrong"}, method="POST")
check("the other history route shares the gate", (s, b) == (401, UNAUTHORIZED), (s, b))

server.server_close()
print()
if failures:
    print(f"FAILED: {len(failures)} — {failures}")
    sys.exit(1)
print("PASS — agent-api private history gate")
