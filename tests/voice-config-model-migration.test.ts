/**
 * An existing install still on the old seeded 3.1 model moves to 3.8 once, keeping everything else.
 *
 * The workspace config is never rewritten by an app update and the template is copied only when
 * the file is missing, so without this a new default reaches new installs only.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { LEGACY_SEEDED_MODEL, MIGRATED_MODEL, MODEL_MIGRATION_KEY, migrateLegacyModel } from '../src/voice-config.js';

function configWith(body: unknown): string {
	const dir = mkdtempSync(join(tmpdir(), 'voice-migrate-'));
	const path = join(dir, 'voice-agent.json');
	writeFileSync(path, typeof body === 'string' ? body : JSON.stringify(body, null, 2));
	return path;
}

test('the old seeded 3.1 moves to 3.8, every other key kept, original backed up', () => {
	const path = configWith({ _comment: 'keep me', model: LEGACY_SEEDED_MODEL, googleSearch: false, mediaResolution: 'MEDIA_RESOLUTION_LOW' });
	const result = migrateLegacyModel(path, new Date('2026-10-04T00:00:00Z'));
	assert.equal(result.migrated, true);
	const after = JSON.parse(readFileSync(path, 'utf-8'));
	assert.equal(after.model, MIGRATED_MODEL);
	assert.equal(after.googleSearch, false);
	assert.equal(after.mediaResolution, 'MEDIA_RESOLUTION_LOW');
	assert.equal(after._comment, 'keep me');
	assert.match(String(after[MODEL_MIGRATION_KEY]), /2026-10-04/);
	assert.equal(JSON.parse(readFileSync(result.backup!, 'utf-8')).model, LEGACY_SEEDED_MODEL);
});

test('it happens once: a user who switches back to 3.1 afterwards keeps 3.1', () => {
	const path = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: false });
	assert.equal(migrateLegacyModel(path).migrated, true);
	const switchedBack = { ...JSON.parse(readFileSync(path, 'utf-8')), model: LEGACY_SEEDED_MODEL };
	writeFileSync(path, JSON.stringify(switchedBack));
	assert.equal(migrateLegacyModel(path).migrated, false);
	assert.equal(JSON.parse(readFileSync(path, 'utf-8')).model, LEGACY_SEEDED_MODEL);
});

test('any other model, a missing file, or an unreadable one is left alone', () => {
	const other = configWith({ model: 'gemini-2.5-flash-native-audio-preview-12-2025', googleSearch: true });
	assert.equal(migrateLegacyModel(other).migrated, false);
	assert.equal(existsSync(`${other}.bak-3.1`), false);
	assert.equal(migrateLegacyModel(join(mkdtempSync(join(tmpdir(), 'voice-migrate-')), 'none.json')).migrated, false);
	const broken = configWith('{ not json');
	assert.equal(migrateLegacyModel(broken).migrated, false);
	assert.equal(readFileSync(broken, 'utf-8'), '{ not json');
});
