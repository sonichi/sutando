#!/usr/bin/env python3
"""`AgentHandler.check_auth` compares the bearer through `local_api_gate.bearer_matches`, the one
constant-time comparer every local-API route shares, and only carries the verdict to the wire.

Contract: with no API_TOKEN the check passes without reading the header; with one, the header's
value is handed to `bearer_matches` with the module's API_TOKEN, a match returns True and sends
nothing, a mismatch sends 401 `{"error": "unauthorized"}` and returns False. The wire cases that
the old in-place `replace("Bearer ", "")` compare answered the same way still hold: a bare token
and a `Bearer`-prefixed one both pass, a wrong one and an absent header both fail.

Run: python3 tests/agent-api-check-auth-bearer.test.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

_WS = tempfile.mkdtemp(prefix="agent-api-check-auth-")
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
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def handler(headers):
    h = api.Handler.__new__(api.Handler)
    h.client_address = ("127.0.0.1", 1)
    h.headers = headers
    h.sent = []
    h.send_json = lambda status, body: h.sent.append((status, body))
    return h


print("== delegation ==")
api.API_TOKEN = "secret"
for verdict in (True, False):
    h = handler({"Authorization": "Bearer presented"})
    with mock.patch.object(api.local_api_gate, "bearer_matches", return_value=verdict) as gate:
        allowed = h.check_auth()
    check(f"verdict={verdict}: bearer_matches gets the header value and the module token",
          gate.call_args == mock.call("Bearer presented", "secret"), gate.call_args)
    if verdict:
        check("a match returns True and sends nothing", allowed is True and h.sent == [], h.sent)
    else:
        check("a mismatch returns False and sends 401 unauthorized",
              allowed is False and h.sent == [(401, {"error": "unauthorized"})], h.sent)

h = handler({})
with mock.patch.object(api.local_api_gate, "bearer_matches", return_value=False) as gate:
    h.check_auth()
check("an absent header is forwarded as the empty string", gate.call_args == mock.call("", "secret"), gate.call_args)

api.API_TOKEN = ""
h = handler({"Authorization": "Bearer anything"})
with mock.patch.object(api.local_api_gate, "bearer_matches") as gate:
    allowed = h.check_auth()
check("no configured token: passes without consulting the comparer", allowed is True and not gate.called and h.sent == [])

print("== wire cases the old compare answered ==")
api.API_TOKEN = "secret"
for header, expected in (("Bearer secret", True), ("secret", True), ("Bearer wrong", False), ("", False),
                         ("bearer secret", True), ("Bearer  secret ", True)):
    h = handler({"Authorization": header} if header else {})
    got = h.check_auth()
    check(f"Authorization={header!r} -> {expected}", got is expected and (h.sent == [] if expected else h.sent == [(401, {"error": "unauthorized"})]), h.sent)

api.API_TOKEN = ""
print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all pass")
