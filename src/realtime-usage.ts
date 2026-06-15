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
import type { Actor } from './observability/events.js';

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
		access_tier: u.isOwner ? 'owner' : 'public',
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
