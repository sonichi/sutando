# Sutando documentation

This is the canonical map of Sutando's maintained documentation. Start with the
path that matches what you are trying to do; feature-specific agent procedures
remain colocated in `skills/*/SKILL.md` and are linked rather than duplicated.

Machine-readable ownership and lifecycle metadata lives in
[`catalog.json`](catalog.json). CI checks that every Markdown document under
`docs/` is listed here and in the catalog, and that local Markdown links resolve.

## Start here

- [Configure a workspace](workspace-config.md) — defaults, overrides, and
  resolver APIs.
- [Run Codex as the core](codex-core.md) — core selection, setup, and rollback.
- [Gemini as the non-owner sandbox](gemini-sandbox.md) — `sandbox.runtime`, for installs without Codex CLI.
- [Use built-in tools](built-in-tools.md) — authoritative capability catalog.
- [External runtime dependencies](runtime-dependencies.md) — what must be
  installed, what only a feature needs, and what to vendor when embedding.

## Agent instruction detail (relocated from CLAUDE.md, 2026-08-17 context diet)

- [Channel access control](access-control.md) — per-channel tier rules and gates.
- [Core pool + standing sessions](core-pool-standing-sessions.md) — how the lead-follower pool composes with standing sessions; design record, not yet implemented.
- [Migration transition window](migration-transition-window.md) — 30-day reader fallback.
- [Learn from demonstration](learn-from-demonstration.md) — owner-taught preference capture.
- [Tutorial delivery](tutorial-delivery.md) — walkthrough procedure.
- [Graceful shutdown](graceful-shutdown.md) — which restart path signals the core to exit cleanly.
- [CLAUDE.md moved detail](claude-md-moved-detail.md) — verbatim parking for relocated snippets.
- [Subagent delegation](subagent-delegation.md) — when to spawn a subagent and how to pick its model.

## Guides and examples

- [Community use cases](community-use-cases/README.md)
  - [Self-healing install](community-use-cases/self-healing-install.md)
- [Set up automatic wire-list regeneration](regen-wire-list-setup.md)
- [GAIA-100 benchmark run (2026-08-26)](benchmarks/gaia-100-2026-08-26.md) — 76/100 blind-scored, and how to rebuild the suite.

## Operations

- [Release process and migrations](release-process.md)
- [Workspace sync across machines](workspace-sync.md)
- [Per-host workspace convention](workspace-hosts-convention.md)
- [Per-host carried-path rules](workspace-per-host-paths.md)
- [State-sync allowlist design](state-sync-allowlist.md)
- [Testing and coverage](testing-coverage.md)
- [Black-box benchmarks](benchmarking.md)
  - [Comprehensive benchmark: revision `3a73e03`, 2026-08-24](benchmark-reports/2026-08-24-3a73e03.md)
- [Voice-agent test framework](voice-agent-test-framework.md)

## Reference

- [Host CLI bindings](host-cli-bindings.md)
- [Remote gateway protocol](remote-gateway-protocol.md)
- [Slack bridge](slack-bridge.md)
- [Generated `src/` module map](src-map.md)
- [Workspace operational contract](workspace-contract.md)

## Architecture and decisions

- [Architecture boundaries](architecture-boundaries.md)
- [ag2-sparrow v1 delivery contract](sparrow-v1-contract.md)
- [D1 identity/state census (strangler Slice 1)](census/d1-identity-census.md)
- [The file delivery protocol as a formal state machine](delivery-protocol.md)
- [Mediated capability layer RFC](design-mediated-capability-layer.md)
- [Claude Code hook contract v1](runtime/claude-hook-contract-v1.md)
- [Workspace two-space model](workspace-design.md)
- [Core health verdict + severity gate](design-core-health-verdict.md)
- [Pointer Teacher design](pointer-teacher-design.md)
- [Credential resolution by capability (G8)](design-credential-capability-resolver.md)
- [ADR 0001: Pointer Teacher brain](adr/0001-pointer-teacher-brain.md)

## Documentation contract

Each `docs/**/*.md` file must:

1. be linked from this index;
2. have one entry in `docs/catalog.json` with `title`, `audience`, `status`,
   `canonical_for`, and `last_verified`;
3. use relative links for repository-local content;
4. name one canonical document for a policy or contract instead of copying the
   same normative text into multiple files.

Allowed lifecycle statuses are:

- `canonical` — authoritative for the named contract or policy;
- `active` — maintained guidance or reference, but not the sole authority;
- `draft` — under discussion and not operational policy;
- `historical` — retained for context and not current guidance.

`last_verified` is an ISO date when the document was checked against current
behavior. It may be `null` for legacy documents not yet verified under this
contract; release preparation must surface and resolve `null` for every document
affected by that release.

Run the audit locally:

```bash
python3 skills/release/scripts/docs_audit.py
```
