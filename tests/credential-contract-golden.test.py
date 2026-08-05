#!/usr/bin/env python3
"""Credential-contract goldens + vendored-copy identity guard.

Owner-ratified narrow scope (Feature Haul 2026-08-05): the shared credential
contract is PURE parsing/precedence only — consumers keep their runtime
adapters. Three sections:

  A. IDENTITY GUARD — skills/agent-room-ops/_credential_contract.py must be
     byte-identical to the canonical
     packages/ag2-sparrow/ag2_sparrow/gateway_credentials.py below the
     4-line vendored header. Edit-one-place is machine-enforced.

  B. ROOM-OPS GOLDENS — the legacy `_gateway.gateway()` resolver's
     (base_url, token) frozen across the ratified env matrix, and compared
     against the pure contract composed the way room-ops' facade will
     compose it in PR2. KNOWN DIVERGENCE, frozen not hidden: room-ops today
     splits combined tokens only on a literal '|' with a CASE-SENSITIVE
     scheme check; the contract adopts sparrow's semantics ('|' or '%7C',
     case-insensitive scheme). The two %7C/case scenarios assert the legacy
     behavior AND the contract behavior separately — converging room-ops is
     PR2's explicitly-named behavior change (enabling-only: tokens that
     failed auth start working), not a silent golden mismatch.

  C. SPARROW GOLDENS — the real module-level resolution of
     remote_gateway_bridge (env → device-env file → parse → URL chain,
     TOKEN_FILE carry, media-marker injection) frozen via subprocess import
     under controlled env. Vault-tier position is pinned by sparrow's own
     test_vault_token_tier.py (which shadows the Keychain); these scenarios
     all resolve before the vault tier so no host Keychain is ever read.

Run: python3 tests/credential-contract-golden.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow.gateway_credentials import (  # noqa: E402
    parse_onboarding_token, normalize_credentials, resolve_alias_precedence,
    TOKEN_ALIAS_PRECEDENCE, URL_ALIAS_PRECEDENCE)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    print(("  ok  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    passed += bool(cond)
    failed += not cond


# ── A. vendored-copy identity guard ─────────────────────────────────────────
canonical = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" /
             "gateway_credentials.py").read_text()
vendored_lines = (REPO / "skills" / "agent-room-ops" /
                  "_credential_contract.py").read_text().splitlines(keepends=True)
check("vendored header is exactly 4 lines of '#' comment",
      len(vendored_lines) >= 4 and all(l.startswith("#") for l in vendored_lines[:4]))
check("vendored copy byte-identical to canonical below the header",
      "".join(vendored_lines[4:]) == canonical)

# ── B. room-ops goldens (legacy resolver frozen; contract compared) ─────────
_spec = importlib.util.spec_from_file_location(
    "_legacy_gateway", REPO / "skills" / "agent-room-ops" / "_gateway.py")
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)

ALL_VARS = TOKEN_ALIAS_PRECEDENCE + URL_ALIAS_PRECEDENCE + ("REMOTE_TASK_TOKEN_FILE",)


@contextmanager
def env_scenario(env: dict):
    saved = {v: os.environ.pop(v, None) for v in ALL_VARS}
    os.environ.update(env)
    orig = legacy._token_from_vault
    legacy._token_from_vault = lambda vault_get=None: ""  # vault empty; tier position untouched
    try:
        yield
    finally:
        legacy._token_from_vault = orig
        for v in ALL_VARS:
            os.environ.pop(v, None)
            if saved[v] is not None:
                os.environ[v] = saved[v]


def contract_resolve(env: dict):
    """Compose the pure contract exactly as room-ops' PR2 facade will:
    env alias chain for token + URL, then normalize."""
    raw, name = resolve_alias_precedence(env, ("GATEWAY_TOKEN", "RELAY_TOKEN",
                                               "REMOTE_TASK_TOKEN"))
    explicit_url, _ = resolve_alias_precedence(env, ("GATEWAY_URL", "RELAY_URL",
                                                     "REMOTE_TASK_URL"))
    return normalize_credentials(raw, explicit_url=explicit_url,
                                 source="env" if raw else "none")


def golden(name, env, expect_same=True):
    with env_scenario(env):
        base, headers = legacy.gateway()
        legacy_token = headers.get("Authorization", "").removeprefix("Bearer ")
    creds = contract_resolve(env)
    same = (base == creds.base_url and legacy_token == creds.token)
    check(name, same if expect_same else not same,
          detail=f"legacy=({base!r},{legacy_token!r}) contract=({creds.base_url!r},{creds.token!r})")


golden("explicit URL + explicit token",
       {"GATEWAY_URL": "https://gw.example", "GATEWAY_TOKEN": "sek1"})
golden("combined url|secret via REMOTE_TASK_TOKEN",
       {"REMOTE_TASK_TOKEN": "https://gw.example|sek2"})
golden("explicit GATEWAY_TOKEN in combined form still splits",
       {"GATEWAY_TOKEN": "https://gw.example|sek2b"})
golden("bare token + URL env",
       {"REMOTE_TASK_TOKEN": "sek3", "REMOTE_TASK_URL": "https://gw3.example/"})
golden("conflicting aliases: GATEWAY_* > RELAY_* > REMOTE_TASK_*",
       {"GATEWAY_TOKEN": "win", "RELAY_TOKEN": "lose1", "REMOTE_TASK_TOKEN": "lose2",
        "GATEWAY_URL": "https://win.example", "RELAY_URL": "https://lose.example"})
golden("empty env -> unconfigured", {})
golden("malformed combined (no scheme) stays one bearer",
       {"REMOTE_TASK_TOKEN": "not-a-url|still-one-bearer", "GATEWAY_URL": "https://gw.example"})
golden("bearer containing '|' without scheme stays intact",
       {"GATEWAY_TOKEN": "sek|with|pipes", "GATEWAY_URL": "https://gw.example"})
golden("trailing slash normalized identically",
       {"GATEWAY_URL": "https://gw.example/", "GATEWAY_TOKEN": "sek4"})

# The KNOWN divergence, frozen explicitly (contract=sparrow semantics):
golden("%7C combined: legacy room-ops does NOT split (frozen divergence)",
       {"REMOTE_TASK_TOKEN": "https://gw.example%7Csek5"}, expect_same=False)
with env_scenario({"REMOTE_TASK_TOKEN": "https://gw.example%7Csek5"}):
    base, headers = legacy.gateway()
check("  legacy %7C behavior pinned: whole value is the bearer, no base URL",
      base == "" and headers.get("Authorization", "").removeprefix("Bearer ")
      == "https://gw.example%7Csek5")
check("  contract %7C behavior pinned: splits into URL + verbatim secret",
      parse_onboarding_token("https://gw.example%7Csek5") == ("https://gw.example", "sek5"))
golden("uppercase-scheme combined: legacy case-sensitive check misses (frozen divergence)",
       {"REMOTE_TASK_TOKEN": "HTTPS://gw.example|sek6", "GATEWAY_URL": "https://gw.example"},
       expect_same=False)
check("  contract is case-insensitive on scheme",
      parse_onboarding_token("HTTPS://gw.example|sek6") == ("HTTPS://gw.example", "sek6"))
check("  bare secret containing %7C never touched",
      parse_onboarding_token("sekret%7Cstill-opaque") == ("", "sekret%7Cstill-opaque"))

# ── C. sparrow goldens (module-level resolution via subprocess import) ──────
SPARROW_SNIPPET = (
    "import json,sys,ag2_sparrow.remote_gateway_bridge as m;"
    "print(json.dumps({'URL': m.URL, 'TOKEN': m.TOKEN, 'TOKEN_FILE': m.TOKEN_FILE,"
    " 'MM': __import__('os').environ.get('REMOTE_MEDIA_MARKER','')}))")


def sparrow_resolve(env: dict) -> dict:
    """Import the bridge in a subprocess under a controlled env; return its
    module-level resolution. Env is minimal: PATH+PYTHONPATH plus scenario."""
    base_env = {"PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(REPO / "packages" / "ag2-sparrow"),
                "HOME": os.environ.get("HOME", "/tmp")}
    proc = subprocess.run([sys.executable, "-c", SPARROW_SNIPPET],
                          env={**base_env, **env}, capture_output=True,
                          text=True, timeout=30)
    if proc.returncode != 0:
        return {"error": proc.stderr[-300:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


r = sparrow_resolve({"REMOTE_TASK_TOKEN": "https://gws.example|seks1"})
check("sparrow: env combined | splits",
      r.get("URL") == "https://gws.example" and r.get("TOKEN") == "seks1", detail=str(r))
r = sparrow_resolve({"REMOTE_TASK_TOKEN": "https://gws.example%7Cseks2"})
check("sparrow: env combined %7C splits (the divergence's other side)",
      r.get("URL") == "https://gws.example" and r.get("TOKEN") == "seks2", detail=str(r))
r = sparrow_resolve({"REMOTE_TASK_TOKEN": "bare%7Csecret",
                     "REMOTE_TASK_URL": "https://gws3.example"})
check("sparrow: bare secret with %7C untouched",
      r.get("TOKEN") == "bare%7Csecret" and r.get("URL") == "https://gws3.example", detail=str(r))
r = sparrow_resolve({"AG2_REMOTE_TOKEN": "legacy-tok", "AG2_REMOTE_URL": "https://gws4.example/"})
check("sparrow: legacy aliases honored + trailing slash stripped",
      r.get("TOKEN") == "legacy-tok" and r.get("URL") == "https://gws4.example", detail=str(r))
r = sparrow_resolve({"REMOTE_TASK_TOKEN": "new-wins", "AG2_REMOTE_TOKEN": "old-loses",
                     "REMOTE_TASK_URL": "https://gws5.example"})
check("sparrow: REMOTE_TASK_TOKEN beats AG2_REMOTE_TOKEN",
      r.get("TOKEN") == "new-wins", detail=str(r))

with tempfile.TemporaryDirectory() as td:
    dev_env = Path(td) / "device.env"
    dev_env.write_text("REMOTE_TASK_TOKEN=https://gwd.example|sekd\n"
                       "REMOTE_MEDIA_MARKER=[test-media]\n")
    r = sparrow_resolve({"AG2_DEVICE_ENV": str(dev_env)})
    check("sparrow: device-env fallback — combined token from file",
          r.get("URL") == "https://gwd.example" and r.get("TOKEN") == "sekd", detail=str(r))
    check("sparrow: device-env carries TOKEN_FILE for auth-recovery",
          r.get("TOKEN_FILE") == str(dev_env), detail=str(r))
    check("sparrow: device-env injects REMOTE_MEDIA_MARKER when env unset",
          r.get("MM") == "[test-media]", detail=str(r))
    split_env = Path(td) / "split.env"
    split_env.write_text("REMOTE_TASK_TOKEN=bare-sekf\nREMOTE_TASK_URL=https://gwf.example\n")
    r = sparrow_resolve({"AG2_DEVICE_ENV": str(split_env)})
    check("sparrow: device-env split layout — URL carried from file",
          r.get("URL") == "https://gwf.example" and r.get("TOKEN") == "bare-sekf", detail=str(r))
    r = sparrow_resolve({"AG2_DEVICE_ENV": str(dev_env),
                         "REMOTE_TASK_TOKEN": "env-beats-file",
                         "REMOTE_TASK_URL": "https://gwe.example"})
    check("sparrow: env token beats device-env file",
          r.get("TOKEN") == "env-beats-file", detail=str(r))

print(f"\n{passed}/{passed + failed} passed")
raise SystemExit(0 if failed == 0 else 1)
