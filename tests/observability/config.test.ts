import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
	loadObservabilityConfig,
	OBSERVABILITY_DEFAULTS,
} from '../../src/observability/config.js';

const KNOBS = [
	'SUTANDO_TENANT_ID',
	'SUTANDO_TENANT_MODE',
	'SUTANDO_METERING_ENABLED',
	'SUTANDO_METERING_ENDPOINT',
	'SUTANDO_METERING_HEADERS',
];
let saved: Record<string, string | undefined>;
let ws: string;

beforeEach(() => {
	saved = {};
	for (const k of KNOBS) {
		saved[k] = process.env[k];
		delete process.env[k];
	}
	ws = mkdtempSync(join(tmpdir(), 'obscfg-'));
});

afterEach(() => {
	for (const k of KNOBS) {
		if (saved[k] === undefined) delete process.env[k];
		else process.env[k] = saved[k];
	}
	rmSync(ws, { recursive: true, force: true });
});

describe('kernel/config/observability-config', () => {
	it('clean env + no override → equals OBSERVABILITY_DEFAULTS', () => {
		assert.deepEqual(loadObservabilityConfig({ workspace: ws }), OBSERVABILITY_DEFAULTS);
	});

	it('env knobs beat the in-code defaults', () => {
		process.env.SUTANDO_TENANT_ID = 'acct_123';
		process.env.SUTANDO_TENANT_MODE = 'managed';
		process.env.SUTANDO_METERING_ENABLED = 'true';
		process.env.SUTANDO_METERING_ENDPOINT = 'https://meter.example';
		const cfg = loadObservabilityConfig({ workspace: ws });
		assert.equal(cfg.tenant.id, 'acct_123');
		assert.equal(cfg.tenant.mode, 'managed');
		assert.equal(cfg.metering.enabled, true);
		assert.equal(cfg.metering.endpoint, 'https://meter.example');
	});

	it('SUTANDO_METERING_ENABLED parses only truthy tokens', () => {
		process.env.SUTANDO_METERING_ENABLED = 'no';
		assert.equal(loadObservabilityConfig({ workspace: ws }).metering.enabled, false);
		process.env.SUTANDO_METERING_ENABLED = 'on';
		assert.equal(loadObservabilityConfig({ workspace: ws }).metering.enabled, true);
	});

	it('workspace override wins over env (documented order)', () => {
		process.env.SUTANDO_TENANT_MODE = 'managed';
		mkdirSync(join(ws, 'config'), { recursive: true });
		writeFileSync(
			join(ws, 'config', 'observability.json'),
			JSON.stringify({ tenant: { mode: 'byok' }, observability: { sampling: { trace: 0.5 } } }),
		);
		const cfg = loadObservabilityConfig({ workspace: ws });
		assert.equal(cfg.tenant.mode, 'byok'); // workspace overrides the env knob
		assert.equal(cfg.observability.sampling.trace, 0.5);
		// untouched keys keep their resolved value (metering still defaulted)
		assert.equal(cfg.metering.batchMax, 100);
	});

	it('malformed workspace override → warns and falls back, never throws', () => {
		mkdirSync(join(ws, 'config'), { recursive: true });
		writeFileSync(join(ws, 'config', 'observability.json'), '{ not valid json');
		const cfg = loadObservabilityConfig({ workspace: ws });
		assert.deepEqual(cfg, OBSERVABILITY_DEFAULTS);
	});
});
