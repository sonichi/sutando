/**
 * Realtime surface usage metering — voice agent (Gemini Live) and phone calls
 * (Twilio + Gemini Live). Thin domain helpers over `meter.record()` and the
 * generic `startTicker` from `./ticker.ts`.
 *
 * Two billing axes for a phone call:
 *   phone.seconds  — the Twilio telephony leg (with advisory cost)
 *   voice.seconds  — the in-call Gemini Live model leg
 * Both use `provider_ref: callSid` so they're joinable; the compound key
 * (meter, source) keeps them separable for billing.
 *
 * bucket usage_id format:
 *   <meter>:<id>:b<bucketStartEpochSec>  (incremental tickers)
 *   <meter>:<id>                          (one-shot helpers)
 *
 * `cost_usd` is ADVISORY only — populated solely from the documented public
 * list-price table below, and only for the Twilio telephony leg where a per-
 * time rate exists. Override `REALTIME_RATES` per deployment.
 */

import { record } from './meter.js';
import { startTicker, USAGE_TICK_MS } from './ticker.js';
import type { UsageRecord, UsageAttrs } from './usage.js';
import type { Actor, AccessTier } from './events.js';

export { USAGE_TICK_MS } from './ticker.js';

// ---------------------------------------------------------------------------
// Rates
// ---------------------------------------------------------------------------

export interface RealtimeRate {
	usdPerSecond?: number;
	usdPerMinute?: number;
}

/** Advisory list prices (NOT the billed figure). Update per deployment / contract.
 *  Source: public pay-as-you-go list price, 2026-06. */
export const REALTIME_RATES: Record<string, RealtimeRate> = {
	twilio: { usdPerMinute: 0.0085 }, // US local inbound voice
};

/** Advisory USD for `seconds` at `provider`'s list rate, or undefined if unknown. */
export function advisoryCostUsd(provider: string, seconds: number): number | undefined {
	const r = REALTIME_RATES[provider];
	if (!r) return undefined;
	const perSec = r.usdPerSecond ?? (r.usdPerMinute != null ? r.usdPerMinute / 60 : undefined);
	if (perSec === undefined) return undefined;
	return Math.round(perSec * seconds * 1e6) / 1e6;
}

// ---------------------------------------------------------------------------
// Shared utilities
// ---------------------------------------------------------------------------

/** ms → whole seconds, floored at 0. */
export function durationSeconds(ms: number): number {
	return Math.max(0, Math.round(ms / 1000));
}

/** Strip undefined-valued keys so the in-memory record matches its JSONL line. */
function compactAttrs(a: UsageAttrs): UsageAttrs {
	const out: UsageAttrs = {};
	for (const [k, v] of Object.entries(a)) if (v !== undefined) out[k] = v;
	return out;
}

function phoneActor(isOwner?: boolean): Actor {
	return {
		user_id: isOwner ? 'owner' : 'caller',
		channel: 'phone',
		access_tier: (isOwner ? 'owner' : 'public') as AccessTier,
		tenant_id: null,
	};
}

const VOICE_ACTOR: Actor = { user_id: 'owner', channel: 'voice', access_tier: 'owner', tenant_id: null };

// ---------------------------------------------------------------------------
// One-shot helpers (full session / call duration at end)
// ---------------------------------------------------------------------------

export interface VoiceSessionUsage {
	sessionId: string;
	durationMs: number;
	model: string;
	provider?: string; // default 'gemini-live'
	toolCalls?: number;
}

/**
 * Record one complete voice-agent session as `voice.seconds`. Returns the
 * stamped record, or null for a zero-length session. The `usage_id` is keyed
 * on session id so a double-flush deduplicates downstream.
 */
export function recordVoiceSession(u: VoiceSessionUsage): UsageRecord | null {
	const seconds = durationSeconds(u.durationMs);
	if (seconds <= 0) return null;
	const provider = u.provider ?? 'gemini-live';
	return record({
		source: 'voice-agent', meter: 'voice.seconds', quantity: seconds, unit: 'seconds',
		provider, provider_ref: u.sessionId,
		usage_id: `voice.seconds:${u.sessionId}`,
		actor: VOICE_ACTOR,
		attrs: compactAttrs({ model: u.model, tool_calls: u.toolCalls, cost_usd: advisoryCostUsd(provider, seconds) }),
	});
}

export interface PhoneCallUsage {
	callSid: string;
	durationMs: number;
	model: string;
	isOwner?: boolean;
	isMeeting?: boolean;
	toolCalls?: number;
	modelProvider?: string; // default 'gemini-live'
}

/**
 * Record one phone call on both axes: Twilio telephony (`phone.seconds`) and
 * the in-call Gemini Live leg (`voice.seconds`), both keyed by Call SID.
 * Returns the two records written (empty for a zero-length call).
 */
export function recordPhoneCall(u: PhoneCallUsage): UsageRecord[] {
	const seconds = durationSeconds(u.durationMs);
	if (seconds <= 0) return [];
	const actor = phoneActor(u.isOwner);
	const modelProvider = u.modelProvider ?? 'gemini-live';
	const telephony = record({
		source: 'phone', meter: 'phone.seconds', quantity: seconds, unit: 'seconds',
		provider: 'twilio', provider_ref: u.callSid,
		usage_id: `phone.seconds:${u.callSid}`,
		actor,
		attrs: compactAttrs({ is_meeting: u.isMeeting, is_owner: u.isOwner, tool_calls: u.toolCalls, cost_usd: advisoryCostUsd('twilio', seconds) }),
	});
	const model = record({
		source: 'phone', meter: 'voice.seconds', quantity: seconds, unit: 'seconds',
		provider: modelProvider, provider_ref: u.callSid,
		usage_id: `voice.seconds:${u.callSid}`,
		actor,
		attrs: compactAttrs({ model: u.model, is_meeting: u.isMeeting, is_owner: u.isOwner, tool_calls: u.toolCalls, cost_usd: advisoryCostUsd(modelProvider, seconds) }),
	});
	return [telephony, model];
}

// ---------------------------------------------------------------------------
// Incremental tickers (emit while live)
// ---------------------------------------------------------------------------

function recordVoiceIncrement(opts: {
	sessionId: string; model: string; provider?: string; toolCalls?: number;
}, durationMs: number, bucketStartMs: number): UsageRecord | null {
	const seconds = durationSeconds(durationMs);
	if (seconds <= 0) return null;
	const provider = opts.provider ?? 'gemini-live';
	const bucketSec = Math.floor(bucketStartMs / 1000);
	return record({
		source: 'voice-agent', meter: 'voice.seconds', quantity: seconds, unit: 'seconds',
		provider, provider_ref: opts.sessionId,
		usage_id: `voice.seconds:${opts.sessionId}:b${bucketSec}`,
		actor: VOICE_ACTOR,
		attrs: compactAttrs({ model: opts.model, tool_calls: opts.toolCalls, cost_usd: advisoryCostUsd(provider, seconds) }),
	});
}

function recordPhoneIncrement(opts: {
	callSid: string; model: string; isOwner?: boolean; isMeeting?: boolean;
	modelProvider?: string; toolCalls?: number;
}, durationMs: number, bucketStartMs: number): UsageRecord[] {
	const seconds = durationSeconds(durationMs);
	if (seconds <= 0) return [];
	const actor = phoneActor(opts.isOwner);
	const modelProvider = opts.modelProvider ?? 'gemini-live';
	const bucketSec = Math.floor(bucketStartMs / 1000);
	const telephony = record({
		source: 'phone', meter: 'phone.seconds', quantity: seconds, unit: 'seconds',
		provider: 'twilio', provider_ref: opts.callSid,
		usage_id: `phone.seconds:${opts.callSid}:b${bucketSec}`,
		actor,
		attrs: compactAttrs({ is_meeting: opts.isMeeting, is_owner: opts.isOwner, tool_calls: opts.toolCalls, cost_usd: advisoryCostUsd('twilio', seconds) }),
	});
	const model = record({
		source: 'phone', meter: 'voice.seconds', quantity: seconds, unit: 'seconds',
		provider: modelProvider, provider_ref: opts.callSid,
		usage_id: `voice.seconds:${opts.callSid}:b${bucketSec}`,
		actor,
		attrs: compactAttrs({ model: opts.model, is_meeting: opts.isMeeting, is_owner: opts.isOwner, tool_calls: opts.toolCalls, cost_usd: advisoryCostUsd(modelProvider, seconds) }),
	});
	return [telephony, model];
}

export interface VoiceTickerHandle {
	stop: () => UsageRecord | null;
}

export interface PhoneTickerHandle {
	stop: () => UsageRecord[];
}

/**
 * Start a periodic usage ticker for a realtime voice session. Emits one
 * `voice.seconds` record per interval while the session is live. Call
 * `handle.stop()` on session end to flush the final partial bucket.
 */
export function startVoiceTicker(
	opts: { sessionId: string; model: string; provider?: string; toolCallsGetter?: () => number },
	intervalMs = USAGE_TICK_MS,
	_nowFn: () => number = Date.now,
): VoiceTickerHandle {
	const ticker = startTicker<UsageRecord | null>(
		(durationMs, bucketStartMs) => recordVoiceIncrement(
			{ ...opts, toolCalls: opts.toolCallsGetter?.() }, durationMs, bucketStartMs,
		),
		intervalMs, _nowFn,
	);
	return { stop: () => ticker.stop() ?? null };
}

/**
 * Start a periodic usage ticker for a phone call (both Twilio + Gemini Live
 * axes). Emits two records per interval. Call `handle.stop()` on call end.
 */
export function startPhoneTicker(
	opts: { callSid: string; model: string; isOwner?: boolean; isMeeting?: boolean; modelProvider?: string; toolCallsGetter?: () => number },
	intervalMs = USAGE_TICK_MS,
	_nowFn: () => number = Date.now,
): PhoneTickerHandle {
	const ticker = startTicker<UsageRecord[]>(
		(durationMs, bucketStartMs) => recordPhoneIncrement(
			{ ...opts, toolCalls: opts.toolCallsGetter?.() }, durationMs, bucketStartMs,
		),
		intervalMs, _nowFn,
	);
	return { stop: () => ticker.stop() ?? [] };
}
