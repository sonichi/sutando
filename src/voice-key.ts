/**
 * Shared Gemini API-key resolution for voice surfaces (voice-agent,
 * phone-conversation, and any plugin voice surface).
 *
 * Chain: GEMINI_VOICE_API_KEY → GEMINI_API_KEY → ''.
 *
 * GEMINI_VOICE_API_KEY isolates voice billing onto a dedicated key (paid-tier
 * for the model+grounding combos voice uses). MAIN-key fallback preserves the
 * single-key setup path for fresh installs.
 *
 * Why a util: all three voice surfaces should pick the same key the same way,
 * so a tier upgrade on the VOICE key benefits all three at once. Pre-this-util,
 * only voice-agent.ts used the chain; phone + plugin surfaces read GEMINI_API_KEY
 * directly, which forced any tier-isolation to be done at the env-pointer level
 * (set the same value to both vars) instead of at the chain level.
 *
 * G8: resolution now goes through `resolveCredential('gemini-voice', …)` so a
 * managed source (AgentUniverse) can supply the token interchangeably with the
 * BYO env chain. With no managed source registered (the default) this is
 * byte-identical to the previous GEMINI_VOICE_API_KEY → GEMINI_API_KEY → '' chain.
 */
import { resolveCredential } from './credential-resolver.js';

export function voiceApiKey(): string {
	return resolveCredential('gemini-voice', [
		process.env.GEMINI_VOICE_API_KEY,
		process.env.GEMINI_API_KEY,
	]);
}
