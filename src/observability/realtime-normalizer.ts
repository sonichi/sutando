/**
 * Realtime voice + phone usage → spine primitives, as a composable collector
 * Normalizer. This is the metering source for the two realtime surfaces: it
 * turns the raw `{kind:'voice.session'|'phone.call', ...}` payloads the voice
 * agent / phone server POST to `/ingest/realtime` into durable `voice.seconds` /
 * `phone.seconds` ledger records plus their `usage.recorded` obs events.
 *
 *   decode(unknown) → RawRealtimeUsage | null   (validate + discriminate by kind)
 *   map(RawRealtimeUsage) → { events, usage }    (the pure mapRealtime)
 *
 * Registered on the same collector as the CC sources (boot.ts). The collector
 * writes usage through the meter ledger and events through the sink-set; nothing
 * here re-stamps the trace_id that mapRealtime derives from the session/call id.
 */

import { AbstractNormalizer, type NormalizeContext, type NormalizeResult } from './collector/normalizer.js';
import { mapRealtime, type RawRealtimeUsage, type RawVoiceUsage, type RawPhoneUsage } from './realtime-map.js';

export const REALTIME_SOURCE = 'realtime';

export class RealtimeNormalizer extends AbstractNormalizer<RawRealtimeUsage> {
	readonly source = REALTIME_SOURCE;

	/** Validate + discriminate on `kind`; drop anything missing required fields. */
	decode(payload: unknown): RawRealtimeUsage | null {
		if (!payload || typeof payload !== 'object') return null;
		const p = payload as Record<string, unknown>;
		const hasDur = typeof p.durationMs === 'number';
		const hasModel = typeof p.model === 'string';
		if (p.kind === 'voice.session' && typeof p.sessionId === 'string' && hasDur && hasModel) {
			return p as unknown as RawVoiceUsage;
		}
		if (p.kind === 'phone.call' && typeof p.callSid === 'string' && hasDur && hasModel) {
			return p as unknown as RawPhoneUsage;
		}
		return null;
	}

	map(p: RawRealtimeUsage, ctx: NormalizeContext): NormalizeResult {
		return mapRealtime(p, { node: ctx.node, receivedAt: ctx.receivedAt });
	}
}
