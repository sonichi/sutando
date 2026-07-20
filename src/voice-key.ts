/**
 * Shared Gemini API-key resolution for voice surfaces (voice-agent,
 * phone-conversation, and any plugin voice surface).
 *
 * As of G8 this is a thin wrapper over the credential resolver
 * (src/credential-resolver.ts), which prepends a managed tier
 * (desktop/AU-provisioned managed-credentials.json) to the existing chain:
 *
 *   managed(voice → text) → GEMINI_VOICE_API_KEY → GEMINI_API_KEY → ''.
 *
 * With no managed file present the chain is byte-for-byte the pre-G8
 * behavior. GEMINI_VOICE_API_KEY isolates voice billing onto a dedicated key
 * (paid-tier for the model+grounding combos voice uses); MAIN-key fallback
 * preserves the single-key setup path for fresh installs.
 *
 * Why a util: all three voice surfaces should pick the same key the same way,
 * so a tier upgrade on the VOICE key benefits all three at once.
 */
import { resolveCredential } from './credential-resolver.js';

export function voiceApiKey(): string {
	return resolveCredential('gemini-voice').key;
}
