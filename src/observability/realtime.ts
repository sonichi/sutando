/**
 * Realtime usage CLIENT — the voice agent (`src/voice-agent.ts`) and phone
 * server (`skills/phone-conversation`) SEND raw usage payloads to the collector
 * (`POST /ingest/realtime`); the collector's RealtimeNormalizer maps them to
 * spine primitives and writes them through the SAME sink-set + meter ledger as
 * every other source. This file does NOT write the ledger or emit events
 * itself — everything goes through the one collector ingestion point, exactly
 * like the Claude Code hook / OTel sources.
 *
 * Best-effort, fire-and-forget: with no `SUTANDO_OBS_ENDPOINT` (capture off) or a
 * down collector, the POST is skipped/dropped — it never blocks or throws on the
 * realtime hot path. Durable storage is the collector's responsibility (a
 * store-and-forward buffer for offline gaps is the post-parity shipper, not here).
 *
 * Two ways to send:
 *   - startVoiceTicker / startPhoneTicker — emit a payload every USAGE_TICK_MS
 *     WHILE the session/call is live (incremental, bucket-keyed), flushing the
 *     final partial bucket on stop().
 *   - sendVoiceUsage / sendPhoneUsage — one-shot for a full session/call.
 */

import { startTicker, USAGE_TICK_MS } from './ticker.js';
import type { RawRealtimeUsage } from './realtime-map.js';

export { USAGE_TICK_MS } from './ticker.js';

/** Collector ingest base, trailing slash trimmed; undefined when capture is off. */
function endpoint(): string | undefined {
	const e = process.env.SUTANDO_OBS_ENDPOINT?.trim();
	return e ? e.replace(/\/+$/, '') : undefined;
}

/** Fire-and-forget POST of a raw payload to the collector. Never throws; a
 *  missing endpoint or a down collector silently drops (capture is off / best
 *  effort, same contract as the CC hook). */
export function sendRealtimeUsage(payload: RawRealtimeUsage): void {
	const base = endpoint();
	if (!base) return;
	try {
		void fetch(`${base}/ingest/realtime`, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(payload),
		}).catch(() => {});
	} catch {
		/* never throw on the realtime hot path */
	}
}

export interface VoiceUsage {
	sessionId: string;
	durationMs: number;
	model: string;
	provider?: string;
	toolCalls?: number;
}

export interface PhoneUsage {
	callSid: string;
	durationMs: number;
	model: string;
	isOwner?: boolean;
	isMeeting?: boolean;
	modelProvider?: string;
	toolCalls?: number;
}

/** One-shot: send a complete voice session's duration. */
export function sendVoiceUsage(u: VoiceUsage): void {
	sendRealtimeUsage({ kind: 'voice.session', ...u });
}

/** One-shot: send a complete phone call's duration (both legs derived collector-side). */
export function sendPhoneUsage(u: PhoneUsage): void {
	sendRealtimeUsage({ kind: 'phone.call', ...u });
}

/** Handle returned by the tickers — stop() flushes the final partial bucket and
 *  cancels the interval (idempotent). */
export interface TickerControl {
	stop: () => void;
}

/**
 * Start a periodic usage ticker for a realtime voice session. POSTs a
 * `voice.session` payload (with the elapsed bucket) every interval while live.
 * `_nowFn` is injectable for deterministic tests.
 */
export function startVoiceTicker(
	opts: { sessionId: string; model: string; provider?: string; toolCallsGetter?: () => number },
	intervalMs = USAGE_TICK_MS,
	_nowFn: () => number = Date.now,
): TickerControl {
	const t = startTicker<void>(
		(durationMs, bucketStartMs) =>
			sendRealtimeUsage({
				kind: 'voice.session',
				sessionId: opts.sessionId,
				model: opts.model,
				provider: opts.provider,
				toolCalls: opts.toolCallsGetter?.(),
				durationMs,
				bucketStartMs,
			}),
		intervalMs,
		_nowFn,
	);
	return { stop: () => void t.stop() };
}

/**
 * Start a periodic usage ticker for a phone call. POSTs a `phone.call` payload
 * (both legs derived collector-side) every interval while live.
 */
export function startPhoneTicker(
	opts: { callSid: string; model: string; isOwner?: boolean; isMeeting?: boolean; modelProvider?: string; toolCallsGetter?: () => number },
	intervalMs = USAGE_TICK_MS,
	_nowFn: () => number = Date.now,
): TickerControl {
	const t = startTicker<void>(
		(durationMs, bucketStartMs) =>
			sendRealtimeUsage({
				kind: 'phone.call',
				callSid: opts.callSid,
				model: opts.model,
				isOwner: opts.isOwner,
				isMeeting: opts.isMeeting,
				modelProvider: opts.modelProvider,
				toolCalls: opts.toolCallsGetter?.(),
				durationMs,
				bucketStartMs,
			}),
		intervalMs,
		_nowFn,
	);
	return { stop: () => void t.stop() };
}
