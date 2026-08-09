#!/usr/bin/env python3
"""Contract tests for ephemeral runtime capability discovery and reads.

Run: python3 tests/runtime-api-capability-read.test.py  (stdlib only)
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
RUNTIME_API = REPO / "src" / "runtime-api"
sys.path.insert(0, str(RUNTIME_API))

from capability_registry import EphemeralCapabilityRegistry  # noqa: E402
from protocol import ProtocolError, parse_line  # noqa: E402

_server_spec = importlib.util.spec_from_file_location(
    "capability_test_server", RUNTIME_API / "server.py")
server = importlib.util.module_from_spec(_server_spec)
_server_spec.loader.exec_module(server)

_cli_spec = importlib.util.spec_from_file_location(
    "capability_test_cli", REPO / "src" / "runtime-cli" / "sutando-runtime.py")
cli = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(cli)

FAILS: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label}"
          + ("" if condition else f" — {detail}"))
    if not condition:
        FAILS.append(label)


def run(coro):
    return asyncio.run(coro)


def raises_protocol(label, coro_factory, code=-32602, contains=None):
    try:
        run(coro_factory())
    except ProtocolError as exc:
        check(label, exc.code == code and (contains is None or contains in exc.message),
              f"code={exc.code}, message={exc.message!r}")
        return
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"wrong exception: {type(exc).__name__}: {exc}")
        return
    check(label, False, "no exception")


CAPABILITY_ID = "example.activity"
DESCRIPTOR = {
    "id": CAPABILITY_ID,
    "version": 1,
    "availability": "ready",
    "operations": ["identity.get", "records.list"],
    "description": "Example read-only activity.",
    "identity": {"kind": "local-user"},
    "constraints": {"maxItems": 100},
    "setup": {},
}


print("── protocol + registry registration ──")
for method in ("capability.list", "capability.read"):
    parsed = parse_line(json.dumps({
        "jsonrpc": "2.0", "id": "x", "method": method, "params": {},
    }).encode())
    check(f"protocol accepts {method}", parsed[1] == method, str(parsed))

for label, descriptors, readers in (
    ("descriptor id must match key", {CAPABILITY_ID: {**DESCRIPTOR, "id": "other.id"}}, {}),
    ("descriptor rejects unknown fields", {CAPABILITY_ID: {**DESCRIPTOR, "token": "x"}}, {}),
    ("reader requires descriptor", {CAPABILITY_ID: DESCRIPTOR}, {"other.id": lambda _p: {}}),
    ("reader must be callable", {CAPABILITY_ID: DESCRIPTOR}, {CAPABILITY_ID: "no"}),
):
    try:
        EphemeralCapabilityRegistry(descriptors, readers)
        check(label, False, "registration succeeded")
    except ValueError:
        check(label, True)

try:
    EphemeralCapabilityRegistry(
        {CAPABILITY_ID: DESCRIPTOR}, {}, read_timeout_s=10.01)
    check("reader timeout has a hard upper bound", False, "registration succeeded")
except ValueError:
    check("reader timeout has a hard upper bound", True)


print("── dispatcher/server wiring and non-durability ──")
seen: list[dict] = []


def read_provider(params):
    seen.append(params)
    return {"items": [{"id": "one"}], "nextCursor": {"after": "one"}}


registry = EphemeralCapabilityRegistry(
    {CAPABILITY_ID: DESCRIPTOR}, {CAPABILITY_ID: read_provider})
tmp = Path(tempfile.mkdtemp(prefix="rt-cap-read-"))
db_path = tmp / "state.sqlite"
srv = server.RuntimeServer(
    socket_path=str(tmp / "runtime.sock"), db_path=str(db_path),
    ha_dir=str(tmp / "ha"), capability_registry=registry)
check("server passes the injected registry to its dispatcher",
      srv.dispatcher.capability_registry is registry)

# If either method enters the durable request creation path, fail immediately.
srv.store.create = lambda *_a, **_k: (_ for _ in ()).throw(
    AssertionError("ephemeral operation touched RequestStore.create"))

listed = run(srv.dispatcher.handle("capability.list", {}))
check("capability.list returns public descriptors",
      listed == {"capabilities": [DESCRIPTOR]}, str(listed))
# The returned value is detached from registry state.
listed["capabilities"][0]["availability"] = "unavailable"
listed_again = run(srv.dispatcher.handle("capability.list", {}))
check("capability.list returns a defensive copy",
      listed_again["capabilities"][0]["availability"] == "ready")

result = run(srv.dispatcher.handle("capability.read", {
    "capabilityId": CAPABILITY_ID,
    "operation": "records.list",
    "resource": {"scope": "mine"},
    "cursor": {"after": "zero"},
    "limit": 7,
}))
check("capability.read returns the reader's object directly",
      result["items"] == [{"id": "one"}], str(result))
check("reader receives only validated normalized params", seen == [{
    "capabilityId": CAPABILITY_ID,
    "operation": "records.list",
    "resource": {"scope": "mine"},
    "cursor": {"after": "zero"},
    "limit": 7,
}], str(seen))
with sqlite3.connect(db_path) as conn:
    row_count = conn.execute("SELECT count(*) FROM runtime_requests").fetchone()[0]
check("list/read create zero durable request rows", row_count == 0, str(row_count))


print("── strict request validation ──")
raises_protocol("capability.list rejects params",
                lambda: srv.dispatcher.handle("capability.list", {"prefix": "x"}),
                contains="does not accept")
for label, params, fragment in (
    ("read rejects unknown params", {
        "capabilityId": CAPABILITY_ID, "operation": "identity.get", "extra": True,
    }, "unknown"),
    ("read requires a valid capability id", {
        "capabilityId": "", "operation": "identity.get",
    }, "capabilityId"),
    ("read rejects unknown capability", {
        "capabilityId": "unknown.activity", "operation": "identity.get",
    }, "unknown capabilityId"),
    ("read rejects undeclared operation", {
        "capabilityId": CAPABILITY_ID, "operation": "records.delete",
    }, "not declared"),
    ("read resource must be an object", {
        "capabilityId": CAPABILITY_ID, "operation": "identity.get", "resource": [],
    }, "resource"),
    ("read cursor must be an object", {
        "capabilityId": CAPABILITY_ID, "operation": "identity.get", "cursor": [],
    }, "cursor"),
    ("read limit rejects bool", {
        "capabilityId": CAPABILITY_ID, "operation": "identity.get", "limit": True,
    }, "limit"),
    ("read limit is capped", {
        "capabilityId": CAPABILITY_ID, "operation": "identity.get", "limit": 101,
    }, "limit"),
):
    raises_protocol(label, lambda params=params: srv.dispatcher.handle(
        "capability.read", params), contains=fragment)


print("── timeout, error redaction and result-size cap ──")


def slow_reader(_params):
    time.sleep(0.08)
    return {"ok": True}


slow_registry = EphemeralCapabilityRegistry(
    {CAPABILITY_ID: DESCRIPTOR}, {CAPABILITY_ID: slow_reader}, read_timeout_s=0.01)
raises_protocol("slow reader hits daemon timeout", lambda: slow_registry.read({
    "capabilityId": CAPABILITY_ID, "operation": "identity.get",
}), code=-32000, contains="timed out")


def failed_reader(_params):
    raise RuntimeError("private-token-value")


failed_registry = EphemeralCapabilityRegistry(
    {CAPABILITY_ID: DESCRIPTOR}, {CAPABILITY_ID: failed_reader})
try:
    run(failed_registry.read({
        "capabilityId": CAPABILITY_ID, "operation": "identity.get",
    }))
    check("reader exception is surfaced", False, "no exception")
except ProtocolError as exc:
    check("reader exception is redacted", exc.code == -32000
          and "private-token-value" not in exc.message, exc.message)

large_registry = EphemeralCapabilityRegistry(
    {CAPABILITY_ID: DESCRIPTOR},
    {CAPABILITY_ID: lambda _params: {"body": "x" * 2048}},
    max_result_bytes=1024)
raises_protocol("oversized reader result is rejected", lambda: large_registry.read({
    "capabilityId": CAPABILITY_ID, "operation": "identity.get",
}), code=-32000, contains="byte limit")

wrong_shape_registry = EphemeralCapabilityRegistry(
    {CAPABILITY_ID: DESCRIPTOR}, {CAPABILITY_ID: lambda _params: []})
raises_protocol("reader result must be an object", lambda: wrong_shape_registry.read({
    "capabilityId": CAPABILITY_ID, "operation": "identity.get",
}), code=-32000, contains="non-object")


print("── CLI wiring ──")
rpc_calls: list[tuple] = []


def fake_rpc(method, params, timeout):
    rpc_calls.append((method, params, timeout))
    return {"ok": True}


cli._rpc = fake_rpc
with contextlib.redirect_stdout(io.StringIO()):
    list_rc = cli.main(["capability", "list"])
    read_rc = cli.main([
        "capability", "read", "--capability", CAPABILITY_ID,
        "--operation", "records.list", "--resource", '{"scope":"mine"}',
        "--cursor", '{"after":"zero"}', "--limit", "7",
    ])
check("CLI capability list dispatches capability.list",
      list_rc == 0 and rpc_calls[0] == ("capability.list", {}, 15), str(rpc_calls))
check("CLI capability read dispatches exact read params",
      read_rc == 0 and rpc_calls[1] == ("capability.read", {
          "capabilityId": CAPABILITY_ID,
          "operation": "records.list",
          "resource": {"scope": "mine"},
          "cursor": {"after": "zero"},
          "limit": 7,
      }, 15), str(rpc_calls))


print()
if FAILS:
    print(f"FAIL — {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print("PASS — runtime-api ephemeral capability reads")
