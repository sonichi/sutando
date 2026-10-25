#!/usr/bin/env python3
"""E2E test for the connections routes on agent-api — a REAL HTTP server on an
ephemeral port, the module's workspace patched to a temp dir, the vault, the
gateway probe, the resolver and the bridge spawn replaced by fakes.

Contract: every /api/v1/connections route, GET included, needs the local API
bearer (SUTANDO_API_TOKEN, else state/agent-api.token), a loopback Host on the
bound port and an absent or loopback Origin; refusals are 401/403 and never
carry a wildcard CORS grant. With the bearer: GET lists rows joined with status
(server_source included, never a secret); POST previews, then creates on
confirm (201 / 200 existing / 409 mismatch, token_rejected, vault_key_in_use /
400 / 503 vault); DELETE <instance> is 204 or 409 primary; POST
<instance>/primary is 200; POST <instance>/reconnect is 202.

Run: python3 tests/agent-api-connections.test.py
"""
import http.client
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

_WS = tempfile.mkdtemp(prefix="agent-api-connections-")
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
import connections as C  # noqa: E402
import dotspace  # noqa: E402

WS = Path(_WS)
assert api.WORKSPACE_DIR == WS.resolve(), api.WORKSPACE_DIR
api.API_TOKEN = ""

TOKEN_A = "https://chat.alpha.example|secret-alpha"
TOKEN_B = "https://relay.beta.example|secret-beta"
REGISTRY = {"instances": [{"server": "alpha.example", "gateway_host": "chat.alpha.example"},
                          {"server": "beta.example", "gateway_host": "relay.beta.example"}]}
C.DEFAULT_RESOLVER = lambda host: dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)


class FakeVault:
    store = {}

    def get(self, key):
        return self.store[key]

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


GATEWAY = {"https://chat.alpha.example": ["@sutando:alpha.example"],
           "https://relay.beta.example": ["@sutando:beta.example"]}
SPAWNED = []


def fetch(url, token):
    if url == "https://rejecting.example":
        raise urllib.error.HTTPError(url, 401, "nope", {}, None)
    return {"agents": [{"id": i} for i in GATEWAY.get(url, [])]}


def spawn(argv, env, log_path):
    SPAWNED.append(env["GATEWAY_INSTANCE"])
    return 99


# The route calls connections_http with the module defaults; bind the fakes there.
_real = api.connections_http
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn, kill=lambda *a: None)

server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
PORT = server.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"
BEARER = api.connections_bearer()
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


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


def req(method, path, body=None, headers=None, auth=True, host=None):
    """Raw request: (status, parsed body or None, headers). `host` overrides the Host header."""
    def go():
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        hdrs = {"Host": host or f"127.0.0.1:{PORT}"}
        if auth:
            hdrs["Authorization"] = f"Bearer {BEARER}" if auth is True else auth
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        parsed = json.loads(raw) if raw else None
        return resp.status, parsed, dict(resp.getheaders())
    return serve_while(go)


def no_secret(body):
    return "secret-alpha" not in json.dumps(body) and "secret-beta" not in json.dumps(body)


print("== the per-install bearer ==")
token_path = WS / "state" / api.local_api_gate.LOCAL_API_TOKEN_NAME
check("the bearer was minted into state/agent-api.token", token_path.read_text().strip() == BEARER and len(BEARER) >= 32)
check("the token file is 0600", oct(token_path.stat().st_mode & 0o777) == "0o600")
check("minting is idempotent", api.connections_bearer() == BEARER)

print("== fail closed without the bearer (the security review's repros) ==")
for method, path, body in (("GET", "/api/v1/connections", None),
                           ("POST", "/api/v1/connections", {"token": TOKEN_A}),
                           ("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha.example"}),
                           ("DELETE", "/api/v1/connections/x?force=1", None),
                           ("POST", "/api/v1/connections/x/primary", {}),
                           ("POST", "/api/v1/connections/x/reconnect", {})):
    s, b, h = req(method, path, body, auth=False)
    check(f"{method} {path} without a bearer is 401", s == 401, f"{s} {b}")
    check(f"{method} {path} refusal carries no wildcard CORS grant", "Access-Control-Allow-Origin" not in h, h)
    s, b, _ = req(method, path, body, auth="Bearer wrong")
    check(f"{method} {path} with a wrong bearer is 401", s == 401, f"{s} {b}")
for origin in ("http://localhost:3000", "http://127.0.0.1:1", "http://x.localhost"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha.example"},
                  headers={"Origin": origin}, auth=False)
    check(f"confirm from loopback Origin {origin} without a bearer is 401", s == 401, f"{s} {b}")
for origin in ("https://evil.example", "null"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha.example"},
                  headers={"Origin": origin}, auth=False)
    check(f"confirm from Origin {origin} without a bearer is refused (403)", s == 403, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", host="evil.example")
check("a forged Host (DNS rebinding) is 403 even with the bearer", s == 403, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", host=f"127.0.0.1:{PORT + 1}")
check("a loopback Host on another port is 403", s == 403, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", host="localhost")
check("a loopback Host without a port passes the host check", s == 200, f"{s} {b}")
for origin in ("https://evil.example", "null", "http://localhost.evil.example"):
    s, b, h = req("POST", "/api/v1/connections", {"token": TOKEN_A}, headers={"Origin": origin})
    check(f"a non-loopback Origin {origin} is 403 even with the bearer", s == 403, f"{s} {b}")
    check(f"no CORS grant is echoed to {origin}", "Access-Control-Allow-Origin" not in h, h)
check("nothing was stored by the refused requests", (FakeVault.store, SPAWNED) == ({}, []))
s, b, _ = req("GET", "/api/v1/connections")
check("nothing was stored by the refused requests (record)", b == {"connections": []}, b)

print("== CORS: echo the loopback Origin only, never * ==")
s, b, h = req("GET", "/api/v1/connections", headers={"Origin": "http://localhost:5173"})
check("a loopback Origin with the bearer is echoed", s == 200 and h.get("Access-Control-Allow-Origin") == "http://localhost:5173", h)
check("Vary: Origin accompanies the echo", h.get("Vary") == "Origin", h)
s, b, h = req("GET", "/api/v1/connections", headers={"Origin": "http://localhost:5173"}, auth=False)
check("the 401 to a loopback Origin still has no grant", s == 401 and "Access-Control-Allow-Origin" not in h, h)
s, b, h = req("OPTIONS", "/api/v1/connections", headers={"Origin": "http://localhost:5173"}, auth=False)
check("preflight from loopback is 204 echoing the Origin", s == 204 and h.get("Access-Control-Allow-Origin") == "http://localhost:5173", (s, h))
check("preflight allows DELETE and the bearer header",
      "DELETE" in h.get("Access-Control-Allow-Methods", "") and "Authorization" in h.get("Access-Control-Allow-Headers", ""), h)
s, b, h = req("OPTIONS", "/api/v1/connections", headers={"Origin": "https://evil.example"}, auth=False)
check("preflight from elsewhere grants nothing", s == 204 and "Access-Control-Allow-Origin" not in h, h)
s, b, h = req("OPTIONS", "/ping", headers={"Origin": "https://evil.example"}, auth=False)
check("other routes keep their own preflight", s == 200, s)

print("== preview, then confirm ==")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A})
check("preview is 200", s == 200, f"{s} {b}")
p = (b or {}).get("preview") or {}
check("preview names the registered, identity-confirmed server",
      (p.get("server"), p.get("server_source"), p.get("verified")) == ("alpha.example", "identity", True), p)
check("preview carries no secret", no_secret(b))
s, b, _ = req("GET", "/api/v1/connections")
check("a preview saves nothing", b == {"connections": []})

s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True})
check("confirm without expected_server is 400", s == 400, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "chat.alpha.example"})
check("a stale expected_server is 409 mismatch",
      s == 409 and b == {"error": "mismatch", "server_from_token": "alpha.example", "expected_server": "chat.alpha.example"},
      f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha.example"})
check("confirm creates with 201", s == 201 and (b or {}).get("created") is True, f"{s} {b}")
row = (b or {}).get("connection") or {}
check("the row is primary, pending, identity-sourced",
      (row.get("primary"), row.get("state"), row.get("server_source"), row.get("added_by"))
      == (True, "pending", "identity", "ui"), row)
check("the row carries the vault key name, not the secret",
      row.get("token_ref") == "AG2_REMOTE_TOKEN_ALPHA_EXAMPLE" and no_secret(b))
check("the secret went to the vault", FakeVault.store.get("AG2_REMOTE_TOKEN_ALPHA_EXAMPLE") == TOKEN_A)
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://chat.alpha.example|rotated", "confirm": True,
                                              "expected_server": "alpha.example"})
check("an existing connection is 200 created:false", s == 200 and (b or {}).get("created") is False, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_B, "confirm": True, "expected_server": "beta.example"})
check("a second server is 201 and not primary", s == 201 and not b["connection"]["primary"], f"{s} {b}")

print("== refusals the review asked for ==")
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://rejecting.example|bad", "confirm": True,
                                              "expected_server": "rejecting.example"})
check("a token the gateway rejects is 409 token_rejected", (s, b) == (409, {"error": "token_rejected"}), f"{s} {b}")
FakeVault.store["AG2_REMOTE_TOKEN_GW_UNREGISTERED_EXAMPLE"] = "foreign"
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://gw.unregistered.example|u", "confirm": True,
                                              "expected_server": "gw.unregistered.example"})
check("a vault key no row owns is 409 vault_key_in_use", (s, b) == (409, {"error": "vault_key_in_use"}), f"{s} {b}")
check("the foreign key is untouched", FakeVault.store["AG2_REMOTE_TOKEN_GW_UNREGISTERED_EXAMPLE"] == "foreign")
del FakeVault.store["AG2_REMOTE_TOKEN_GW_UNREGISTERED_EXAMPLE"]
for host in ("127.0.0.1:9", "10.0.0.1", "localhost", "intranet"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": f"http://{host}/relay|s"})
    check(f"a token aimed at {host} is 400 invalid_gateway, unprobed", (s, b) == (400, {"error": "invalid_gateway"}), f"{s} {b}")
for inst in (["x"], 5):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_B, "confirm": True, "expected_server": "beta.example",
                                                  "instance": inst})
    check(f"instance {inst!r} is 400, not a crash", s == 400 and "error" in (b or {}), f"{s} {b}")

print("== list with status ==")
(WS / "state" / "gateway-status.alpha-example.json").write_text(json.dumps({"connected": True, "last_ok_ts": 7}))
s, b, _ = req("GET", "/api/v1/connections")
rows = (b or {}).get("connections") or []
check("GET lists both rows joined with status and server_source",
      [(r["instance"], r["state"], r["server_source"]) for r in rows]
      == [("alpha-example", "connected", "identity"), ("beta-example", "pending", "identity")], rows)
check("GET leaks no secret", no_secret(b))

print("== bad input ==")
for body, label in (({}, "missing token"), ({"token": ""}, "empty token"), ({"token": TOKEN_A, "confirm": "yes"}, "confirm not bool"),
                    ({"token": "opaque"}, "token without gateway URL")):
    s, b, _ = req("POST", "/api/v1/connections", body)
    check(f"{label} is 400", s == 400 and "error" in (b or {}), f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/nope/primary", {})
check("unknown instance is 404", s == 404, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/alpha-example/unknown", {})
check("unknown sub-route is 404", s == 404, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", b"{not json")
check("invalid JSON is 400", s == 400, f"{s} {b}")

print("== primary, reconnect, delete ==")
s, b, _ = req("POST", "/api/v1/connections/beta-example/primary", {})
check("set primary is 200", s == 200 and b["connection"]["primary"] is True, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections")
check("exactly one primary after the move", [r["primary"] for r in b["connections"]] == [False, True])
s, b, _ = req("POST", "/api/v1/connections/alpha-example/reconnect", {})
check("reconnect is 202 with a launch record", s == 202 and b["launch"]["ok"] is True and b["launch"]["pid"] == 99, f"{s} {b}")
check("reconnect spawned exactly that bridge", SPAWNED == ["alpha-example"], SPAWNED)
s, b, _ = req("DELETE", "/api/v1/connections/beta-example")
check("deleting the primary is 409 primary", (s, b) == (409, {"error": "primary"}), f"{s} {b}")
s, b, h = req("DELETE", "/api/v1/connections/alpha-example")
check("delete is 204 with no body", s == 204 and b is None, f"{s} {b}")
check("the vault key went with the row", "AG2_REMOTE_TOKEN_ALPHA_EXAMPLE" not in FakeVault.store)
s, b, _ = req("GET", "/api/v1/connections")
check("the other row is untouched", [r["instance"] for r in b["connections"]] == ["beta-example"])
s, b, _ = req("DELETE", "/api/v1/connections/beta-example?force=1")
check("force deletes the primary", s == 204, f"{s} {b}")
s, b, _ = req("DELETE", "/api/v1/connections/beta-example")
check("deleting an unknown instance is 404", s == 404, f"{s} {b}")

print("== vault unavailable ==")
class RefusingVault(FakeVault):
    def set(self, key, value):
        raise RuntimeError("locked")
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=RefusingVault(), fetch=fetch, spawn=spawn)
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha.example"})
check("a refusing vault is 503 vault_unavailable", (s, b) == (503, {"error": "vault_unavailable"}), f"{s} {b}")
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn)

print("== SUTANDO_API_TOKEN, when set, is the bearer ==")
api.API_TOKEN = "operator-token"
s, b, _ = req("GET", "/api/v1/connections")
check("the minted token stops working once the operator set one", s == 401, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", auth="Bearer operator-token")
check("the operator's token reads", s == 200, f"{s} {b}")
api.API_TOKEN = ""

print("== DELETE elsewhere ==")
s, b, _ = req("DELETE", "/task")
check("DELETE outside connections is 404", s == 404, f"{s} {b}")

print("== in-process handler branches ==")


class FakeRFile:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body


def handler(method, path, headers, body=b""):
    h = api.Handler.__new__(api.Handler)
    h.path, h.headers, h.rfile, h.out = path, headers, FakeRFile(body), []
    h.send_response = lambda status: h.out.append(status)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.wfile = type("W", (), {"write": lambda self_, data: h.out.append(json.loads(data))})()
    h.server = server
    h.client_address = ("127.0.0.1", 1)
    h._connections_route(method)
    return h.out[0], (h.out[1] if len(h.out) > 1 else None)
AUTH = {"Host": f"127.0.0.1:{PORT}", "Authorization": f"Bearer {BEARER}"}
check("a bad Content-Length is 400",
      handler("POST", "/api/v1/connections", dict(AUTH, **{"Content-Length": "nope"}))[0] == 400)
check("an oversized body is 413",
      handler("POST", "/api/v1/connections", dict(AUTH, **{"Content-Length": str(api.CONNECTIONS_BODY_CAP + 1)}))[0] == 413)
check("a JSON body that is not an object is 400",
      handler("POST", "/api/v1/connections", dict(AUTH, **{"Content-Length": "2"}), b"[]") == (400, {"error": "body must be a JSON object"}))
check("an unparseable Origin is refused",
      handler("POST", "/api/v1/connections", dict(AUTH, **{"Content-Length": "0", "Origin": "http://[bad"}))[0] == 403)
h = api.Handler.__new__(api.Handler)
h.server = object()
check("a handler without a bound port still gates on host and bearer",
      api.local_api_gate.bearer_request_allowed(None, "127.0.0.1", f"Bearer {BEARER}", expected_token=BEARER, bound_port=None)
      == (200, None))
(WS / "state" / "connections.json").write_text("{broken")
status, body = _real("GET", "/api/v1/connections", {}, {}, workspace=WS)
check("a broken record is 500 with the schema error", status == 500 and "connections.json" in body["error"], (status, body))
(WS / "state" / "connections.json").unlink()

server.server_close()
print()
if failures:
    print(f"FAILED: {len(failures)} — {failures}")
    sys.exit(1)
print("PASS — agent-api connections routes")
