/**
 * Credential resolver — capability, not key (G8, desktop-parity plan).
 *
 * Consumers ask for a CAPABILITY ('gemini-voice', 'gemini-text') and the
 * resolver decides which credential satisfies it, walking tiers in order:
 *
 *   1. managed — desktop/AU-provisioned `<workspace>/state/auth/managed-credentials.json`
 *                (per-host durable install state, same contract as cloud-auth.json:
 *                never wiped by transient-state cleanup)
 *   2. env     — BYO keys from the workspace `.env` / exported environment
 *                (GEMINI_VOICE_API_KEY / GEMINI_API_KEY — today's behavior)
 *
 * Within each tier a voice capability falls back to the text credential,
 * mirroring the existing GEMINI_VOICE_API_KEY → GEMINI_API_KEY chain, so a
 * single-key setup keeps working unchanged at every tier.
 *
 * `voicePreference` truth table (design 2b; amendment S1 — the SHARED
 * credential-source table this resolver, the supervisor injection/`requires`
 * gate, `startup-runtime.sh`'s shell gate, `health-check.py`, and Rust status
 * all implement; `tests/voice-preference-consumers.test.sh` pins agreement):
 *
 *   - unset (legacy — every pre-preference install): managed(voice→text)→env,
 *     exactly the tier walk above.
 *   - 'managed': ONLY a non-quarantined managed entry satisfies the voice
 *     capability. A present env key must NOT silently satisfy a managed
 *     preference — that would be the logout-quarantine bypass. No usable
 *     managed entry ⇒ `{key:'', source:'none'}` (fail actionably).
 *   - 'byok': the managed tier is skipped entirely for the voice capability
 *     (both fallback slots); only env keys satisfy. No env key ⇒
 *     `{key:'', source:'none'}` (fail actionably).
 *   - `quarantined: true` (signed-out quarantine, design 2b): every managed
 *     entry is treated as ABSENT in every mode and for every capability.
 *
 * `voicePreference` scopes the VOICE capability; 'gemini-text' resolution is
 * preference-independent (its consumers are not voice surfaces) but still
 * honors the quarantine marker — quarantine is about revoking managed
 * credentials after logout, not about source choice.
 *
 * With no managed file present (every pre-managed install), resolution is
 * byte-for-byte identical to the legacy env chain — this module changes where
 * the decision lives, not what it decides.
 *
 * The `source` field is the point of G8: Settings and health-check surface
 * WHICH tier satisfied the capability ("voice: managed" / "voice: BYO"), so
 * managed-vs-BYO drop-in is observable rather than asserted.
 * `credentialSourceLabel()` maps it onto the design's user-facing
 * 'managed' | 'byok' | 'none' vocabulary without breaking 'env' consumers.
 *
 * Managed-file schema (version 1):
 *   { "version": 1, "capabilities": { "gemini-voice": { "key": "...",
 *     "generation": "cg1-…"? }, ... },
 *     "voicePreference": "managed"|"byok"?, "quarantined": bool?,
 *     "preferenceRevision": u64?, "sessionRevision": u64? }
 * `preferenceRevision`/`sessionRevision` are top-level coordination metadata
 * committed in the same atomic write as the policy fields (amendment R15);
 * this read side tolerates and ignores them. Malformed or unreadable files
 * skip the managed tier (empty caps, unset preference, not quarantined) —
 * never throw.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

export type Capability = 'gemini-voice' | 'gemini-text';

export type CredentialSource = 'managed' | 'env' | 'none';

export interface ResolvedCredential {
	key: string;
	source: CredentialSource;
	/**
	 * Opaque credential-generation ID (e.g. `cg1-<UUID>`) minted by the Rust
	 * host when the credential was committed/materialized (design 1a′;
	 * amendments R7/S3). This resolver only REPORTS it — never mints, never
	 * derives it from the secret:
	 *   - managed tier: the managed entry's `generation` field, when present;
	 *   - env tier (voice): `SUTANDO_VOICE_CREDENTIAL_GENERATION`, injected
	 *     into the child env by the launcher after materialization.
	 * Legacy credentials without a generation omit the field.
	 */
	credentialGeneration?: string;
}

/** Per-capability lookup order within a tier (voice falls back to text). */
const CAPABILITY_FALLBACKS: Record<Capability, string[]> = {
	'gemini-voice': ['gemini-voice', 'gemini-text'],
	'gemini-text': ['gemini-text'],
};

/** Env-var names per capability slot, in existing-chain order. */
const ENV_VARS: Record<string, string> = {
	'gemini-voice': 'GEMINI_VOICE_API_KEY',
	'gemini-text': 'GEMINI_API_KEY',
};

export function managedCredentialsPath(): string {
	return join(resolveWorkspace(), 'state', 'auth', 'managed-credentials.json');
}

/** The user-committed voice credential-source preference (design 2b). */
export type VoicePreference = 'managed' | 'byok';

interface ManagedFile {
	caps: Record<string, { key?: unknown; generation?: unknown }>;
	/** Top-level `voicePreference`; anything but the two literals ⇒ unset. */
	voicePreference: VoicePreference | undefined;
	/**
	 * Top-level signed-out quarantine marker (strictly `true` — the only
	 * writer is the Rust host, which writes real JSON booleans; a whole-file
	 * corruption fails the parse and skips the managed tier anyway).
	 */
	quarantined: boolean;
}

function readManaged(path: string): ManagedFile {
	try {
		const parsed = JSON.parse(readFileSync(path, 'utf8'));
		const caps = parsed?.capabilities;
		const pref = parsed?.voicePreference;
		return {
			caps: caps && typeof caps === 'object' && !Array.isArray(caps) ? caps : {},
			voicePreference: pref === 'managed' || pref === 'byok' ? pref : undefined,
			quarantined: parsed?.quarantined === true,
		};
	} catch {
		return { caps: {}, voicePreference: undefined, quarantined: false };
	}
}

export function resolveCredential(
	capability: Capability,
	opts?: { managedPath?: string },
): ResolvedCredential {
	const slots = CAPABILITY_FALLBACKS[capability];
	const managed = readManaged(opts?.managedPath ?? managedCredentialsPath());
	// S1: the preference governs the VOICE capability; quarantine hides
	// managed entries from every capability in every mode.
	const preference = capability === 'gemini-voice' ? managed.voicePreference : undefined;
	if (preference !== 'byok' && !managed.quarantined) {
		for (const slot of slots) {
			const entry = managed.caps[slot];
			const key = entry?.key;
			if (typeof key === 'string' && key) {
				// S3: managed entries may carry an opaque Rust-minted `generation`.
				// Report it verbatim; legacy entries without one omit the field.
				const generation = entry?.generation;
				return {
					key,
					source: 'managed',
					...(typeof generation === 'string' && generation
						? { credentialGeneration: generation }
						: {}),
				};
			}
		}
	}
	if (preference === 'managed') {
		// S1: ONLY a non-quarantined managed entry satisfies a managed
		// preference — a present env key must not silently satisfy it (the
		// logout-quarantine bypass the design closes). Fail actionably.
		return { key: '', source: 'none' };
	}
	for (const slot of slots) {
		const key = process.env[ENV_VARS[slot]];
		if (key) {
			// S3/U4: for the voice capability the launcher injects
			// SUTANDO_VOICE_CREDENTIAL_GENERATION beside a materialized BYOK
			// key. Report it verbatim; manual/legacy .env keys (no injected
			// generation) stay generationless (Y4/Z4: a generic vault write
			// never carries a transactional generation).
			const generation =
				capability === 'gemini-voice'
					? process.env.SUTANDO_VOICE_CREDENTIAL_GENERATION
					: undefined;
			return {
				key,
				source: 'env',
				...(generation ? { credentialGeneration: generation } : {}),
			};
		}
	}
	return { key: '', source: 'none' };
}

/**
 * Map the resolver's internal source onto the design's user-facing
 * vocabulary (design 2b / impl plan WS2 Step 3): `agent.state`, Settings and
 * health surfaces say 'byok' where the resolver says 'env'. Kept as a mapper
 * (not a rename) so existing 'env' consumers keep working unchanged.
 */
export function credentialSourceLabel(
	source: CredentialSource,
): 'managed' | 'byok' | 'none' {
	if (source === 'managed') return 'managed';
	if (source === 'env') return 'byok';
	return 'none';
}
