/**
 * The fallback config layers: manifest defaults < per-host override file <
 * env, every key the proxy reads declared in the manifest, malformed values
 * falling back per field, and the live re-read when the override file moves.
 */
import { test } from 'node:test';
import assert from 'node:assert';
import { mkdtempSync, writeFileSync, utimesSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import {
	CONFIG_KEYS, SKILL_MANIFEST_PATH, createConfigReader, manifestConfig, mergeLayers, parseFallbackConfig, parseFamilyLevels,
} from '../skills/quota-tracker/scripts/quota-fallback-config.ts';
import { DEFAULT_FALLBACK_CONFIG as D } from '../skills/quota-tracker/scripts/quota-fallback-policy.ts';

test('the manifest declares exactly the keys the proxy reads, and parses to the built-in defaults', () => {
	const m = manifestConfig(SKILL_MANIFEST_PATH);
	assert.deepStrictEqual(Object.keys(m).sort(), [...CONFIG_KEYS].sort());
	assert.deepStrictEqual(parseFallbackConfig(m), D, 'shipped manifest == built-in defaults');
});

test('precedence: env beats the override file, which beats the manifest; empty env is unset', () => {
	const merged = mergeLayers(
		{ SUTANDO_QUOTA_FALLBACK_7D_LEVEL1: '0.85', SUTANDO_QUOTA_FALLBACK_7D_LEVEL2: '0.95', SUTANDO_QUOTA_FALLBACK_HYSTERESIS: '0.03' },
		{ SUTANDO_QUOTA_FALLBACK_7D_LEVEL1: '0.80', SUTANDO_QUOTA_FALLBACK_HYSTERESIS: '0.05' },
		{ SUTANDO_QUOTA_FALLBACK_HYSTERESIS: '0.01', SUTANDO_QUOTA_FALLBACK_7D_LEVEL2: '' },
	);
	assert.strictEqual(merged.SUTANDO_QUOTA_FALLBACK_7D_LEVEL1, '0.80');
	assert.strictEqual(merged.SUTANDO_QUOTA_FALLBACK_HYSTERESIS, '0.01');
	assert.strictEqual(merged.SUTANDO_QUOTA_FALLBACK_7D_LEVEL2, '0.95');
});

test('malformed values fall back per field; an inverted ladder keeps the default pair', () => {
	const c = parseFallbackConfig({
		SUTANDO_QUOTA_FALLBACK_ENABLED: 'maybe',
		SUTANDO_QUOTA_FALLBACK_5H_LEVEL1: '0.99', SUTANDO_QUOTA_FALLBACK_5H_LEVEL2: '0.50',
		SUTANDO_QUOTA_FALLBACK_7D_LEVEL1: '0.80',
		SUTANDO_QUOTA_FALLBACK_HYSTERESIS: 'abc',
		SUTANDO_QUOTA_FALLBACK_5H_PROJECTION: 'off',
		SUTANDO_QUOTA_FALLBACK_5H_PROJECTION_CLEAR_SAMPLES: '-4',
		SUTANDO_QUOTA_FALLBACK_LOW_PRIORITY: 'true',
		SUTANDO_QUOTA_FALLBACK_LEVEL2_MODEL: '  ',
		SUTANDO_QUOTA_FALLBACK_FAMILY_LEVELS: 'fable:1,opus:9',
	});
	assert.strictEqual(c.enabled, true);
	assert.deepStrictEqual(c.thresholds['5h'], D.thresholds['5h'], 'inverted ladder ignored');
	assert.deepStrictEqual(c.thresholds['7d'], { level1: 0.80, level2: 0.95 });
	assert.strictEqual(c.hysteresis, 0.03);
	assert.strictEqual(c.projection5h.enabled, false);
	assert.strictEqual(c.projection5h.clearSamples, 3);
	assert.strictEqual(c.lowPriorityEnabled, true);
	assert.strictEqual(c.level2Model, 'claude-opus-5-5');
	assert.deepStrictEqual(c.familyLevels, D.familyLevels, 'a bad level keeps the default map');
	assert.strictEqual(c.dmMinIntervalSec, 1800);
	const typo = parseFallbackConfig({ SUTANDO_QUOTA_FALLBACK_LEVEL2_MODEL: 'claude-opsu-5-5', SUTANDO_QUOTA_FALLBACK_LEVEL3_MODEL: 'claude-opus-5-5', SUTANDO_QUOTA_FALLBACK_DM_MIN_INTERVAL_SEC: '0' });
	assert.strictEqual(typo.level2Model, 'claude-opus-5-5', 'a typo is refused, never routed');
	assert.strictEqual(typo.level3Model, 'claude-sonnet-5', 'an Opus id is not a level-3 model');
	assert.strictEqual(typo.dmMinIntervalSec, 0);
	const custom = parseFallbackConfig({ SUTANDO_QUOTA_FALLBACK_FAMILY_LEVELS: 'fable:1,opus:2,haiku:3', SUTANDO_QUOTA_FALLBACK_LEVEL3_MODEL: 'claude-haiku-4-5' });
	assert.strictEqual(custom.level3Model, 'claude-haiku-4-5', 'validated against the configured families');
	assert.deepStrictEqual(parseFamilyLevels('fable:1, opus:2', D.familyLevels), { fable: 1, opus: 2 });
});

test('the reader picks up an override written later without a restart, and ignores the file when absent', () => {
	const dir = mkdtempSync(join(tmpdir(), 'qf-cfg-'));
	try {
		const override = join(dir, 'quota-fallback-config.json');
		let now = 1_000_000;
		const read = createConfigReader({ manifestPath: SKILL_MANIFEST_PATH, overridePath: override, env: {}, now: () => now, minReadIntervalMs: 1000 });
		assert.strictEqual(read().thresholds['7d'].level1, 0.85);
		writeFileSync(override, JSON.stringify({ SUTANDO_QUOTA_FALLBACK_7D_LEVEL1: '0.90' }));
		utimesSync(override, new Date(now + 5000), new Date(now + 5000));
		assert.strictEqual(read().thresholds['7d'].level1, 0.85, 'within the check interval: cached');
		now += 1500;
		assert.strictEqual(read().thresholds['7d'].level1, 0.90, 'past the interval: the new mtime triggers a re-read');
		writeFileSync(override, 'not json');
		utimesSync(override, new Date(now + 9000), new Date(now + 9000));
		now += 1500;
		assert.strictEqual(read().thresholds['7d'].level1, 0.85, 'a corrupt override file means no override');
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
