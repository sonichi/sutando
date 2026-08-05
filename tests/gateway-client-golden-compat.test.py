#!/usr/bin/env python3
"""Golden compatibility: ag2-gateway-client vs the room-ops legacy resolver.

Owner merge-gate for the extraction (Feature Haul 2026-08-05): under the SAME
env + the SAME vault fixture, the new package's resolve_credentials() must
produce byte-identical (base_url, token) to the legacy room-ops
_gateway.gateway() — for every scenario in the ratified matrix:

  explicit URL + explicit token / combined url|secret / bare token + URL env /
  vault combined / vault bare / conflicting aliases / empty + malformed /
  env overrides vault

The legacy resolver reads os.environ and its own vault hook, so each scenario
runs with a patched environ and a patched _gateway._token_from_vault (the
vault INTERNALS are #2648's tested territory; what matters here is tier
POSITION and precedence). The package gets the same env as a plain mapping
and the same fake vault via injection — the injection seam is the package's
contract (no src/ import), so exercising it IS the shipped path.

Run: python3 tests/gateway-client-golden-compat.test.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-gateway-client"))
from ag2_gateway_client import resolve_credentials  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_legacy_gateway", REPO / "skills" / "agent-room-ops" / "_gateway.py")
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)

# Every env var either resolver consults — cleared between scenarios so no
# host env leaks into the matrix.
ALL_VARS = ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN",
            "AG2_REMOTE_TOKEN", "GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL",
            "REMOTE_TASK_TOKEN_FILE")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    print(("  ok  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    passed += bool(cond)
    failed += not cond


@contextmanager
def scenario(env: dict, vault: dict):
    """Patch os.environ (legacy reads it) + legacy vault hook; restore after."""
    saved = {v: os.environ.pop(v, None) for v in ALL_VARS}
    os.environ.update(env)
    orig_vault = legacy._token_from_vault

    def fake_legacy_vault(vault_get=None):
        for var in ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN",
                    "AG2_REMOTE_TOKEN"):
            if vault.get(var):
                return vault[var]
        return ""

    legacy._token_from_vault = fake_legacy_vault
    try:
        yield
    finally:
        legacy._token_from_vault = orig_vault
        for v in ALL_VARS:
            os.environ.pop(v, None)
            if saved[v] is not None:
                os.environ[v] = saved[v]


def compare(name: str, env: dict, vault: dict):
    """Assert legacy (base, bearer) == package (base, bearer) for a scenario."""
    with scenario(env, vault):
        legacy_base, legacy_headers = legacy.gateway()
        legacy_token = legacy_headers.get("Authorization", "").removeprefix("Bearer ")
        creds = resolve_credentials(
            env=dict(env), vault_token_reader=lambda var: vault.get(var, ""))
        same = (legacy_base == creds.base_url) and (legacy_token == creds.token)
        check(name, same,
              detail=f"legacy=({legacy_base!r},{legacy_token!r}) new=({creds.base_url!r},{creds.token!r})")


# ── the ratified matrix ──────────────────────────────────────────────────────
compare("explicit URL + explicit token",
        {"GATEWAY_URL": "https://gw.example", "GATEWAY_TOKEN": "sek1"}, {})
compare("combined url|secret via REMOTE_TASK_TOKEN",
        {"REMOTE_TASK_TOKEN": "https://gw.example|sek2"}, {})
compare("explicit GATEWAY_TOKEN in combined form still splits",
        {"GATEWAY_TOKEN": "https://gw.example|sek2b"}, {})
compare("bare token + URL env",
        {"REMOTE_TASK_TOKEN": "sek3", "REMOTE_TASK_URL": "https://gw3.example/"}, {})
compare("vault combined token (env empty)",
        {}, {"REMOTE_TASK_TOKEN": "https://gwv.example|sekv"})
compare("vault bare token (no URL anywhere -> base empty, degrade)",
        {}, {"GATEWAY_TOKEN": "sekv2"})
compare("conflicting aliases: GATEWAY_* beats RELAY_* beats REMOTE_TASK_*",
        {"GATEWAY_TOKEN": "win", "RELAY_TOKEN": "lose1", "REMOTE_TASK_TOKEN": "lose2",
         "GATEWAY_URL": "https://win.example", "RELAY_URL": "https://lose.example"}, {})
compare("empty env + empty vault -> unconfigured",
        {}, {})
compare("malformed combined (no scheme) -> whole value stays the bearer",
        {"REMOTE_TASK_TOKEN": "not-a-url|still-one-bearer",
         "GATEWAY_URL": "https://gw.example"}, {})
compare("explicit bearer containing '|' but not combined stays intact",
        {"GATEWAY_TOKEN": "sek|with|pipes", "GATEWAY_URL": "https://gw.example"}, {})
compare("env overrides vault (stored value never shadows fresher env)",
        {"REMOTE_TASK_TOKEN": "env-wins"}, {"GATEWAY_TOKEN": "vault-loses"})
compare("trailing slash on URL normalized identically",
        {"GATEWAY_URL": "https://gw.example/", "GATEWAY_TOKEN": "sek4"}, {})

# ── package-only tiers/behaviors the legacy resolver never had ───────────────
import tempfile  # noqa: E402

with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tf:
    tf.write("# durable token\nREMOTE_TASK_TOKEN=https://gwf.example|sekf\n")
    tfpath = tf.name
try:
    creds = resolve_credentials(env={"REMOTE_TASK_TOKEN_FILE": tfpath},
                                vault_token_reader=lambda v: "")
    check("token-file tier (sparrow parity): combined form from file",
          creds == ("https://gwf.example", "sekf", "token_file"),
          detail=repr(creds))
    creds = resolve_credentials(env={"REMOTE_TASK_TOKEN": "env-first",
                                     "REMOTE_TASK_TOKEN_FILE": tfpath})
    check("env beats token file", creds.token == "env-first")
finally:
    os.unlink(tfpath)

from ag2_gateway_client import request  # noqa: E402
try:
    request("GET", "http://127.0.0.1:1/x", profile="nope")
    check("unknown profile raises ValueError", False)
except ValueError:
    check("unknown profile raises ValueError", True)
except Exception as e:  # noqa: BLE001
    check("unknown profile raises ValueError", False, detail=repr(e))

from ag2_gateway_client import resolve_identity  # noqa: E402
check("identity: AGENT_MXID beats AGENT_ID",
      resolve_identity({"AGENT_MXID": "@a:x", "AGENT_ID": "@b:x"}) == "@a:x")
check("identity: AGENT_ID fallback",
      resolve_identity({"AGENT_ID": "@b:x"}) == "@b:x")
check("identity: empty when undeclared", resolve_identity({}) == "")

print(f"\n{passed}/{passed + failed} passed")
raise SystemExit(0 if failed == 0 else 1)
