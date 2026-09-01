# Credential resolution by capability, not key (G8)

*Status: draft for review — G8 of the desktop-parity plan
(`design-desktop-onboarding-parity.md` in the roadmap vault). Companion to the
first refactor slice, **#2197 (MERGED 2026-08-03)**, which introduced
`src/credential-resolver.ts` + the TS `GEMINI_*` reader sweep — file paths
below are now current `main`. Owner call outstanding:
confirm **managed pilot = Gemini voice**.*

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
resolveCredential('gemini-voice')  →  { key, source: 'managed' | 'env' | 'none', credentialGeneration? }
```

Three rules, all load-bearing. Rules 1–2 and the *resolver-seam* half of rule 3
are implemented by #2197's `src/credential-resolver.ts`; rule 3's Settings/health
*presentation* is follow-up (each rule states what is shipped vs. follow-up):

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
3. **`source` is surfaced, not swallowed.** #2197 implements this at the
   *resolver seam*: `resolveCredential()` returns `source: 'managed' | 'env' |
   'none'`, so which tier satisfied the capability is a returned, inspectable
   property rather than something swallowed. **Not yet implemented:** the
   Settings / health-check *presentation* of that property as
   "voice: managed" / "voice: BYO" / "voice: none" — `src/health-check.py`
   today reports "managed voice credential configured" / "Gemini voice
   credential configured" / disabled and does not yet consume
   `ResolvedCredential.source`. Turning the returned `source` into that
   owner-visible three-state vocabulary is follow-up work — exactly what the
   desktop first-run banner (G5) and Settings (G6) build on the returned
   property to do.

### Voice-source policy gates (S1)

Two top-level managed-file fields modulate rules 1–2 for the **voice**
capability (design authority: the desktop repo's
`docs/design-voice-reliability-and-onboarding.md` §2b, amendment S1):

- **`voicePreference`** (`"managed" | "byok"`; unset ⇒ legacy):
  - *unset* — rules 1–2 apply unchanged (managed → env, voice → text).
  - *`managed`* — only a non-quarantined **managed** entry satisfies
    `gemini-voice`; env keys never silently substitute. A managed-preference
    install with no managed key resolves `none` — a typed absence the
    desktop enable-voice flow routes on, rather than a wrong-source key.
  - *`byok`* — only **env** keys satisfy `gemini-voice`; managed slots are
    skipped entirely.
- **`quarantined`** (honored only as strict JSON `true`; stamped by the
  desktop host at logout): every managed entry is treated as absent, in
  every mode — a preserved managed key must never satisfy a resolution
  after logout.

The preference governs the voice capability; `gemini-text` resolution is
preference-independent (its consumers are not voice surfaces) but still
honors `quarantined`. Resolution also returns the satisfying credential's
**`credentialGeneration`** (opaque `cg1-<UUID>`: the managed entry's
`generation` field, or `SUTANDO_VOICE_CREDENTIAL_GENERATION` for env keys
when present) so a supervisor can verify *which* credential a running agent
loaded. The full truth table is pinned one-for-one across all four
consumers — TS resolver, Python twin, startup gate, health check — by
`tests/voice-preference-consumers.test.sh` +
`tests/fixtures/voice-preference-matrix.json` (the desktop supervisor
asserts the same fixture verbatim on its side).

## Managed-file schema

```json
{
  "version": 1,
  "capabilities": {
    "gemini-voice": { "key": "…", "generation": "cg1-…" },
    "gemini-text": { "key": "…" }
  },
  "voicePreference": "managed",
  "preferenceRevision": 4,
  "sessionRevision": 2,
  "quarantined": false
}
```

The policy fields (`voicePreference`, `preferenceRevision`,
`sessionRevision`, `quarantined`) and the per-entry `generation` are written
by the desktop host's credential provisioner (desktop design doc §2b; the
Rust writer is in flight as of 2026-08). Readers here honor them per S1 and
keep the existing failure posture: malformed file ⇒ managed tier skipped,
`voicePreference` outside the two literals ⇒ unset, `quarantined` anything
but strict `true` ⇒ not quarantined.

Writers (**required of a future provisioner — not yet implemented**): the
desktop onboarding flow (G1/G2, air's lane) or AU provisioning. **No production
writer exists today** — `origin/main` ships only readers (`#2197`'s
`readManaged()`, startup, health) plus test-fixture writes; a production
writer-pattern scan across `src`/`electron`/`integrations` returns none. When
that provisioner is built it MUST write the file atomically (tmp + rename), mode
0600, only under `state/auth/`. Until then, treat the atomic/0600 persistence as
a requirement on the writer, not a shipped guarantee — do not rely on it as an
existing security property.
`version` is reserved for future schema changes. **Not yet enforced:** #2197's
`readManaged()` reads `capabilities` regardless of `version`, so an unknown
version does not currently skip the tier — a `{ "version": 999, … }` file with a
valid capability still resolves as managed. Enforcing unknown-version → skip
(fail open to BYO, never crash a working voice setup) is a follow-up on the
resolver, not part of #2197.

## Capability vocabulary

A capability names a *function* — a role a consumer needs — decoupling the
consumer from the credential and, ultimately, the provider. The **shipped**
decoupling is from the *key*: a consumer asks for `gemini-voice` and never reads
an env var or names a file. Today's canonical IDs are still **provider-branded
for continuity** with the existing env chain (`gemini-voice` / `gemini-text`
mirror `GEMINI_VOICE_API_KEY` / `GEMINI_API_KEY`), so a provider swap today would
still change the ID — provider *neutrality* is a follow-up, not a current
property of the vocabulary.

The migration contract that makes a provider swap consumer-invisible: introduce
role IDs (`voice-llm`, `text-llm`) and register the current provider-branded IDs
as **aliases** to the same role in the resolver's fallback table, so both resolve
identically and consumers migrate at their own pace. Until those aliases land,
the portability claim is scoped to "same provider, different tier/key"
(managed↔BYO) — which the `source` field already makes observable. New
capabilities are added to the resolver's fallback table, not scattered as new
env reads.

## Interaction with G7 (vault)

G7 (owner call open: BYO keys → vault vs `.env`) slots in as **a third tier or
an env-tier replacement**, behind the same contract: if BYO keys move to the
Tauri secure store, the resolver gains a `vault` source and consumers change
*nothing*. The G7 decision therefore does not block reader migration — only
the tier list grows.

## Migration plan (reader sweep)

- **Phase 1 — TS surfaces (#2197, MERGED — the implementing PR):** `voice-agent.ts`, `voice-key.ts`,
  `browser-tools.ts`, `recording-tools.ts` + `startup-runtime.sh` gate +
  health-check surface. Tests: `tests/credential-resolver.test.ts` (tier
  order, fallback, malformed-file, byte-identical-legacy).
- **Phase 2 — Python readers (OPEN: PR #2575):** `grep -rn "GEMINI\w*_API_KEY"
  --include="*.py"` enumerates the surface; a `src/credential_resolver.py`
  twin implements the SAME contract (shared test vectors, per the policy-twin
  lesson from #2516: twins share canaries so a latent defect can't survive in
  one language). The twin has landed as **PR #2575** (john-approved, awaiting
  merge); the follow-on reader sweep routing the enumerated `GEMINI_*` readers
  through `resolve_credential` follows once it merges.
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
3. Phase-2 Python twin — **PR #2575** (open, john-approved, awaiting merge); reader sweep follows its merge.
4. Desktop consumers of `source` (G5 banner, G6 Settings — air's lane).
