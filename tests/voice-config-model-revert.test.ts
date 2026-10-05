/**
 * A config the migration moved to 3.8 goes back to 3.1 once when Gemini refuses the model; a
 * bad key, a network drop, or a user who chose 3.8 themselves is never touched.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
	LEGACY_SEEDED_MODEL,
	MIGRATED_MODEL,
	MODEL_MIGRATION_KEY,
	MODEL_REVERT_KEY,
	migrateLegacyModel,
	revertModelMigration,
} from '../src/voice-config.js';
import { isModelUnavailableClose } from '../src/voice-error-classifier.js';

function configWith(body: unknown): string {
	const dir = mkdtempSync(join(tmpdir(), 'voice-revert-'));
	const path = join(dir, 'voice-agent.json');
	writeFileSync(path, JSON.stringify(body, null, 2));
	return path;
}

const read = (path: string) => JSON.parse(readFileSync(path, 'utf-8'));

test('an unknown model (1008) is model-unavailable; a bad key (1007), a drop, or a stale handle is not', () => {
	assert.equal(isModelUnavailableClose(1008, 'models/gemini-9.9-live is not found for API version v1beta, or is not supported for bidiGenerateContent. Call ModelService.'), true);
	assert.equal(isModelUnavailableClose(1007, 'API key not valid. Please pass a valid API key.'), false);
	assert.equal(isModelUnavailableClose(1006, ''), false);
	assert.equal(isModelUnavailableClose(1011, 'Internal error encountered.'), false);
	assert.equal(isModelUnavailableClose(1008, 'Requested entity was not found.'), false);
	assert.equal(isModelUnavailableClose(1011, 'models/gemini-9.9-live is not found for API version v1beta'), false);
});

test('a migrated install goes back to its 3.1 model once, other keys kept, and is not migrated again', () => {
	const path = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: false, mediaResolution: 'MEDIA_RESOLUTION_LOW' });
	assert.equal(migrateLegacyModel(path).migrated, true);
	const result = revertModelMigration(path, new Date('2026-10-04T00:00:00Z'));
	assert.equal(result.reverted, true);
	assert.equal(result.model, LEGACY_SEEDED_MODEL);
	const after = read(path);
	assert.equal(after.model, LEGACY_SEEDED_MODEL);
	assert.equal(after.googleSearch, false);
	assert.equal(after.mediaResolution, 'MEDIA_RESOLUTION_LOW');
	assert.notEqual(after[MODEL_MIGRATION_KEY], undefined);
	assert.match(String(after[MODEL_REVERT_KEY]), /2026-10-04/);

	assert.equal(migrateLegacyModel(path).migrated, false);
	assert.equal(read(path).model, LEGACY_SEEDED_MODEL);
	writeFileSync(path, JSON.stringify({ ...read(path), model: MIGRATED_MODEL }));
	assert.equal(revertModelMigration(path).reverted, false);
	assert.equal(read(path).model, MIGRATED_MODEL);
});

test('the model comes from the .bak-3.1 copy, and edits made since the migration are kept', () => {
	const path = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: true });
	assert.equal(migrateLegacyModel(path).migrated, true);
	writeFileSync(`${path}.bak-3.1`, JSON.stringify({ model: 'gemini-3.1-flash-live-preview-custom', googleSearch: true }));
	writeFileSync(path, JSON.stringify({ ...read(path), googleSearch: false }));
	assert.equal(revertModelMigration(path).model, 'gemini-3.1-flash-live-preview-custom');
	assert.equal(read(path).googleSearch, false);
});

test('a user who chose 3.8 themselves, or moved off 3.8 since, is left alone', () => {
	const chosen = configWith({ model: MIGRATED_MODEL, googleSearch: false });
	assert.equal(revertModelMigration(chosen).reverted, false);
	assert.equal(read(chosen).model, MIGRATED_MODEL);

	const movedOn = configWith({ model: LEGACY_SEEDED_MODEL, googleSearch: false });
	migrateLegacyModel(movedOn);
	writeFileSync(movedOn, JSON.stringify({ ...read(movedOn), model: 'gemini-2.5-flash-native-audio-preview-12-2025' }));
	assert.equal(revertModelMigration(movedOn).reverted, false);
	assert.equal(read(movedOn).model, 'gemini-2.5-flash-native-audio-preview-12-2025');

	assert.equal(revertModelMigration(join(mkdtempSync(join(tmpdir(), 'voice-revert-')), 'none.json')).reverted, false);
});
