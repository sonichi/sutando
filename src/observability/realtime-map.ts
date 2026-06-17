/**
 * Realtime surface usage MAP — the pure transform from a raw voice/phone usage
 * payload into spine primitives: UsageRecord(s) + the matching `usage.recorded`
 * ObsEvent(s). Runs INSIDE the collector (via RealtimeNormalizer). The voice
 * agent and phone server only SEND raw payloads (see realtime.ts, the client) —
 * they never map or write. One ingestion point, one place mapping happens.
 *
 * Pure: no Date.now()/random — `ts` comes from the payload or the collector ctx.
 *
 * trace_id is DERIVED from the session/call id (`voice-sess:<id>` /
 * `phone-call:<sid>`), mirroring the CC `cc-sess:<id>` scheme, so every record
 * AND event for one session/call — both phone legs and every incremental tick —
 * shares a single trace without any coordination.
 *
 * A phone call is metered on BOTH axes it consumes: the Twilio telephony leg
 * (`phone.seconds`, with advisory cost) and the in-call realtime model leg
 * (`voice.seconds`), joined by the Call SID and separable by `(meter, source)`.
 *
 * `cost_usd` is ADVISORY only — from the documented list-price table below, and
 * only where a per-time rate exists (telephony). Never bill from it.
 */

import type { ObsEvent, Actor, AccessTier, UsageAdvisory } from './events.js';
import type { UsageRecord, UsageAttrs } from './usage.js';

// --- raw payloads (what the client POSTs to /ingest/realtime) ----------------

export interface RawVoiceUsage {
	kind: 'voice.session';
	sessionId: string;
	durationMs: number;
	model: string;
	provider?: string; // default 'gemini-live'
	toolCalls?: number;
	bucketStartMs?: number; // present for an incremental tick; absent for a one-shot
	ts?: number; // float unix seconds; defaults to the collector receive time
}

export interface RawPhoneUsage {
	kind: 'phone.call';
	callSid: string;
	durationMs: number;
	model: string; // in-call realtime model
	isOwner?: boolean;
	isMeeting?: boolean;
	modelProvider?: string; // default 'gemini-live'
	toolCalls?: number;
	bucketStartMs?: number;
	ts?: number;
}

export type RawRealtimeUsage = RawVoiceUsage | RawPhoneUsage;

export interface RealtimeMapContext {
	node: string;
	receivedAt: number; // float unix seconds
}

export interface RealtimeMapResult {
	events: ObsEvent[];
	usage: UsageRecord[];
}

// --- rates -------------------------------------------------------------------

export interface RealtimeRate {
	usdPerSecond?: number;
	usdPerMinute?: number;
}

/** Advisory list prices (NOT the billed figure). Telephony only — realtime
 *  model usage is token-priced and the transport doesn't surface tokens yet.
 *  Source: public pay-as-you-go list price, 2026-06. Override per deployment. */
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

/** ms → whole seconds, floored at 0. */
export function durationSeconds(ms: number): number {
	return Math.max(0, Math.round(ms / 1000));
}

// --- internals ---------------------------------------------------------------

/** Drop undefined-valued keys so the record matches its JSONL line byte-for-byte. */
function compact<T extends Record<string, unknown>>(o: T): T {
	const out: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v;
	return out as T;
}

const VOICE_ACTOR: Actor = { user_id: 'owner', channel: 'voice', access_tier: 'owner', tenant_id: null };

function phoneActor(isOwner?: boolean): Actor {
	return {
		user_id: isOwner ? 'owner' : 'caller',
		channel: 'phone',
		access_tier: (isOwner ? 'owner' : 'public') as AccessTier,
		tenant_id: null,
	};
}

function bucketSuffix(bucketStartMs?: number): string {
	return bucketStartMs === undefined ? '' : `:b${Math.floor(bucketStartMs / 1000)}`;
}

function makeRecord(f: Omit<UsageRecord, 'schema' | 'tenant_id'>): UsageRecord {
	// tenant_id is null here (BYOK floor); the managed-tenant shipper assigns it
	// downstream — post-parity, intentionally not resolved in the map.
	return { schema: 1, tenant_id: null, ...f };
}

/** The advisory `usage.recorded` obs event that mirrors a usage record — same
 *  trace_id, so the ledger row and the event correlate. Mirrors meter.record(). */
function usageEvent(rec: UsageRecord, node: string): ObsEvent {
	const usage: UsageAdvisory = compact({
		provider: rec.provider,
		model: rec.attrs.model,
		input_tokens: rec.attrs.input_tokens,
		output_tokens: rec.attrs.output_tokens,
		cache_read: rec.attrs.cache_read,
		cache_creation: rec.attrs.cache_creation,
		cost_usd: rec.attrs.cost_usd,
	});
	return {
		schema: 1,
		ts: rec.ts,
		trace_id: rec.trace_id,
		node,
		source: rec.source,
		actor: rec.actor,
		kind: 'usage.recorded',
		outcome: 'ok',
		usage,
		data: {
			meter: rec.meter,
			quantity: rec.quantity,
			unit: rec.unit,
			usage_id: rec.usage_id,
			provider_ref: rec.provider_ref,
		},
	};
}

// --- the map -----------------------------------------------------------------

export function mapRealtime(p: RawRealtimeUsage, ctx: RealtimeMapContext): RealtimeMapResult {
	const ts = p.ts ?? ctx.receivedAt;
	const seconds = durationSeconds(p.durationMs);
	if (seconds <= 0) return { events: [], usage: [] };

	if (p.kind === 'voice.session') {
		const provider = p.provider ?? 'gemini-live';
		const rec = makeRecord({
			usage_id: `voice.seconds:${p.sessionId}${bucketSuffix(p.bucketStartMs)}`,
			ts,
			trace_id: `voice-sess:${p.sessionId}`,
			actor: VOICE_ACTOR,
			source: 'voice-agent',
			meter: 'voice.seconds',
			quantity: seconds,
			unit: 'seconds',
			provider,
			provider_ref: p.sessionId,
			attrs: compact({ model: p.model, tool_calls: p.toolCalls, cost_usd: advisoryCostUsd(provider, seconds) }),
		});
		return { events: [usageEvent(rec, ctx.node)], usage: [rec] };
	}

	// phone.call → two legs sharing one trace + Call SID
	const trace = `phone-call:${p.callSid}`;
	const actor = phoneActor(p.isOwner);
	const modelProvider = p.modelProvider ?? 'gemini-live';
	const suffix = bucketSuffix(p.bucketStartMs);
	const telephony = makeRecord({
		usage_id: `phone.seconds:${p.callSid}${suffix}`,
		ts,
		trace_id: trace,
		actor,
		source: 'phone',
		meter: 'phone.seconds',
		quantity: seconds,
		unit: 'seconds',
		provider: 'twilio',
		provider_ref: p.callSid,
		attrs: compact({ is_meeting: p.isMeeting, is_owner: p.isOwner, tool_calls: p.toolCalls, cost_usd: advisoryCostUsd('twilio', seconds) }),
	});
	const model = makeRecord({
		usage_id: `voice.seconds:${p.callSid}${suffix}`,
		ts,
		trace_id: trace,
		actor,
		source: 'phone',
		meter: 'voice.seconds',
		quantity: seconds,
		unit: 'seconds',
		provider: modelProvider,
		provider_ref: p.callSid,
		attrs: compact({ model: p.model, is_meeting: p.isMeeting, is_owner: p.isOwner, tool_calls: p.toolCalls, cost_usd: advisoryCostUsd(modelProvider, seconds) }),
	});
	return { events: [usageEvent(telephony, ctx.node), usageEvent(model, ctx.node)], usage: [telephony, model] };
}
