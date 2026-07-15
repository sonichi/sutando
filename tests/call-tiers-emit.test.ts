/**
 * emit-call-tiers.ts — the core-advertise half of the availability-driven
 * call-tier menu (Track 9). Covers tier composition from reachability detection
 * and the state-file write that `sutando-config.sh runtime` folds into the
 * descriptor's `call_tiers`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, rmSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { composeCallTiers, emitCallTiers, type CallTiersFile } from '../src/emit-call-tiers.ts';

// composeCallTiers reads process.env (SUTANDO_LAN_SHARE) via reachability-endpoints.
// With LAN sharing OFF (default in CI), both direct tiers are unreachable — the
// exact "nothing to offer -> client falls back to cloud" default.
test('composeCallTiers: both direct tiers present, unreachable when LAN-share off', () => {
	const prev = process.env.SUTANDO_LAN_SHARE;
	delete process.env.SUTANDO_LAN_SHARE;
	try {
		const tiers = composeCallTiers();
		assert.equal(tiers.length, 2);
		const byTier = Object.fromEntries(tiers.map((t) => [t.tier, t]));
		assert.deepEqual(Object.keys(byTier).sort(), ['direct-lan', 'direct-tailnet']);
		for (const t of tiers) {
			assert.equal(t.url, null, `${t.tier} url should be null when LAN-share off`);
			assert.equal(t.reachable, false, `${t.tier} unreachable when url null`);
			assert.equal(typeof t.label, 'string');
			assert.ok(t.label.length > 0);
		}
	} finally {
		if (prev !== undefined) process.env.SUTANDO_LAN_SHARE = prev;
	}
});

test('reachable is exactly (url !== null) — the invariant the client binds to', () => {
	for (const t of composeCallTiers()) {
		assert.equal(t.reachable, t.url !== null);
	}
});

test('emitCallTiers writes a parseable state file with the descriptor shape', () => {
	const dir = mkdtempSync(join(tmpdir(), 'call-tiers-'));
	const dest = join(dir, 'call-tiers.json');
	try {
		const written = emitCallTiers(dest);
		assert.equal(written, dest);
		const rec = JSON.parse(readFileSync(dest, 'utf8')) as CallTiersFile;
		assert.equal(typeof rec.ts, 'number');
		assert.equal(typeof rec.pid, 'number');
		assert.ok(Array.isArray(rec.call_tiers));
		assert.equal(rec.call_tiers.length, 2);
		// Every entry carries the four keys the client renders from.
		for (const t of rec.call_tiers) {
			assert.deepEqual(Object.keys(t).sort(), ['label', 'reachable', 'tier', 'url']);
		}
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
