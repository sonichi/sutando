/**
 * `dedupUsage(records)` — collapse token-usage records that the three sources
 * report for the SAME request, so no turn is double-counted and no single source
 * is load-bearing. Pure and order-independent (does not mutate its input).
 *
 * Rule: group `claude.tokens` records by `provider_ref` (the request_id). Within
 * a group, keep only the records from the single HIGHEST-rank source present and
 * drop the rest:  otel-span (3) > otel-metric (2) > jsonl (1). So:
 *   - span present  → keep the span's one record, drop metric + jsonl.
 *   - only metrics  → keep all per-type metric records (they're the sole source).
 *   - only jsonl    → keep the degraded-but-present fallback.
 * Records with no `provider_ref` (coarse fallback) and non-token records pass
 * through. A `claude.cost` record's USD is folded into the surviving token record
 * of the same request as `attrs.cost_usd`, and the cost record is also kept.
 */

import type { UsageRecord } from '../../../../kernel/meter/types.js';

const RANK: Record<string, number> = { 'otel-span': 3, 'otel-metric': 2, jsonl: 1 };

function rankOf(r: UsageRecord): number {
	const src = r.attrs?._cc_source;
	return (typeof src === 'string' && RANK[src]) || 0;
}

export function dedupUsage(records: UsageRecord[]): UsageRecord[] {
	const tokenGroups = new Map<string, UsageRecord[]>();
	const others: UsageRecord[] = [];
	for (const r of records) {
		if (r.meter === 'claude.tokens' && r.provider_ref) {
			const g = tokenGroups.get(r.provider_ref) ?? [];
			g.push(r);
			tokenGroups.set(r.provider_ref, g);
		} else {
			others.push(r);
		}
	}

	// cost lookup (by request) for the fold
	const costByRef = new Map<string, number>();
	for (const r of others) {
		if (r.meter === 'claude.cost' && r.provider_ref && typeof r.quantity === 'number') {
			costByRef.set(r.provider_ref, r.quantity);
		}
	}

	const survivors: UsageRecord[] = [];
	for (const recs of tokenGroups.values()) {
		const maxRank = Math.max(...recs.map(rankOf));
		for (const r of recs) {
			if (rankOf(r) !== maxRank) continue;
			const cost = r.provider_ref ? costByRef.get(r.provider_ref) : undefined;
			if (cost !== undefined && r.attrs?.cost_usd === undefined) {
				survivors.push({ ...r, attrs: { ...r.attrs, cost_usd: cost } }); // no input mutation
			} else {
				survivors.push(r);
			}
		}
	}

	return [...survivors, ...others];
}
