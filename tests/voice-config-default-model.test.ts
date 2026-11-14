/**
 * The built-in voice default is Gemini 3.8 Live, the model the seeded-install migration moves to,
 * and an install that runs on that default is never migrated or reverted.
 *
 * The packaged app copies no template, so a config-less install runs on VOICE_CONFIG_DEFAULTS.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';

import {
	LEGACY_SEEDED_MODELS,
	MIGRATED_MODEL,
	MODEL_MIGRATION_KEY,
	VOICE_CONFIG_DEFAULTS,
	loadVoiceConfig,
	migrateLegacyModel,
	revertModelMigration,
} from '../src/voice-config.js';

function configWith(body: unknown): string {
	const dir = mkdtempSync(join(tmpdir(), 'voice-default-'));
	const path = join(dir, 'voice-agent.json');
	writeFileSync(path, JSON.stringify(body, null, 2));
	return path;
}

const missingPath = () => join(mkdtempSync(join(tmpdir(), 'voice-default-')), 'voice-agent.json');

test('the default is gemini-3.8-live with search on, the same model the migration moves seeded installs to', () => {
	// Spelled out, not read from MIGRATED_MODEL: a default left behind on an old model must fail here.
	assert.equal(VOICE_CONFIG_DEFAULTS.model, 'gemini-3.8-live');
	assert.equal(MIGRATED_MODEL, 'gemini-3.8-live');
	assert.equal(VOICE_CONFIG_DEFAULTS.googleSearch, true, 'the migration keeps search, so the default keeps it too');
	assert.equal(LEGACY_SEEDED_MODELS.includes(VOICE_CONFIG_DEFAULTS.model), false, 'the default is not a model to migrate from');
});

test('a missing config and a config without a model both run on 3.8', () => {
	const none = loadVoiceConfig(missingPath());
	assert.equal(none.model, 'gemini-3.8-live');
	assert.equal(none.googleSearch, true);
	const partial = loadVoiceConfig(configWith({ owner_mode: true }));
	assert.equal(partial.model, 'gemini-3.8-live');
	assert.equal(partial.owner_mode, true);
});

test('an install on the default is not migrated: no file, no model key, or the default model written out', () => {
	assert.equal(migrateLegacyModel(missingPath()).migrated, false);
	for (const body of [{ googleSearch: true }, { ...VOICE_CONFIG_DEFAULTS }]) {
		const path = configWith(body);
		const before = readFileSync(path, 'utf-8');
		assert.equal(migrateLegacyModel(path).migrated, false, JSON.stringify(body));
		assert.equal(readFileSync(path, 'utf-8'), before, 'file untouched');
		assert.deepEqual(readdirSync(dirname(path)), ['voice-agent.json'], 'no backup copy');
	}
});

test('an install that started on 3.8 is never reverted when Gemini refuses it', () => {
	// The revert is for installs the migration moved; one that started on 3.8 has no older model to go back to.
	assert.equal(revertModelMigration(missingPath()).reverted, false);
	const path = configWith({ ...VOICE_CONFIG_DEFAULTS });
	const before = readFileSync(path, 'utf-8');
	const r = revertModelMigration(path);
	assert.equal(r.reverted, false);
	assert.equal(r.reason, 'not moved by the migration');
	assert.equal(readFileSync(path, 'utf-8'), before);
	assert.equal(JSON.parse(before)[MODEL_MIGRATION_KEY], undefined);
});

test('the shipped templates: phone carries the package default, voice-agent the same model with search off', () => {
	const phone = JSON.parse(readFileSync(new URL('../skills/phone-conversation/config.json.example', import.meta.url), 'utf-8'));
	assert.equal(phone.model, VOICE_CONFIG_DEFAULTS.model);
	assert.equal(phone.googleSearch, VOICE_CONFIG_DEFAULTS.googleSearch);
	const agent = JSON.parse(readFileSync(new URL('../src/voice-agent.config.json.example', import.meta.url), 'utf-8'));
	assert.equal(agent.model, VOICE_CONFIG_DEFAULTS.model);
	assert.equal(agent.googleSearch, false);
});
