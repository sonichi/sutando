import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { record, ledgerPath } from '../../src/observability/meter.js';
import { resetSinks, registerSink } from '../../src/observability/obs.js';
import type { ObsEvent } from '../../src/observability/events.js';
import type { Sink } from '../../src/observability/sink.js';
import type { UsageRecordInput } from '../../src/observability/usage.js';

const ENV = ['SUTANDO_WORKSPACE', 'SUTANDO_TENANT_ID', 'SUTANDO_TENANT_MODE', 'SUTANDO_METERING_FSYNC'];
let saved: Record<string, string | undefined>;
let ws: string;
let cap: { type: string; events: ObsEvent[]; write(ev: ObsEvent): void };

function baseUsage(): UsageRecordInput {
	return {
		source: 'core-cli',
		actor: { user_id: 'u1', channel: 'claude-code', access_tier: 'owner' },
		meter: 'claude.tokens',
		quantity: 1840,
		unit: 'tokens',
		provider: 'anthropic',
		provider_ref: 'req_01H',
		attrs: { model: 'claude-opus-4-8', input_tokens: 1500, output_tokens: 340 },
	};
}

beforeEach(() => {
	saved = {};
	for (const k of ENV) {
		saved[k] = process.env[k];
		delete process.env[k];
	}
	ws = mkdtempSync(join(tmpdir(), 'meter-'));
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

function readLedger(rec: { ts: number }): string[] {
	const path = ledgerPath(rec.ts * 1000, ws);
	if (!existsSync(path)) return [];
	return readFileSync(path, 'utf-8').split('\n').filter((l) => l.length > 0);
}

describe('kernel/meter/record', () => {
	it('writes a byte-exact compact ledger line at the dated path', () => {
		const rec = record({ ...baseUsage(), usage_id: 'ux_FIXED0000000000000000000', ts: 1_717_900_000.12 });
		const lines = readLedger(rec);
		assert.equal(lines.length, 1);
		assert.equal(lines[0], JSON.stringify(rec));
		assert.deepEqual(JSON.parse(lines[0]), rec);
		assert.match(ledgerPath(rec.ts * 1000, ws), /\/data\/usage\/usage-\d{4}-\d{2}-\d{2}\.jsonl$/);
	});

	it('stamps schema/usage_id/ts/trace_id and defaults provider_ref', () => {
		const rec = record({ ...baseUsage(), provider_ref: undefined });
		assert.equal(rec.schema, 1);
		assert.match(rec.usage_id, /^ux_/);
		assert.match(rec.trace_id, /^tr_/);
		assert.equal(typeof rec.ts, 'number');
		assert.equal(rec.provider_ref, null);
	});

	it('honors a supplied usage_id verbatim (idempotency key survives re-append)', () => {
		const a = record({ ...baseUsage(), usage_id: 'ux_SAME0000000000000000000', ts: 1_717_900_000 });
		const b = record({ ...baseUsage(), usage_id: 'ux_SAME0000000000000000000', ts: 1_717_900_000 });
		const lines = readLedger(a);
		assert.equal(lines.length, 2); // append-only, at-least-once
		assert.equal(a.usage_id, b.usage_id);
		for (const l of lines) assert.equal(JSON.parse(l).usage_id, 'ux_SAME0000000000000000000');
	});

	it('auto-mints distinct usage_ids when none supplied', () => {
		const a = record(baseUsage());
		const b = record(baseUsage());
		assert.notEqual(a.usage_id, b.usage_id);
	});

	it('emits an advisory usage.recorded obs event AFTER the durable write', () => {
		const rec = record(baseUsage());
		const adv = cap.events.find((e) => e.kind === 'usage.recorded');
		assert.ok(adv, 'expected a usage.recorded advisory event');
		assert.equal(adv!.source, 'core-cli');
		assert.equal((adv!.data as Record<string, unknown>).usage_id, rec.usage_id);
		assert.equal(adv!.usage?.input_tokens, 1500);
	});

	it('defaults tenant_id from config, and honors an explicit value', () => {
		assert.equal(record(baseUsage()).tenant_id, null); // BYOK default
		process.env.SUTANDO_TENANT_ID = 'acct_9';
		assert.equal(record(baseUsage()).tenant_id, 'acct_9');
		assert.equal(record({ ...baseUsage(), tenant_id: 'explicit' }).tenant_id, 'explicit');
	});
});
