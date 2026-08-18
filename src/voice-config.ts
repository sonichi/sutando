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
	/** Run a second (batch gemini-2.5-flash) transcription pass and LOG
	 * divergences from what the Live model heard. Observation-only. Default false. */
	shadowStt: boolean;
	/** With shadowStt: on a detected mishear, speak a one-sentence
	 * self-correction. Default false. */
	divergenceCorrection: boolean;
	/** Per-channel overrides, keyed by voice channel id. */
	channels: Record<string, VoiceChannelConfig>;
	/** Phase 0.5 seam (design §2.1): context-window compression. ABSENT = off
	 *  (today's wire behaviour). `{}` = on with the SERVER's defaults (trigger
	 *  at 80% of the model limit, target half the trigger). Explicit
	 *  thresholds must be safe positive integers with
	 *  0 < targetTokens < triggerTokens, both-or-neither. */
	compressionConfig?: { triggerTokens?: number; targetTokens?: number };
	/** Phase 0.5 seam (design §2.2): session-wide media token cost for
	 *  realtime-input images (LOW = 64 tokens/frame). Session-wide means it
	 *  reaches one-shot send_vision_frame too — realtime input has no
	 *  per-send override. ABSENT = unset (server default). */
	mediaResolution?:
		| 'MEDIA_RESOLUTION_LOW'
		| 'MEDIA_RESOLUTION_MEDIUM'
		| 'MEDIA_RESOLUTION_HIGH';
}

export const VOICE_CONFIG_DEFAULTS: VoiceConfig = {
	model: 'gemini-2.5-flash-native-audio-preview-12-2025',
	googleSearch: true,
	owner_mode: false,
	shadowStt: false,
	divergenceCorrection: false,
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

/** What resolveSessionTuning hands the VoiceSession config. Keys are REALLY
 *  absent when off — `undefined` is not absent at the provider boundary. */
export interface VoiceSessionTuning {
	compressionConfig?: { triggerTokens?: number; targetTokens?: number };
	mediaResolution?: VoiceConfig['mediaResolution'];
}

const MEDIA_RESOLUTIONS = [
	'MEDIA_RESOLUTION_LOW',
	'MEDIA_RESOLUTION_MEDIUM',
	'MEDIA_RESOLUTION_HIGH',
] as const;

/** A compression threshold must be a safe positive integer — the Live API
 *  models these as int64, and a float or 0 is operator error, not tuning. */
function parseThreshold(name: string, value: unknown): number {
	const n = typeof value === 'string' ? Number(value) : value;
	if (typeof n !== 'number' || !Number.isSafeInteger(n) || n <= 0) {
		throw new Error(
			`[voice-config] ${name} must be a positive integer, got ${JSON.stringify(value)}`,
		);
	}
	return n;
}

/** Both-or-neither + 0 < target < trigger (design §2.1) — an inverted or
 *  half-set pair is a silently-degrading misconfiguration, so it throws. */
function validatePair(
	source: string,
	trigger: unknown,
	target: unknown,
): { triggerTokens: number; targetTokens: number } {
	if (trigger === undefined || target === undefined) {
		throw new Error(
			`[voice-config] ${source}: set BOTH triggerTokens and targetTokens or neither ` +
				`(omit both for the server's defaults)`,
		);
	}
	const triggerTokens = parseThreshold(`${source} triggerTokens`, trigger);
	const targetTokens = parseThreshold(`${source} targetTokens`, target);
	if (targetTokens >= triggerTokens) {
		throw new Error(
			`[voice-config] ${source}: need 0 < targetTokens < triggerTokens, ` +
				`got trigger=${triggerTokens} target=${targetTokens}`,
		);
	}
	return { triggerTokens, targetTokens };
}

/**
 * Resolve the Phase 0.5 session-tuning seams (design §2.1/§2.2) from the
 * loaded config plus the two env overrides, validating at load time.
 *
 * With nothing set the result is `{}` — the VoiceSession config carries
 * NEITHER key, so the wire behaviour is byte-identical to a build without
 * the seams (the Phase 0.5 gate). VOICE_CTX_TRIGGER_TOKENS /
 * VOICE_CTX_TARGET_TOKENS override file thresholds and on their own enable
 * compression; invalid shapes throw with a clear message so startup fails
 * loudly instead of shipping a silently-degrading pair.
 */
export function resolveSessionTuning(
	config: VoiceConfig,
	env: Record<string, string | undefined> = process.env,
): VoiceSessionTuning {
	const out: VoiceSessionTuning = {};

	if (config.mediaResolution !== undefined) {
		if (!(MEDIA_RESOLUTIONS as readonly string[]).includes(config.mediaResolution as string)) {
			throw new Error(
				`[voice-config] mediaResolution must be one of ${MEDIA_RESOLUTIONS.join(' | ')}, ` +
					`got ${JSON.stringify(config.mediaResolution)}`,
			);
		}
		out.mediaResolution = config.mediaResolution;
	}

	const envTrigger = env.VOICE_CTX_TRIGGER_TOKENS;
	const envTarget = env.VOICE_CTX_TARGET_TOKENS;
	if (envTrigger !== undefined || envTarget !== undefined) {
		// Env wins over the file and on its own enables compression.
		out.compressionConfig = validatePair(
			'VOICE_CTX_TRIGGER_TOKENS/VOICE_CTX_TARGET_TOKENS',
			envTrigger,
			envTarget,
		);
	} else if (config.compressionConfig !== undefined) {
		const cc = config.compressionConfig;
		if (cc === null || typeof cc !== 'object' || Array.isArray(cc)) {
			throw new Error(
				`[voice-config] compressionConfig must be an object ({} enables server defaults), ` +
					`got ${JSON.stringify(cc)}`,
			);
		}
		if (cc.triggerTokens === undefined && cc.targetTokens === undefined) {
			// {} = enabled with the server's own defaults (§2.1 path 2) — the
			// vendor's tuning, tracking the model limit, no locally-guessed constant.
			out.compressionConfig = {};
		} else {
			out.compressionConfig = validatePair(
				'compressionConfig',
				cc.triggerTokens,
				cc.targetTokens,
			);
		}
	}

	return out;
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
