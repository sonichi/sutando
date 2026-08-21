/**
 * ACTIVE-silence recovery policy (#2963 family, fourth guard) — the pure event
 * reducer from docs/design-voice-active-silence-recovery.md (desktop repo).
 * Own module because voice-agent.ts runs main() at import time. Settles the
 * design's open residue items 5 (retry timers: `schedule-retry` effects,
 * retryDue re-validated at delivery) and 6 (fatal backoff: cleared explicitly;
 * human retry parks during backoff rather than overriding it).
 */

export const DEFAULT_ACTIVE_SILENCE_TICKS = 3; // >=75s continuous silence
export const MIN_ACTIVE_SILENCE_TICKS = 2; // floor >=45s
export const MAX_ACTIVE_SILENCE_TICKS = 40; // cap ~20min
export const QUIESCENCE_MS = 2_000;
export const ATTEMPT_COOLDOWN_MS = 60_000;
export const EPISODE_ATTEMPT_LIMIT = 3;
export const SILENCE_TICK_MIN_MS = 15_000;

/** Matrix facts consumed by the trigger (see voice-health-matrix.ts). */
export interface MatrixFactsLike {
	factsAvailable: boolean;
	speechInWindow: boolean;
	speechObservedAt: number | null;
	ingressAdvanced: boolean;
	modelSilentFor15s: boolean;
}

export type RecoveryPhase = 'idle' | 'restarting' | 'waiting-retry' | 'terminal';
export type RecoveryEffect =
	| 'none'
	| 'restart'
	| 'notify-stalled'
	| 'record-only'
	| 'schedule-retry';

// All `at`/`until` values share one process-local monotonic clock domain.
export interface RecoveryState {
	phase: RecoveryPhase;
	origin: 'active-silence' | null;
	currentTransportEpoch: number | null;
	currentClientEpoch: number | null; // retained across detach as a stale-event fence
	clientAttached: boolean;
	streak: number;
	speechLatched: boolean;
	firstSpeechAt: number | null;
	lastAboveFloorAt: number | null;
	silenceAnchorAt: number | null;
	meetingMode: boolean;
	episodeAttempts: number; // consumed at restart AUTHORIZATION
	lastActionAt: number | null;
	retryNotBefore: number | null;
	attemptEpoch: number;
	backoffUntil: number;
}

export type RecoveryEvent =
	| { kind: 'tick'; at: number; state: string; facts: MatrixFactsLike; pendingToolCount: number; requiredTicks?: number }
	| { kind: 'speechObserved'; at: number }
	| { kind: 'meetingModeChanged'; active: boolean; at: number }
	| { kind: 'clientAttached'; clientEpoch: number; at: number }
	| { kind: 'clientDetached'; clientEpoch: number; at: number }
	| { kind: 'transportActive'; transportEpoch: number; attemptEpoch: number | null; at: number }
	| { kind: 'closedObserved'; transportEpoch: number; attemptEpoch: number | null; at: number }
	| { kind: 'dialFailed'; attemptEpoch: number; at: number }
	| { kind: 'retryDue'; attemptEpoch: number; at: number }
	| { kind: 'shadowRestarted'; attemptEpoch: number; at: number }
	| { kind: 'modelEvent'; at: number; transportEpoch: number }
	| { kind: 'toolOutcome'; at: number; transportEpoch: number }
	| {
			kind: 'userVisibleResponse';
			at: number;
			transportEpoch: number;
			clientEpoch: number;
			channel: 'audio-egress' | 'typed-tool-route';
	  }
	| { kind: 'fatalBackoff'; until: number }
	| { kind: 'fatalBackoffCleared'; at: number }
	| { kind: 'retry'; stalledAttemptEpoch: number; clientEpoch: number; requestId: string; at: number };

export function initialRecoveryState(): RecoveryState {
	return {
		phase: 'idle',
		origin: null,
		currentTransportEpoch: null,
		currentClientEpoch: null,
		clientAttached: false,
		streak: 0,
		speechLatched: false,
		firstSpeechAt: null,
		lastAboveFloorAt: null,
		silenceAnchorAt: null,
		meetingMode: false,
		episodeAttempts: 0,
		lastActionAt: null,
		retryNotBefore: null,
		attemptEpoch: 0,
		backoffUntil: 0,
	};
}

function maxOf(a: number | null, b: number): number {
	return a === null ? b : Math.max(a, b);
}

/** "Full reset" per the design: keep identities + meeting mode, clear the episode. */
function fullReset(s: RecoveryState): RecoveryState {
	return {
		...s,
		phase: 'idle',
		origin: null,
		streak: 0,
		speechLatched: false,
		firstSpeechAt: null,
		lastAboveFloorAt: null,
		silenceAnchorAt: null,
		episodeAttempts: 0,
		retryNotBefore: null,
	};
}

function authorize(s: RecoveryState, at: number): { state: RecoveryState; effect: RecoveryEffect } {
	return {
		state: {
			...s,
			phase: 'restarting',
			origin: 'active-silence',
			episodeAttempts: s.episodeAttempts + 1,
			attemptEpoch: s.attemptEpoch + 1,
			lastActionAt: at,
			retryNotBefore: null,
		},
		effect: 'restart',
	};
}

function toWaitingRetry(s: RecoveryState): { state: RecoveryState; effect: RecoveryEffect } {
	const notBefore = Math.max((s.lastActionAt ?? 0) + ATTEMPT_COOLDOWN_MS, s.backoffUntil);
	return { state: { ...s, phase: 'waiting-retry', retryNotBefore: notBefore }, effect: 'schedule-retry' };
}

export function reduceRecovery(
	s: RecoveryState,
	ev: RecoveryEvent,
): { state: RecoveryState; effect: RecoveryEffect } {
	const none = (state: RecoveryState) => ({ state, effect: 'none' as RecoveryEffect });
	const recordOnly = { state: s, effect: 'record-only' as RecoveryEffect };

	switch (ev.kind) {
		case 'fatalBackoff': {
			const backoffUntil = Math.max(s.backoffUntil, ev.until);
			if (s.phase === 'waiting-retry') {
				const retryNotBefore = Math.max(s.retryNotBefore ?? 0, backoffUntil);
				return { state: { ...s, backoffUntil, retryNotBefore }, effect: 'schedule-retry' };
			}
			return none({ ...s, backoffUntil });
		}
		case 'fatalBackoffCleared': {
			if (s.phase === 'waiting-retry') {
				const retryNotBefore = (s.lastActionAt ?? 0) + ATTEMPT_COOLDOWN_MS;
				return { state: { ...s, backoffUntil: 0, retryNotBefore }, effect: 'schedule-retry' };
			}
			return none({ ...s, backoffUntil: 0 });
		}
		case 'meetingModeChanged': {
			if (ev.active) {
				return none({
					...s,
					meetingMode: true,
					speechLatched: false,
					firstSpeechAt: null,
					lastAboveFloorAt: null,
					streak: 0,
				});
			}
			return none({ ...s, meetingMode: false });
		}
		case 'speechObserved': {
			if (s.meetingMode) return none(s);
			if (!s.speechLatched) {
				return none({
					...s,
					speechLatched: true,
					firstSpeechAt: ev.at,
					lastAboveFloorAt: ev.at,
					silenceAnchorAt: maxOf(s.silenceAnchorAt, ev.at),
				});
			}
			return none({ ...s, lastAboveFloorAt: ev.at });
		}
		case 'clientAttached': {
			if (s.currentClientEpoch !== null && ev.clientEpoch <= s.currentClientEpoch) return recordOnly;
			const attached = { ...s, currentClientEpoch: ev.clientEpoch, clientAttached: true };
			if (s.phase === 'terminal') return { state: attached, effect: 'notify-stalled' };
			if (
				s.phase === 'waiting-retry' &&
				s.retryNotBefore !== null &&
				ev.at >= s.retryNotBefore &&
				ev.at >= s.backoffUntil
			) {
				return { state: attached, effect: 'schedule-retry' };
			}
			return none(attached);
		}
		case 'clientDetached': {
			if (ev.clientEpoch !== s.currentClientEpoch) return recordOnly;
			if (s.phase === 'terminal') return none({ ...s, clientAttached: false });
			return none({
				...s,
				clientAttached: false,
				speechLatched: false,
				firstSpeechAt: null,
				lastAboveFloorAt: null,
				streak: 0,
			});
		}
		case 'modelEvent':
		case 'toolOutcome': {
			if (ev.transportEpoch !== s.currentTransportEpoch) return recordOnly;
			return none({ ...s, silenceAnchorAt: maxOf(s.silenceAnchorAt, ev.at), streak: 0 });
		}
		case 'userVisibleResponse': {
			if (ev.transportEpoch !== s.currentTransportEpoch) return recordOnly;
			if (!s.clientAttached || ev.clientEpoch !== s.currentClientEpoch) return recordOnly;
			return none(fullReset(s));
		}
		case 'transportActive': {
			const newer = s.currentTransportEpoch === null || ev.transportEpoch > s.currentTransportEpoch;
			if (ev.attemptEpoch !== null) {
				// Watchdog-correlated activation: must be the in-flight attempt AND
				// a strictly newer transport generation.
				if (ev.attemptEpoch !== s.attemptEpoch || s.phase !== 'restarting' || !newer) return recordOnly;
				return none({
					...s,
					currentTransportEpoch: ev.transportEpoch,
					phase: 'idle',
					streak: 0,
					silenceAnchorAt: ev.at,
					retryNotBefore: null,
				});
			}
			// Ordinary/initial activation: strictly newer, and never mid-restart
			// (a null-attempt activation cannot end a watchdog attempt).
			if (!newer || s.phase === 'restarting') return recordOnly;
			if (s.phase === 'terminal') {
				return none({
					...s,
					currentTransportEpoch: ev.transportEpoch,
					silenceAnchorAt: maxOf(s.silenceAnchorAt, ev.at),
				});
			}
			return none({
				...s,
				currentTransportEpoch: ev.transportEpoch,
				phase: 'idle',
				streak: 0,
				silenceAnchorAt: ev.at,
				retryNotBefore: null,
			});
		}
		case 'closedObserved': {
			if (ev.transportEpoch !== s.currentTransportEpoch) return recordOnly;
			if (s.phase === 'restarting' && ev.attemptEpoch === s.attemptEpoch) return none(s);
			if (s.phase === 'waiting-retry' || s.phase === 'terminal') return none(s);
			if (s.phase === 'idle' && s.origin === 'active-silence') {
				if (s.episodeAttempts >= EPISODE_ATTEMPT_LIMIT) {
					return { state: { ...s, phase: 'terminal' }, effect: 'notify-stalled' };
				}
				return toWaitingRetry(s);
			}
			return none(s); // ordinary-CLOSED sessions belong to the existing guard
		}
		case 'dialFailed': {
			if (ev.attemptEpoch !== s.attemptEpoch || s.phase !== 'restarting') return recordOnly;
			if (s.episodeAttempts >= EPISODE_ATTEMPT_LIMIT) {
				return { state: { ...s, phase: 'terminal' }, effect: 'notify-stalled' };
			}
			return toWaitingRetry(s);
		}
		case 'retryDue': {
			if (ev.attemptEpoch !== s.attemptEpoch || s.phase !== 'waiting-retry') return recordOnly;
			if (
				!s.clientAttached ||
				(s.retryNotBefore !== null && ev.at < s.retryNotBefore) ||
				ev.at < s.backoffUntil
			) {
				return none(s);
			}
			if (s.episodeAttempts >= EPISODE_ATTEMPT_LIMIT) {
				return { state: { ...s, phase: 'terminal' }, effect: 'notify-stalled' };
			}
			return authorize(s, ev.at);
		}
		case 'shadowRestarted': {
			if (ev.attemptEpoch !== s.attemptEpoch || s.phase !== 'restarting') return recordOnly;
			return none({ ...s, phase: 'idle', streak: 0, silenceAnchorAt: ev.at, retryNotBefore: null });
		}
		case 'retry': {
			if (s.phase !== 'terminal') return recordOnly;
			if (ev.stalledAttemptEpoch !== s.attemptEpoch) return recordOnly;
			if (!s.clientAttached || ev.clientEpoch !== s.currentClientEpoch) return recordOnly;
			const fresh: RecoveryState = { ...s, episodeAttempts: 0, origin: 'active-silence' };
			if (ev.at < s.backoffUntil) {
				// Residue-6 decision: human retry never overrides fatal backoff.
				const parked = {
					...fresh,
					phase: 'waiting-retry' as RecoveryPhase,
					retryNotBefore: s.backoffUntil,
					episodeAttempts: 0,
				};
				return { state: parked, effect: 'schedule-retry' };
			}
			return authorize(fresh, ev.at);
		}
		case 'tick': {
			if (s.phase !== 'idle') return none(s);
			const f = ev.facts;
			const qualifies =
				ev.state === 'ACTIVE' &&
				s.clientAttached &&
				!s.meetingMode &&
				s.speechLatched &&
				f.factsAvailable &&
				f.ingressAdvanced &&
				f.modelSilentFor15s &&
				s.silenceAnchorAt !== null &&
				ev.at - s.silenceAnchorAt > SILENCE_TICK_MIN_MS &&
				ev.pendingToolCount === 0;
			if (!qualifies) return none({ ...s, streak: 0 });

			const required = ev.requiredTicks ?? DEFAULT_ACTIVE_SILENCE_TICKS;
			const streak = Math.min(s.streak + 1, required);
			if (streak < required) return none({ ...s, streak });

			// Threshold reached: authorization vetoes cap the streak, never reset it.
			const quiescent = s.lastAboveFloorAt !== null && ev.at - s.lastAboveFloorAt >= QUIESCENCE_MS;
			const cooldownOk = s.lastActionAt === null || ev.at - s.lastActionAt > ATTEMPT_COOLDOWN_MS;
			const backoffOk = ev.at >= s.backoffUntil;
			if (!quiescent || !cooldownOk || !backoffOk) return none({ ...s, streak: required });
			if (s.episodeAttempts >= EPISODE_ATTEMPT_LIMIT) {
				return { state: { ...s, streak: required, phase: 'terminal' }, effect: 'notify-stalled' };
			}
			return authorize({ ...s, streak: required }, ev.at);
		}
	}
}

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

/** VOICE_ACTIVE_SILENCE_MODE: off|shadow|armed; default shadow; invalid warns. */
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
