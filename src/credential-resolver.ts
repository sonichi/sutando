/**
 * credential-resolver — resolve a *capability's* credential, not a hardcoded env
 * key, so a BYO key and an AgentUniverse-managed token are interchangeable
 * sources behind one call. Swapping the source changes zero consumer code — the
 * "managed drop-in" property (desktop parity G8).
 *
 * This is the SHELL: it establishes the seam. The managed source is absent by
 * default (`getManagedCredentialSource()` → null), so every consumer keeps its
 * exact current behavior — the resolver falls straight through to the BYO env
 * chain. A composition root registers a real source only once a managed session
 * is active (that lands with the AgentUniverse account; W4).
 *
 * TS services read BYO keys from the environment (the supervisor injects them
 * from the vault/Keychain at startup), so on this side "vault" and "env" are one
 * source — the vault-vs-.env question (G7) is a Python/supervisor concern, not
 * this module's.
 */

/** A capability the app needs a credential for. Extend as consumers are routed. */
export type Capability =
	| 'gemini'
	| 'gemini-voice'
	| 'anthropic'
	| 'cartesia'
	| 'twilio';

/**
 * A source of managed credentials (e.g. AgentUniverse). Returns the token for a
 * capability, or `undefined`/`''` when it can't provide one — in which case the
 * resolver falls through to the BYO env chain.
 */
export interface ManagedCredentialSource {
	get(cap: Capability): string | undefined;
}

let _managed: ManagedCredentialSource | null = null;

/**
 * Register (or clear, with `null`) the managed credential source. Called at the
 * composition root when a managed session becomes active. Idempotent.
 */
export function setManagedCredentialSource(source: ManagedCredentialSource | null): void {
	_managed = source;
}

/** The current managed source, or null when none is registered (the default). */
export function getManagedCredentialSource(): ManagedCredentialSource | null {
	return _managed;
}

/**
 * Resolve a capability's credential.
 *
 * Order (first non-empty wins):
 *   1. managed source, when registered (recommended default once connected:
 *      managed = the paid/metered path the user opted into).
 *   2. the BYO env chain, in order (e.g. dedicated key → main key).
 *   3. '' — the caller asserts/degrades exactly as it does today.
 *
 * PRECEDENCE NOTE (G8 owner call #3): managed-first is the recommended default.
 * To make BYO win when both are present, swap the two blocks below — a one-line
 * flip, deliberately isolated here so the decision touches nothing else.
 */
export function resolveCredential(cap: Capability, envChain: Array<string | undefined>): string {
	const managed = _managed?.get(cap);
	if (managed) return managed;
	for (const v of envChain) {
		if (v) return v;
	}
	return '';
}
