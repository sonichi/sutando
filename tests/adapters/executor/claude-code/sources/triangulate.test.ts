import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { dedupUsage } from '../../../../../src/adapters/executor/claude-code/sources/triangulate.js';
import type { UsageRecord } from '../../../../../src/kernel/meter/types.js';

function tok(src: string, requestId: string | null, q: number, suffix = ''): UsageRecord {
	return {
		schema: 1,
		usage_id: requestId ? `cc:tok:${requestId}${suffix}` : 'cc:tok:none',
		ts: 1,
		tenant_id: null,
		trace_id: 't',
		actor: { user_id: 'core', channel: 'claude-code', access_tier: 'owner' },
		source: 'core-cli',
		meter: 'claude.tokens',
		quantity: q,
		unit: 'tokens',
		provider: 'anthropic',
		provider_ref: requestId,
		attrs: { _cc_source: src },
	};
}
function cost(requestId: string, usd: number): UsageRecord {
	return {
		schema: 1,
		usage_id: `cc:tok:${requestId}:cost`,
		ts: 1,
		tenant_id: null,
		trace_id: 't',
		actor: { user_id: 'core', channel: 'claude-code', access_tier: 'owner' },
		source: 'core-cli',
		meter: 'claude.cost',
		quantity: usd,
		unit: 'usd',
		provider: 'anthropic',
		provider_ref: requestId,
		attrs: { _cc_source: 'otel-metric', cost_usd: usd },
	};
}

const tokens = (rs: UsageRecord[]) => rs.filter((r) => r.meter === 'claude.tokens');

describe('dedupUsage — triangulation', () => {
	it('span beats jsonl for the same request (no single source load-bearing)', () => {
		const out = tokens(dedupUsage([tok('jsonl', 'req_1', 1840), tok('otel-span', 'req_1', 1840)]));
		assert.equal(out.length, 1);
		assert.equal(out[0].attrs._cc_source, 'otel-span');
	});

	it('metrics-only: all per-type records survive (sole source)', () => {
		const out = tokens(dedupUsage([tok('otel-metric', 'req_1', 1500, ':input'), tok('otel-metric', 'req_1', 340, ':output')]));
		assert.equal(out.length, 2);
	});

	it('jsonl alone still yields a usable record (degraded fallback)', () => {
		const out = tokens(dedupUsage([tok('jsonl', 'req_1', 1840)]));
		assert.equal(out.length, 1);
		assert.equal(out[0].attrs._cc_source, 'jsonl');
	});

	it('folds claude.cost into the surviving token record AND keeps the cost row', () => {
		const out = dedupUsage([tok('otel-span', 'req_1', 1840), cost('req_1', 0.0061)]);
		const t = out.find((r) => r.meter === 'claude.tokens')!;
		const c = out.find((r) => r.meter === 'claude.cost');
		assert.equal(t.attrs.cost_usd, 0.0061);
		assert.ok(c, 'cost row preserved for the cost meter');
	});

	it('is order-independent', () => {
		const a = [tok('jsonl', 'req_1', 1840), tok('otel-span', 'req_1', 1840), cost('req_1', 0.0061)];
		const key = (r: UsageRecord) => r.meter + '|' + r.attrs._cc_source + '|' + r.quantity;
		const norm = (rs: UsageRecord[]) => rs.map(key).sort();
		assert.deepEqual(norm(dedupUsage(a)), norm(dedupUsage([...a].reverse())));
	});

	it('passes through token records with no provider_ref (coarse fallback)', () => {
		const out = dedupUsage([tok('otel-metric', null, 5)]);
		assert.equal(out.length, 1);
	});
});
