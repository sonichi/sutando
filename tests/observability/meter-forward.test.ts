import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { meterForwarderFromConfig, type FetchLike } from '../../src/observability/meter-forward.js';
import { Collector } from '../../src/observability/collector/collector.js';
import type { MeteringSection } from '../../src/observability/config.js';
import type { UsageRecord } from '../../src/observability/usage.js';

function rec(usage_id: string): UsageRecord {
	return {
		schema: 1,
		usage_id,
		ts: 1_700_000_000,
		tenant_id: null,
		trace_id: 'voice-sess:s1',
		actor: { user_id: 'owner', channel: 'voice', access_tier: 'owner', tenant_id: null },
		source: 'voice-agent',
		meter: 'voice.seconds',
		quantity: 10,
		unit: 'seconds',
		provider: 'gemini-live',
		provider_ref: 's1',
		attrs: { model: 'gemini-live' },
	};
}

const ON: MeteringSection = { enabled: true, endpoint: 'http://127.0.0.1:9999/usage', batchMax: 3 };

/** Stub fetch that records each POST and replies with the queued ok/err verdicts. */
function stubFetch(verdicts: boolean[] = []) {
	const posts: { url: string; usageIds: string[] }[] = [];
	let i = 0;
	const impl: FetchLike = async (url, init) => {
		const body = JSON.parse(String(init.body)) as { usage: UsageRecord[] };
		posts.push({ url, usageIds: body.usage.map((u) => u.usage_id) });
		const ok = i < verdicts.length ? verdicts[i] : true;
		i++;
		return { ok };
	};
	return { impl, posts };
}

const tick = () => new Promise((r) => setImmediate(r));

describe('meterForwarderFromConfig — gating (export off by default)', () => {
	it('returns null when metering disabled', () => {
		assert.equal(meterForwarderFromConfig({ enabled: false, endpoint: 'http://x', batchMax: 100 }), null);
	});
	it('returns null when no endpoint is set', () => {
		assert.equal(meterForwarderFromConfig({ enabled: true, endpoint: null, batchMax: 100 }), null);
	});
	it('builds a forwarder only when enabled AND endpoint are set', () => {
		assert.ok(meterForwarderFromConfig(ON, { fetchImpl: stubFetch().impl }));
	});
});

describe('HttpMeterForwarder', () => {
	it('flushes the queue to the endpoint as a { usage: [...] } envelope', async () => {
		const { impl, posts } = stubFetch();
		const f = meterForwarderFromConfig(ON, { fetchImpl: impl })!;
		f.forward(rec('a'));
		await f.flush();
		assert.equal(posts.length, 1);
		assert.equal(posts[0].url, ON.endpoint);
		assert.deepEqual(posts[0].usageIds, ['a']);
	});

	it('auto-flushes when the queue reaches batchMax', async () => {
		const { impl, posts } = stubFetch();
		const f = meterForwarderFromConfig(ON, { fetchImpl: impl })!;
		f.forward(rec('a'));
		f.forward(rec('b'));
		f.forward(rec('c')); // batchMax = 3 → immediate flush
		await tick();
		assert.equal(posts.length, 1);
		assert.deepEqual(posts[0].usageIds, ['a', 'b', 'c']);
	});

	it('requeues at the head on a non-2xx and retries the same batch next flush', async () => {
		const { impl, posts } = stubFetch([false]); // first POST fails, rest ok
		const f = meterForwarderFromConfig(ON, { fetchImpl: impl })!;
		f.forward(rec('a'));
		await f.flush(); // attempt 1 → 5xx → requeue 'a'
		await f.flush(); // attempt 2 → ok
		assert.deepEqual(posts.map((p) => p.usageIds), [['a'], ['a']]);
	});

	it('never throws when fetch rejects (network down)', async () => {
		const f = meterForwarderFromConfig(ON, {
			fetchImpl: async () => {
				throw new Error('ECONNREFUSED');
			},
		})!;
		f.forward(rec('a'));
		await f.flush(); // must resolve, not reject
		assert.ok(true);
	});

	it('stop() flushes the final partial batch', async () => {
		const { impl, posts } = stubFetch();
		const f = meterForwarderFromConfig(ON, { fetchImpl: impl })!;
		f.forward(rec('a'));
		await f.stop();
		assert.deepEqual(posts.map((p) => p.usageIds), [['a']]);
	});
});

describe('Collector ↔ usageForwarder', () => {
	it('accept() fans each usage record to the forwarder (alongside the ledger)', () => {
		const forwarded: string[] = [];
		const written: string[] = [];
		const mock = {
			forward: (u: UsageRecord) => forwarded.push(u.usage_id),
			flush: async () => {},
			stop: async () => {},
		};
		const c = new Collector({ usageWriter: (u) => void written.push(u.usage_id), usageForwarder: mock });
		c.accept({ events: [], usage: [rec('x'), rec('y')] });
		assert.deepEqual(written, ['x', 'y']);
		assert.deepEqual(forwarded, ['x', 'y']);
	});

	it('usageForwarder: null disables export without touching the ledger write', () => {
		const written: string[] = [];
		const c = new Collector({ usageWriter: (u) => void written.push(u.usage_id), usageForwarder: null });
		c.accept({ events: [], usage: [rec('x')] }); // must not throw
		assert.deepEqual(written, ['x']);
	});
});
