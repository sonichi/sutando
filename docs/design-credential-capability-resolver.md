# Credential resolution by capability, not key (G8)

*Status: draft for review — G8 of the desktop-parity plan
(`design-desktop-onboarding-parity.md` in the roadmap vault). Companion to the
first refactor slice, #2197 (`src/credential-resolver.ts` + the TS `GEMINI_*`
reader sweep). Owner call outstanding: confirm **managed pilot = Gemini voice**.*

## Problem

Consumers throughout the codebase read **raw key names** (`GEMINI_API_KEY`,
`GEMINI_VOICE_API_KEY`, …) straight from the environment. That couples every
consumer to (a) the provider, (b) the shape of the user's setup (BYO `.env`),
and (c) nothing else — there is no seam where a *managed* credential
(desktop-onboarding or AgentUniverse-provisioned) can drop in, and no way for
Settings or health-check to say **which** credential is in use.

## Contract

A consumer asks for a **capability** — what it needs to *do* — and the
resolver decides which credential satisfies it:

```ts
resolveCredential('gemini-voice')  →  { key, source: 'managed' | 'env' | 'none' }
```

Three rules, all load-bearing (implemented in `src/credential-resolver.ts`):

1. **Tier order: managed → env.** The managed tier reads
   `<workspace>/state/auth/managed-credentials.json` — per-host durable
   install state under `state/auth/`, same never-wiped contract as
   `cloud-auth.json`/`device.json`. The env tier is today's BYO behavior,
   unchanged. Malformed or missing managed files skip the tier; they never
   throw and never block BYO.
2. **In-tier fallback mirrors the legacy chain.** `gemini-voice` falls back to
   `gemini-text` *within each tier* (the existing `GEMINI_VOICE_API_KEY` →
   `GEMINI_API_KEY` chain, generalized), so a single-key setup works
   identically at every tier. With no managed file present, resolution is
   **byte-for-byte identical** to the legacy env chain — the refactor moves
   where the decision lives, not what it decides.
3. **`source` is surfaced, not swallowed.** Settings and health-check report
   "voice: managed" / "voice: BYO" / "voice: none". Managed-vs-BYO drop-in is
   observable rather than asserted — this is the property the desktop
   first-run banner (G5) and Settings (G6) build on.

## Managed-file schema

```json
{ "version": 1, "capabilities": { "gemini-voice": { "key": "…" }, "gemini-text": { "key": "…" } } }
```

Writers: the desktop onboarding flow (G1/G2, air's lane) or AU provisioning.
Written atomically (tmp + rename), mode 0600, only under `state/auth/`.
`version` gates future shape changes; unknown versions skip the tier (fail
open to BYO, never crash a working voice setup).

## Capability vocabulary

Capabilities name *functions*, not providers-and-keys: `gemini-voice`,
`gemini-text` today; `phone-tts`, `browser-llm`, … as consumers migrate. A
capability may be satisfied by different providers over time without touching
consumers — that is the point of the seam. New capabilities are added to the
resolver's fallback table, not scattered as new env reads.

## Interaction with G7 (vault)

G7 (owner call open: BYO keys → vault vs `.env`) slots in as **a third tier or
an env-tier replacement**, behind the same contract: if BYO keys move to the
Tauri secure store, the resolver gains a `vault` source and consumers change
*nothing*. The G7 decision therefore does not block reader migration — only
the tier list grows.

## Migration plan (reader sweep)

- **Phase 1 — TS surfaces (#2197, done):** `voice-agent.ts`, `voice-key.ts`,
  `browser-tools.ts`, `recording-tools.ts` + `startup-runtime.sh` gate +
  health-check surface. Tests: `tests/credential-resolver.test.ts` (tier
  order, fallback, malformed-file, byte-identical-legacy).
- **Phase 2 — Python readers:** `grep -rn "GEMINI\w*_API_KEY" --include="*.py"`
  enumerates the surface; a `src/credential_resolver.py` twin implements the
  SAME contract (shared test vectors, per the policy-twin lesson from #2516:
  twins share canaries so a latent defect can't survive in one language).
- **Phase 3 — non-Gemini keys:** one provider at a time, each a
  capability-vocabulary addition + reader sweep, never a big-bang.

## Security notes

- The resolver **reads** credentials; it never writes or logs them. `source`
  is loggable; `key` is not.
- The managed tier widens the read surface to one well-known file path with
  the `state/auth/` durability contract — no new secret *storage* mechanism is
  introduced by G8 itself (that is G7's decision).
- Tier order (managed wins over env) is deliberate: a provisioned install
  behaves as provisioned even if stale BYO keys linger in `.env`. The
  `source` surfacing is the guard against surprise — a user who expects BYO
  sees "managed" in Settings and knows exactly what to remove.

## Open items

1. **Owner:** confirm managed pilot = Gemini voice (G8 call, stands).
2. **Owner:** G7 vault-vs-`.env` (shapes the tier list, blocks nothing here).
3. Phase-2 Python twin (mine, after #2197 lands).
4. Desktop consumers of `source` (G5 banner, G6 Settings — air's lane).
