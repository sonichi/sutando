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
 * With no managed file present (every pre-managed install), resolution is
 * byte-for-byte identical to the legacy env chain — this module changes where
 * the decision lives, not what it decides.
 *
 * The `source` field is the point of G8: Settings and health-check surface
 * WHICH tier satisfied the capability ("voice: managed" / "voice: BYO"), so
 * managed-vs-BYO drop-in is observable rather than asserted.
 *
 * Managed-file schema (version 1):
 *   { "version": 1, "capabilities": { "gemini-voice": { "key": "..." }, ... } }
 * Malformed or unreadable files skip the managed tier — never throw.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

export type Capability = 'gemini-voice' | 'gemini-text';

export type CredentialSource = 'managed' | 'env' | 'none';

export interface ResolvedCredential {
	key: string;
	source: CredentialSource;
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

function readManaged(path: string): Record<string, { key?: unknown }> {
	try {
		const parsed = JSON.parse(readFileSync(path, 'utf8'));
		const caps = parsed?.capabilities;
		return caps && typeof caps === 'object' && !Array.isArray(caps) ? caps : {};
	} catch {
		return {};
	}
}

export function resolveCredential(
	capability: Capability,
	opts?: { managedPath?: string },
): ResolvedCredential {
	const slots = CAPABILITY_FALLBACKS[capability];
	const managed = readManaged(opts?.managedPath ?? managedCredentialsPath());
	for (const slot of slots) {
		const key = managed[slot]?.key;
		if (typeof key === 'string' && key) return { key, source: 'managed' };
	}
	for (const slot of slots) {
		const key = process.env[ENV_VARS[slot]];
		if (key) return { key, source: 'env' };
	}
	return { key: '', source: 'none' };
}
