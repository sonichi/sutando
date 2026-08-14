#!/usr/bin/env python3
"""Credential-contract goldens + vendored-copy identity guard.

Owner-ratified narrow scope (Feature Haul 2026-08-05): the shared credential
contract is PURE parsing/precedence only — consumers keep their runtime
adapters. Three sections:

  A. GENERATED-COPY GUARD — tools/sync_gateway_credentials.py --check
     regenerates both consumer copies from the canonical
     shared/ag2_gateway_credentials.py and byte-compares (copies are checked
     against fresh generation, not each other, so both drifting together
     still fails). Plus the secret-leak guard: the token never appears in
     GatewayCredentials repr/str.

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
sys.path.insert(0, str(REPO / "shared"))
from ag2_gateway_credentials import (  # noqa: E402
    parse_onboarding_token, normalize_credentials, resolve_alias_precedence,
    GatewayCredentials, TOKEN_ALIAS_PRECEDENCE, URL_ALIAS_PRECEDENCE)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    print(("  ok  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    passed += bool(cond)
    failed += not cond


# ── A. generated-copy guard (regenerate-then-diff) + secret-leak guard ─────
sync = subprocess.run([sys.executable, str(REPO / "tools" / "sync_gateway_credentials.py"),
                       "--check"], capture_output=True, text=True)
check("sync --check: both generated copies match canonical",
      sync.returncode == 0, detail=sync.stdout + sync.stderr)
_c = GatewayCredentials(base_url="https://gw", token="SECRET-VALUE", source="env:GATEWAY_TOKEN")
check("token never appears in repr (secret-leak guard)",
      "SECRET-VALUE" not in repr(_c) and "SECRET-VALUE" not in str(_c), detail=repr(_c))
check("source carries qualified origin, base_url visible in repr",
      "env:GATEWAY_TOKEN" in repr(_c) and "https://gw" in repr(_c), detail=repr(_c))

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
                                 source=f"env:{name}" if raw else "none")


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

# The former divergence, CONVERGED by PR2 (the named enabling-only change
# ratified in #2668): room-ops' facade now delegates to the contract, so
# %7C and uppercase-scheme combined tokens split identically on both sides.
golden("%7C combined: room-ops now MATCHES the contract (PR2 convergence)",
       {"REMOTE_TASK_TOKEN": "https://gw.example%7Csek5"})
with env_scenario({"REMOTE_TASK_TOKEN": "https://gw.example%7Csek5"}):
    base, headers = legacy.gateway()
check("  converged %7C behavior: splits into URL + verbatim secret",
      base == "https://gw.example"
      and headers.get("Authorization", "").removeprefix("Bearer ") == "sek5")
check("  contract %7C behavior unchanged",
      parse_onboarding_token("https://gw.example%7Csek5") == ("https://gw.example", "sek5"))
golden("uppercase-scheme combined: converged (PR2)",
       {"REMOTE_TASK_TOKEN": "HTTPS://gw.example|sek6", "GATEWAY_URL": "https://gw.example"})
check("  contract is case-insensitive on scheme",
      parse_onboarding_token("HTTPS://gw.example|sek6") == ("HTTPS://gw.example", "sek6"))
check("  bare secret containing %7C never touched",
      parse_onboarding_token("sekret%7Cstill-opaque") == ("", "sekret%7Cstill-opaque"))

# Literal-pipe preference (#2670 review finding): a URL half legitimately
# carrying an encoded %7C must NOT be split at the encoding when a literal
# "|" separator exists — a raw pipe cannot occur inside a URL, so it IS the
# separator. Legacy room-ops (literal-| only) already got this right; the
# contract now agrees, so this golden holds on BOTH sides.
golden("URL containing %7C before the '|' separator splits at the pipe",
       {"REMOTE_TASK_TOKEN": "https://gw.example/a%7Cb|sec"})
check("  contract prefers the literal pipe; URL's %7C intact",
      parse_onboarding_token("https://gw.example/a%7Cb|sec")
      == ("https://gw.example/a%7Cb", "sec"))
check("  %7C-only combined still splits at the encoding (fallback intact)",
      parse_onboarding_token("https://gw.example%7Csek5") == ("https://gw.example", "sek5"))
check("  secret half keeps %7C verbatim after a pipe split",
      parse_onboarding_token("https://gw.example|a%7Cb") == ("https://gw.example", "a%7Cb"))
# The documented TRADE (review on #2679): pipe-preference is not a strict
# superset. A %7C-separated token whose SECRET contains a literal | now splits
# at the pipe inside the secret — previously parsed correctly. Both edge
# classes are rare and fail loudly; literal-pipe-wins is the better default
# because a transport that encodes the separator most likely encodes the
# whole value, secret pipes included. Pinned so the trade stays on the record:
check("  %7C-separated token with a literal | in the secret splits at the pipe (documented trade)",
      parse_onboarding_token("https://gw/path%7Csec|ret") == ("https://gw/path%7Csec", "ret"))

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
    # channel .env fallback (candidate 2: $CLAUDE_CONFIG_DIR/channels/ag2space/.env)
    cfg = Path(td) / "cfgdir"
    (cfg / "channels" / "ag2space").mkdir(parents=True)
    (cfg / "channels" / "ag2space" / ".env").write_text(
        "REMOTE_TASK_TOKEN=https://gwc.example|sekc\n")
    r = sparrow_resolve({"CLAUDE_CONFIG_DIR": str(cfg)})
    check("sparrow: channel .env fallback via CLAUDE_CONFIG_DIR",
          r.get("URL") == "https://gwc.example" and r.get("TOKEN") == "sekc", detail=str(r))
    r = sparrow_resolve({"AG2_DEVICE_ENV": str(dev_env), "CLAUDE_CONFIG_DIR": str(cfg)})
    check("sparrow: AG2_DEVICE_ENV beats channel .env",
          r.get("TOKEN") == "sekd", detail=str(r))

# legacy-alias deprecation warning behavior (frozen: stderr, once)
proc = subprocess.run([sys.executable, "-c", SPARROW_SNIPPET],
                      env={"PATH": os.environ.get("PATH", ""),
                           "PYTHONPATH": str(REPO / "packages" / "ag2-sparrow"),
                           "HOME": os.environ.get("HOME", "/tmp"),
                           "AG2_REMOTE_TOKEN": "legacy-tok",
                           "AG2_REMOTE_URL": "https://gwl.example"},
                      capture_output=True, text=True, timeout=30)
check("sparrow: legacy alias emits deprecation warning on stderr",
      "deprecated" in proc.stderr and "AG2_REMOTE_TOKEN" in proc.stderr,
      detail=proc.stderr[-200:])

# explicit REMOTE_TASK_TOKEN_FILE arms the rotation source
with tempfile.TemporaryDirectory() as td2:
    tokf = Path(td2) / "token.env"
    tokf.write_text("REMOTE_TASK_TOKEN=https://gwr.example|sekr\n")
    r = sparrow_resolve({"REMOTE_TASK_TOKEN": "https://gwr.example|sekr",
                         "REMOTE_TASK_TOKEN_FILE": str(tokf)})
    check("sparrow: explicit REMOTE_TASK_TOKEN_FILE preserved as rotation source",
          r.get("TOKEN_FILE") == str(tokf), detail=str(r))

print(f"\n{passed}/{passed + failed} passed")
raise SystemExit(0 if failed == 0 else 1)
