# VENDORED COPY — canonical source: packages/ag2-sparrow/ag2_sparrow/gateway_credentials.py
# Edit the canonical file ONLY; CI (tests/credential-contract-golden.test.py)
# fails if this copy diverges byte-for-byte below this 4-line header.
# Rationale: sparrow ships zero-dep + room-ops must not import ag2_sparrow (#2666).
"""gateway_credentials — the CANONICAL pure credential contract (no package).

The one implementation of AG2 Space gateway credential PARSING/PRECEDENCE
semantics, shared by ag2-sparrow and the agent-room-ops skill. Scope is
deliberately narrow (owner ruling, Feature Haul 2026-08-05):

  SHARED here (pure functions over plain values — no I/O, no env reads):
    - parse_onboarding_token(): combined ``url|secret`` / ``url%7Csecret``
      splitting (sparrow's semantics — the onboarding-format author's)
    - the alias-precedence tables (canonical order, both token and URL)
    - normalize_gateway_url()
    - GatewayCredentials value object

  NOT here (consumer-owned runtime adapters, by design):
    - sparrow: desktop .env discovery, token-file rotation/live reload,
      vault-tier invocation, diagnostics, long-poll networking
    - room-ops: interactive env/vault adapter, client room gate, graceful
      degradation, HTTP behavior

Sharing mechanism (constraints: sparrow ships zero-dependency; the
events-plane boundary forbids room-ops importing ag2_sparrow): this file is
canonical; ``skills/agent-room-ops/_credential_contract.py`` is a
byte-identical VENDORED copy. Edit THIS file only — the CI identity guard
(tests/credential-contract-golden.test.py) fails on any divergence.
Promotion to a real ag2-gateway-client package happens when a third
consumer (CLI/MCP/external) adopts it; the vendored copy then becomes a
dependency with this API unchanged.
"""
from __future__ import annotations

import re
from typing import Mapping, NamedTuple, Optional, Sequence

# Canonical alias precedence (earlier wins). GATEWAY_* is the primary name;
# RELAY_* and REMOTE_TASK_* are honored as transition aliases; AG2_* legacy.
TOKEN_ALIAS_PRECEDENCE: tuple = (
    "GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
URL_ALIAS_PRECEDENCE: tuple = (
    "GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL", "AG2_REMOTE_URL")

# The combined onboarding form separates URL from secret with "|" — which some
# transports URL-encode as %7C/%7c. Only a value that STARTS with an http(s)
# scheme is combined; a bare secret is opaque and never touched even if it
# contains either separator (#2307: secret bytes are split, never mutated).
_SEPARATOR_RE = re.compile(r"\||%7[Cc]")


class GatewayCredentials(NamedTuple):
    """Normalized resolution result. ``base_url`` is '' when unconfigured
    (callers degrade gracefully). ``source`` names the winning tier for
    diagnostics — consumer-defined vocabulary (e.g. 'env', 'device_env',
    'token_file', 'vault', 'none')."""
    base_url: str
    token: str
    source: str = "none"


def parse_onboarding_token(raw: str) -> "tuple[str, str]":
    """Split an onboarding string into (url_from_token, secret).

    Sparrow-semantics by owner ruling (the format author's): case-insensitive
    scheme detection; splits at the FIRST ``|`` or ``%7C``/``%7c``; both
    halves returned verbatim (never mutated). A bare secret — no leading
    scheme — is returned untouched as ('', raw) even if it contains a
    separator. A scheme-prefixed value with no separator is ('', raw) too
    (the caller's URL-less guard speaks)."""
    if not raw.lower().startswith(("http://", "https://")):
        return "", raw
    m = _SEPARATOR_RE.search(raw)
    if m is None:
        return "", raw
    return raw[: m.start()], raw[m.end():]


def normalize_gateway_url(url: str) -> str:
    """Canonical URL normalization: strip trailing slashes (and nothing else —
    schemes/hosts/paths pass through verbatim)."""
    return (url or "").rstrip("/")


def resolve_alias_precedence(env: Mapping[str, str],
                             names: Sequence[str]) -> "tuple[str, str]":
    """First non-empty value in canonical order → (value, winning_name).
    ('' , '') when none set. Pure: ``env`` is any mapping, never os.environ
    implicitly — the CALLER decides which sources exist and feeds them in."""
    for name in names:
        v = env.get(name) or ""
        if v:
            return v, name
    return "", ""


def normalize_credentials(raw_token_value: str,
                          explicit_url: str = "",
                          fallback_url: str = "",
                          source: str = "none") -> GatewayCredentials:
    """Compose parse + URL chain into the value object. URL precedence:
    explicit (env alias chain) > url-embedded-in-token > caller fallback
    (e.g. a device-env file's REMOTE_TASK_URL line)."""
    url_from_token, token = parse_onboarding_token(raw_token_value) if raw_token_value else ("", "")
    base = normalize_gateway_url(explicit_url or url_from_token or fallback_url)
    return GatewayCredentials(base_url=base, token=token,
                              source=source if token else "none")
