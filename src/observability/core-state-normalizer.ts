/**
 * Core supervisor state → obs events, as a composable collector Normalizer.
 * The monitor (src/core-input-watch.py) POSTs `{kind:'core.state', from, to,
 * detail, gate, session}` to `/ingest/core-state` on every supervisor state
 * transition, and `{kind:'core.turn_refused', line, session}` once per turn the
 * CLI refused at its idle footer; this validates the shape and hands it to the
 * pure map, which names it (`core.auth.login_required`, `core.auth.turn_refused`,
 * `core.session.crashed`, `core.gateway.down`, ...). Registered on the same
 * collector as the CC and realtime sources (boot.ts). Events only — no usage.
 */

import { AbstractNormalizer, type NormalizeContext, type NormalizeResult } from './collector/normalizer.js';
import { mapCoreRecord, type RawCoreRecord } from './core-state-map.js';

export const CORE_STATE_SOURCE = 'core-state';

export class CoreStateNormalizer extends AbstractNormalizer<RawCoreRecord> {
	readonly source = CORE_STATE_SOURCE;

	/** Accept only a well-formed record; anything else is dropped, never thrown. */
	decode(payload: unknown): RawCoreRecord | null {
		if (!payload || typeof payload !== 'object') return null;
		const p = payload as Record<string, unknown>;
		if (typeof p.session !== 'string' || !p.session) return null;
		if (p.ts !== undefined && typeof p.ts !== 'number') return null;
		if (p.kind === 'core.turn_refused') {
			if (typeof p.line !== 'string' || !p.line) return null;
			return p as unknown as RawCoreRecord;
		}
		if (p.kind !== 'core.state') return null;
		if (typeof p.to !== 'string' || !p.to) return null;
		if (p.from !== undefined && p.from !== null && typeof p.from !== 'string') return null;
		return p as unknown as RawCoreRecord;
	}

	map(p: RawCoreRecord, ctx: NormalizeContext): NormalizeResult {
		return mapCoreRecord(p, { node: ctx.node, receivedAt: ctx.receivedAt });
	}
}
