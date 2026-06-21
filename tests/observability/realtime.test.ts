import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import type { ObsEvent } from '../../src/observability/events.js';
import type { UsageRecord } from '../../src/observability/usage.js';
import type { Sink } from '../../src/observability/sink.js';
import { Collector } from '../../src/observability/collector/collector.js';
import { RealtimeNormalizer, REALTIME_SOURCE } from '../../src/observability/realtime-normalizer.js';
import { mapRealtime, advisoryCostUsd, durationSeconds } from '../../src/observability/realtime-map.js';
import { sendVoiceUsage, startVoiceTicker, startPhoneTicker } from '../../src/observability/realtime.js';

const CTX = { node: 'test-node', receivedAt: 1_700_000_000 };

describe('helpers', () => {
	it('durationSeconds floors at 0 and rounds', () => {
		assert.equal(durationSeconds(1499), 1);
		assert.equal(durationSeconds(1500), 2);
		assert.equal(durationSeconds(-5), 0);
	});
	it('advisoryCostUsd: known telephony rate, undefined for model providers', () => {
		assert.equal(advisoryCostUsd('twilio', 60), 0.0085); // 1 min @ $0.0085/min
		assert.equal(advisoryCostUsd('gemini-live', 60), undefined);
		assert.equal(advisoryCostUsd('made-up', 60), undefined);
	});
});

describe('mapRealtime — voice', () => {
	it('one voice.session → 1 usage record + 1 usage.recorded event, trace derived from session id', () => {
		const { events, usage } = mapRealtime(
			{ kind: 'voice.session', sessionId: 'session_42', durationMs: 90_000, model: 'gemini-3-flash-live', toolCalls: 3 },
			CTX,
		);
		assert.equal(usage.length, 1);
		assert.equal(events.length, 1);
		const rec = usage[0];
		assert.equal(rec.meter, 'voice.seconds');
		assert.equal(rec.quantity, 90);
		assert.equal(rec.unit, 'seconds');
		assert.equal(rec.provider, 'gemini-live');
		assert.equal(rec.source, 'voice-agent');
		assert.equal(rec.provider_ref, 'session_42');
		assert.equal(rec.usage_id, 'voice.seconds:session_42:t1700000000000'); // one-shot → ts-keyed suffix (unique per emission)
		assert.equal(rec.trace_id, 'voice-sess:session_42'); // derived, not minted
		assert.equal(rec.tenant_id, null);
		assert.equal(rec.attrs.model, 'gemini-3-flash-live');
		assert.equal(rec.attrs.tool_calls, 3);
		assert.equal(rec.attrs.cost_usd, undefined); // realtime model: no advisory rate

		const adv = events[0];
		assert.equal(adv.kind, 'usage.recorded');
		assert.equal(adv.source, 'voice-agent');
		assert.equal(adv.trace_id, rec.trace_id); // ledger ↔ event correlate
		assert.equal((adv.data as Record<string, unknown>).usage_id, 'voice.seconds:session_42:t1700000000000');
		assert.equal(adv.node, 'test-node');
	});

	it('bucketStartMs → bucket-keyed usage_id (incremental tick)', () => {
		const { usage } = mapRealtime(
			{ kind: 'voice.session', sessionId: 's1', durationMs: 30_000, model: 'm', bucketStartMs: 1_700_000_000_000 },
			CTX,
		);
		assert.equal(usage[0].usage_id, 'voice.seconds:s1:b1700000000');
	});

	it('zero-length session → dropped', () => {
		assert.deepEqual(mapRealtime({ kind: 'voice.session', sessionId: 's0', durationMs: 200, model: 'm' }, CTX), { events: [], usage: [] });
	});
});

describe('mapRealtime — phone', () => {
	it('one phone.call → twilio + gemini-live legs sharing trace + Call SID', () => {
		const { events, usage } = mapRealtime(
			{ kind: 'phone.call', callSid: 'CA123', durationMs: 120_000, model: 'gemini-2.5-flash', isOwner: true, isMeeting: false, toolCalls: 2 },
			CTX,
		);
		assert.equal(usage.length, 2);
		assert.equal(events.length, 2);

		const tel = usage.find((r) => r.meter === 'phone.seconds')!;
		assert.equal(tel.provider, 'twilio');
		assert.equal(tel.source, 'phone');
		assert.equal(tel.quantity, 120);
		assert.equal(tel.usage_id, 'phone.seconds:CA123:t1700000000000');
		assert.equal(tel.attrs.cost_usd, 0.017); // 2 min @ $0.0085/min
		assert.equal(tel.attrs.is_owner, true);

		const model = usage.find((r) => r.meter === 'voice.seconds')!;
		assert.equal(model.provider, 'gemini-live');
		assert.equal(model.usage_id, 'voice.seconds:CA123:t1700000000000');
		assert.equal(model.attrs.model, 'gemini-2.5-flash');
		assert.equal(model.attrs.cost_usd, undefined);

		// both legs share ONE trace (joined by Call SID)
		assert.equal(tel.trace_id, 'phone-call:CA123');
		assert.equal(model.trace_id, 'phone-call:CA123');
	});

	it('non-owner caller → public access tier', () => {
		const { usage } = mapRealtime({ kind: 'phone.call', callSid: 'CA9', durationMs: 30_000, model: 'm', isOwner: false }, CTX);
		assert.equal(usage[0].actor.access_tier, 'public');
		assert.equal(usage[0].actor.user_id, 'caller');
		assert.equal(usage[0].actor.channel, 'phone');
	});
});

describe('RealtimeNormalizer.decode', () => {
	const n = new RealtimeNormalizer();
	it('accepts well-formed voice + phone payloads', () => {
		assert.ok(n.decode({ kind: 'voice.session', sessionId: 's', durationMs: 1000, model: 'm' }));
		assert.ok(n.decode({ kind: 'phone.call', callSid: 'CA', durationMs: 1000, model: 'm' }));
	});
	it('rejects junk / missing required fields', () => {
		assert.equal(n.decode(null), null);
		assert.equal(n.decode('nope'), null);
		assert.equal(n.decode({ kind: 'voice.session', durationMs: 1000, model: 'm' }), null); // no sessionId
		assert.equal(n.decode({ kind: 'phone.call', callSid: 'CA', model: 'm' }), null); // no durationMs
		assert.equal(n.decode({ kind: 'other', sessionId: 's', durationMs: 1, model: 'm' }), null);
	});
});

describe('collector end-to-end (the real write path)', () => {
	it('ingest("realtime", payload) routes through the normalizer to sinks + ledger', () => {
		const events: ObsEvent[] = [];
		const usage: UsageRecord[] = [];
		const sink: Sink = { type: 'capture', write: (e) => events.push(e) };
		const collector = new Collector({ sinks: [sink], usageWriter: (u) => usage.push(u) });
		collector.register(new RealtimeNormalizer());

		const stat = collector.ingest(REALTIME_SOURCE, {
			kind: 'phone.call',
			callSid: 'CAe2e',
			durationMs: 60_000,
			model: 'gemini-2.5-flash',
			isOwner: true,
		});

		assert.equal(stat.ok, true);
		assert.equal(stat.usage, 2); // phone.seconds + voice.seconds
		assert.equal(stat.events, 2); // two usage.recorded events
		assert.equal(usage.length, 2);
		assert.equal(events.length, 2);
		assert.deepEqual(
			usage.map((u) => u.meter).sort(),
			['phone.seconds', 'voice.seconds'],
		);
	});
});

describe('client → collector POST', () => {
	const realFetch = globalThis.fetch;
	let calls: { url: string; body: unknown }[];

	beforeEach(() => {
		calls = [];
		process.env.SUTANDO_OBS_ENDPOINT = 'http://localhost:4000';
		// @ts-expect-error test stub
		globalThis.fetch = (url: string, init?: { body?: string }) => {
			calls.push({ url, body: init?.body ? JSON.parse(init.body) : undefined });
			return Promise.resolve({ ok: true });
		};
	});

	afterEach(() => {
		globalThis.fetch = realFetch;
		delete process.env.SUTANDO_OBS_ENDPOINT;
	});

	it('sendVoiceUsage POSTs a voice.session payload to /ingest/realtime', () => {
		sendVoiceUsage({ sessionId: 's1', durationMs: 42_000, model: 'm', toolCalls: 1 });
		assert.equal(calls.length, 1);
		assert.equal(calls[0].url, 'http://localhost:4000/ingest/realtime');
		assert.deepEqual(calls[0].body, { kind: 'voice.session', sessionId: 's1', durationMs: 42_000, model: 'm', toolCalls: 1 });
	});

	it('voice ticker stop() POSTs the final bucket with elapsed duration + bucketStartMs', () => {
		let fakeNow = 1_700_000_000_000;
		const ticker = startVoiceTicker({ sessionId: 's2', model: 'm', toolCallsGetter: () => 5 }, 60_000, () => fakeNow);
		fakeNow += 45_000;
		ticker.stop();
		assert.equal(calls.length, 1);
		const b = calls[0].body as Record<string, unknown>;
		assert.equal(b.kind, 'voice.session');
		assert.equal(b.durationMs, 45_000);
		assert.equal(b.bucketStartMs, 1_700_000_000_000);
		assert.equal(b.toolCalls, 5);
	});

	it('voice ticker stop() is idempotent — second stop POSTs nothing more', () => {
		let fakeNow = 1_700_000_000_000;
		const ticker = startVoiceTicker({ sessionId: 's3', model: 'm' }, 60_000, () => fakeNow);
		fakeNow += 10_000;
		ticker.stop();
		fakeNow += 10_000;
		ticker.stop();
		assert.equal(calls.length, 1);
	});

	it('phone ticker stop() POSTs one phone.call payload (both legs derived collector-side)', () => {
		let fakeNow = 1_700_000_000_000;
		const ticker = startPhoneTicker({ callSid: 'CAx', model: 'm', isOwner: true }, 60_000, () => fakeNow);
		fakeNow += 90_000;
		ticker.stop();
		assert.equal(calls.length, 1);
		const b = calls[0].body as Record<string, unknown>;
		assert.equal(b.kind, 'phone.call');
		assert.equal(b.callSid, 'CAx');
		assert.equal(b.durationMs, 90_000);
	});

	it('no endpoint configured → no POST (capture off)', () => {
		delete process.env.SUTANDO_OBS_ENDPOINT;
		sendVoiceUsage({ sessionId: 's', durationMs: 5000, model: 'm' });
		assert.equal(calls.length, 0);
	});
});
