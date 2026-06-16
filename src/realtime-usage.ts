/**
 * Realtime voice + phone usage metering — a thin domain helper over
 * `meter.record()` for the two realtime surfaces: the Gemini-Live voice agent
 * (`src/voice-agent.ts`) and Twilio phone calls (`skills/phone-conversation`).
 *
 * It builds a spine `UsageRecord` with the right meter / provider / unit names
 * and a stable `usage_id`, then calls `record()`. `record()` (meter.ts:104-131)
 * does TWO things in one call: it appends the durable, billable ledger line AND
 * emits a `usage.recorded` obs event through the SAME sink-set every event uses.
 * So "usage is emitted like events" is automatic — callers just call record().
 *
 * What we meter: SESSION / CALL DURATION in seconds. The realtime transports
 * (bodhi `VoiceSession` / Gemini Live) do not surface token or audio-frame
 * counts today, so seconds is the authoritative available signal (the same
 * constraint the sibling `stando` project hit). Token attrs are intentionally
 * left absent until the transport exposes them.
 *
 * A phone call is metered on BOTH axes it actually consumes — the Twilio
 * telephony leg (`phone.seconds`) and the in-call realtime model leg
 * (`voice.seconds`, gemini-live) — tied together by the Call SID in
 * `provider_ref`. That is two distinct cost axes, not double-counting.
 *
 * `cost_usd` is ADVISORY only (types.ts:19) and is populated solely from the
 * documented list-price table below, and only when a per-time rate is known
 * (telephony). Model legs carry no cost until token counts land. Override
 * `REALTIME_RATES` per deployment — never bill from this figure.
 */

import { record } from './observability/meter.js';
import type { UsageRecord, UsageAttrs } from './observability/usage.js';
import type { Actor, AccessTier } from './observability/events.js';

/** How often the ticker emits a usage record while a session/call is live. */
export const USAGE_TICK_MS = 30_000;

export interface RealtimeRate {
	usdPerSecond?: number;
	usdPerMinute?: number;
}

/** Advisory list prices (NOT the billed figure). Telephony only — realtime
 *  model usage is token-priced and the transport doesn't surface tokens yet, so
 *  there is deliberately no model rate here. Update per deployment / contract.
 *  Source: public pay-as-you-go list price, 2026-06. */
export const REALTIME_RATES: Record<string, RealtimeRate> = {
	twilio: { usdPerMinute: 0.0085 }, // US local inbound voice
};

/** Advisory USD for `seconds` at `provider`'s rate, or undefined if unknown. */
export function advisoryCostUsd(provider: string, seconds: number): number | undefined {
	const r = REALTIME_RATES[provider];
	if (!r) return undefined;
	const perSec = r.usdPerSecond ?? (r.usdPerMinute != null ? r.usdPerMinute / 60 : undefined);
	if (perSec === undefined) return undefined;
	return Math.round(perSec * seconds * 1e6) / 1e6;
}

/** ms → whole seconds, floored at 0. */
export function durationSeconds(ms: number): number {
	return Math.max(0, Math.round(ms / 1000));
}

/** Drop undefined-valued keys so the returned record equals its serialized
 *  ledger line byte-for-byte (JSON.stringify omits undefined, so leaving them in
 *  would make the in-memory record and the ledger diverge). */
function compactAttrs(a: UsageAttrs): UsageAttrs {
	const out: UsageAttrs = {};
	for (const [k, v] of Object.entries(a)) if (v !== undefined) out[k] = v;
	return out;
}

export interface VoiceSessionUsage {
	sessionId: string;
	durationMs: number;
	model: string; // the realtime (native-audio) model name
	provider?: string; // default 'gemini-live'
	toolCalls?: number;
}

/**
 * Record one realtime voice-agent session as `voice.seconds`. Returns the
 * stamped record, or null for a zero-length session (nothing to meter). The
 * `usage_id` is keyed on the session id so a double-flush re-append dedups
 * downstream to one billed record.
 */
export function recordVoiceSession(u: VoiceSessionUsage): UsageRecord | null {
	const seconds = durationSeconds(u.durationMs);
	if (seconds <= 0) return null;
	const provider = u.provider ?? 'gemini-live';
	const actor: Actor = { user_id: 'owner', channel: 'voice', access_tier: 'owner', tenant_id: null };
	return record({
		source: 'voice-agent',
		meter: 'voice.seconds',
		quantity: seconds,
		unit: 'seconds',
		provider,
		provider_ref: u.sessionId,
		usage_id: `voice.seconds:${u.sessionId}`,
		actor,
		attrs: compactAttrs({ model: u.model, tool_calls: u.toolCalls, cost_usd: advisoryCostUsd(provider, seconds) }),
	});
}

export interface PhoneCallUsage {
	callSid: string;
	durationMs: number;
	model: string; // in-call realtime model (Gemini)
	isOwner?: boolean;
	isMeeting?: boolean;
	toolCalls?: number;
	modelProvider?: string; // default 'gemini-live'
}

/**
 * Record one phone call on both axes it consumes: the Twilio telephony leg
 * (`phone.seconds`, with advisory cost) and the in-call realtime model leg
 * (`voice.seconds`, gemini-live), both keyed by Call SID. Returns the records
 * written (empty for a zero-length call).
 */
export function recordPhoneCall(u: PhoneCallUsage): UsageRecord[] {
	const seconds = durationSeconds(u.durationMs);
	if (seconds <= 0) return [];
	const actor: Actor = {
		user_id: u.isOwner ? 'owner' : 'caller',
		channel: 'phone',
		access_tier: (u.isOwner ? 'owner' : 'public') as AccessTier,
		tenant_id: null,
	};
	const modelProvider = u.modelProvider ?? 'gemini-live';
	const telephony = record({
		source: 'phone',
		meter: 'phone.seconds',
		quantity: seconds,
		unit: 'seconds',
		provider: 'twilio',
		provider_ref: u.callSid,
		usage_id: `phone.seconds:${u.callSid}`,
		actor,
		attrs: compactAttrs({ is_meeting: u.isMeeting, is_owner: u.isOwner, tool_calls: u.toolCalls, cost_usd: advisoryCostUsd('twilio', seconds) }),
	});
	const model = record({
		source: 'phone',
		meter: 'voice.seconds',
		quantity: seconds,
		unit: 'seconds',
		provider: modelProvider,
		provider_ref: u.callSid,
		usage_id: `voice.seconds:${u.callSid}`,
		actor,
		attrs: compactAttrs({ model: u.model, is_meeting: u.isMeeting, is_owner: u.isOwner, tool_calls: u.toolCalls, cost_usd: advisoryCostUsd(modelProvider, seconds) }),
	});
	return [telephony, model];
}

// ---------------------------------------------------------------------------
// Incremental tickers — emit usage records while a session/call is still live
// so the ledger and visualizer reflect real-time consumption, not just a single
// end-of-session burst. Each bucket gets a stable usage_id keyed on the bucket
// start epoch (seconds), so downstream aggregation can sum buckets correctly.
// ---------------------------------------------------------------------------

function voiceActor(): Actor {
	return { user_id: 'owner', channel: 'voice', access_tier: 'owner', tenant_id: null };
}

function phoneActor(isOwner?: boolean): Actor {
	return {
		user_id: isOwner ? 'owner' : 'caller',
		channel: 'phone',
		access_tier: (isOwner ? 'owner' : 'public') as AccessTier,
		tenant_id: null,
	};
}

function recordVoiceIncrement(opts: {
	sessionId: string; model: string; provider?: string;
	toolCalls?: number; durationMs: number; bucketStartMs: number;
}): UsageRecord | null {
	const seconds = durationSeconds(opts.durationMs);
	if (seconds <= 0) return null;
	const provider = opts.provider ?? 'gemini-live';
	const bucketSec = Math.floor(opts.bucketStartMs / 1000);
	return record({
		source: 'voice-agent', meter: 'voice.seconds', quantity: seconds, unit: 'seconds',
		provider, provider_ref: opts.sessionId,
		usage_id: `voice.seconds:${opts.sessionId}:b${bucketSec}`,
		actor: voiceActor(),
		attrs: compactAttrs({ model: opts.model, tool_calls: opts.toolCalls, cost_usd: advisoryCostUsd(provider, seconds) }),
	});
}

function recordPhoneIncrement(opts: {
	callSid: string; model: string; isOwner?: boolean; isMeeting?: boolean;
	modelProvider?: string; toolCalls?: number; durationMs: number; bucketStartMs: number;
}): UsageRecord[] {
	const seconds = durationSeconds(opts.durationMs);
	if (seconds <= 0) return [];
	const actor = phoneActor(opts.isOwner);
	const modelProvider = opts.modelProvider ?? 'gemini-live';
	const bucketSec = Math.floor(opts.bucketStartMs / 1000);
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
	/** Emit final partial bucket and cancel the interval. Idempotent. */
	stop: () => UsageRecord | null;
}

export interface PhoneTickerHandle {
	/** Emit final partial bucket and cancel the interval. Idempotent. */
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
	let lastMs = _nowFn();
	let stopped = false;
	const timer = setInterval(() => {
		const now = _nowFn();
		try {
			recordVoiceIncrement({ ...opts, toolCalls: opts.toolCallsGetter?.(), durationMs: now - lastMs, bucketStartMs: lastMs });
		} catch { /* meter is non-throwing; belt-and-suspenders */ }
		lastMs = now;
	}, intervalMs);
	return {
		stop: () => {
			if (stopped) return null;
			stopped = true;
			clearInterval(timer);
			const now = _nowFn();
			try {
				return recordVoiceIncrement({ ...opts, toolCalls: opts.toolCallsGetter?.(), durationMs: now - lastMs, bucketStartMs: lastMs });
			} catch { return null; }
		},
	};
}

/**
 * Start a periodic usage ticker for a phone call (both Twilio + Gemini-Live
 * axes). Emits two records per interval. Call `handle.stop()` on call end.
 */
export function startPhoneTicker(
	opts: { callSid: string; model: string; isOwner?: boolean; isMeeting?: boolean; modelProvider?: string; toolCallsGetter?: () => number },
	intervalMs = USAGE_TICK_MS,
	_nowFn: () => number = Date.now,
): PhoneTickerHandle {
	let lastMs = _nowFn();
	let stopped = false;
	const timer = setInterval(() => {
		const now = _nowFn();
		try {
			recordPhoneIncrement({ ...opts, toolCalls: opts.toolCallsGetter?.(), durationMs: now - lastMs, bucketStartMs: lastMs });
		} catch { /* meter is non-throwing; belt-and-suspenders */ }
		lastMs = now;
	}, intervalMs);
	return {
		stop: () => {
			if (stopped) return [];
			stopped = true;
			clearInterval(timer);
			const now = _nowFn();
			try {
				return recordPhoneIncrement({ ...opts, toolCalls: opts.toolCallsGetter?.(), durationMs: now - lastMs, bucketStartMs: lastMs });
			} catch { return []; }
		},
	};
}
