/**
 * Per-surface voice configuration loader.
 *
 * `loadVoiceConfig(path)` is path-agnostic — each caller decides where its
 * config lives and passes the absolute path in. The config is per-user DATA
 * (model + grounding prefs the operator tunes), not code, so it does NOT live
 * in the git repo — it lives in the workspace:
 *
 *   - voice-agent        → `$SUTANDO_WORKSPACE/config/voice-agent.json`
 *   - phone-conversation → `$SUTANDO_WORKSPACE/config/phone-conversation.json`
 *   - discord-voice      → `$SUTANDO_WORKSPACE/config/discord-voice.json`
 *
 * Each surface ships a committed `*.example` template (`src/voice-agent.config
 * .json.example`, `skills/<surface>/config.json.example`); on first run the
 * surface copies the template into the workspace if the live config is
 * missing. Schema:
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
 * web client's code-heavy workload) ship a `.example` template carrying that
 * override. Phone inherits the default; discord-voice's template carries it
 * too, so a fresh install behaves identically.
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
