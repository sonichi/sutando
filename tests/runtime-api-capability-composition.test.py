#!/usr/bin/env python3
"""Contract tests for provider-neutral runtime capability composition."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
RUNTIME_API = REPO / "src" / "runtime-api"
sys.path.insert(0, str(RUNTIME_API))

from capability_registry import compose_capability_registry  # noqa: E402

SERVER_SPEC = importlib.util.spec_from_file_location(
    "capability_composition_server", RUNTIME_API / "server.py"
)
server = importlib.util.module_from_spec(SERVER_SPEC)
assert SERVER_SPEC.loader
SERVER_SPEC.loader.exec_module(server)

FAILS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}"
          + ("" if condition else f" — {detail}"))
    if not condition:
        FAILS.append(label)


def descriptor(capability_id):
    return {
        "id": capability_id,
        "version": 1,
        "availability": "ready",
        "operations": ["records.list"],
    }


def provider(capability_id, item_id):
    def factory():
        return (
            {capability_id: descriptor(capability_id)},
            {capability_id: lambda _params: {"items": [{"id": item_id}]}},
        )

    return factory


print("── generic composition ──")
empty = compose_capability_registry()
check("zero factories produce an empty registry", empty.list({}) == {"capabilities": []})
none = compose_capability_registry(None)
check("None factories preserve an empty optional registry",
      none.list({}) == {"capabilities": []})

one = provider("example.one", "one")
two = provider("example.two", "two")
combined = compose_capability_registry([one, two])
check(
    "multiple injected factories compose without provider knowledge",
    [row["id"] for row in combined.list({})["capabilities"]]
    == ["example.one", "example.two"],
)
result = asyncio.run(combined.read({
    "capabilityId": "example.two",
    "operation": "records.list",
    "limit": 1,
}))
check("composed reader remains callable", result == {"items": [{"id": "two"}]})

try:
    compose_capability_registry([one, provider("example.one", "shadow")])
    check("duplicate capability factories fail closed", False, "composition succeeded")
except ValueError as exc:
    check("duplicate capability factories fail closed", "example.one" in str(exc))

for label, factories in (
    ("non-callable factory is rejected", ["bad"]),
    ("non-iterable factories are rejected", 7),
    ("malformed factory result is rejected", [lambda: {}]),
    ("provider descriptors must be a mapping", [lambda: ([], {})]),
    ("provider readers must be a mapping", [lambda: ({}, [])]),
    ("cross-factory reader attachment is rejected", [
        lambda: ({"example.split": descriptor("example.split")}, {}),
        lambda: ({}, {"example.split": lambda _params: {}}),
    ]),
):
    try:
        compose_capability_registry(factories)
        check(label, False, "composition succeeded")
    except ValueError:
        check(label, True)

try:
    compose_capability_registry([lambda: (_ for _ in ()).throw(
        RuntimeError("private-token-value")
    )])
    check("factory exceptions are redacted", False, "composition succeeded")
except ValueError as exc:
    check("factory exceptions are redacted", "private-token-value" not in str(exc))


print("── startup composition ──")
tmp = Path(tempfile.mkdtemp(prefix="runtime-cap-composition-"))
plain = server.build_runtime_server(
    state_dir=tmp / "plain-state", runtime_socket=str(tmp / "plain.sock")
)
check(
    "default startup boots with no providers",
    plain.dispatcher.capability_registry.list({}) == {"capabilities": []},
)

factory_calls = []


def startup_factory():
    factory_calls.append("called")
    return one()


wired = server.build_runtime_server(
    [startup_factory],
    state_dir=tmp / "wired-state",
    runtime_socket=str(tmp / "wired.sock"),
)
check("startup invokes each injected factory once", factory_calls == ["called"])
check(
    "startup installs the composed registry",
    wired.dispatcher.capability_registry.list({})["capabilities"][0]["id"]
    == "example.one",
)
check("startup socket override reaches the server", wired.socket_path == str(tmp / "wired.sock"))

forwarded = []
original_build = server.build_runtime_server
original_run = server.asyncio.run


class FakeRuntime:
    async def serve(self):
        return None

    def mark_stopped(self):
        # This branch's main() marks clean shutdown in its finally.
        return None


def fake_build(factories=()):
    forwarded.extend(factories)
    return FakeRuntime()


def close_coroutine(coroutine):
    coroutine.close()


try:
    server.build_runtime_server = fake_build
    server.asyncio.run = close_coroutine
    server.main([startup_factory])
finally:
    server.build_runtime_server = original_build
    server.asyncio.run = original_run
check("main forwards explicit factories into startup composition", forwarded == [startup_factory])

plain.store.close()
wired.store.close()

print()
if FAILS:
    print(f"FAIL — {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print("PASS — runtime-api provider-neutral capability composition")
