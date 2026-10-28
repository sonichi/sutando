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
<instance>/primary is 200; POST <instance>/disconnect is 200 or 409
primary_cannot_disconnect; POST <instance>/reconnect is 202; POST <instance>/token
is 200 or 409 token_server_mismatch / token_agent_mismatch / token_rejected; GET
known-servers is 200 and is never read as an instance.

Run: python3 tests/agent-api-connections.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

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

# Isolated before agent-api.py is imported (its WORKSPACE_DIR is read at import), so no outer
# channel env file can be imported into the temp record by an empty list.
_WS = tempfile.mkdtemp(prefix="agent-api-connections-")
isolate_os_environ(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=_WS, SUTANDO_TELEMETRY="0",
                   SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1", SUTANDO_BRIDGE_LOCK="0")  # the spawn fake takes no lease

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


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

TOKEN_A = "https://chat.alpha-fixture.net|secret-alpha"
TOKEN_B = "https://relay.beta-fixture.net|secret-beta"
REGISTRY = {"instances": [{"server": "alpha-fixture.net", "gateway_host": "chat.alpha-fixture.net"},
                          {"server": "beta-fixture.net", "gateway_host": "relay.beta-fixture.net"}]}
C.DEFAULT_RESOLVER = lambda host: dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)


class FakeVault:
    store = {}

    def get(self, key):
        return self.store[key]

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


GATEWAY = {"https://chat.alpha-fixture.net": ["@sutando:alpha-fixture.net"],
           "https://relay.beta-fixture.net": ["@sutando:beta-fixture.net"]}
SPAWNED = []


def public_getaddrinfo(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def fetch(url, token, address=None):
    if url == "https://rejecting-fixture.net":
        raise urllib.error.HTTPError(url, 401, "nope", {}, None)
    return {"agents": [{"id": i} for i in GATEWAY.get(url, [])]}


# Above any pid_max: the spawn fake's pid never runs, so launch sees it exit at once.
DEAD_PID = 4_194_404


# Set while a promote runs: the spawned bridge also reports the gateway poll promote waits for.
CONNECTS = []


def spawn(argv, env, log_path):
    SPAWNED.append(env["GATEWAY_INSTANCE"])
    if CONNECTS:
        status = WS / "state" / f"gateway-status.{env['GATEWAY_INSTANCE']}.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({"pid": DEAD_PID, "connected": True}))
    return DEAD_PID


# The route calls connections_http with the module defaults; bind the fakes there.
_real = api.connections_http
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn, kill=lambda *a: None,
    getaddrinfo=public_getaddrinfo)

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
                           ("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha-fixture.net"}),
                           ("DELETE", "/api/v1/connections/x?force=1", None),
                           ("POST", "/api/v1/connections/x/primary", {}),
                           ("GET", "/api/v1/connections/known-servers", None),
                           ("POST", "/api/v1/connections/x/promote", {}),
                           ("POST", "/api/v1/connections/x/unpark", {}),
                           ("POST", "/api/v1/connections/x/disconnect", {}),
                           ("POST", "/api/v1/connections/x/reconnect", {}),
                           ("POST", "/api/v1/connections/x/token", {"token": TOKEN_A})):
    s, b, h = req(method, path, body, auth=False)
    check(f"{method} {path} without a bearer is 401", s == 401, f"{s} {b}")
    check(f"{method} {path} refusal carries no wildcard CORS grant", "Access-Control-Allow-Origin" not in h, h)
    s, b, _ = req(method, path, body, auth="Bearer wrong")
    check(f"{method} {path} with a wrong bearer is 401", s == 401, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", auth="Bearer caf\u00e9")
check("a non-ASCII bearer is a clean 401, not a dropped connection", s == 401, f"{s} {b}")
for origin in ("http://localhost:3000", "http://127.0.0.1:1", "http://x.localhost"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha-fixture.net"},
                  headers={"Origin": origin}, auth=False)
    check(f"confirm from loopback Origin {origin} without a bearer is 401", s == 401, f"{s} {b}")
for origin in ("https://evil-fixture.net", "null"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha-fixture.net"},
                  headers={"Origin": origin}, auth=False)
    check(f"confirm from Origin {origin} without a bearer is refused (403)", s == 403, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", host="evil-fixture.net")
check("a forged Host (DNS rebinding) is 403 even with the bearer", s == 403, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", host=f"127.0.0.1:{PORT + 1}")
check("a loopback Host on another port is 403", s == 403, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", host="localhost")
check("a loopback Host without a port passes the host check", s == 200, f"{s} {b}")
for origin in ("https://evil-fixture.net", "null", "http://localhost.evil-fixture.net"):
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
check("the 401 to a loopback Origin is echoed too, so a webview can read it and drop a stale bearer",
      s == 401 and h.get("Access-Control-Allow-Origin") == "http://localhost:5173" and h.get("Vary") == "Origin", (s, h))
for origin in ("tauri://localhost", "http://tauri.localhost"):
    s, b, h = req("GET", "/api/v1/connections", headers={"Origin": origin}, auth="Bearer wrong")
    check(f"a wrong bearer from the desktop webview {origin} is a readable 401", s == 401 and h.get("Access-Control-Allow-Origin") == origin, (s, h))
s, b, h = req("GET", "/api/v1/connections", headers={"Origin": "https://evil-fixture.net"}, auth=False)
check("a refusal to a non-loopback Origin still grants nothing", s == 403 and "Access-Control-Allow-Origin" not in h, (s, h))
s, b, h = req("OPTIONS", "/api/v1/connections", headers={"Origin": "http://localhost:5173"}, auth=False)
check("preflight from loopback is 204 echoing the Origin", s == 204 and h.get("Access-Control-Allow-Origin") == "http://localhost:5173", (s, h))
check("preflight allows DELETE and the bearer header",
      "DELETE" in h.get("Access-Control-Allow-Methods", "") and "Authorization" in h.get("Access-Control-Allow-Headers", ""), h)
s, b, h = req("OPTIONS", "/api/v1/connections", headers={"Origin": "https://evil-fixture.net"}, auth=False)
check("preflight from elsewhere grants nothing", s == 204 and "Access-Control-Allow-Origin" not in h, h)
s, b, h = req("OPTIONS", "/ping", headers={"Origin": "https://evil-fixture.net"}, auth=False)
check("other routes keep their own preflight", s == 200, s)

print("== preview, then confirm ==")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A})
check("preview is 200", s == 200, f"{s} {b}")
p = (b or {}).get("preview") or {}
check("preview names the registered, identity-confirmed server",
      (p.get("server"), p.get("server_source"), p.get("verified")) == ("alpha-fixture.net", "identity", True), p)
check("preview carries no secret", no_secret(b))
s, b, _ = req("GET", "/api/v1/connections")
check("a preview saves nothing", b == {"connections": []})

s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True})
check("confirm without expected_server is 400", s == 400, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "chat.alpha-fixture.net"})
check("a stale expected_server is 409 mismatch",
      s == 409 and b == {"error": "mismatch", "server_from_token": "alpha-fixture.net", "expected_server": "chat.alpha-fixture.net"},
      f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha-fixture.net"})
check("confirm creates with 201", s == 201 and (b or {}).get("created") is True, f"{s} {b}")
row = (b or {}).get("connection") or {}
check("the row is primary, pending, identity-sourced",
      (row.get("primary"), row.get("state"), row.get("server_source"), row.get("added_by"))
      == (True, "pending", "identity", "ui"), row)
check("the row carries the vault key name, not the secret",
      row.get("token_ref") == "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET" and no_secret(b))
check("the secret went to the vault", FakeVault.store.get("AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET") == TOKEN_A)
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://chat.alpha-fixture.net|rotated", "confirm": True,
                                              "expected_server": "alpha-fixture.net"})
check("an existing connection is 200 created:false", s == 200 and (b or {}).get("created") is False, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_B, "confirm": True, "expected_server": "beta-fixture.net"})
check("a second server is 201 and not primary", s == 201 and not b["connection"]["primary"], f"{s} {b}")
check("confirm started each new bridge, the existing one again while it was down",
      SPAWNED == ["alpha-fixture-net", "alpha-fixture-net", "beta-fixture-net"] and b["launch"]["ok"] is True, (SPAWNED, b))
check("the second lane does not take the primary", b["connection"]["primary"] is False and b["connection"]["running"] is False, b)
SPAWNED.clear()

print("== refusals the review asked for ==")
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://rejecting-fixture.net|bad", "confirm": True,
                                              "expected_server": "rejecting-fixture.net"})
check("a token the gateway rejects is 409 token_rejected", (s, b) == (409, {"error": "token_rejected"}), f"{s} {b}")
FakeVault.store["AG2_REMOTE_TOKEN_GW_UNREGISTERED_FIXTURE_NET"] = "foreign"
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://gw.unregistered-fixture.net|u", "confirm": True,
                                              "expected_server": "gw.unregistered-fixture.net"})
check("a vault key no row owns is 409 vault_key_in_use", (s, b) == (409, {"error": "vault_key_in_use"}), f"{s} {b}")
check("the foreign key is untouched", FakeVault.store["AG2_REMOTE_TOKEN_GW_UNREGISTERED_FIXTURE_NET"] == "foreign")
del FakeVault.store["AG2_REMOTE_TOKEN_GW_UNREGISTERED_FIXTURE_NET"]
for host in ("127.0.0.1:9", "10.0.0.1", "localhost", "intranet", "foo.localhost:7843", "printer.local", "gw.internal"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": f"https://{host}/relay|s"})
    check(f"a token aimed at {host} is 400 invalid_gateway, unprobed", (s, b) == (400, {"error": "invalid_gateway"}), f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": "http://chat.alpha-fixture.net/relay|s"})
check("an http gateway is 400 invalid_gateway", (s, b) == (400, {"error": "invalid_gateway"}), f"{s} {b}")
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=lambda *a: check("never probed", False) or {},
    spawn=spawn, getaddrinfo=lambda h, p, **k: [(2, 1, 6, "", ("10.0.0.5", p))])
s, b, _ = req("POST", "/api/v1/connections", {"token": "https://chat.alpha-fixture.net/relay|s", "confirm": True,
                                              "expected_server": "alpha-fixture.net"})
check("a name resolving to a private address is 400 invalid_gateway on confirm, unprobed",
      (s, b) == (400, {"error": "invalid_gateway"}), f"{s} {b}")
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn, kill=lambda *a: None,
    getaddrinfo=public_getaddrinfo)
for inst in (["x"], 5):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_B, "confirm": True, "expected_server": "beta-fixture.net",
                                                  "instance": inst})
    check(f"instance {inst!r} is 400, not a crash", s == 400 and "error" in (b or {}), f"{s} {b}")
for inst in C.RESERVED_INSTANCES:
    s, b, _ = req("POST", "/api/v1/connections", {"token": "https://gw.unregistered-fixture.net|u", "confirm": True,
                                                  "expected_server": "gw.unregistered-fixture.net", "instance": inst})
    check(f"the fixed route name {inst!r} is refused as an instance (400 reserved)",
          s == 400 and "reserved" in (b or {}).get("error", ""), f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections")
check("no reserved-name row was written", [r["instance"] for r in b["connections"]] == ["alpha-fixture-net", "beta-fixture-net"], b)

print("== list with status ==")
(WS / "state" / "gateway-status.alpha-fixture-net.json").write_text(json.dumps({"connected": True, "last_ok_ts": 7}))
s, b, _ = req("GET", "/api/v1/connections")
rows = (b or {}).get("connections") or []
check("GET lists both rows joined with status and server_source",
      [(r["instance"], r["state"], r["server_source"]) for r in rows]
      == [("alpha-fixture-net", "connected", "identity"), ("beta-fixture-net", "pending", "identity")], rows)
check("GET leaks no secret", no_secret(b))

print("== bad input ==")
for body, label in (({}, "missing token"), ({"token": ""}, "empty token"), ({"token": TOKEN_A, "confirm": "yes"}, "confirm not bool"),
                    ({"token": "opaque"}, "token without gateway URL")):
    s, b, _ = req("POST", "/api/v1/connections", body)
    check(f"{label} is 400", s == 400 and "error" in (b or {}), f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/nope/primary", {})
check("unknown instance is 404", s == 404, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/alpha-fixture-net/unknown", {})
check("unknown sub-route is 404", s == 404, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", b"{not json")
check("invalid JSON is 400", s == 400, f"{s} {b}")

print("== known servers, disconnect, reconnect ==")
dotspace.registered_instances = lambda: [dotspace.DotSpace(server="alpha-fixture.net"),
                                         dotspace.DotSpace(server="gamma-fixture.net")]
s, b, _ = req("GET", "/api/v1/connections/known-servers")
check("known-servers is 200 with the registry joined to the rows", s == 200 and b == {"servers": [
    {"server": "alpha-fixture.net", "connected": True, "instance": "alpha-fixture-net", "primary": True, "disabled": False},
    {"server": "gamma-fixture.net", "connected": False, "instance": None, "primary": False, "disabled": False}]}, f"{s} {b}")
check("known-servers leaks no secret or vault key", no_secret(b) and "token_ref" not in json.dumps(b))
s, b, _ = req("POST", "/api/v1/connections/known-servers", {})
check("POST known-servers is 405, never a slug lookup", s == 405, f"{s} {b}")
s, b, _ = req("DELETE", "/api/v1/connections/known-servers")
check("DELETE known-servers is 405, never a removal", s == 405, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/alpha-fixture-net/disconnect", {})
check("disconnecting the primary is 409 primary_cannot_disconnect", (s, b) == (409, {"error": "primary_cannot_disconnect"}), f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/disconnect", {})
check("disconnect is 200 with the parked row", s == 200 and (b["connection"]["disabled"], b["connection"]["state"],
      b["connection"]["reason"]) == (True, "parked", "disabled"), f"{s} {b}")
check("a restart's launch plan leaves the parked row down",
      [e["instance"] for e in C.launch_plan({}, workspace=WS)] == ["alpha-fixture-net"])
check("disconnect leaks no secret", no_secret(b))
check("the token stays in the vault", "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET" in FakeVault.store)
s, b, _ = req("GET", "/api/v1/connections")
check("the list shows the flag and the state", [(r["instance"], r["disabled"], r["state"]) for r in b["connections"]]
      == [("alpha-fixture-net", False, "connected"), ("beta-fixture-net", True, "parked")], b)
check("every row spells out the identity fields", all(k in r for r in b["connections"] for k in C.IDENTITY_FIELDS))
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/disconnect", {})
check("disconnecting twice is still 200", s == 200 and b["connection"]["disabled"] is True, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/primary", {})
check("a disabled row made primary is 409 disabled_cannot_be_primary", (s, b) == (409, {"error": "disabled_cannot_be_primary"}), f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections")
check("the primary did not move", [r["primary"] for r in b["connections"]] == [True, False], b)
# A production-writer lease the route's kill fake cannot clear: the stop deadline passes.
from _helpers import bridge_lease  # noqa: E402
_holder = bridge_lease.start(WS, "alpha-fixture-net")
C.STOP_TIMEOUT_S, _saved_timeout = 0.3, C.STOP_TIMEOUT_S
s, b, _ = req("DELETE", "/api/v1/connections/alpha-fixture-net?force=1")
check("removing a row whose bridge will not stop is 409 bridge_still_running",
      (s, b) == (409, {"error": "bridge_still_running"}), f"{s} {b}")
C.STOP_TIMEOUT_S = _saved_timeout
bridge_lease.stop(_holder)
s, b, _ = req("GET", "/api/v1/connections")
check("the row and its key stayed", [r["instance"] for r in b["connections"]] == ["alpha-fixture-net", "beta-fixture-net"]
      and "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET" in FakeVault.store, b)
s, b, _ = req("POST", "/api/v1/connections/nope/disconnect", {})
check("disconnecting an unknown instance is 404", s == 404, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/reconnect", {})
check("reconnect is 202 and re-enables", s == 202 and b["launch"]["ok"] is True and SPAWNED == ["beta-fixture-net"], f"{s} {b} {SPAWNED}")
s, b, _ = req("GET", "/api/v1/connections")
check("the flag is cleared", [r["disabled"] for r in b["connections"]] == [False, False], b)
SPAWNED.clear()

print("== update token ==")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/disconnect", {})
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {})
check("a missing token is 400", s == 400 and (b or {}).get("error") == "missing required field: token", f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": TOKEN_A})
check("a code for another server is 409 token_server_mismatch", (s, b) == (409, {"error": "token_server_mismatch"}), f"{s} {b}")
GATEWAY["https://relay.beta-fixture.net"] = ["@other:beta-fixture.net"]
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": "https://relay.beta-fixture.net|secret-beta-rotated"})
check("a code for another agent is 409 token_agent_mismatch", (s, b) == (409, {"error": "token_agent_mismatch"}), f"{s} {b}")
GATEWAY["https://relay.beta-fixture.net"] = []
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": "https://relay.beta-fixture.net|secret-beta-rotated"})
check("a code whose gateway names no agent for a row that names one is 409 token_unverified",
      (s, b) == (409, {"error": "token_unverified"}), f"{s} {b}")
GATEWAY["https://relay.beta-fixture.net"] = ["@sutando:beta-fixture.net"]
os.environ["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"] = TOKEN_B
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": "https://relay.beta-fixture.net|secret-beta-rotated"})
del os.environ["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"]
check("a token_ref set in the environment is 409 token_source_shadowed", (s, b) == (409, {"error": "token_source_shadowed"}), f"{s} {b}")
check("a refused code changed nothing", FakeVault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"] == TOKEN_B and SPAWNED == [], SPAWNED)
s, b, _ = req("GET", "/api/v1/connections")
check("the refused row is still disabled", [r["disabled"] for r in b["connections"]] == [False, True], b)


def _unknown(*a, **k):
    raise C.TokenStateUnknown("token_state_unknown: the update of dev failed after the vault write began (OSError: disk full) and reading AG2_REMOTE_TOKEN_DEV back failed, so it is not known which secret it holds")


_saved_update, api._connections.update_token = api._connections.update_token, _unknown
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": "https://relay.beta-fixture.net|secret-beta-rotated"})
api._connections.update_token = _saved_update
check("a vault that cannot be read back is 409 token_state_unknown, no secret",
      (s, b) == (409, {"error": "token_state_unknown"}), f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": "https://relay.beta-fixture.net|secret-beta-rotated"})
check("the same connection's new code is 200 with the row", s == 200 and (b["connection"]["instance"], b["connection"]["token_ref"],
      b["connection"]["disabled"], b["connection"]["state"]) == ("beta-fixture-net", "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET", False, "pending"), f"{s} {b}")
check("update leaks no secret", no_secret(b) and "secret-beta-rotated" not in json.dumps(b))
check("the secret was replaced under the same key", FakeVault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"] == "https://relay.beta-fixture.net|secret-beta-rotated")
check("the bridge was relaunched once", SPAWNED == ["beta-fixture-net"], SPAWNED)
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/token", {"token": "https://rejecting-fixture.net|bad"})
check("a code the gateway rejects is 409 token_rejected", (s, b) == (409, {"error": "token_rejected"}), f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/nope/token", {"token": TOKEN_B})
check("updating an unknown instance is 404", s == 404, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/token", {"token": TOKEN_B})
check("/connections/token alone is 404, not an instance route", s == 404, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections/beta-fixture-net/token")
check("GET <instance>/token is 404", s == 404, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections/known-servers")
check("known-servers still answers beside the token route", s == 200 and "servers" in (b or {}), f"{s} {b}")
SPAWNED.clear()

print("== primary, reconnect, delete ==")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/primary", {})
check("set primary is 200", s == 200 and b["connection"]["primary"] is True, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections")
check("exactly one primary after the move", [r["primary"] for r in b["connections"]] == [False, True])
s, b, _ = req("POST", "/api/v1/connections/alpha-fixture-net/reconnect", {})
check("reconnect is 202 with a launch record", s == 202 and b["launch"]["ok"] is True and b["launch"]["pid"] == DEAD_PID, f"{s} {b}")
check("reconnect spawned exactly that bridge", SPAWNED == ["alpha-fixture-net"], SPAWNED)
CONNECTS.append(True)
s, b, _ = req("POST", "/api/v1/connections/alpha-fixture-net/promote", {})
check("promote is 200 naming the parked row", s == 200 and (b["connection"]["primary"], b["previous"], b["changed"],
      b["relaunch_needed"]) == (True, "beta-fixture-net", True, False), f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections")
check("the old primary is parked, not removed", [(r["instance"], r["primary"], r["parked"], r["reason"]) for r in b["connections"]]
      == [("alpha-fixture-net", True, False, None), ("beta-fixture-net", False, True, "parked")], b)
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/unpark", {})
check("unpark is 200 and starts the bridge", s == 200 and b["connection"]["parked"] is False and b["launch"]["ok"] is True, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/promote", {})
check("a promote back is 200", s == 200 and b["previous"] == "alpha-fixture-net", f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections/beta-fixture-net/promote", {})
check("promoting the primary again changes nothing", s == 200 and (b["changed"], b["previous"]) == (False, None), f"{s} {b}")
CONNECTS.clear()
with C._transaction(WS) as rec:
    rec[C.LEGACY_PARKED_KEY] = True
s, b, _ = req("POST", "/api/v1/connections/legacy/unpark", {})
check("POST /connections/legacy/unpark reaches the legacy row (instance '')", s == 200 and
      (b["connection"]["instance"], b["connection"]["legacy"], b["connection"]["parked"], b["launch"]) == ("", True, False, None),
      f"{s} {b}")
check("and clears its park", C.legacy_parked(WS) is False)
s, b, _ = req("GET", "/api/v1/connections")
check("named rows say legacy false", [r["legacy"] for r in b["connections"]] == [False, False], b)
s, b, _ = req("DELETE", "/api/v1/connections/beta-fixture-net")
check("deleting the primary is 409 primary", (s, b) == (409, {"error": "primary"}), f"{s} {b}")
s, b, h = req("DELETE", "/api/v1/connections/alpha-fixture-net")
check("delete is 204 with no body", s == 204 and b is None, f"{s} {b}")
check("the vault key went with the row", "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET" not in FakeVault.store)
s, b, _ = req("GET", "/api/v1/connections")
check("the other row is untouched", [r["instance"] for r in b["connections"]] == ["beta-fixture-net"])
s, b, _ = req("DELETE", "/api/v1/connections/beta-fixture-net?force=1")
check("force deletes the primary", s == 204, f"{s} {b}")
s, b, _ = req("DELETE", "/api/v1/connections/beta-fixture-net")
check("deleting an unknown instance is 404", s == 404, f"{s} {b}")

print("== vault unavailable ==")
class RefusingVault(FakeVault):
    def set(self, key, value):
        raise RuntimeError("locked")
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=RefusingVault(), fetch=fetch, spawn=spawn, getaddrinfo=public_getaddrinfo)
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha-fixture.net"})
check("a refusing vault is 503 vault_unavailable", (s, b) == (503, {"error": "vault_unavailable"}), f"{s} {b}")
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn, getaddrinfo=public_getaddrinfo)

print("== a vault refusal while starting the added bridge ==")
_real_launch_env = C.launch_env
def _refusing_launch_env(*_a, **_kw):
    raise C.VaultUnavailable("secret vault not importable (ImportError)")
C.launch_env = _refusing_launch_env
try:
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A, "confirm": True, "expected_server": "alpha-fixture.net"})
finally:
    C.launch_env = _real_launch_env
check("the add still succeeds with the saved row", s in (200, 201) and (b or {}).get("connection", {}).get("instance") == "alpha-fixture-net", f"{s} {b}")
check("its launch is a failed record naming the class",
      ((b or {}).get("launch") or {}).get("ok") is False and b["launch"].get("reason") == "vault_unavailable", f"{b}")
s, b, _ = req("GET", "/api/v1/connections")
check("the row persists", "alpha-fixture-net" in [r["instance"] for r in (b or {}).get("connections") or []], f"{s} {b}")

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

print("== an empty record lists the token the connect flow wrote to the channel env file ==")
_HOME = Path(tempfile.mkdtemp(prefix="agent-api-claude-home-"))
(_HOME / "channels" / "ag2space").mkdir(parents=True)
(_HOME / "channels" / "ag2space" / "relay-client.env").write_text(f"REMOTE_TASK_TOKEN={TOKEN_A}\n")
os.environ["CLAUDE_CONFIG_DIR"] = str(_HOME)
try:
    s, b, _ = req("GET", "/api/v1/connections")
finally:
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
rows = (b or {}).get("connections") or []
check("GET on an empty record answers the imported primary row without a restart",
      s == 200 and [(r["instance"], r["token_ref"], r["primary"], r["added_by"]) for r in rows]
      == [("", "REMOTE_TASK_TOKEN", True, "env")], f"{s} {b}")
check("the imported secret went to the vault, never the response",
      FakeVault.store.get("REMOTE_TASK_TOKEN") == TOKEN_A and no_secret(b))
check("the row is persisted", (WS / "state" / "connections.json").is_file())
s, b, _ = req("GET", "/api/v1/connections")
check("a later GET lists the same row from the record", [r["token_ref"] for r in (b or {}).get("connections", [])] == ["REMOTE_TASK_TOKEN"], b)

server.server_close()
print()
if failures:
    print(f"FAILED: {len(failures)} — {failures}")
    sys.exit(1)
print("PASS — agent-api connections routes")
