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
 *
 * Each surface ships a committed `*.example` template (`src/voice-agent.config
 * .json.example`, `skills/<surface>/config.json.example`); on first run the
 * surface copies the template into the workspace if the live config is
 * missing. Schema:
 *
 *   {
 *     "model": "gemini-2.5-flash-native-audio-preview-12-2025",
 *     "googleSearch": true,
 *     "owner_mode": false,
 *     "channels": { "<voice_channel_id>": { "owner_mode": true } }
 *   }
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
 * override. Phone inherits the default, so a fresh install behaves identically.
 */

import { readFileSync, existsSync } from 'fs';

/** Per-channel override entry. Object-shaped so it stays extensible. */
export interface VoiceChannelConfig {
	owner_mode?: boolean;
}

export interface VoiceConfig {
	model: string;
	googleSearch: boolean;
	/** Skill-wide default for owner-mode. Safe default: false (read-only). */
	owner_mode: boolean;
	/** Per-channel overrides, keyed by voice channel id. */
	channels: Record<string, VoiceChannelConfig>;
}

export const VOICE_CONFIG_DEFAULTS: VoiceConfig = {
	model: 'gemini-2.5-flash-native-audio-preview-12-2025',
	googleSearch: true,
	owner_mode: false,
	channels: {},
};

/**
 * Resolve the effective owner-mode for a voice channel — fail-closed.
 *
 * The config is raw JSON spread into `VoiceConfig`, so a hand-edited file can
 * carry a non-boolean value (string `"false"`, `null`, a number, a typo). A
 * loose `?? false` / truthy check would treat the *string* `"false"` as
 * truthy and grant owner tier to every speaker — a trust-boundary bug. Owner
 * mode is therefore granted ONLY when the value is the boolean literal `true`;
 * every other shape fails closed to `false`.
 *
 * Precedence (must NOT collapse to an OR of the two levels — that would break
 * a channel's explicit opt-out of a skill-wide default):
 *   1. If the channel entry exists AND carries an `owner_mode` key, that key
 *      decides — `=== true` grants, present-but-not-`true` (incl. `false`)
 *      denies. A channel-explicit `false` correctly overrides a skill default
 *      of `true`.
 *   2. Otherwise the skill-wide `config.owner_mode` decides (`=== true`).
 *   3. Otherwise `false`.
 */
export function resolveOwnerMode(
	config: VoiceConfig,
	channelId?: string,
): boolean {
	const channelEntry =
		channelId !== undefined ? config.channels?.[channelId] : undefined;
	if (
		channelEntry &&
		Object.prototype.hasOwnProperty.call(channelEntry, 'owner_mode')
	) {
		return channelEntry.owner_mode === true;
	}
	return config.owner_mode === true;
}

export function loadVoiceConfig(configPath: string): VoiceConfig {
	if (!existsSync(configPath)) return { ...VOICE_CONFIG_DEFAULTS, channels: {} };
	try {
		const raw = JSON.parse(readFileSync(configPath, 'utf-8'));
		return {
			...VOICE_CONFIG_DEFAULTS,
			...raw,
			// channels is a nested object — spread can't deep-merge, so take the
			// file's map verbatim when present, else fall back to the empty default.
			channels: raw.channels ?? {},
		};
	} catch (e) {
		console.warn(`[voice-config] failed to parse ${configPath}, using defaults: ${(e as Error).message}`);
		return { ...VOICE_CONFIG_DEFAULTS, channels: {} };
	}
}
