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
 * capability rather than picking a safe baseline). The browser voice-agent
 * surface explicitly ships a newer `.example` template; phone inherits this
 * package default unless its own config overrides it.
 */

import { readFileSync, existsSync, writeFileSync, renameSync, copyFileSync } from 'fs';

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
	/** Phase 0.5 seam (design §2.1): context-window compression. ABSENT = take
	 *  whatever the built-in default says. `{}` = on with the SERVER's defaults
	 *  (trigger at 80% of the model limit, target half the trigger). Explicit
	 *  thresholds must be safe positive integers with
	 *  0 < targetTokens < triggerTokens, both-or-neither. `null`/`false` =
	 *  DELIBERATELY DISABLED — the user-side off-switch that survives the value
	 *  becoming a default (design §Phase 3, step 3e): once a default exists,
	 *  deleting the key stops meaning "off", so absent and disabled must be
	 *  distinguishable. */
	compressionConfig?: { triggerTokens?: number; targetTokens?: number } | null | false;
	/** Phase 0.5 seam (design §2.2): session-wide media token cost for
	 *  realtime-input images (LOW = 64 tokens/frame). Session-wide means it
	 *  reaches one-shot send_vision_frame too — realtime input has no
	 *  per-send override. ABSENT = built-in default; `null`/`false` =
	 *  deliberately disabled (same off-switch rule as compressionConfig). */
	mediaResolution?:
		| 'MEDIA_RESOLUTION_LOW'
		| 'MEDIA_RESOLUTION_MEDIUM'
		| 'MEDIA_RESOLUTION_HIGH'
		| null
		| false;
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
	/** The RESOLVED value only ever carries a real enum member — a disabled or
	 *  absent seam is real key absence here, never null. */
	mediaResolution?: 'MEDIA_RESOLUTION_LOW' | 'MEDIA_RESOLUTION_MEDIUM' | 'MEDIA_RESOLUTION_HIGH';
}

/** The explicit off-switch: `null` or `false` means the operator disabled the
 *  seam on purpose. Distinct from ABSENT, which defers to the built-in
 *  default — a distinction that only starts to matter when a value graduates
 *  into VOICE_CONFIG_DEFAULTS, and which has to exist BEFORE it does or the
 *  only user-side rollback is downgrading the app (design Phase 3, step 3e). */
function isDisabled(v: unknown): v is null | false {
	return v === null || v === false;
}

const MEDIA_RESOLUTIONS = [
	'MEDIA_RESOLUTION_LOW',
	'MEDIA_RESOLUTION_MEDIUM',
	'MEDIA_RESOLUTION_HIGH',
] as const;

/** A compression threshold must be a safe positive integer — the Live API
 *  models these as int64, and a float or 0 is operator error, not tuning.
 *  FILE values must already BE numbers: a JSON string "3000" is a schema
 *  violation and coercing it would normalize operator error (codex P2). */
function parseFileThreshold(name: string, value: unknown): number {
	if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
		throw new Error(
			`[voice-config] ${name} must be a positive integer, got ${JSON.stringify(value)}`,
		);
	}
	return value;
}

/** Env values are inherently strings — accept EXACTLY digits (no floats,
 *  exponents, hex, sign, or padding), then the same safe-integer rule. */
function parseEnvThreshold(name: string, value: unknown): number {
	if (typeof value !== 'string' || !/^\d+$/.test(value)) {
		throw new Error(
			`[voice-config] ${name} must be a positive integer, got ${JSON.stringify(value)}`,
		);
	}
	const n = Number(value);
	if (!Number.isSafeInteger(n) || n <= 0) {
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
	parse: (name: string, value: unknown) => number,
): { triggerTokens: number; targetTokens: number } {
	if (trigger === undefined || target === undefined) {
		throw new Error(
			`[voice-config] ${source}: set BOTH triggerTokens and targetTokens or neither ` +
				`(omit both for the server's defaults)`,
		);
	}
	const triggerTokens = parse(`${source} triggerTokens`, trigger);
	const targetTokens = parse(`${source} targetTokens`, target);
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

	// `null`/`false` = deliberately disabled: the key stays ABSENT from the
	// session config, exactly as if unset, but a future built-in default
	// cannot override the user's choice (design step 3e).
	if (config.mediaResolution !== undefined && !isDisabled(config.mediaResolution)) {
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
			parseEnvThreshold,
		);
	} else if (config.compressionConfig !== undefined && !isDisabled(config.compressionConfig)) {
		const cc = config.compressionConfig;
		if (typeof cc !== 'object' || Array.isArray(cc)) {
			throw new Error(
				`[voice-config] compressionConfig must be an object ({} enables server defaults, ` +
					`null/false disables), got ${JSON.stringify(cc)}`,
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
				parseFileThreshold,
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

/** The model every install was seeded with before 3.8, and what it moves to. */
export const LEGACY_SEEDED_MODEL = 'gemini-3.1-flash-live-preview';
export const MIGRATED_MODEL = 'gemini-3.8-live';
/** Written once the move is made, so it is made once: a user who switches back to 3.1 keeps it. */
export const MODEL_MIGRATION_KEY = 'modelMigration';

export interface ModelMigration {
	migrated: boolean;
	backup?: string;
	reason: string;
}

/**
 * Move a config still on the old seeded 3.1 model to 3.8, once.
 *
 * The config is per-user data that an app update never rewrites, and the template is copied only
 * when the file is missing, so a new default reaches new installs only. This is the one place an
 * existing install moves. Only `model` changes; every other key (search, tuning, comments) is kept,
 * the original is copied beside it first, and a stamp records the move so it never repeats. The
 * file cannot say whether 3.1 was seeded or chosen, so a user who chose it is moved once and can
 * switch back; the stamp keeps that choice. No voice preset yields 3.1 + search, so that config
 * comes back only from the `.bak-3.1` copy or a hand edit.
 */
export function migrateLegacyModel(configPath: string, now: Date = new Date()): ModelMigration {
	if (!existsSync(configPath)) return { migrated: false, reason: 'no config file' };
	let raw: Record<string, unknown>;
	try {
		raw = JSON.parse(readFileSync(configPath, 'utf-8'));
	} catch {
		return { migrated: false, reason: 'config unreadable; left for loadVoiceConfig to report' };
	}
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { migrated: false, reason: 'config is not an object' };
	if (raw[MODEL_MIGRATION_KEY] !== undefined) return { migrated: false, reason: 'already migrated once' };
	if (raw.model !== LEGACY_SEEDED_MODEL) return { migrated: false, reason: `model is ${String(raw.model)}, not the old default` };
	const backup = `${configPath}.bak-3.1`;
	if (!existsSync(backup)) copyFileSync(configPath, backup);
	const next = { ...raw, model: MIGRATED_MODEL, [MODEL_MIGRATION_KEY]: `${LEGACY_SEEDED_MODEL} -> ${MIGRATED_MODEL} on ${now.toISOString().slice(0, 10)}` };
	const tmp = `${configPath}.tmp`;
	writeFileSync(tmp, JSON.stringify(next, null, 2) + '\n');
	renameSync(tmp, configPath);
	return { migrated: true, backup, reason: 'moved from the old seeded default' };
}

/** Written by the voice switch tool: a model the user picked is never reverted. */
export const MODEL_CHOSEN_KEY = 'modelChosenBySwitch';
/** Written when a migrated install is put back on 3.1, so the revert also happens at most once. */
export const MODEL_REVERT_KEY = 'modelMigrationReverted';

export interface ModelRevert {
	reverted: boolean;
	model?: string;
	reason: string;
}

/**
 * Put a config the migration moved to 3.8 back on its old model, once, when 3.8 is unavailable.
 *
 * Only a config carrying the migration stamp and still on the migrated model is touched, and not
 * one whose model the voice switch tool wrote, so a user who chose 3.8 themselves is not moved. The model comes from the `.bak-3.1` copy when it
 * names one; every other current key is kept. The migration stamp stays, so it never re-runs.
 */
export function revertModelMigration(configPath: string, now: Date = new Date()): ModelRevert {
	if (!existsSync(configPath)) return { reverted: false, reason: 'no config file' };
	let raw: Record<string, unknown>;
	try {
		raw = JSON.parse(readFileSync(configPath, 'utf-8'));
	} catch {
		return { reverted: false, reason: 'config unreadable' };
	}
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { reverted: false, reason: 'config is not an object' };
	if (raw[MODEL_MIGRATION_KEY] === undefined) return { reverted: false, reason: 'not moved by the migration' };
	if (raw[MODEL_REVERT_KEY] !== undefined) return { reverted: false, reason: 'already reverted once' };
	if (raw.model !== MIGRATED_MODEL) return { reverted: false, reason: `model is ${String(raw.model)}, not the migrated one` };
	if (raw[MODEL_CHOSEN_KEY] === raw.model) return { reverted: false, reason: 'model was chosen with the voice switch' };
	let model = LEGACY_SEEDED_MODEL;
	try {
		const backupModel = JSON.parse(readFileSync(`${configPath}.bak-3.1`, 'utf-8'))?.model;
		if (typeof backupModel === 'string' && backupModel && backupModel !== MIGRATED_MODEL) model = backupModel;
	} catch { /* no usable backup: the migration only ever moved the legacy model */ }
	const next = { ...raw, model, [MODEL_REVERT_KEY]: `${MIGRATED_MODEL} -> ${model} on ${now.toISOString().slice(0, 10)} (model unavailable)` };
	const tmp = `${configPath}.tmp`;
	writeFileSync(tmp, JSON.stringify(next, null, 2) + '\n');
	renameSync(tmp, configPath);
	return { reverted: true, model, reason: 'migrated model unavailable' };
}
