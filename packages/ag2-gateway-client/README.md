# ag2-gateway-client

The **one** credential/identity/request implementation for AG2 Space gateway
clients. Consumers: `ag2-sparrow` (resident task/event transport) and the
`agent-room-ops` skill (on-demand capability client). After the extraction
lands (PR2/PR3), re-implementing any of this in a consumer is a CI-failing
boundary violation.

## Public API (v0 — deliberately small)

- `resolve_credentials(env=None, vault_token_reader=None) -> Credentials`
  — the one true precedence chain: explicit `GATEWAY_TOKEN`/`RELAY_TOKEN` >
  `REMOTE_TASK_TOKEN` > token file (`REMOTE_TASK_TOKEN_FILE`) > injected
  vault reader; URL: `GATEWAY_URL` > `RELAY_URL` > `REMOTE_TASK_URL` >
  url-from-combined `url|secret`. Vault access is an **injected callable**;
  this package never imports a vault implementation.
- `resolve_identity(env=None) -> str` — the CLIENT-DECLARED identity
  (`AGENT_MXID` > `AGENT_ID`). **Non-authoritative by contract**: the
  gateway's authenticated token subject is the effective agent identity;
  the declared MXID is at most a consistency check.
- `request(method, url, *, profile, ...)` / `request_json(...)` — `profile`
  is REQUIRED, one of `interactive` (15s), `long_poll` (35s), `upload`,
  `download`. No default: the interactive/long-poll timeout split is
  deliberate and call sites never scatter magic numbers.
- `degrade_reason(code)` — uniform non-2xx mapping (401 bearer ≠ 403
  membership).

## Install paths (no import-fallbacks, by rule)

- **ag2-sparrow** (pipx/pip): declares `ag2-gateway-client>=0.1,<0.2` in its
  `pyproject.toml` (PR3).
- **Sutando runtime / Desktop**: the bootstrap installs the monorepo package
  (`pip install -e packages/ag2-gateway-client`) at the same step that
  installs sparrow (PR2 wires this).
- A consumer that cannot import this package **fails loudly with an install
  instruction**. There is deliberately NO fallback to a legacy local
  resolver — a fallback would keep the duplicate implementation alive
  forever.

## Promotion contract (owner-ratified, 2026-08-05)

    Current home:
      sonichi/sutando/packages/ag2-gateway-client  (incubation)
    Intended ownership:
      provider-neutral AG2 Space gateway client
    Promotion trigger (any of):
      - ag2space-cli or ag2space-mcp adopts it; or
      - a third-party consumer adopts it; or
      - the capability-lane repo layout is ratified
    Promotion target:
      ag2-space/ag2-gateway-client
    Compatibility requirement:
      promotion changes package source/version pinning only,
      not its public Python API

## Day-one constraints

- stdlib only; never imports the Sutando repo's `src/`
- package name carries no `sutando`
- no sparrow lease/task logic; no room-ops gate/policy; no resident events
  runtime
- provider-neutral public API; independently publishable metadata
