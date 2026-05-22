/**
 * Per-skill voice configuration loader.
 *
 * Each voice surface (voice-agent, phone-conversation, discord-voice) ships a
 * sibling `config.json` (or `*.config.json` for core-resident surfaces). Schema:
 *
 *   { "model": "gemini-2.5-flash-native-audio-preview-12-2025", "googleSearch": true }
 *
 * Missing file → defaults. Partial file → fill in missing keys from defaults.
 *
 * Defaults: 2.5 + search:true. Rationale: 2.5+search is the only combo that
 * works on BOTH the MAIN and VOICE Gemini keys (3.1+search needs paid-tier
 * entitlement that only MAIN currently has on most setups; 3.1 without search
 * works on either key but loses Web grounding by default — that's degrading
 * capability rather than picking a safe baseline). Surfaces that explicitly
 * want a different combo (e.g. voice-agent prefers 3.1 + search:false for the
 * web client's code-heavy workload) ship their own `config.json` with the
 * override. Phone and discord-voice inherit the default and don't need a file
 * unless they later want to diverge.
 */

import { readFileSync, existsSync } from 'fs';

export interface VoiceConfig {
	model: string;
	googleSearch: boolean;
}

export const VOICE_CONFIG_DEFAULTS: VoiceConfig = {
	model: 'gemini-2.5-flash-native-audio-preview-12-2025',
	googleSearch: true,
};

export function loadVoiceConfig(configPath: string): VoiceConfig {
	if (!existsSync(configPath)) return { ...VOICE_CONFIG_DEFAULTS };
	try {
		const raw = JSON.parse(readFileSync(configPath, 'utf-8'));
		return { ...VOICE_CONFIG_DEFAULTS, ...raw };
	} catch (e) {
		console.warn(`[voice-config] failed to parse ${configPath}, using defaults: ${(e as Error).message}`);
		return { ...VOICE_CONFIG_DEFAULTS };
	}
}
