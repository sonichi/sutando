/**
 * Env settings for bodhi's upstream recovery: whether active-silence recovery is armed and after
 * how many health ticks, and when a stuck dial is replaced. Its own module because voice-agent.ts
 * runs main() at import time.
 */

export const DEFAULT_ACTIVE_SILENCE_TICKS = 3; // >=75s continuous silence
export const MIN_ACTIVE_SILENCE_TICKS = 2;
export const MAX_ACTIVE_SILENCE_TICKS = 40;

/** VOICE_ACTIVE_SILENCE_TICKS: non-negative safe integer; 0 disables; clamps to
 *  [MIN, MAX]; anything else (incl. empty/whitespace) warns and defaults. */
export function parseActiveSilenceTicks(
	raw: string | undefined,
	warn: (m: string) => void = console.warn,
): number {
	if (raw === undefined) return DEFAULT_ACTIVE_SILENCE_TICKS;
	const trimmed = raw.trim();
	if (trimmed === '') {
		warn(`[voice] VOICE_ACTIVE_SILENCE_TICKS=${JSON.stringify(raw)} is empty; using ${DEFAULT_ACTIVE_SILENCE_TICKS}`);
		return DEFAULT_ACTIVE_SILENCE_TICKS;
	}
	const n = Number(trimmed);
	if (!Number.isSafeInteger(n) || n < 0) {
		warn(`[voice] VOICE_ACTIVE_SILENCE_TICKS=${JSON.stringify(raw)} is not a non-negative integer; using ${DEFAULT_ACTIVE_SILENCE_TICKS}`);
		return DEFAULT_ACTIVE_SILENCE_TICKS;
	}
	if (n === 0) return 0;
	if (n < MIN_ACTIVE_SILENCE_TICKS) {
		warn(`[voice] VOICE_ACTIVE_SILENCE_TICKS=${n} is below the ${MIN_ACTIVE_SILENCE_TICKS}-tick floor; clamping`);
		return MIN_ACTIVE_SILENCE_TICKS;
	}
	if (n > MAX_ACTIVE_SILENCE_TICKS) {
		warn(`[voice] VOICE_ACTIVE_SILENCE_TICKS=${n} exceeds the ${MAX_ACTIVE_SILENCE_TICKS}-tick cap; clamping`);
		return MAX_ACTIVE_SILENCE_TICKS;
	}
	return n;
}

export type ActiveSilenceMode = 'off' | 'shadow' | 'armed';

/** VOICE_ACTIVE_SILENCE_MODE: off|shadow|armed; default shadow; invalid warns. Only `armed` acts. */
export function parseActiveSilenceMode(
	raw: string | undefined,
	warn: (m: string) => void = console.warn,
): ActiveSilenceMode {
	if (raw === undefined || raw.trim() === '') return 'shadow';
	const v = raw.trim().toLowerCase();
	if (v === 'off' || v === 'shadow' || v === 'armed') return v;
	warn(`[voice] VOICE_ACTIVE_SILENCE_MODE=${JSON.stringify(raw)} is not off|shadow|armed; using shadow`);
	return 'shadow';
}

/** The active-silence ticks bodhi gets: the parsed count when armed, 0 (off) otherwise. Armed with
 *  0 ticks stays off, with a warning, as before. */
export function activeSilenceTicksFromEnv(
	env: { VOICE_ACTIVE_SILENCE_MODE?: string; VOICE_ACTIVE_SILENCE_TICKS?: string },
	warn: (m: string) => void = console.warn,
): number {
	if (parseActiveSilenceMode(env.VOICE_ACTIVE_SILENCE_MODE, warn) !== 'armed') return 0;
	const ticks = parseActiveSilenceTicks(env.VOICE_ACTIVE_SILENCE_TICKS, warn);
	if (ticks === 0) warn('[voice] VOICE_ACTIVE_SILENCE_MODE=armed but VOICE_ACTIVE_SILENCE_TICKS=0 disables it; staying off');
	return ticks;
}

export const DEFAULT_STUCK_CONNECTING_MS = 120_000;
/** A positive override below twice the dial deadline (30-45 s) would kill dials that are still on time. */
export const MIN_STUCK_CONNECTING_MS = 60_000;

/** VOICE_STUCK_CONNECTING_MS: 0 disables; a positive value below the floor clamps; invalid warns and defaults. */
export function parseStuckConnectingMs(
	raw: string | undefined,
	warn: (m: string) => void = console.warn,
): number {
	if (raw === undefined || raw.trim() === '') return DEFAULT_STUCK_CONNECTING_MS;
	const n = Number(raw);
	if (!Number.isFinite(n) || n < 0) {
		warn(`[voice] VOICE_STUCK_CONNECTING_MS=${JSON.stringify(raw)} is not a non-negative number; `
			+ `using ${DEFAULT_STUCK_CONNECTING_MS}ms`);
		return DEFAULT_STUCK_CONNECTING_MS;
	}
	if (n > 0 && n < MIN_STUCK_CONNECTING_MS) {
		warn(`[voice] VOICE_STUCK_CONNECTING_MS=${JSON.stringify(raw)} is below the safe floor `
			+ `(upstream dial deadline is 30-45s); clamping to ${MIN_STUCK_CONNECTING_MS}ms`);
		return MIN_STUCK_CONNECTING_MS;
	}
	return n;
}
