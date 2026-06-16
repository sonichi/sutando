/**
 * Claude Code OTel metrics → spine primitives, as a composable collector
 * Normalizer. This is the METERING source: it turns CC's native
 * `claude_code.token.usage` / `claude_code.cost.usage` OTLP metrics into durable
 * `claude.tokens` / `claude.cost` ledger records (plus a few counter obs events).
 *
 *   decode(unknown) → OtelRecord[] | null   (cc-otel: OTLP/HTTP metrics decoder)
 *   map(OtelRecord[]) → { events, usage }    (the already-tested otelMap)
 *
 * Registered on the same collector as the hook source — Claude Code is one
 * source TYPE that emits via two channels (hooks for obs, OTel for usage). The
 * collector writes usage through the meter ledger; nothing here re-stamps the
 * CC-derived ids that otelMap adopts.
 */

import { AbstractNormalizer, type NormalizeContext, type NormalizeResult } from '../collector/normalizer.js';
import type { ObsEvent } from '../events.js';
import type { UsageRecord } from '../usage.js';
import type { OtelRecord } from './cc-records.js';
import { decodeOtlpMetrics } from './cc-otel.js';
import { otelMap } from './otel-map.js';

export const CC_OTEL_SOURCE = 'claude-code-otel';

export class ClaudeCodeOtelNormalizer extends AbstractNormalizer<OtelRecord[]> {
	readonly source = CC_OTEL_SOURCE;

	/** Detect + flatten the OTLP metrics body. `null` (no records) → dropped. */
	decode(payload: unknown): OtelRecord[] | null {
		const recs = decodeOtlpMetrics(payload);
		return recs.length > 0 ? recs : null;
	}

	/** Map every decoded record through the canonical `otelMap`. */
	map(recs: OtelRecord[], ctx: NormalizeContext): NormalizeResult {
		const events: ObsEvent[] = [];
		const usage: UsageRecord[] = [];
		for (const rec of recs) {
			const r = otelMap(rec, { node: ctx.node, receivedAt: ctx.receivedAt });
			events.push(...r.events);
			usage.push(...r.usage);
		}
		return { events, usage };
	}
}
