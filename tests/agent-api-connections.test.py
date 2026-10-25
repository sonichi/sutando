#!/usr/bin/env python3
"""E2E test for the connections routes on agent-api — a REAL HTTP server on an
ephemeral port, the module's workspace patched to a temp dir, the vault, the
gateway probe and the bridge spawn replaced by fakes.

Contract: GET /api/v1/connections lists rows joined with status (server_source
included, never a secret); POST previews, then creates on confirm (201 / 200
existing / 409 mismatch / 400 / 503 vault); DELETE <instance> is 204 or 409
primary; POST <instance>/primary is 200; POST <instance>/reconnect is 202.
Mutations are gated like POST /task (bearer when SUTANDO_API_TOKEN is set) and
refuse a non-loopback Origin; the preflight allows DELETE and Content-Type.

Run: python3 tests/agent-api-connections.test.py
"""
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


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
import connections as C  # noqa: E402

WS = Path(_WS)
assert api.WORKSPACE_DIR == WS.resolve(), api.WORKSPACE_DIR
api.API_TOKEN = ""

TOKEN_A = "https://chat.alpha.example|secret-alpha"
TOKEN_B = "https://relay.beta.example|secret-beta"


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
    return {"agents": [{"id": i} for i in GATEWAY.get(url, [])]}


def spawn(argv, env, log_path):
    SPAWNED.append(env["GATEWAY_INSTANCE"])
    return 99


# The route calls connections_http with the module defaults; bind the fakes there.
_real = api.connections_http
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn, kill=lambda *a: None)
C.DEFAULT_RESOLVER = lambda host: None

server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
BASE = f"http://127.0.0.1:{server.server_address[1]}"
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def req(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    if data is not None:
        r.add_header("Content-Type", "application/json")
    out = {}

    def go():
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                out["status"], out["headers"], raw = resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            out["status"], out["headers"], raw = e.code, dict(e.headers), e.read()
        except Exception as e:  # noqa: BLE001 - a reset is a finding, not a crash
            out["status"], out["headers"], raw = None, {}, json.dumps({"transport_error": str(e)}).encode()
        out["body"] = json.loads(raw) if raw else None
    t = threading.Thread(target=go)
    t.start()
    while t.is_alive():
        server.handle_request()
    t.join()
    return out["status"], out["body"], out["headers"]


def no_secret(body):
    return "secret-alpha" not in json.dumps(body) and "secret-beta" not in json.dumps(body)


print("== CORS preflight ==")
s, _, h = req("OPTIONS", "/api/v1/connections")
check("preflight is 200", s == 200)
methods = h.get("Access-Control-Allow-Methods", "")
check("preflight allows DELETE", "DELETE" in methods, methods)
check("preflight keeps Content-Type", "Content-Type" in h.get("Access-Control-Allow-Headers", ""))

print("== empty list ==")
s, b, _ = req("GET", "/api/v1/connections")
check("GET is 200 with an empty list", (s, b) == (200, {"connections": []}), f"{s} {b}")

print("== preview, then confirm ==")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A})
check("preview is 200", s == 200, f"{s} {b}")
p = (b or {}).get("preview") or {}
check("preview names the identity server", p.get("server") == "alpha.example" and p.get("server_source") == "identity", p)
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
check("an existing server is 200 created:false", s == 200 and (b or {}).get("created") is False, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_B, "confirm": True, "expected_server": "beta.example"})
check("a second server is 201 and not primary", s == 201 and not b["connection"]["primary"], f"{s} {b}")

print("== list with status ==")
(WS / "state" / "gateway-status.alpha-example.json").write_text(json.dumps({"connected": True, "last_ok_ts": 7}))
s, b, _ = req("GET", "/api/v1/connections")
rows = (b or {}).get("connections") or []
check("GET lists both rows joined with status",
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
r = urllib.request.Request(BASE + "/api/v1/connections", data=b"{not json", method="POST",
                           headers={"Content-Type": "application/json"})
t = threading.Thread(target=lambda: (lambda: None)())
try:
    def go_raw():
        try:
            urllib.request.urlopen(r, timeout=5)
            raw_status.append(200)
        except urllib.error.HTTPError as e:
            raw_status.append(e.code)
    raw_status = []
    t = threading.Thread(target=go_raw)
    t.start()
    while t.is_alive():
        server.handle_request()
    t.join()
    check("invalid JSON is 400", raw_status == [400], raw_status)
finally:
    pass

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
s, b, _ = req("DELETE", "/api/v1/connections/alpha-example")
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

print("== origin gate ==")
for origin in ("https://evil.example", "null", "http://localhost.evil.example"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A}, headers={"Origin": origin})
    check(f"a mutation from Origin {origin} is 403", s == 403, f"{s} {b}")
    s, b, _ = req("DELETE", "/api/v1/connections/x", headers={"Origin": origin})
    check(f"a DELETE from Origin {origin} is 403", s == 403, f"{s} {b}")
for origin in ("http://localhost:5173", "http://127.0.0.1:7843", "app://x.localhost"):
    s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A}, headers={"Origin": origin})
    check(f"a mutation from loopback Origin {origin} passes the gate", s == 200, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", headers={"Origin": "https://evil.example"})
check("GET is not origin-gated (rows carry no secret)", s == 200, f"{s} {b}")

print("== bearer gate, like POST /task ==")
api.API_TOKEN = "test-token-123"
s, b, _ = req("GET", "/api/v1/connections")
check("GET without the bearer is 401", s == 401, f"{s} {b}")
s, b, _ = req("POST", "/api/v1/connections", {"token": TOKEN_A})
check("POST without the bearer is 401", s == 401, f"{s} {b}")
s, b, _ = req("DELETE", "/api/v1/connections/x", headers={"Authorization": "Bearer wrong"})
check("DELETE with a wrong bearer is 401", s == 401, f"{s} {b}")
s, b, _ = req("GET", "/api/v1/connections", headers={"Authorization": "Bearer test-token-123"})
check("the right bearer reads", s == 200, f"{s} {b}")
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
    h.send_json = lambda status, data: h.out.append((status, data))
    h.send_response = lambda status: h.out.append((status, None))
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    h.client_address = ("127.0.0.1", 1)
    h._connections_route(method)
    return h.out[0]
check("a bad Content-Length is 400",
      handler("POST", "/api/v1/connections", {"Content-Length": "nope"})[0] == 400)
check("an oversized body is 413",
      handler("POST", "/api/v1/connections", {"Content-Length": str(api.CONNECTIONS_BODY_CAP + 1)})[0] == 413)
check("a JSON body that is not an object is 400",
      handler("POST", "/api/v1/connections", {"Content-Length": "2"}, b"[]") == (400, {"error": "body must be a JSON object"}))
check("an unparseable Origin is refused",
      handler("POST", "/api/v1/connections", {"Content-Length": "0", "Origin": "http://[bad"})[0] == 403)
check("_loopback_origin accepts no Origin and loopback hosts only",
      api._loopback_origin("") and api._loopback_origin("http://localhost:1") and not api._loopback_origin("https://x.example"))
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
