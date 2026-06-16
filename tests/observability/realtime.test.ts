import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ledgerPath } from '../../src/observability/meter.js';
import { resetSinks, registerSink } from '../../src/observability/obs.js';
import type { ObsEvent } from '../../src/observability/events.js';
import type { Sink } from '../../src/observability/sink.js';
import { recordVoiceSession, recordPhoneCall, startVoiceTicker, startPhoneTicker, advisoryCostUsd, durationSeconds } from '../../src/observability/realtime.js';

const ENV = ['SUTANDO_WORKSPACE', 'SUTANDO_TENANT_ID', 'SUTANDO_TENANT_MODE', 'SUTANDO_METERING_FSYNC'];
let saved: Record<string, string | undefined>;
let ws: string;
let cap: { type: string; events: ObsEvent[]; write(ev: ObsEvent): void };

beforeEach(() => {
	saved = {};
	for (const k of ENV) {
		saved[k] = process.env[k];
		delete process.env[k];
	}
	ws = mkdtempSync(join(tmpdir(), 'realtime-usage-'));
	process.env.SUTANDO_WORKSPACE = ws;
	resetSinks();
	cap = { type: 'capture', events: [], write(ev) { this.events.push(ev); } };
	registerSink(cap as Sink);
});

afterEach(() => {
	for (const k of ENV) {
		if (saved[k] === undefined) delete process.env[k];
		else process.env[k] = saved[k];
	}
	rmSync(ws, { recursive: true, force: true });
	resetSinks();
});

function ledgerLines(ts: number): string[] {
	const path = ledgerPath(ts * 1000, ws);
	if (!existsSync(path)) return [];
	return readFileSync(path, 'utf-8').split('\n').filter((l) => l.length > 0);
}

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

describe('recordVoiceSession', () => {
	it('writes a voice.seconds ledger line AND emits a usage.recorded event', () => {
		const rec = recordVoiceSession({ sessionId: 'session_42', durationMs: 90_000, model: 'gemini-3-flash-live', toolCalls: 3 });
		assert.ok(rec);
		assert.equal(rec!.meter, 'voice.seconds');
		assert.equal(rec!.quantity, 90);
		assert.equal(rec!.unit, 'seconds');
		assert.equal(rec!.provider, 'gemini-live');
		assert.equal(rec!.source, 'voice-agent');
		assert.equal(rec!.provider_ref, 'session_42');
		assert.equal(rec!.usage_id, 'voice.seconds:session_42'); // stable, dedup-friendly
		assert.equal(rec!.attrs.model, 'gemini-3-flash-live');
		assert.equal(rec!.attrs.tool_calls, 3);
		assert.equal(rec!.attrs.cost_usd, undefined); // realtime model: no advisory rate yet

		// emitted like an event
		const adv = cap.events.find((e) => e.kind === 'usage.recorded');
		assert.ok(adv, 'expected a usage.recorded obs event');
		assert.equal(adv!.source, 'voice-agent');
		assert.equal((adv!.data as Record<string, unknown>).meter, 'voice.seconds');
		assert.equal((adv!.data as Record<string, unknown>).usage_id, 'voice.seconds:session_42');

		// durable ledger
		const lines = ledgerLines(rec!.ts);
		assert.equal(lines.length, 1);
		assert.deepEqual(JSON.parse(lines[0]), rec);
	});

	it('skips a zero-length session (no record, no event)', () => {
		const rec = recordVoiceSession({ sessionId: 's0', durationMs: 200, model: 'm' });
		assert.equal(rec, null);
		assert.equal(cap.events.filter((e) => e.kind === 'usage.recorded').length, 0);
	});
});

describe('recordPhoneCall', () => {
	it('emits BOTH a twilio telephony leg and a gemini-live model leg, keyed by Call SID', () => {
		const recs = recordPhoneCall({ callSid: 'CA123', durationMs: 120_000, model: 'gemini-2.5-flash', isOwner: true, isMeeting: false, toolCalls: 2 });
		assert.equal(recs.length, 2);

		const tel = recs.find((r) => r.meter === 'phone.seconds')!;
		assert.equal(tel.provider, 'twilio');
		assert.equal(tel.source, 'phone');
		assert.equal(tel.quantity, 120);
		assert.equal(tel.provider_ref, 'CA123');
		assert.equal(tel.usage_id, 'phone.seconds:CA123');
		assert.equal(tel.attrs.cost_usd, 0.017); // 2 min @ $0.0085/min
		assert.equal(tel.attrs.is_owner, true);

		const model = recs.find((r) => r.meter === 'voice.seconds')!;
		assert.equal(model.provider, 'gemini-live');
		assert.equal(model.provider_ref, 'CA123');
		assert.equal(model.usage_id, 'voice.seconds:CA123');
		assert.equal(model.attrs.model, 'gemini-2.5-flash');
		assert.equal(model.attrs.cost_usd, undefined);

		// both emitted as events + both on the ledger
		assert.equal(cap.events.filter((e) => e.kind === 'usage.recorded').length, 2);
		assert.equal(ledgerLines(tel.ts).length, 2);
	});

	it('non-owner caller → public access tier on the actor', () => {
		const [tel] = recordPhoneCall({ callSid: 'CA9', durationMs: 30_000, model: 'm', isOwner: false });
		assert.equal(tel.actor.access_tier, 'public');
		assert.equal(tel.actor.user_id, 'caller');
		assert.equal(tel.actor.channel, 'phone');
	});

	it('skips a zero-length call', () => {
		assert.deepEqual(recordPhoneCall({ callSid: 'CA0', durationMs: 0, model: 'm' }), []);
	});
});

describe('startVoiceTicker', () => {
	it('stop() emits the final partial bucket with a bucket-keyed usage_id', () => {
		// fake clock: start=0, stop=45s later
		let fakeNow = 1_000_000_000_000;
		const ticker = startVoiceTicker({ sessionId: 'sess_tick', model: 'gemini-3-flash-live' }, 60_000, () => fakeNow);
		fakeNow += 45_000;
		const rec = ticker.stop();
		assert.ok(rec, 'stop() should emit a record for the partial bucket');
		assert.equal(rec!.meter, 'voice.seconds');
		assert.equal(rec!.quantity, 45);
		assert.equal(rec!.source, 'voice-agent');
		assert.ok(rec!.usage_id.startsWith('voice.seconds:sess_tick:b'), 'usage_id must include bucket timestamp');
		assert.ok(cap.events.some((e) => e.kind === 'usage.recorded'));
	});

	it('stop() is idempotent — second call returns null without double-write', () => {
		let fakeNow = 1_000_000_000_000;
		const ticker = startVoiceTicker({ sessionId: 'sess_idem', model: 'm' }, 60_000, () => fakeNow);
		fakeNow += 10_000;
		ticker.stop();
		fakeNow += 10_000;
		const second = ticker.stop();
		assert.equal(second, null);
		assert.equal(cap.events.filter((e) => e.kind === 'usage.recorded').length, 1);
	});

	it('toolCallsGetter is sampled at flush time', () => {
		let count = 0;
		let fakeNow = 1_000_000_000_000;
		const ticker = startVoiceTicker({ sessionId: 'sess_tc', model: 'm', toolCallsGetter: () => count }, 60_000, () => fakeNow);
		count = 7;
		fakeNow += 20_000;
		const rec = ticker.stop();
		assert.equal(rec!.attrs.tool_calls, 7);
	});
});

describe('startPhoneTicker', () => {
	it('stop() emits both phone.seconds and voice.seconds with bucket-keyed usage_ids', () => {
		let fakeNow = 1_000_000_000_000;
		const ticker = startPhoneTicker({ callSid: 'CA_tick', model: 'gemini-2.5-flash', isOwner: true }, 60_000, () => fakeNow);
		fakeNow += 90_000;
		const recs = ticker.stop();
		assert.equal(recs.length, 2);
		const tel = recs.find((r) => r.meter === 'phone.seconds')!;
		const model = recs.find((r) => r.meter === 'voice.seconds')!;
		assert.equal(tel.quantity, 90);
		assert.ok(tel.usage_id.startsWith('phone.seconds:CA_tick:b'));
		assert.ok(model.usage_id.startsWith('voice.seconds:CA_tick:b'));
		assert.equal(cap.events.filter((e) => e.kind === 'usage.recorded').length, 2);
	});

	it('stop() is idempotent — second call returns [] without double-write', () => {
		let fakeNow = 1_000_000_000_000;
		const ticker = startPhoneTicker({ callSid: 'CA_idem', model: 'm' }, 60_000, () => fakeNow);
		fakeNow += 30_000;
		ticker.stop();
		fakeNow += 30_000;
		assert.deepEqual(ticker.stop(), []);
		assert.equal(cap.events.filter((e) => e.kind === 'usage.recorded').length, 2); // only from first stop
	});
});
